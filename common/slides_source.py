"""课件工作台 · 来源 PDF 打包。

把「教案文字 + 教材 OCR 文字 + 教材页图片」合成一份 PDF，作为 NotebookLM 的上传来源。

字体优先用项目内置的 Noto Sans SC（开源思源黑体，TrueType 轮廓，可嵌入 PDF），
线上/本地都可用；找不到才回退 macOS 本机字体或 STSong-Light CID 字体。
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Image, Spacer

# 2026-11-07 修：STSong-Light 是 Adobe CID 字体，reportlab 用 UnicodeCIDFont 生成时**不会把
# 字体嵌入 PDF**（字体是 n/a 引用）。本机有 Adobe 亚洲语言包能显示、pymupdf 有内置 CID 映射能
# 提取，但 NotebookLM 云端没有这个字体，读文字层就是乱码。改成「能嵌入的 TTF」中文字体，
# 字体随 PDF 嵌入，任何环境（含 NotebookLM）都能正确读出中文。
_FONT = "STSong-Light"          # 兜底 CID 字体名，会被 _ensure_font 替换为可嵌入的 TTF 注册名
_EMBED_FONT_NAME = "SourceHanEmbed"
_FONT_DIR = Path(__file__).resolve().parent / "fonts"       # 项目内置字体目录（随代码部署到线上）
_EMBED_FONT_CANDIDATES = [
    (str(_FONT_DIR / "NotoSansSC-400.ttf"), 0),             # 项目内置思源黑体（首选，线上/本地通用）
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),        # macOS 华文黑体（仅本机）
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),      # 冬青黑体（仅本机）
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 0),   # 宋体（仅本机）
]
_IMG_MAX_W = 540.0   # 图片限宽（pt），A4 宽 595 减去左右边距
_IMG_MAX_H = 640.0   # 图片限高（pt），留出标题空间
CHARS_REF_DIR = Path(__file__).resolve().parent / "characters_ref"   # 人物参考图库

_registered = False


def _ensure_font():
    global _registered, _FONT
    if _registered:
        return
    # 优先用能嵌入的 TTF/OTC 中文字体；找不到才回退不嵌入的 CID 字体（旧行为）
    for path, subfont in _EMBED_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont(_EMBED_FONT_NAME, path, subfontIndex=subfont))
                _FONT = _EMBED_FONT_NAME
                _registered = True
                return
            except Exception:
                continue
    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    _registered = True


def _styles() -> dict:
    return {
        "title": ParagraphStyle("title", fontName=_FONT, fontSize=18, leading=24, spaceAfter=14),
        "h": ParagraphStyle("h", fontName=_FONT, fontSize=14, leading=20, spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("body", fontName=_FONT, fontSize=10.5, leading=16),
    }


def _append_text(story, text, style):
    """按行写入文字，空行跳过。转义 XML 特殊字符，避免 & < > 把 Paragraph 弄崩。"""
    if not text:
        return
    for line in text.split("\n"):
        line = line.strip()
        if line:
            story.append(Paragraph(escape(line), style))


_GENDER_CN = {"女": "女生", "男": "男生", "female": "girl", "male": "boy"}


def _char_line(c) -> str:
    """把一个人物渲染成来源 PDF 里的一行中文说明（姓名：性别，特征）。"""
    if isinstance(c, str):
        return c
    name = c.get("name", "")
    gender = _GENDER_CN.get(c.get("gender", ""), "")
    feat = (c.get("features") or "").strip()
    if gender and feat:
        return f"{name}：{gender}，{feat}"
    if gender:
        return f"{name}：{gender}"
    if feat:
        return f"{name}：{feat}"
    return name


def _fit_image(path: str):
    """等比缩放图片到限宽/限高内（只缩不放），失败返回 None。"""
    try:
        ir = ImageReader(path)
        iw, ih = ir.getSize()
    except Exception:
        return None
    scale = min(_IMG_MAX_W / iw, _IMG_MAX_H / ih, 1.0)
    return Image(path, width=iw * scale, height=ih * scale)


def build_source_pdf(plan_text: str, textbook_text: str,
                     image_paths: list[str], out_path, characters=None) -> Path:
    """合成来源 PDF。characters 是人物档案列表，会写进「人物形象」一节，避免 NotebookLM 混淆性别。"""
    _ensure_font()
    s = _styles()
    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=54, rightMargin=54, topMargin=48, bottomMargin=48,
    )
    story = [Paragraph("NotebookLM 来源材料（上传到 NotebookLM 即可）", s["title"])]

    story.append(Paragraph("一、教案", s["h"]))
    _append_text(story, plan_text, s["body"])

    story.append(Paragraph("二、教材内容", s["h"]))
    _append_text(story, textbook_text, s["body"])

    if characters:
        story.append(Paragraph("三、教材人物形象（生成时严格参照下方参考图，保持长相/发型/服装一致）", s["h"]))
        for c in characters:
            story.append(Paragraph(escape(_char_line(c)), s["body"]))
            img_file = c.get("image", "") if isinstance(c, dict) else ""
            own = c.get("image_path", "") if isinstance(c, dict) else ""
            if own or img_file:
                img_path = Path(own) if own else CHARS_REF_DIR / img_file
                if img_path.exists():
                    img = _fit_image(str(img_path))
                    if img is not None:
                        story.append(img)
                        story.append(Spacer(1, 10))

    if image_paths:
        story.append(Paragraph("四、教材页图片", s["h"]))
        for p in image_paths:
            img = _fit_image(p)
            if img is not None:
                story.append(img)
                story.append(Spacer(1, 12))

    doc.build(story)
    return Path(out_path)
