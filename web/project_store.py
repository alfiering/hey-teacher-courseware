"""Local-only course projects. External tasks are claimed before sending requests."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import shutil
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

from common.slides_prompt import list_subjects

STAGES = ('materials', 'lesson', 'outline', 'notebook', 'canva', 'export', 'unified', 'automation')
STATUSES = {'ready', 'submitting', 'submitted', 'running', 'succeeded', 'failed',
            'submission_unknown', 'quota_exceeded', 'needs_login', 'paused'}
MAX_FILE = 10 * 1024 * 1024
MAX_COURSEWARE = 50 * 1024 * 1024
MAX_RESULT = 500 * 1024 * 1024  # layered PPTX exported from Canva can be large (15 pages ≈ 80 MB)
MAX_IMAGE_PIXELS = 80_000_000


def valid_id(value):
    try:
        return str(uuid.UUID(str(value))) == value
    except (ValueError, TypeError, AttributeError):
        return False


def validate_file(name, data, kind=None):
    if not isinstance(name, str) or not name or len(name) > 180 or '/' in name or '\\' in name or name in ('.', '..'):
        raise ValueError('文件名不合法')
    ext = Path(name).suffix.lower()
    limit = (MAX_RESULT if kind == 'result' else MAX_COURSEWARE) if ext in ('.pptx', '.pdf') else MAX_FILE
    if not data or len(data) > limit:
        raise ValueError(f'单文件须非空且不超过 {limit // 1024 // 1024} MB')
    try:
        if kind == 'skill':
            from web.lesson_skill import read_lesson_skill
            read_lesson_skill(name, data)
        elif ext in ('.txt', '.md'):
            data.decode('utf-8-sig')
        elif ext in ('.png', '.jpg', '.jpeg', '.webp'):
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                expected = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}[ext]
                if image.format != expected:
                    raise ValueError('图片内容与文件扩展名不符')
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError('图片超过 8000 万像素上限')
                image.verify()
        elif ext == '.pdf':
            import pymupdf as fitz
            with fitz.open(stream=data, filetype='pdf') as doc:
                if doc.needs_pass or not 0 < doc.page_count <= 100:
                    raise ValueError('PDF 须未加密且不超过 100 页')
        elif ext in ('.docx', '.pptx'):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > 10000 or sum(x.file_size for x in members) > 200 * 1024 * 1024:
                    raise ValueError('文档解压大小超限')
                if any(x.filename.startswith(('/', '\\')) or '..' in Path(x.filename.replace('\\', '/')).parts for x in members):
                    raise ValueError('文档包含不安全路径')
                main = 'word/document.xml' if ext == '.docx' else 'ppt/presentation.xml'
                root = ElementTree.fromstring(archive.read(main))
                expected = '{http://schemas.openxmlformats.org/' + ('wordprocessingml/2006/main}document' if ext == '.docx' else 'presentationml/2006/main}presentation')
                if root.tag != expected:
                    raise ValueError('文档主节点不合法')
                if ext == '.docx':
                    from docx import Document
                    Document(io.BytesIO(data))
                else:
                    from pptx import Presentation
                    presentation = Presentation(io.BytesIO(data))
                    if not 0 < len(presentation.slides) <= 100:
                        raise ValueError('课件须包含 1 至 100 页')
        else:
            raise ValueError('支持图片、PDF、DOCX、PPTX、TXT 和 Markdown')
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('文件损坏、格式不符或超过限制') from exc
    return ext


class ProjectStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, metadata TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY, project_id TEXT, stage TEXT, payload TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, project_id TEXT, name TEXT, kind TEXT, sha256 TEXT, ext TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project_id TEXT, stage TEXT, input_version TEXT, input_hash TEXT, status TEXT, result TEXT, stale INTEGER DEFAULT 0, created REAL,
                    UNIQUE(project_id,stage,input_version,input_hash));
            ''')
            # Image snapshot tasks depend only on the image hash; earlier builds wrongly marked them stale.
            db.execute("UPDATE tasks SET stale=0 WHERE stage='canva' AND stale=1 AND input_version IN (SELECT id FROM versions WHERE stage='canva')")

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.root / 'projects.sqlite3'), timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_project(self, metadata: dict):
        title = str(metadata.get('title', '')).strip()
        if not title or len(title) > 120 or metadata.get('subject') not in {s['id'] for s in list_subjects()}:
            raise ValueError('请填写课程名称并选择学科')
        allowed = ('title', 'subject', 'grade', 'edition', 'unit', 'language', 'periods', 'minutes', 'target_pages')
        clean = {k: metadata[k] for k in allowed if k in metadata}
        clean['title'] = title
        for key, default, low, high in [('periods', 1, 1, 30), ('minutes', 35, 5, 180), ('target_pages', 15, 1, 100)]:
            clean[key] = int(clean.get(key, default))
            if not low <= clean[key] <= high:
                raise ValueError('课时、时长或页数超出范围')
        ident = str(uuid.uuid4())
        with self.db() as db:
            db.execute('INSERT INTO projects VALUES(?,?,?)', (ident, json.dumps(clean, ensure_ascii=False), time.time()))
        return self.get_project(ident)

    def list_projects(self):
        with self.db() as db:
            return [{'id': r['id'], 'metadata': json.loads(r['metadata'])} for r in db.execute('SELECT * FROM projects ORDER BY created DESC')]

    def get_project(self, ident):
        if not valid_id(ident):
            raise FileNotFoundError('项目不存在')
        with self.db() as db:
            p = db.execute('SELECT * FROM projects WHERE id=?', (ident,)).fetchone()
            if not p:
                raise FileNotFoundError('项目不存在')
            versions = [dict(r) for r in db.execute('SELECT * FROM versions WHERE project_id=? ORDER BY created DESC', (ident,))]
            for v in versions:
                v['payload'] = json.loads(v['payload'])
            assets = [dict(r) for r in db.execute('SELECT * FROM assets WHERE project_id=? ORDER BY created', (ident,))]
            tasks = [self._task(r) for r in db.execute('SELECT * FROM tasks WHERE project_id=? ORDER BY created', (ident,))]
        return {'id': ident, 'metadata': json.loads(p['metadata']), 'versions': versions, 'assets': assets, 'tasks': tasks}

    def save_version(self, project_id, stage, payload):
        self.get_project(project_id)
        if stage not in STAGES or not isinstance(payload, dict) or len(json.dumps(payload)) > 500000:
            raise ValueError('版本内容不合法')
        ident = str(uuid.uuid4())
        with self.db() as db:
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?)', (ident, project_id, stage, json.dumps(payload, ensure_ascii=False), time.time()))
            # The automation task owns its own sequence; its internal version writes must not stale it.
            downstream = [s for s in STAGES[STAGES.index(stage) + 1:] if s != 'automation']
            for s in downstream:
                # Image snapshot tasks (input version stage 'canva') are bound to their image hash, not to text revisions.
                db.execute("UPDATE tasks SET stale=1 WHERE project_id=? AND stage=? AND input_version NOT IN (SELECT id FROM versions WHERE project_id=? AND stage='canva')",
                           (project_id, s, project_id))
        return {'id': ident, 'stage': stage, 'payload': payload}

    def save_asset(self, project_id, filename, content, kind):
        self.get_project(project_id)
        if kind not in ('textbook', 'lesson', 'standard', 'supplement', 'courseware', 'result', 'page', 'character', 'template', 'skill'):
            raise ValueError('资料类型不合法')
        ext = validate_file(filename, content, kind)
        ident = str(uuid.uuid4())
        folder = self.root / 'files' / project_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (ident + ext)
        digest = hashlib.sha256(content).hexdigest()
        try:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute('SELECT id,name,kind,sha256,ext FROM assets WHERE project_id=? AND name=? AND kind=? AND sha256=? ORDER BY created LIMIT 1',
                                      (project_id, filename, kind, digest)).fetchone()
                if existing and (folder / (existing['id'] + existing['ext'])).is_file():
                    return dict(existing) | {'reused': True}
                path.write_bytes(content)
                db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?)', (ident, project_id, filename, kind, digest, ext, time.time()))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {'id': ident, 'name': filename, 'kind': kind, 'sha256': digest, 'ext': ext, 'reused': False}

    def asset_path(self, project_id, asset_id):
        self.get_project(project_id)
        with self.db() as db:
            row = db.execute('SELECT * FROM assets WHERE id=? AND project_id=?', (asset_id, project_id)).fetchone()
        if not row:
            raise FileNotFoundError('资料不存在')
        path = (self.root / 'files' / project_id / (row['id'] + row['ext'])).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError('资料不存在')
        return path

    def _trash_dir(self, label):
        target = self.root / 'trash' / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-{uuid.uuid4().hex[:6]}"
        target.mkdir(parents=True, exist_ok=False)
        return target

    def delete_project(self, project_id):
        """Remove a project from the workbench; its rows and files are kept in trash/ for manual recovery."""
        project = self.get_project(project_id)
        target = self._trash_dir(project_id)
        (target / 'project.json').write_text(json.dumps(project, ensure_ascii=False, indent=1), encoding='utf-8')
        for sub in ('files', 'audits'):
            source = self.root / sub / project_id
            if source.is_dir():
                shutil.move(str(source), str(target / sub))
        with self.db() as db:
            for table in ('tasks', 'assets', 'versions'):
                db.execute(f'DELETE FROM {table} WHERE project_id=?', (project_id,))
            db.execute('DELETE FROM projects WHERE id=?', (project_id,))
        return {'deleted': project_id, 'title': project['metadata']['title']}

    def delete_asset(self, project_id, asset_id):
        """Remove one file from a project; the file is moved to trash/ rather than erased."""
        project = self.get_project(project_id)
        asset = next((a for a in project['assets'] if a['id'] == asset_id), None)
        if not asset:
            raise FileNotFoundError('资料不存在')
        target = self._trash_dir(f'{project_id}-{asset_id}')
        (target / 'asset.json').write_text(json.dumps(asset, ensure_ascii=False, indent=1), encoding='utf-8')
        path = self.root / 'files' / project_id / (asset['id'] + asset['ext'])
        if path.is_file():
            shutil.move(str(path), str(target / (Path(asset['name']).name or path.name)))
        audit = self.root / 'audits' / project_id / asset_id
        if audit.is_dir():
            shutil.move(str(audit), str(target / 'audit'))
        with self.db() as db:
            db.execute('DELETE FROM assets WHERE id=? AND project_id=?', (asset_id, project_id))
        return {'deleted': asset_id, 'name': asset['name']}

    def create_task(self, project_id, stage, input_version, input_hash):
        self.get_project(project_id)
        if stage not in STAGES or not input_hash or len(input_hash) > 128:
            raise ValueError('任务输入不合法')
        with self.db() as db:
            if not db.execute('SELECT 1 FROM versions WHERE id=? AND project_id=?', (input_version, project_id)).fetchone():
                raise ValueError('输入版本不属于该项目')
            db.execute('INSERT OR IGNORE INTO tasks(id,project_id,stage,input_version,input_hash,status,result,created) VALUES(?,?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()), project_id, stage, input_version, input_hash, 'ready', '{}', time.time()))
            return self._task(db.execute('SELECT * FROM tasks WHERE project_id=? AND stage=? AND input_version=? AND input_hash=?',
                                        (project_id, stage, input_version, input_hash)).fetchone())

    def prepare_image_task(self, project_id, asset_id):
        """One immutable image identity, one task; never reset an existing claim."""
        self.get_project(project_id)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            asset = db.execute('SELECT * FROM assets WHERE id=? AND project_id=?', (asset_id, project_id)).fetchone()
            if not asset or asset['ext'] not in ('.png', '.jpg', '.jpeg', '.webp'):
                raise ValueError('请选择项目内的单页图片')
            existing = db.execute("SELECT * FROM tasks WHERE project_id=? AND stage='canva' AND input_hash=? ORDER BY created LIMIT 1", (project_id, asset['sha256'])).fetchone()
            if existing:
                return self._task(existing)
            version_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
            payload = json.dumps({'asset_id': asset_id})
            # Image snapshots do not revise course materials or invalidate sibling image tasks.
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?)', (version_id, project_id, 'canva', payload, time.time()))
            db.execute('INSERT INTO tasks(id,project_id,stage,input_version,input_hash,status,result,created) VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, project_id, 'canva', version_id, asset['sha256'], 'ready', payload, time.time()))
            return self._task(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def prepare_deck(self, project_id, source_asset_id, page_asset_ids, title):
        """One deck task per source file; its page tasks are ordinary image tasks."""
        project = self.get_project(project_id)
        source = next((a for a in project['assets'] if a['id'] == source_asset_id), None)
        if not source or source['ext'] not in ('.pptx', '.pdf') or not page_asset_ids:
            raise ValueError('请选择生成好的 PPTX 或 PDF')
        page_tasks = [self.prepare_image_task(project_id, a)['id'] for a in page_asset_ids]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute("SELECT * FROM tasks WHERE project_id=? AND stage='export' AND input_hash=? ORDER BY created LIMIT 1",
                                  (project_id, 'deck:' + source['sha256'])).fetchone()
            if existing:
                return self._task(existing)
            version_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
            payload = {'deck_source': source_asset_id, 'page_asset_ids': page_asset_ids}
            # Snapshot version uses stage 'canva' so text revisions never mark the deck stale.
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?)', (version_id, project_id, 'canva', json.dumps(payload), time.time()))
            result = {'source_asset_id': source_asset_id, 'page_asset_ids': page_asset_ids, 'page_task_ids': page_tasks,
                      'phase': 'pages', 'title': title}
            db.execute('INSERT INTO tasks(id,project_id,stage,input_version,input_hash,status,result,created) VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, project_id, 'export', version_id, 'deck:' + source['sha256'], 'ready', json.dumps(result, ensure_ascii=False), time.time()))
            return self._task(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def prepare_unified(self, project_id, sources, title):
        """Persist an inspection before any Canva export/import request is made."""
        self.get_project(project_id)
        if not isinstance(sources, list) or not 0 < len(sources) <= 30 or not isinstance(title, str):
            raise ValueError('请选择 1 至 30 个来源')
        version_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
        result = {'sources': sources, 'title': title[:120] or '合并课件', 'phase': 'inspecting', 'pages': []}
        with self.db() as db:
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?)', (version_id, project_id, 'canva', '{}', time.time()))
            db.execute('INSERT INTO tasks(id,project_id,stage,input_version,input_hash,status,result,created) VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, project_id, 'unified', version_id, task_id, 'running', json.dumps(result, ensure_ascii=False), time.time()))
        return self.get_task(task_id)

    def prepare_hybrid_deck(self, project_id, inspection_id, page_refs, title):
        """Only image pages become Magic Layers tasks; editable pages retain source design references."""
        inspection = self.get_task(inspection_id)
        if inspection['project_id'] != project_id or inspection['stage'] != 'unified' or not page_refs:
            raise ValueError('检查结果不属于该项目')
        page_tasks = []
        for ref in page_refs:
            if ref['type'] == 'image':
                task = self.prepare_image_task(project_id, ref['asset_id'])
                page_tasks.append(task['id'])
                ref['task_id'] = task['id']
            elif ref['type'] != 'design':
                raise ValueError('页面来源不合法')
        with self.db() as db:
            existing = db.execute("SELECT * FROM tasks WHERE project_id=? AND stage='export' AND input_hash=?", (project_id, 'hybrid:' + inspection_id)).fetchone()
            if existing:
                return self._task(existing)
            version_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
            result = {'inspection_id': inspection_id, 'page_refs': page_refs, 'page_task_ids': page_tasks,
                      'source_asset_id': inspection['result']['sources'][0].get('asset_id'),
                      'phase': 'pages', 'title': title[:120], 'expected_pages': len(page_refs)}
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?)', (version_id, project_id, 'canva', '{}', time.time()))
            db.execute('INSERT INTO tasks(id,project_id,stage,input_version,input_hash,status,result,created) VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, project_id, 'export', version_id, 'hybrid:' + inspection_id, 'ready', json.dumps(result, ensure_ascii=False), time.time()))
        return self.get_task(task_id)

    @staticmethod
    def _task(row):
        result = dict(row)
        result['result'] = json.loads(result['result'])
        result['stale'] = bool(result['stale'])
        return result

    def get_task(self, task_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise FileNotFoundError('任务不存在')
        return self._task(row)

    def update_task(self, task_id, status, result):
        if status not in STATUSES or not isinstance(result, dict):
            raise ValueError('任务状态不合法')
        with self.db() as db:
            n = db.execute('UPDATE tasks SET status=?,result=? WHERE id=?', (status, json.dumps(result, ensure_ascii=False), task_id)).rowcount
        if not n:
            raise FileNotFoundError('任务不存在')
        return self.get_task(task_id)

    RETRYABLE = ('failed', 'paused', 'quota_exceeded', 'submission_unknown')

    def reset_task_for_retry(self, task_id, acknowledge_unknown=False):
        """Teacher-initiated retry: archive the previous attempt and make the task submittable again."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise FileNotFoundError('任务不存在')
            task = self._task(row)
            result = task['result']
            if task['stale']:
                raise ValueError('输入版本已过期，不能重试该任务')
            if task['status'] not in self.RETRYABLE or result.get('design_id'):
                raise ValueError('该任务当前状态不能重试')
            if task['status'] == 'submission_unknown' and not acknowledge_unknown:
                raise ValueError('上次提交结果未知：请先到 Canva 确认没有生成设计，再确认重试')
            keep = {k: result[k] for k in ('asset_id',) if k in result}
            attempt = {k: v for k, v in result.items() if k not in ('asset_id', 'attempts')}
            attempt.update({'status': task['status'], 'reset_at': time.time()})
            keep['attempts'] = (result.get('attempts') or [])[-9:] + [attempt]
            db.execute("UPDATE tasks SET status='ready',result=? WHERE id=?", (json.dumps(keep, ensure_ascii=False), task_id))
            return self._task(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def claim_task(self, task_id):
        with self.db() as db:
            return bool(db.execute("UPDATE tasks SET status='submitting' WHERE id=? AND status='ready' AND stale=0", (task_id,)).rowcount)
