"""Disk-backed, bounded upload of a whole textbook PDF before selecting lesson pages."""
from __future__ import annotations

import base64
import binascii
import re
import threading
import time
import uuid
from pathlib import Path

MAX_BOOK = 200 * 1024 * 1024
CHUNK = 4 * 1024 * 1024
MAX_SELECTED_PAGES = 30  # one lesson can span several textbook spreads
SESSION_AGE = 60 * 60


def parse_pages(value, count):
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise ValueError('请输入页码，例如 12-18 或 12-18,20')
    selected = set()
    for token in value.replace('，', ',').split(','):
        match = re.fullmatch(r'\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*', token)
        if not match:
            raise ValueError('页码格式不正确，请输入例如 12-18,20')
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first or last > count:
            raise ValueError(f'页码须在 1–{count} 之间，且起始页不能大于结束页')
        if last - first + 1 > MAX_SELECTED_PAGES:
            raise ValueError(f'每次最多选择 {MAX_SELECTED_PAGES} 页教材')
        selected.update(range(first, last + 1))
        if len(selected) > MAX_SELECTED_PAGES:
            raise ValueError(f'每次最多选择 {MAX_SELECTED_PAGES} 页教材')
    return sorted(selected)


class LargeTextbookUploads:
    def __init__(self, store):
        self.store = store
        self.root = store.root / 'pending_textbooks'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.sessions = {}

    def _clean(self):
        now = time.time()
        for ident, session in list(self.sessions.items()):
            if now - session['created'] > SESSION_AGE:
                self._remove(ident)
        for path in self.root.glob('*.pdf'):
            if now - path.stat().st_mtime > SESSION_AGE:
                path.unlink(missing_ok=True)

    def _remove(self, ident):
        self.sessions.pop(ident, None)
        (self.root / f'{ident}.pdf').unlink(missing_ok=True)

    def _session(self, project_id, ident):
        session = self.sessions.get(ident)
        if not session or session['project_id'] != project_id:
            raise ValueError('整册上传会话已失效，请重新选择文件')
        return session

    def start(self, project_id, name, size):
        self.store.get_project(project_id)
        if not isinstance(name, str) or not name.lower().endswith('.pdf') or len(name) > 180 or '/' in name or '\\' in name:
            raise ValueError('整册教材只支持 PDF 文件')
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BOOK:
            raise ValueError('教材 PDF 须非空且不超过 200 MB')
        with self.lock:
            self._clean()
            if len(self.sessions) >= 3:
                raise ValueError('已有整册教材在上传，请先完成页码选择')
            ident = str(uuid.uuid4())
            (self.root / f'{ident}.pdf').touch(exist_ok=False)
            chunk_size = size if size <= 50 * 1024 * 1024 else CHUNK
            self.sessions[ident] = {'project_id': project_id, 'name': name, 'size': size,
                                    'chunk_size': chunk_size,
                                    'received': 0, 'next_index': 0, 'created': time.time()}
            return {'upload_id': ident, 'chunk_size': chunk_size}

    def append(self, project_id, ident, index, encoded):
        with self.lock:
            session = self._session(project_id, ident)
            if not isinstance(index, int) or isinstance(index, bool) or index != session['next_index']:
                raise ValueError('上传分块顺序不正确，请重新选择文件')
            chunk_size = session['chunk_size']
            if not isinstance(encoded, str) or len(encoded) > (chunk_size * 4 // 3 + 8):
                raise ValueError('上传分块过大')
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError('上传分块格式不正确') from exc
            remaining = session['size'] - session['received']
            if not data or len(data) > chunk_size or len(data) > remaining or (remaining > chunk_size and len(data) != chunk_size):
                raise ValueError('上传分块大小不正确')
            with (self.root / f'{ident}.pdf').open('ab') as output:
                output.write(data)
            session['received'] += len(data)
            session['next_index'] += 1
            return {'received': session['received'], 'size': session['size'], 'next_index': session['next_index']}

    def status(self, project_id, ident):
        with self.lock:
            session = self._session(project_id, ident)
            return {'received': session['received'], 'size': session['size'],
                    'next_index': session['next_index']}

    def inspect(self, project_id, ident):
        import pymupdf
        with self.lock:
            session = self._session(project_id, ident)
            if session['received'] != session['size']:
                raise ValueError('文件尚未上传完成')
            try:
                with pymupdf.open(self.root / f'{ident}.pdf') as doc:
                    if doc.needs_pass or not doc.page_count:
                        raise ValueError('PDF 损坏或已加密')
                    session['pages'] = doc.page_count
            except (pymupdf.FileDataError, pymupdf.EmptyFileError) as exc:
                raise ValueError('PDF 损坏或格式不正确') from exc
            return {'pages': session['pages'], 'max_selected_pages': MAX_SELECTED_PAGES,
                    'name': session['name']}

    def finish(self, project_id, ident, selection):
        import pymupdf
        with self.lock:
            session = self._session(project_id, ident)
            if not session.get('pages'):
                raise ValueError('请先完成文件检查')
            pages = parse_pages(selection, session['pages'])
            if len(pages) == session['pages'] and session['size'] <= 50 * 1024 * 1024:
                filename = session['name']
                data = (self.root / f'{ident}.pdf').read_bytes()
            else:
                with pymupdf.open(self.root / f'{ident}.pdf') as source, pymupdf.open() as subset:
                    for page in pages:
                        subset.insert_pdf(source, from_page=page - 1, to_page=page - 1)
                    data = subset.tobytes(garbage=4, deflate=True)
                stem = Path(session['name']).stem[:110]
                label = (f'{pages[0]}-{pages[-1]}' if pages == list(range(pages[0], pages[-1] + 1))
                         else '_'.join(map(str, pages)))
                filename = f'{stem}-第{label}页.pdf'
            asset = self.store.save_asset(project_id, filename, data, 'textbook')
            self._remove(ident)
            return asset | {'selected_pages': pages}

    def cancel(self, project_id, ident):
        with self.lock:
            self._session(project_id, ident)
            self._remove(ident)
            return {'cancelled': True}
