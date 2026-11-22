"""Bounded, persistent background processing for Canva deck tasks."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from web.canva_client import CanvaError


BLOCKED = {'failed', 'paused', 'quota_exceeded', 'needs_login', 'submission_unknown'}
ACTIVE = {'submitting', 'submitted', 'running'}


def deck_overview(store, coordinator=None):
    """Read-only progress for every project, including jobs not open in the browser."""
    with store.db() as db:
        projects = {row['id']: json.loads(row['metadata']).get('title', '未命名课程')
                    for row in db.execute('SELECT id,metadata FROM projects')}
        assets = {row['id']: row['name'] for row in db.execute('SELECT id,name FROM assets')}
        rows = list(db.execute("SELECT id,project_id,status,result,created FROM tasks WHERE stage IN ('export','canva')"))
    pages = {row['id']: (row['status'], json.loads(row['result'] or '{}')) for row in rows}
    result = []
    for row in rows:
        if row['status'] not in ('ready', 'running', 'succeeded'):
            continue
        state = json.loads(row['result'] or '{}')
        ids = state.get('page_task_ids')
        if not ids:
            continue
        related = [pages[x] for x in ids if x in pages]
        counts = {status: sum(item[0] == status for item in related)
                  for status in ('ready', 'submitting', 'submitted', 'running', 'succeeded', *BLOCKED)}
        sent = sum(bool(details.get('upload_job_id') or details.get('canva_asset_id') or details.get('design_id'))
                   or status == 'succeeded' for status, details in related)
        queued = bool(state.get('auto_run'))
        worker = coordinator.worker_state(row['id']) if coordinator else None
        result.append({'deck_id': row['id'], 'project_id': row['project_id'],
                       'project_title': projects.get(row['project_id'], '未命名课程'),
                       'source_name': assets.get(state.get('source_asset_id'), '课件'),
                       'total': len(ids), 'sent': sent, 'done': counts['succeeded'],
                       'ready': counts['ready'], 'blocked': sum(counts[x] for x in BLOCKED),
                       'phase': state.get('phase', 'pages'), 'auto_run': queued,
                       'worker': worker or ('待恢复' if queued else ''),
                       'message': state.get('auto_message') or state.get('error') or '',
                       'created': row['created']})
    result.sort(key=lambda x: (not x['auto_run'], -x['created']))
    active = [item for item in result if item['phase'] != 'done' or item['auto_run']]
    finished = [item for item in result if item['phase'] == 'done' and not item['auto_run']][:8]
    return {'max_parallel': coordinator.max_parallel if coordinator else 2,
            'decks': active + finished}


class CanvaCoordinator:
    """At most two decks perform Canva operations at once, independently of browser tabs."""

    def __init__(self, store, tasks, flow, max_parallel=2, poll_interval=4):
        self.store, self.tasks, self.flow = store, tasks, flow
        self.max_parallel, self.poll_interval = max_parallel, poll_interval
        self.lock = threading.RLock()
        self.deck_locks = {}
        self.pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix='canva-deck')
        self.futures = {}

    def worker_state(self, deck_id):
        with self.lock:
            future = self.futures.get(deck_id)
            return '运行中' if future and future.running() else '排队中' if future and not future.done() else ''

    def _deck_lock(self, deck_id):
        with self.lock:
            return self.deck_locks.setdefault(deck_id, threading.RLock())

    def _schedule(self, deck_id):
        future = self.futures.get(deck_id)
        if future and not future.done():
            return
        self.futures[deck_id] = self.pool.submit(self._run, deck_id)

    def queue(self, deck_id, confirm_pages):
        with self._deck_lock(deck_id):
            deck = self.flow._deck(deck_id)
            if deck['result'].get('phase') == 'done':
                raise ValueError('这套课件已经完成')
            pages = self.flow._pages(deck)
            ready = [task for task in pages if task['status'] == 'ready']
            if confirm_pages != len(ready):
                raise ValueError(f'请确认提交剩余 {len(ready)} 页')
            if any(task['status'] in BLOCKED for task in pages) or deck['result'].get('error'):
                raise ValueError('有页面或合并任务需要先处理，不能继续自动提交')
            if not ready and not any(task['status'] in ACTIVE for task in pages) and not all(task['status'] == 'succeeded' for task in pages):
                raise ValueError('没有可以继续处理的页面')
            state = dict(deck['result'])
            state.update(auto_run=True, background_managed=True, auto_message='已加入后台队列')
            self.store.update_task(deck_id, 'running', state)
            with self.lock:
                self._schedule(deck_id)
            return self.flow.view(deck_id)

    def pause(self, deck_id):
        with self._deck_lock(deck_id):
            deck = self.flow._deck(deck_id)
            state = dict(deck['result'])
            state.update(auto_run=False, auto_message='已暂停后台处理；Canva 中已提交的任务仍可能继续运行')
            self.store.update_task(deck_id, deck['status'], state)
            return self.flow.view(deck_id)

    def resume_pending(self):
        with self.store.db() as db:
            rows = list(db.execute("SELECT id,result FROM tasks WHERE stage='export'"))
        with self.lock:
            for row in rows:
                state = json.loads(row['result'] or '{}')
                if state.get('auto_run') and state.get('phase') != 'done':
                    self._schedule(row['id'])

    def _status(self, deck_id, *, running, message):
        with self.lock:
            deck = self.store.get_task(deck_id)
            state = dict(deck['result'])
            if running and not state.get('auto_run'):
                return
            state.update(auto_run=running, auto_message=message)
            self.store.update_task(deck_id, deck['status'], state)

    def _wait(self, deck_id, seconds):
        """Honor Canva's retry window while remaining responsive to Pause."""
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            if not self.store.get_task(deck_id)['result'].get('auto_run'):
                return False
            time.sleep(min(self.poll_interval, deadline - time.monotonic()))
        return True

    def _run(self, deck_id):
        try:
            while True:
                deck = self.flow._deck(deck_id)
                state = deck['result']
                if not state.get('auto_run'):
                    return
                if state.get('error'):
                    self._status(deck_id, running=False, message='已暂停：' + str(state['error'])[:160])
                    return
                if state.get('phase') == 'done':
                    self._status(deck_id, running=False, message='整套课件已完成')
                    return
                if state.get('phase', 'pages') != 'pages':
                    with self._deck_lock(deck_id):
                        if not self.store.get_task(deck_id)['result'].get('auto_run'):
                            return
                        self.flow.poll(deck_id)
                    time.sleep(self.poll_interval)
                    continue
                pages = self.flow._pages(deck)
                blocked = next(((index, task) for index, task in enumerate(pages, 1) if task['status'] in BLOCKED), None)
                if blocked:
                    index, task = blocked
                    self._status(deck_id, running=False, message=f'P{index}：{task["status"]}，请检查后再继续')
                    return
                active = [(index, task) for index, task in enumerate(pages, 1) if task['status'] in ACTIVE]
                if active:
                    index, task = min(active, key=lambda item: item[1].get('updated', 0))
                    with self._deck_lock(deck_id):
                        if not self.store.get_task(deck_id)['result'].get('auto_run'):
                            return
                        result = self.tasks.poll(task['id'])
                    retry_at = result['result'].get('retry_at', 0)
                    if retry_at > time.time():
                        wait = max(1, int(retry_at - time.time()) + 1)
                        self._status(deck_id, running=True, message=f'Canva 限流，约 {wait} 秒后自动继续（P{index} 已保留）')
                    time.sleep(self.poll_interval)
                    continue
                ready = next(((index, task) for index, task in enumerate(pages, 1) if task['status'] == 'ready'), None)
                if ready:
                    index, task = ready
                    capability_retry = 0
                    with self._deck_lock(deck_id):
                        if not self.store.get_task(deck_id)['result'].get('auto_run'):
                            return
                        self._status(deck_id, running=True, message=f'正在提交 P{index}/{len(pages)}')
                        try:
                            result = self.tasks.submit(task['id'])
                        except CanvaError as exc:
                            if exc.status != 429:
                                raise
                            capability_retry = exc.retry_after
                            result = None
                    if capability_retry:
                        self._status(deck_id, running=True, message=f'Canva 限流，约 {capability_retry} 秒后自动继续')
                        if not self._wait(deck_id, capability_retry):
                            return
                        continue
                    if result['status'] in BLOCKED:
                        self._status(deck_id, running=False, message=f'P{index}：{result["status"]}，请检查后再继续')
                        return
                    retry_at = result['result'].get('retry_at', 0)
                    if retry_at > time.time():
                        wait = max(1, int(retry_at - time.time()) + 1)
                        self._status(deck_id, running=True, message=f'Canva 限流，约 {wait} 秒后自动继续（P{index} 尚未扣额度）')
                        if not self._wait(deck_id, retry_at - time.time()):
                            return
                        continue
                    time.sleep(min(2, self.poll_interval))
                    continue
                if all(task['status'] == 'succeeded' for task in pages):
                    with self._deck_lock(deck_id):
                        if not self.store.get_task(deck_id)['result'].get('auto_run'):
                            return
                        self._status(deck_id, running=True, message='逐页拆层完成，开始合并')
                        self.flow.poll(deck_id)
                    continue
                self._status(deck_id, running=False, message='状态不完整，请打开项目检查')
                return
        except Exception as exc:
            self._status(deck_id, running=False, message='后台任务暂停：' + (str(exc) or type(exc).__name__)[:160])
