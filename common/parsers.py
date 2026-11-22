"""文件解析层：把老师上传的文件变成可给 AI 用的文本。

支持：
- PDF（.pdf）→ pymupdf 逐页提文字
- 图片/截图（.png/.jpg/.jpeg/.heic/.webp）→ macOS Vision 框架 OCR（中英混排）
- 文本（.txt/.md）→ 直接读
- Word（.docx）→ python-docx 提段落

这是「上传驱动」工作流的地基：教案模板 / 教材页 / 学情成绩 / 听力模板
四类上传，最终都落到同一套「提取文本 → 给 DeepSeek 结构化」的路径。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path

OCR_SWIFT = Path(__file__).resolve().parent / "ocr_vision.swift"
OCR_BIN = Path(__file__).resolve().parent / "ocr_vision_bin"
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".heic", ".webp", ".gif", ".bmp", ".tiff"}
_PDF_EXT = {".pdf"}
_TEXT_EXT = {".txt", ".md"}
_DOCX_EXT = {".docx"}


def save_upload(data: bytes, filename: str) -> Path:
    """把上传的字节存到 data/uploads/，返回落盘路径。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name
    path = UPLOAD_DIR / safe
    # 重名则加序号，避免覆盖
    i = 1
    while path.exists():
        path = UPLOAD_DIR / f"{Path(safe).stem}_{i}{Path(safe).suffix}"
        i += 1
    path.write_bytes(data)
    return path


