"""Make a Canva-exported PPTX look right in WPS/Keynote, which ignore fonts embedded in the file.

Canva embeds its own fonts and sizes every text box tightly around the text (zero insets). A viewer that
substitutes another font then wraps or overflows text. This pass, run locally and without any model:
  1. replaces every font with one installed font,
  2. measures each text box with that font and shrinks the font size until the text fits the original box,
     keeping one-line boxes (titles, word cards) on one line,
  3. removes the "Gemini Notebook" watermark that Magic Layers turns into a text layer.
The source file is never modified; a new file is written.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import ImageFont
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Emu

A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
FONT_DIR = Path('/System/Library/Fonts/Supplemental')
SYSTEM_FONT_DIR = Path('/System/Library/Fonts')
FONTS = {
    # name shown in the PPTX: (regular file, bold file)
    'Comic Sans MS': (FONT_DIR / 'Comic Sans MS.ttf', FONT_DIR / 'Comic Sans MS Bold.ttf'),
    'Arial Rounded MT Bold': (FONT_DIR / 'Arial Rounded Bold.ttf', FONT_DIR / 'Arial Rounded Bold.ttf'),
    'Arial': (FONT_DIR / 'Arial.ttf', FONT_DIR / 'Arial Bold.ttf'),
    # Both fonts contain CJK glyphs; Comic Sans and Arial do not reliably do so.
    'Arial Unicode MS': (FONT_DIR / 'Arial Unicode.ttf', FONT_DIR / 'Arial Unicode.ttf'),
    'Hiragino Sans GB': (SYSTEM_FONT_DIR / 'Hiragino Sans GB.ttc', SYSTEM_FONT_DIR / 'Hiragino Sans GB.ttc'),
}
# OCR of the watermark varies: 'Gemini Notebook', '@ Geminis Notebook', '@ Gemin’s Notebook', '@ Gennis Notebook'
WATERMARK = re.compile(r'^\W*g[a-z’\']{2,8}\s*notebook\W*$', re.I)
MIN_SCALE = 0.55
LINE = 1.2  # typical line height relative to font size
FIT = 0.94  # leave room for renderer differences (kerning, hinting)


class _Measure:
    def __init__(self, font):
        regular, bold = FONTS[font]
        if not regular.is_file():
            raise ValueError(f'本机没有字体 {font}')
        self.fonts = {False: ImageFont.truetype(str(regular), 100), True: ImageFont.truetype(str(bold if bold.is_file() else regular), 100)}

    def width(self, text, size_pt, bold):
        return self.fonts[bool(bold)].getlength(text) * size_pt / 100


def _shapes(shapes):
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _shapes(shape.shapes)
        else:
            yield shape


def _runs(paragraph):
    return [r for r in paragraph.runs if r.text]


def _size(run, default=18.0):
    return run.font.size.pt if run.font.size is not None else default


def _is_bold(run, original_name):
    return bool(run.font.bold) or 'bold' in (original_name or '').lower() or 'black' in (original_name or '').lower()


def _set_font(run, font, bold):
    rpr = run._r.get_or_add_rPr()
    for tag in ('latin', 'ea', 'cs', 'sym'):
        el = rpr.find(A + tag)
        if el is None:
            el = rpr.makeelement(A + tag, {})
            rpr.append(el)
        el.set('typeface', font)
    if bold:
        run.font.bold = True


def _lines_needed(words_widths, space, width):
    """Greedy word wrap: how many lines a paragraph needs at this width."""
    lines, current = 1, 0.0
    for w in words_widths:
        if current and current + space + w > width:
            lines, current = lines + 1, w
        else:
            current = current + space + w if current else w
    return lines


def _paragraph_metrics(paragraph, measure, scale, styles):
    """(full width, wrap-unit widths, gap, max size) at a size scale.

    Han characters can wrap between characters, unlike a Latin word. Splitting
    only on spaces treated an entire Chinese sentence as one unbreakable word.
    """
    words, full, size_max = [], 0.0, 0.0
    space = 0.0
    for run, (bold, size) in zip(_runs(paragraph), styles):
        s = size * scale
        size_max = max(size_max, s)
        full += measure.width(run.text, s, bold)
        words.extend(measure.width(w, s, bold) for w in re.findall(r'\s+|[\u3400-\u9fff]|[^\s\u3400-\u9fff]+', run.text))
    return full, words, space, size_max


def fix_frame(shape, measure, font):
    tf = shape.text_frame
    paragraphs = [p for p in tf.paragraphs if _runs(p)]
    if not paragraphs:
        return False
    styles = []
    for p in paragraphs:
        row = []
        for run in _runs(p):
            original = run.font.name
            bold = _is_bold(run, original)
            row.append((bold, _size(run)))
            _set_font(run, font, bold)
        styles.append(row)
    body = tf._txBody.find(A + 'bodyPr')
    insets = sum(int(body.get(k, 91440 if k in ('lIns', 'rIns') else 0)) for k in ('lIns', 'rIns')) if body is not None else 182880
    width = Emu(max(shape.width - insets, 1)).pt
    height = Emu(shape.height).pt
    sizes = [max(s for _, s in row) for row in styles]
    original_lines = max(len(paragraphs), round(height / (LINE * (sum(sizes) / len(sizes)))))
    single_line = original_lines <= len(paragraphs)

    def fits(scale):
        if single_line:
            return all(_paragraph_metrics(p, measure, scale, st)[0] <= width * FIT for p, st in zip(paragraphs, styles))
        needed = sum(_lines_needed(m[1], m[2], width * FIT) for m in (_paragraph_metrics(p, measure, scale, st) for p, st in zip(paragraphs, styles)))
        return needed <= original_lines

    scale = 1.0
    while scale > MIN_SCALE and not fits(scale):
        scale -= 0.03
    scale = max(scale, MIN_SCALE)
    if single_line and body is not None:
        body.set('wrap', 'none')  # never break a word card such as "kind" into "kin / d"
        for child in list(body):
            if child.tag == A + 'spAutoFit':
                body.remove(child)  # some viewers re-wrap auto-fit boxes at the box width
    if scale < 0.999:
        for p, row in zip(paragraphs, styles):
            for run, (_bold, size) in zip(_runs(p), row):
                run.font.size = Emu(int(size * scale * 12700))
    return scale < 0.999


def _drop_embedded_fonts(deck):
    """Canva's embedded fonts no longer match the text; some viewers would still pick them by similar names."""
    pres = deck.part._element
    lst = pres.find('{http://schemas.openxmlformats.org/presentationml/2006/main}embeddedFontLst')
    if lst is not None:
        pres.remove(lst)
    for rid, rel in list(deck.part.rels.items()):
        if rel.reltype.endswith('/font'):
            deck.part.drop_rel(rid)


def fix_fonts(source: Path, target: Path, font='Comic Sans MS') -> dict:
    if font not in FONTS:
        raise ValueError('不支持的替换字体')
    measure = _Measure(font)
    deck = Presentation(str(source))
    stats = {'slides': len(deck.slides), 'text_boxes': 0, 'shrunk': 0, 'watermarks_removed': 0, 'font': font}
    for slide in deck.slides:
        for shape in list(_shapes(slide.shapes)):
            if not shape.has_text_frame or not shape.text_frame.text.strip():
                continue
            if WATERMARK.match(shape.text_frame.text):
                shape._element.getparent().remove(shape._element)
                stats['watermarks_removed'] += 1
                continue
            stats['text_boxes'] += 1
            stats['shrunk'] += fix_frame(shape, measure, font)
    _drop_embedded_fonts(deck)
    buf = io.BytesIO()
    deck.save(buf)
    Path(target).write_bytes(buf.getvalue())
    return stats
