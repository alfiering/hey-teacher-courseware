"""Read uploaded files as text, keeping Word tables where they sit in the document.

The shared common.parsers reader appends every Word table after all paragraphs. Lesson plans keep each
period's teaching process in a table under that period's heading, so that order loses which period a
table belongs to. This reader walks the document body in order instead.
"""
from __future__ import annotations

from pathlib import Path

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def docx_text(path: Path) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    lines = []

    def walk(container, parent):
        for child in container.iterchildren():
            tag = child.tag.rsplit('}', 1)[-1]
            if tag == 'p':
                text = Paragraph(child, parent).text
                if text.strip():
                    lines.append(text)
            elif tag == 'tbl':
                for row in Table(child, parent).rows:
                    seen = set()
                    for cell in row.cells:
                        if id(cell._tc) in seen:  # merged cells repeat across a row
                            continue
                        seen.add(id(cell._tc))
                        walk(cell._tc, cell)
            elif tag == 'sdt':
                content = child.find(W + 'sdtContent')
                if content is not None:
                    walk(content, parent)

    walk(doc.element.body, doc)
    return '\n'.join(lines)


def parse_file(path: Path) -> dict:
    """common.parsers.parse_file, except Word files are read in document order."""
    path = Path(path)
    if path.suffix.lower() == '.docx':
        text = docx_text(path)
        return {'kind': 'docx', 'text': text, 'pages': 1, 'note': f'Word 提取 {len(text)} 字'}
    from common import parsers
    return parsers.parse_file(path)
