"""One-page proof with persistent, fail-closed submission records."""
import threading
import time

from web.canva_client import CanvaError

NETWORK_RETRIES = 5
_NETWORK_HINTS = ('timed out', 'timeout', 'connection', 'reset by peer', 'remote end closed', 'eof occurred',
                  'temporarily unavailable', 'name resolution', 'nodename nor servname', 'network is unreachable',
                  '下载中断', 'urlopen error')


def is_network_error(error):
    """A dropped or stalled connection, which retrying can fix (unlike a Canva refusal or a wrong page count)."""
    import http.client
    import urllib.error
    if isinstance(error, BaseException):
        if isinstance(error, urllib.error.HTTPError):
            return False
        return isinstance(error, (OSError, http.client.HTTPException))
    text = str(error or '').lower()
    return any(hint in text for hint in _NETWORK_HINTS)


class CanvaTasks:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.lock = threading.RLock()
        self.task_locks = {}
        self._capabilities_checked = False

    def _lock_for(self, task_id):
        with self.lock:
            return self.task_locks.setdefault(task_id, threading.RLock())

    def submit(self, task_id):
        with self._lock_for(task_id):
            task = self.store.get_task(task_id)
            if task['stale']:
                raise ValueError('输入版本已过期，请检查课程资料')
            if task['status'] != 'ready':
                return task
            if task['result'].get('retry_at', 0) > time.time():
                return task
            with self.lock:
                if not self._capabilities_checked:
                    if 'image_to_design_imports' not in self.client.capabilities().get('capabilities', []):
                        raise ValueError('当前账号尚无 Magic Layers API 能力')
                    self._capabilities_checked = True
            path = self.store.asset_path(task['project_id'], task['result']['asset_id'])
            if not self.store.claim_task(task_id):
                return self.store.get_task(task_id)
            result = dict(task['result'])
            try:
                job = self.client.upload_image(path)['job']
                result.pop('error_code', None)
                result.pop('retry_at', None)
                result['upload_job_id'] = job['id']
                return self.store.update_task(task_id, 'submitted', result)
            except Exception as exc:
                return self._error(task_id, result, exc, ambiguous=True)

    def _error(self, task_id, result, exc, ambiguous=False):
        result = dict(result)
        if isinstance(exc, CanvaError):
            result['error_code'] = exc.code
            if exc.status == 429:
                result['retry_at'] = time.time() + exc.retry_after
                # A 429 explicitly rejects a mutating request, so no remote job was created.
                # Keep existing jobs in place; a rejected upload can safely return to ready.
                status = self.store.get_task(task_id)['status'] if not ambiguous else 'ready'
            elif exc.status == 402:
                status = 'quota_exceeded'
            elif exc.status in (401, 403):
                status = 'needs_login'
            elif not ambiguous and exc.status >= 500:
                status = self.store.get_task(task_id)['status']
                result['retry_at'] = time.time() + exc.retry_after
            else:
                status = 'submission_unknown' if ambiguous and exc.status >= 500 else 'failed'
        else:
            result['error_code'] = 'network_or_invalid_response'
            status = 'submission_unknown' if ambiguous else self.store.get_task(task_id)['status']
            if not ambiguous:
                result['retry_at'] = time.time() + 30
        return self.store.update_task(task_id, status, result)

    def poll(self, task_id):
        with self._lock_for(task_id):
            task = self.store.get_task(task_id)
            result = dict(task['result'])
            if task['status'] not in ('submitted', 'running', 'submitting', 'needs_login') or result.get('retry_at', 0) > time.time():
                return task
            result.pop('retry_at', None)
            try:
                if result.get('export_job_id'):
                    job = self.client.get_export(result['export_job_id'])['job']
                    if job['status'] == 'success':
                        result['download_urls'] = job['urls']
                        self._save_exports(task, result)
                        return self.store.update_task(task_id, 'succeeded', result)
                elif result.get('conversion_job_id'):
                    job = self.client.get_conversion(result['conversion_job_id'])['job']
                    if job['status'] == 'success':
                        design = job['result']['design']
                        result['design_id'] = design['id']
                        result['edit_url'] = design.get('urls', {}).get('edit_url', '')
                        return self.store.update_task(task_id, 'succeeded', result)
                elif result.get('conversion_attempted') or task['status'] == 'submitting':
                    result['error_code'] = 'restart_after_submission'
                    return self.store.update_task(task_id, 'submission_unknown', result)
                else:
                    job = self.client.get_upload(result['upload_job_id'])['job']
                    if job['status'] == 'success':
                        if task['stale']:
                            result['error_code'] = 'stale_input_no_new_conversion'
                            return self.store.update_task(task_id, 'paused', result)
                        result['canva_asset_id'] = job['asset']['id']
                        result['conversion_attempted'] = True
                        self.store.update_task(task_id, 'submitting', result)
                        try:
                            converted = self.client.start_conversion(result['canva_asset_id'], '课件单页拆分测试')['job']
                            result['conversion_job_id'] = converted['id']
                            return self.store.update_task(task_id, 'running', result)
                        except Exception as exc:
                            return self._error(task_id, result, exc, ambiguous=True)
                if job['status'] == 'failed':
                    result['error_code'] = job.get('error', {}).get('code', 'conversion_failed')
                    return self.store.update_task(task_id, 'failed', result)
                return self.store.update_task(task_id, 'running' if result.get('conversion_job_id') else 'submitted', result)
            except Exception as exc:
                return self._error(task_id, result, exc)

    def export(self, task_id):
        with self._lock_for(task_id):
            task = self.store.get_task(task_id)
            result = dict(task['result'])
            if task['stale'] or not result.get('design_id'):
                raise ValueError('请先完成当前版本的转换')
            if result.get('export_job_id'):
                return task
            if result.get('export_attempted'):
                raise ValueError('导出提交结果未知，请先检查 Canva')
            result['export_attempted'] = True
            self.store.update_task(task_id, 'submitting', result)
            try:
                job = self.client.start_export(result['design_id'])['job']
                result['export_job_id'] = job['id']
                return self.store.update_task(task_id, 'running', result)
            except Exception as exc:
                return self._error(task_id, result, exc, ambiguous=True)

    def _save_exports(self, task, result):
        """Persist exported PPTX files into the project; Canva URLs expire, local files do not."""
        project = self.store.get_project(task['project_id'])
        source = next((a for a in project['assets'] if a['id'] == result.get('asset_id')), None)
        stem = (source['name'].rsplit('.', 1)[0] if source else 'canva')[:120]
        urls = result.get('download_urls') or []
        saved = []
        try:
            for index, url in enumerate(urls):
                suffix = f'-{index + 1}' if len(urls) > 1 else ''
                data = self.client.download(url)
                if not isinstance(data, (bytes, bytearray)):
                    raise ValueError('导出文件内容无效')
                asset = self.store.save_asset(task['project_id'], f'{stem}-拆层{suffix}.pptx', bytes(data), 'result')
                saved.append(asset['id'])
        except Exception as exc:  # keep the succeeded design; the teacher can fetch again
            result['download_error'] = str(exc)[:200] or type(exc).__name__
            return result
        result.pop('download_error', None)
        result['result_asset_ids'] = saved
        return result

    def download(self, task_id):
        """Re-fetch the export (fresh URLs from the existing export job) and save it locally. No new paid job."""
        with self._lock_for(task_id):
            task = self.store.get_task(task_id)
            result = dict(task['result'])
            if task['status'] != 'succeeded' or not result.get('export_job_id'):
                raise ValueError('请先完成导出')
            job = self.client.get_export(result['export_job_id'])['job']
            if job.get('status') != 'success':
                raise ValueError('导出尚未完成')
            result['download_urls'] = job['urls']
            self._save_exports(task, result)
            return self.store.update_task(task_id, 'succeeded', result)

    def retry(self, task_id, acknowledge_unknown=False):
        """Teacher-confirmed retry. This may consume Canva AI credits again."""
        with self._lock_for(task_id):
            self.store.reset_task_for_retry(task_id, acknowledge_unknown)
        return self.submit(task_id)