def _ocr_binary() -> str:
    """编译一次 Swift OCR 为二进制（缓存），后续调用走二进制，秒级返回。"""
    if not OCR_BIN.exists():
        r = subprocess.run(["swiftc", "-O", str(OCR_SWIFT), "-o", str(OCR_BIN)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"OCR 编译失败：{r.stderr.strip()[:300]}")
    return str(OCR_BIN)


def extract_pdf(path: Path) -> dict:
    """pymupdf 提文字；若是扫描版（无文字层）则渲染成图走 OCR。"""
    import pymupdf  # noqa: F401  (fitz 的新名字)

    doc = pymupdf.open(str(path))
    pages_text = [page.get_text("text") for page in doc]
    text = "\n\n".join(p.strip() for p in pages_text if p.strip())
    n = len(pages_text)

    if text.strip():
        doc.close()
        return {"kind": "pdf", "text": text, "pages": n,
                "note": f"PDF 共 {n} 页，提取 {len(text)} 字"}

    # 扫描版：渲染成图逐页 OCR。整册教材在上传时先截取本课时页，最多 30 页。
    import tempfile
    max_ocr = min(n, 30)
    parts = []
    try:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            for i in range(max_ocr):
                pix = doc[i].get_pixmap(dpi=170)
                img = tmp / f"p{i}.png"
                pix.save(str(img))
                parts.append(ocr_image(img)["text"])
    finally:
        doc.close()
    text = "\n\n".join(p for p in parts if p.strip())
    return {"kind": "pdf-scanned", "text": text, "pages": n,
            "note": f"扫描版 PDF 共 {n} 页，已 OCR 前 {max_ocr} 页（{len(text)} 字）。"
                    f"若教材单元在更靠后的页，可上传该页截图。"}


# ── RapidOCR（onnxruntime 版，跨平台，中英混排）────────────────────
# 线上 Linux 沙箱没有 macOS Vision 框架，图片 OCR 改用 RapidOCR；macOS Vision 仅作本地兜底。
# 引擎懒加载 + 全局单例 + 锁，避免并发初始化 / 推理冲突。
_rapid_engine = None
_rapid_unavailable = False
_rapid_lock = threading.Lock()


def _prepare_image(path: Path, max_side: int = 2000) -> tuple[Path, bool]:
    """大图先缩放再 OCR：教材页照片动辄 4000+ px，直接 OCR 内存高、易超时甚至 OOM。
    缩到最长边 max_side 以内（文字仍清晰），返回 (路径, 是否临时文件)。失败则原样返回。"""
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
            if max(w, h) <= max_side:
                return path, False
            scale = max_side / max(w, h)
            img = img.convert("RGB").resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            fd, tmp = tempfile.mkstemp(suffix=Path(path).suffix)
            os.close(fd)
            img.save(tmp)
            return Path(tmp), True
    except Exception:
        return path, False


def _rapidocr_text(path: Path) -> str | None:
    """RapidOCR 识别图片文字。返回文字；不可用或失败时返回 None（由调用方决定兜底）。"""
    global _rapid_engine, _rapid_unavailable
    if _rapid_unavailable:
        return None
    prepared, is_tmp = _prepare_image(path)
    try:
        with _rapid_lock:
            if _rapid_engine is None:
                try:
                    # 懒加载：未安装依赖的环境（如离线本地）不会因 import 失败而崩
                    from rapidocr_onnxruntime import RapidOCR
                    _rapid_engine = RapidOCR()
                except Exception:
                    _rapid_unavailable = True
                    return None
            try:
                result, _ = _rapid_engine(str(prepared))
            except Exception:
                return None
    finally:
        if is_tmp:
            try:
                prepared.unlink(missing_ok=True)
            except Exception:
                pass
    if not result:
        return ""
    lines = []
    for item in result:
        txt = ""
        if isinstance(item, dict):
            txt = item.get("txt") or item.get("text") or ""
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            txt = item[1]
        if txt:
            lines.append(str(txt))
    return "\n".join(lines)


def _vision_ocr_text(path: Path) -> str | None:
    """macOS Vision OCR（本地兜底）。失败返回 None。"""
    try:
        bin_ = _ocr_binary()
        r = subprocess.run([bin_, str(path)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None
        text = r.stdout.strip()
        if text.startswith("ERROR") or not text:
            return None
        return text
    except Exception:
        return None


def ocr_image(path: Path) -> dict:
    """图片 OCR：优先 RapidOCR（跨平台、中英混排），失败回退 macOS Vision，再失败降级空文字。"""
    text = _rapidocr_text(path)
    if text is not None:
        return {"kind": "image", "text": text, "pages": 1,
                "note": f"图片 OCR 识别 {len(text)} 字（印刷体识别率高，手写/低分辨率可能漏字）"}

    text = _vision_ocr_text(path)
    if text is not None:
        return {"kind": "image", "text": text, "pages": 1,
                "note": f"图片 OCR 识别 {len(text)} 字（印刷体识别率高，手写/低分辨率可能漏字）"}

    # 两个引擎都不可用：降级为空文字，不崩溃
    return {"kind": "image", "text": "", "pages": 1,
            "note": "图片 OCR 不可用。请上传文字版 PDF，或直接把教材文字粘贴进来"}


def extract_text(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return {"kind": "text", "text": text, "pages": 1,
            "note": f"文本文件 {len(text)} 字"}


def extract_docx(path: Path) -> dict:
    from docx import Document
    doc = Document(str(path))
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    for t in doc.tables:
        for row in t.rows:
            for c in row.cells:
                if c.text.strip():
                    paras.append(c.text.strip())
    text = "\n".join(paras)
    return {"kind": "docx", "text": text, "pages": 1,
            "note": f"Word 提取 {len(text)} 字"}


def parse_file(path: Path) -> dict:
    """按扩展名分发。返回 {kind, text, pages, note, path}。"""
    ext = Path(path).suffix.lower()
    if ext in _PDF_EXT:
        r = extract_pdf(path)
    elif ext in _IMAGE_EXT:
        r = ocr_image(path)
    elif ext in _DOCX_EXT:
        r = extract_docx(path)
    elif ext in _TEXT_EXT:
        r = extract_text(path)
    else:
        raise ValueError(f"不支持的文件类型：{ext}（支持 PDF / 图片 / Word / 文本）")
    r["path"] = str(path)
    r["name"] = Path(path).name
    return r
