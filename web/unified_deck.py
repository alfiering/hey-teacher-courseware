"""Inspect PPTX/Canva sources before selectively sending flat slides to Magic Layers."""
from __future__ import annotations

import json
from io import BytesIO
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from web.canva_client import CanvaError
from web.courseware_files import audit_courseware, inspect_pptx
from web.project_store import validate_file


def parse_canva_link(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('请粘贴 Canva 设计或文件夹链接')
    parts = urlsplit(url.strip())
    if parts.scheme != 'https' or parts.hostname not in ('www.canva.com', 'canva.com') or parts.username or parts.password:
        raise ValueError('只支持 canva.com 的 HTTPS 设计或文件夹链接')
    match = re.fullmatch(r'/(design|folder)/([A-Za-z0-9_-]{6,100})(?:/[^?#]*)?/?', parts.path)
    if not match:
        raise ValueError('请使用 Canva 设计或文件夹的完整链接')
    return match.group(1), match.group(2)


def page_kind(observation):
    if observation.get('flat_image_candidate'):
        return 'flat'
    if observation.get('editability') == 'editable' or observation.get('text_shapes') or observation.get('other_shapes'):
        return 'editable'
    return 'uncertain'


def safe_filename_stem(value):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', str(value or 'Canva 设计')).strip(' ._')[:100] or 'Canva 设计'


class UnifiedDeck:
    """Background inspector. POST jobs are recorded before sending and never blindly retried."""
    def __init__(self, store, client, coordinator):
        self.store, self.client, self.coordinator = store, client, coordinator
        self.lock = threading.RLock()
        self.workers = {}

    def inspect(self, project_id, asset_ids, canva_url, title):
        project = self.store.get_project(project_id)
        if not isinstance(asset_ids, list) or len(asset_ids) > 30:
            raise ValueError('一次最多选择 30 份 PPTX 或 PDF')
        sources = []
        for asset_id in asset_ids:
            asset = next((a for a in project['assets'] if a['id'] == asset_id and a['ext'] in ('.pptx', '.pdf')), None)
            if not asset:
                raise ValueError('请选择本课程内的 PPTX 或 PDF 文件')
            sources.append({'kind': 'local', 'asset_id': asset_id, 'title': asset['name']})
        if canva_url:
            kind, ident = parse_canva_link(canva_url)
            if not self.client:
                raise ValueError('请先连接 Canva 账号')
            sources.append({'kind': kind, 'id': ident})
        if not sources:
            raise ValueError('请上传 PPTX、PDF 或粘贴 Canva 链接')
        task = self.store.prepare_unified(project_id, sources, title)
        self._schedule(task['id'])
        return task

    def resume_pending(self):
        with self.store.db() as db:
            rows = list(db.execute("SELECT id,result FROM tasks WHERE stage='unified' AND status='running'"))
        for row in rows:
            if json.loads(row['result']).get('phase') == 'inspecting':
                self._schedule(row['id'])

    def retry_inspection(self, task_id):
        task = self.store.get_task(task_id)
        if task['stage'] != 'unified' or task['status'] != 'failed':
            raise ValueError('只有中断的检测任务可以继续')
        result = dict(task['result'])
        result.update(phase='inspecting', message='正在查询已提交的 Canva 任务')
        result.pop('error_code', None)
        result.pop('missing_scope', None)
        self._save(task_id, result)
        self._schedule(task_id)
        return self.store.get_task(task_id)

    def _schedule(self, task_id):
        with self.lock:
            thread = self.workers.get(task_id)
            if thread and thread.is_alive():
                return
            thread = threading.Thread(target=self._run, args=(task_id,), daemon=True, name='unified-inspect')
            self.workers[task_id] = thread
            thread.start()

    def _save(self, task_id, result, status='running'):
        return self.store.update_task(task_id, status, result)

    def _wait_job(self, job_id, getter, seconds=240):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            job = getter(job_id)['job']
            if job['status'] == 'success':
                return job
            if job['status'] == 'failed':
                raise ValueError('Canva 任务失败：' + str(job.get('error', {}).get('code', 'unknown')))
            time.sleep(3)
        raise TimeoutError('Canva 任务仍在处理中；稍后可以继续查询同一个任务')

    def _run(self, task_id):
        try:
            task = self.store.get_task(task_id)
            r = dict(task['result'])
            if any(s['kind'] == 'folder' for s in r['sources']):
                expanded = []
                for source in r['sources']:
                    if source['kind'] != 'folder':
                        expanded.append(source)
                        continue
                    for item in self.client.list_folder_designs(source['id']):
                        design = item.get('design') or {}
                        if design.get('id'):
                            expanded.append({'kind': 'design', 'id': design['id'], 'title': design.get('title', 'Canva 设计')})
                if not expanded or len(expanded) > 30:
                    raise ValueError('文件夹中需有 1 至 30 个可导出的设计；请分批处理')
                r['sources'] = expanded
                self._save(task_id, r)
            for index, source in enumerate(r['sources']):
                if source.get('inspected'):
                    continue
                r['message'] = f'检查来源 {index + 1}/{len(r["sources"])}'
                self._save(task_id, r)
                if source['kind'] == 'design':
                    self._inspect_design(task_id, r, source)
                else:
                    self._inspect_asset(task_id, r, source)
                source['inspected'] = True
                self._save(task_id, r)
            r['pages'] = [page for source in r['sources'] for page in source['pages']]
            if not 0 < len(r['pages']) <= 100:
                raise ValueError('合并课件须包含 1 至 100 页')
            r.update(phase='review', message='检测完成，请核对页序与需要拆层的页面')
            self._save(task_id, r, 'succeeded')
        except TimeoutError as exc:
            r = dict(self.store.get_task(task_id)['result'])
            r['message'] = str(exc)
            self._save(task_id, r, 'running')
            timer = threading.Timer(8, self._schedule, args=(task_id,))
            timer.daemon = True
            timer.start()
        except Exception as exc:
            r = dict(self.store.get_task(task_id)['result'])
            if isinstance(exc, CanvaError) and exc.code == 'missing_scope':
                r['error_code'] = 'missing_scope'
                if any(source.get('kind') == 'folder' for source in r['sources']):
                    r['missing_scope'] = 'folder:read'
                    message = 'Canva 授权缺少 folder:read：先在 Developer Portal 启用文件夹读取，再回到工作台重新连接 Canva 账号。'
                else:
                    message = 'Canva 授权缺少所需权限：请检查 Developer Portal 的 Scopes，并在工作台重新连接 Canva 账号。'
            else:
                message = str(exc) or type(exc).__name__
            r.update(phase='error', message=message[:240])
            self._save(task_id, r, 'failed')

    def _inspect_design(self, task_id, r, source):
        design = self.client.get_design(source['id'])['design']
        source['title'] = design.get('title') or source.get('title') or 'Canva 设计'
        if not source.get('export_job_id'):
            if source.get('export_attempted'):
                raise ValueError('导出提交结果未知；请检查 Canva 后重新发起检测')
            source['export_attempted'] = True
            self._save(task_id, r)
            job = self.client.start_export(source['id'])['job']
            source['export_job_id'] = job['id']
            source.pop('export_attempted', None)
            self._save(task_id, r)
        job = self._wait_job(source['export_job_id'], self.client.get_export)
        if not source.get('asset_id'):
            data = self.client.download(job['urls'][0])
            validate_file('Canva-检查.pptx', data, 'result')
            asset = self.store.save_asset(self.store.get_task(task_id)['project_id'], safe_filename_stem(source['title']) + '-Canva导出.pptx', data, 'result')
            source['asset_id'] = asset['id']
            self._save(task_id, r)
        self._inspect_asset(task_id, r, source)

    def _inspect_asset(self, task_id, r, source):
        if source.get('pages'):
            return
        project_id = self.store.get_task(task_id)['project_id']
        asset_id = source['asset_id']
        source_asset = next(a for a in self.store.get_project(project_id)['assets'] if a['id'] == asset_id)
        is_pdf = source_asset['ext'] == '.pdf'
        target = self.store.root / 'audits' / project_id / asset_id
        manifest_path = target / 'manifest.json'
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text())
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target.parent, prefix='unified-') as tmp:
                manifest = audit_courseware(self.store.asset_path(project_id, asset_id), Path(tmp))
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(tmp, target)
        pages = []
        for item in manifest['pages']:
            number = item['number']
            image = self.store.save_asset(project_id, f'{safe_filename_stem(source["title"])[:80]}-P{number:03}.jpg',
                                          (target / item['image']).read_bytes(), 'page')
            pages.append({'source_asset_id': asset_id, 'source_title': source['title'],
                          'design_id': source.get('id') if source['kind'] == 'design' else None,
                          'source_page': number, 'image_asset_id': image['id'],
                          'observation': item['observation'],
                          'kind': 'flat' if is_pdf else page_kind(item['observation'])})
        source['pages'] = pages

    def start(self, task_id, order, flat_pages, confirm_flat):
        task = self.store.get_task(task_id)
        if task['stage'] != 'unified' or task['status'] != 'succeeded' or task['result'].get('phase') != 'review':
            raise ValueError('请先完成页面检测')
        r = dict(task['result'])
        count = len(r['pages'])
        if (not isinstance(order, list) or not isinstance(flat_pages, list) or
            any(not isinstance(i, int) for i in order + flat_pages) or
            sorted(order) != list(range(count)) or len(set(flat_pages)) != len(flat_pages) or
            any(i < 0 or i >= count for i in flat_pages)):
            raise ValueError('页序或拆层选择无效')
        if confirm_flat != len(flat_pages):
            raise ValueError(f'请确认拆层 {len(flat_pages)} 页')
        if any(r['pages'][i]['kind'] != 'editable' and i not in flat_pages for i in range(count)):
            raise ValueError('整页图片或结构不确定的页面需要拆层，才能作为可编辑 PPTX 导出')
        ordered = [r['pages'][i] for i in order]
        flat_set = set(flat_pages)
        if not flat_set and len(r['sources']) == 1 and order == list(range(count)):
            source = self.store.asset_path(task['project_id'], r['sources'][0]['asset_id'])
            asset = self.store.save_asset(task['project_id'], safe_filename_stem(r['title']) + '-可编辑完整版.pptx', source.read_bytes(), 'result')
            r.update(phase='done', result_asset_id=asset['id'], message='保留已检测的可编辑 PPTX，无需拆层或合并')
            return self._save(task_id, r, 'succeeded')
        if not self.client:
            raise ValueError('合并或拆层前请先连接 Canva')
        # Local editable pages must be imported to Canva before the merge can reference them.
        for source in r['sources']:
            if source['kind'] != 'local' or not any(p['source_asset_id'] == source['asset_id'] and p not in [r['pages'][i] for i in flat_set] for p in ordered):
                continue
            if not source.get('import_design_id'):
                if source.get('import_attempted') and not source.get('import_job_id'):
                    raise ValueError('导入提交结果未知；请到 Canva 检查后再继续')
                if not source.get('import_job_id'):
                    source['import_attempted'] = True
                    self._save(task_id, r)
                    job = self.client.start_import(self.store.asset_path(task['project_id'], source['asset_id']), source['title'])['job']
                    source['import_job_id'] = job['id']
                    source.pop('import_attempted', None)
                    self._save(task_id, r)
                job = self._wait_job(source['import_job_id'], self.client.get_import)
                designs = job.get('result', {}).get('designs', [])
                if len(designs) != 1 or not designs[0].get('id'):
                    raise ValueError('Canva 导入结果不是一份完整课件，请单独检查')
                source['import_design_id'] = designs[0]['id']
                self._save(task_id, r)
            if not source.get('import_verified'):
                if not source.get('verify_export_job_id'):
                    if source.get('verify_export_attempted'):
                        raise ValueError('导入验证提交结果未知，请检查 Canva 后重试')
                    source['verify_export_attempted'] = True
                    self._save(task_id, r)
                    job = self.client.start_export(source['import_design_id'])['job']
                    source['verify_export_job_id'] = job['id']
                    source.pop('verify_export_attempted', None)
                    self._save(task_id, r)
                job = self._wait_job(source['verify_export_job_id'], self.client.get_export)
                exported = self.client.download(job['urls'][0])
                validate_file('Canva-导入验证.pptx', exported, 'result')
                observations = inspect_pptx(BytesIO(exported))
                if len(observations) != len(source['pages']):
                    raise ValueError('Canva 导入后页数发生变化，已停止合并')
                selected = [p['source_page'] for index, p in enumerate(r['pages'])
                            if index not in flat_set and p['source_asset_id'] == source['asset_id']]
                if any(page_kind(observations[number - 1]) != 'editable' for number in selected):
                    raise ValueError('Canva 导入使可编辑页变为图片，已停止合并以免损失可编辑性')
                source['import_verified'] = True
                self._save(task_id, r)
        refs = []
        for i in order:
            page = r['pages'][i]
            if i in flat_set:
                refs.append({'type': 'image', 'asset_id': page['image_asset_id']})
            else:
                source = next(s for s in r['sources'] if s['asset_id'] == page['source_asset_id'])
                refs.append({'type': 'design', 'design_id': page['design_id'] or source['import_design_id'],
                             'page_number': page['source_page'], 'asset_id': page['image_asset_id']})
        deck = self.store.prepare_hybrid_deck(task['project_id'], task_id, refs, safe_filename_stem(r['title']))
        self.coordinator.queue(deck['id'], confirm_flat)
        r.update(phase='processing', deck_id=deck['id'], chosen_order=order, flat_pages=flat_pages,
                 message='已建立合并任务')
        return self._save(task_id, r)