class DeckFlow:
    """Merge native Canva pages and selectively converted images into one PPTX."""
    PAGE_DONE = 'succeeded'

    def __init__(self, store, client, tasks):
        self.store, self.client, self.tasks = store, client, tasks
        self.lock = threading.RLock()

    def _deck(self, deck_id):
        deck = self.store.get_task(deck_id)
        if deck['stage'] != 'export' or 'page_task_ids' not in deck['result']:
            raise ValueError('不是整套拆分任务')
        return deck

    def _pages(self, deck):
        return [self.store.get_task(t) for t in deck['result']['page_task_ids']]

    def submit(self, deck_id, confirm_pages):
        with self.lock:
            deck = self._deck(deck_id)
            ready = [t for t in self._pages(deck) if t['status'] == 'ready']
            if not ready:
                raise ValueError('没有待提交的页面')
            if confirm_pages != len(ready):
                raise ValueError(f'请确认提交 {len(ready)} 页')
            for task in ready:
                result = self.tasks.submit(task['id'])
                if result['status'] in ('quota_exceeded', 'needs_login', 'submission_unknown', 'paused'):
                    break  # stop the batch; finished pages are kept
            return self.view(deck_id)

    def resume(self, deck_id, acknowledge_unknown=False):
        with self.lock:
            deck = self._deck(deck_id)
            result = dict(deck['result'])
            unknown = (result.get('merge_attempted') and not result.get('merge_job_id') or
                       result.get('export_attempted') and not result.get('export_job_id'))
            if unknown and acknowledge_unknown:
                result.pop('merge_attempted', None)
                result.pop('export_attempted', None)
            for key in ('error', 'error_kind', 'network_failures', 'retry_at'):
                result.pop(key, None)
            self.store.update_task(deck_id, 'running', result)
            return self.view(deck_id)

    def view(self, deck_id):
        deck = self._deck(deck_id)
        deck['pages'] = [{'task_id': t['id'], 'status': t['status'], 'asset_id': t['result'].get('asset_id'),
                          'edit_url': t['result'].get('edit_url', ''), 'error_code': t['result'].get('error_code', '')}
                         for t in self._pages(deck)]
        return deck

    def poll(self, deck_id):
        with self.lock:
            deck = self._deck(deck_id)
            r = dict(deck['result'])
            if r.get('phase') == 'done' or r.get('error'):
                return self.view(deck_id)
            pages = self._pages(deck)
            if r.get('phase', 'pages') == 'pages':
                if not pages or any(t['status'] != self.PAGE_DONE for t in pages):
                    if not r.get('page_refs') or any(t['status'] != self.PAGE_DONE for t in pages):
                        return self.view(deck_id)
                if r.get('page_refs'):
                    designs = []
                    for ref in r['page_refs']:
                        if ref['type'] == 'image':
                            designs.append({'design_id': self.store.get_task(ref['task_id'])['result']['design_id']})
                        else:
                            designs.append({'design_id': ref['design_id'], 'page_number': ref['page_number']})
                else:
                    designs = [t['result']['design_id'] for t in pages]
                r.update(phase='merging', merge_step=0, designs=designs)
                self.store.update_task(deck_id, 'running', r)
            try:
                if r['phase'] == 'merging':
                    self._merge_step(deck, r)
                elif r['phase'] == 'exporting':
                    self._export_step(deck, r)
            except CanvaError as exc:
                if exc.status == 429 or exc.status >= 500:
                    r['retry_at'] = time.time() + exc.retry_after
                else:
                    r['error'] = f'Canva HTTP {exc.status}（{exc.code}）'
                self.store.update_task(deck_id, 'running', r)
            except Exception as exc:
                message = (str(exc) or type(exc).__name__)[:200]
                if is_network_error(exc) and r.get('network_failures', 0) < NETWORK_RETRIES:
                    # A dropped connection: keep every finished step and try again shortly by itself.
                    r['network_failures'] = r.get('network_failures', 0) + 1
                    wait = 30 * r['network_failures']
                    r['retry_at'] = time.time() + wait
                    r['auto_message'] = f'网络不稳定（{message[:60]}），约 {wait} 秒后自动重试（第 {r["network_failures"]}/{NETWORK_RETRIES} 次）'
                else:
                    r['error'] = message
                    if is_network_error(exc):
                        r['error_kind'] = 'network'
                self.store.update_task(deck_id, 'running', r)
            else:
                # Only a step that actually ran (not one still waiting out retry_at) clears the failure count.
                if 'retry_at' not in r and r.pop('network_failures', None) is not None and r.get('phase') != 'done':
                    r['auto_message'] = '网络已恢复，继续合并和导出'
                    self.store.update_task(deck_id, 'running', r)
            return self.view(deck_id)

    def _save(self, deck, r, status='running'):
        self.store.update_task(deck['id'], status, r)

    def _merge_step(self, deck, r):
        if r.get('retry_at', 0) > time.time():
            return
        if r.pop('retry_at', None) and r.get('network_failures'):
            r['auto_message'] = f'正在重试（第 {r["network_failures"]}/{NETWORK_RETRIES} 次）'
            self._save(deck, r)
        designs, step = r['designs'], r['merge_step']
        if r.get('merge_job_id'):
            job = self.client.get_merge(r['merge_job_id'])['job']
            if job['status'] == 'success':
                design = job['result']['design']
                r['merged_design_id'] = design['id']
                r['edit_url'] = design.get('urls', {}).get('edit_url', r.get('edit_url', ''))
                r['merge_step'] = step + 1
                r.pop('merge_job_id', None)
                if r['merge_step'] >= len(designs):
                    r['phase'] = 'exporting'
            elif job['status'] == 'failed':
                r['error'] = '合并失败：' + job.get('error', {}).get('code', 'merge_error')
            return self._save(deck, r)
        if r.get('merge_attempted') and r.get('merged_design_id') and step > 0:
            # Response of an earlier merge POST was lost: check the merged design before posting again.
            count = self.client.get_design(r['merged_design_id'])['design'].get('page_count', 0)
            if count >= step + 1:
                r['merge_step'] = step + 1
                r.pop('merge_attempted', None)
                if r['merge_step'] >= len(designs):
                    r['phase'] = 'exporting'
                return self._save(deck, r)
        if r.get('merge_attempted'):
            r['error'] = '合并提交结果未知，请到 Canva 检查后明确确认重试'
            return self._save(deck, r)
        item = designs[step]
        source = {'type': 'design', 'design_id': item if isinstance(item, str) else item['design_id']}
        if isinstance(item, dict) and item.get('page_number'):
            source['page_numbers'] = [item['page_number']]
        op = {'type': 'insert_pages', 'source': source}
        if step == 0:
            body = {'type': 'create_new_design', 'title': r.get('title', '拆层课件')[:255], 'operations': [op]}
        else:
            body = {'type': 'modify_existing_design', 'design_id': r['merged_design_id'], 'operations': [op]}
        r['merge_attempted'] = True
        self._save(deck, r)
        job = self.client.start_merge(body)['job']
        r['merge_job_id'] = job['id']
        r.pop('merge_attempted', None)
        self._save(deck, r)

    def _export_step(self, deck, r):
        if r.get('retry_at', 0) > time.time():
            return
        if r.pop('retry_at', None) and r.get('network_failures'):
            r['auto_message'] = f'正在重试（第 {r["network_failures"]}/{NETWORK_RETRIES} 次）'
            self._save(deck, r)
        if r.get('result_asset_id'):
            return self._finish_export(deck, r)
        if not r.get('export_job_id'):
            if r.get('export_attempted'):
                r['error'] = '导出提交结果未知，请到 Canva 检查后明确确认重试'
                return self._save(deck, r)
            r['export_attempted'] = True
            self._save(deck, r)
            job = self.client.start_export(r['merged_design_id'])['job']
            r['export_job_id'] = job['id']
            r['transfer_stage'] = 'canva_rendering'
            r.pop('export_attempted', None)
            return self._save(deck, r)
        job = self.client.get_export(r['export_job_id'])['job']
        if job['status'] == 'failed':
            r['error'] = '导出失败：' + job.get('error', {}).get('code', 'export_failed')
            return self._save(deck, r)
        if job['status'] != 'success':
            if r.get('transfer_stage') != 'canva_rendering':
                r['transfer_stage'] = 'canva_rendering'
                self._save(deck, r)
            return
        r.update(transfer_stage='downloading', download_bytes=0, download_total_bytes=None)
        self._save(deck, r)
        last_report = [0.0]
        def progress(downloaded, total):
            r['download_bytes'] = downloaded
            r['download_total_bytes'] = total
            now = time.monotonic()
            if downloaded == 0 or now - last_report[0] >= .7 or total and downloaded >= total:
                last_report[0] = now
                self._save(deck, r)
        data = self.client.download(job['urls'][0], on_progress=progress)
        r.update(transfer_stage='validating', download_bytes=len(data), download_total_bytes=len(data))
        self._save(deck, r)
        if r.get('expected_pages'):
            from io import BytesIO
            from pptx import Presentation
            from web.courseware_files import inspect_pptx
            actual_pages = len(Presentation(BytesIO(data)).slides)
            if actual_pages != r['expected_pages']:
                r['error'] = f'导出页数不符：预期 {r["expected_pages"]} 页，实际 {actual_pages} 页'
                return self._save(deck, r)
            observations = inspect_pptx(BytesIO(data))
            uncertain = [index + 1 for index, item in enumerate(observations)
                         if item['editability'] != 'editable']
            r['editability_check'] = {'native_pages': actual_pages - len(uncertain),
                                      'needs_review_pages': uncertain}
        r['transfer_stage'] = 'saving'
        self._save(deck, r)
        asset = self.store.save_asset(deck['project_id'], r.get('title', '课件')[:120] + '-拆层完整版.pptx', data, 'result')
        r.update(result_asset_id=asset['id'], transfer_stage='font_fix')
        self._save(deck, r)
        return self._finish_export(deck, r)

    def _finish_export(self, deck, r):
        if not r.get('fixed_asset_id') and not r.get('fix_error'):
            r['transfer_stage'] = 'font_fix'
            self._save(deck, r)
            try:  # WPS ignores Canva's embedded fonts; keep a local font-fixed copy.
                from web.local_server import fix_deck_fonts
                r['fixed_asset_id'] = fix_deck_fonts(self.store, deck['project_id'], r['result_asset_id'])['id']
            except Exception as exc:
                r['fix_error'] = (str(exc) or type(exc).__name__)[:200]
        r.update(phase='done', transfer_stage='done')
        self._save(deck, r, 'succeeded')
