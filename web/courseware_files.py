"""Audit a local slide deck and render its real pages without changing the source."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE


SOFFICE_CANDIDATES = (
    '/Applications/LibreOffice.app/Contents/MacOS/soffice',
    '/opt/homebrew/bin/soffice',
    '/usr/local/bin/soffice',
    str(Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice'),
)


def find_soffice() -> str:
    """LibreOffice renders PPTX pages. The double-click launcher has a minimal PATH, so check known installs too."""
    import os
    import shutil
    for candidate in (os.environ.get('SOFFICE', ''), shutil.which('soffice') or '', *SOFFICE_CANDIDATES):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ValueError('需要 LibreOffice 才能把 PPTX 转成逐页图片。请安装 LibreOffice，或直接上传 Notebook 导出的 PDF')


def audit_courseware(source: Path, output_dir: Path) -> dict:
    source = Path(source)
    output_dir = Path(output_dir)
    if source.suffix.lower() not in ('.pptx', '.pdf'):
        raise ValueError('逐页检查仅支持 PPTX 或 PDF')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    observations = []
    if source.suffix.lower() == '.pptx':
        observations = inspect_pptx(source)
        with tempfile.TemporaryDirectory(prefix='courseware-render-') as tmp:
            profile = Path(tmp) / 'profile'
            command = [find_soffice(), f'-env:UserInstallation={profile.as_uri()}', '--headless', '--convert-to',
                       'pdf', '--outdir', tmp, str(source)]
            run = subprocess.run(command, capture_output=True, text=True, timeout=180)
            rendered = Path(tmp) / (source.stem + '.pdf')
            if run.returncode or not rendered.is_file():
                raise ValueError('PPTX 预览转换失败，请检查 LibreOffice 与源文件')
            pages = _render_pdf(rendered, output_dir, observations)
    else:
        pages = _render_pdf(source, output_dir, observations)
    manifest = {'source_sha256': digest, 'source_name': source.name, 'page_count': len(pages), 'pages': pages}
    (output_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def inspect_pptx(source: Path) -> list[dict]:
    """Inspect native slide objects; a structural signal, not a visual fidelity guarantee."""
    presentation = Presentation(source)
    slide_area = presentation.slide_width * presentation.slide_height
    observations = []
    for slide in presentation.slides:
        text = sum(bool(shape.has_text_frame and shape.text.strip()) for shape in slide.shapes)
        pictures = sum(shape.shape_type == MSO_SHAPE_TYPE.PICTURE for shape in slide.shapes)
        other = len(slide.shapes) - text - pictures
        broad_picture = any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE and
                            shape.width * shape.height >= .8 * slide_area for shape in slide.shapes)
        flat = broad_picture and text == 0 and other == 0
        observations.append({'text_shapes': text, 'pictures': pictures, 'other_shapes': other,
                             'flat_image_candidate': flat,
                             'editability': 'flat' if flat else 'editable' if text or other else 'uncertain'})
    return observations


def _render_pdf(source: Path, output_dir: Path, observations: list[dict]) -> list[dict]:
    pages = []
    with pymupdf.open(source) as pdf:
        if not 0 < pdf.page_count <= 100 or (observations and len(observations) != pdf.page_count):
            raise ValueError('渲染页数与源文件不符或超过 100 页')
        for index, page in enumerate(pdf):
            pixels = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
            image = Image.frombytes('RGB', (pixels.width, pixels.height), pixels.samples)
            filename = f'page-{index + 1:03}.jpg'
            image.save(output_dir / filename, format='JPEG', quality=90, optimize=True)
            data = (output_dir / filename).read_bytes()
            observation = observations[index] if observations else {
                'text_shapes': len(page.get_text().strip().splitlines()), 'pictures': len(page.get_images()),
                'other_shapes': 0, 'flat_image_candidate': not bool(page.get_text().strip())}
            pages.append({'number': index + 1, 'width': pixels.width, 'height': pixels.height,
                          'image': filename, 'image_sha256': hashlib.sha256(data).hexdigest(),
                          'observation': observation})
    return pages
