"""Persistent single-course orchestration; never repeat an uncertain remote submission.

The run walks through fixed "stations" (教材 → 教案 → 提示词 → Notebook → 检测 → Canva → PPTX).
Every phase is persisted, so a restart, a pause or a teacher-confirmed resume continues from the
exact station instead of starting over.
"""
import threading
import time

from web.canva_tasks import is_network_error
from web import docx_text as parsers  # Word tables kept in document order

# Station index of every phase; the UI draws the metro line from this.
STATIONS = ('教材识别', '教案起草', '逐页提示词', 'Notebook 生成', '课件检测', 'Canva 拆层合并', '可编辑 PPTX')
STATION_OF = {
    'extracting': 0,
    'drafting': 1, 'draft_requesting': 1,
    'prompting': 2, 'prompt_requesting': 2,
    'notebook_queue': 3, 'notebook_requesting': 3, 'notebook_waiting': 3,
    'inspecting': 4, 'inspection_waiting': 4,
    'canva_starting': 5, 'canva_waiting': 5,
    'done': 6,
}
# A remote call may have been accepted when the process stopped inside these phases.
UNCERTAIN = ('draft_requesting', 'prompt_requesting', 'notebook_requesting', 'canva_starting')
MAX_SOURCES = 30


class _Stopped(Exception):
    """The teacher paused or cancelled the run while a step was in flight."""


class AutoPipeline:
    def __init__(self, store, draft, prompt, notebook, unified, connect, prompt_lock,
                 canva_ready=None, notebook_busy=None, requeue=None, now=time.time, poll=(5, 3, 4)):
        self.store, self.draft, self.prompt = store, draft, prompt
        self.notebook, self.unified, self.connect = notebook, unified, connect
        self.prompt_lock = prompt_lock
        self.canva_ready = canva_ready          # () -> '' when Canva can be used, else a reason
        self.notebook_busy = notebook_busy or (lambda: False)
        self.requeue = requeue                  # (deck_id) -> re-queue a paused Canva deck
        self.now = now
        self.poll_notebook, self.poll_inspect, self.poll_canva = poll
        self.lock = threading.RLock()
        self.workers = {}

    # ---------- teacher actions ----------

    def start(self, project_id, options):
        with self.lock:
            return self._start(project_id, options)

    def _start(self, project_id, options):
        project = self.store.get_project(project_id)
        parts = options.get('parts')
        cap = options.get('max_canva_pages')
        period = str(options.get('period') or '')
        if parts not in (1, 2) or not isinstance(cap, int) or not 1 <= cap <= 100:
            raise ValueError('请设置 15／30 页模式和本次 Canva 拆层上限（1–100 页）')
        if period not in ('1', '2', '3', '4', '5', ''):
            raise ValueError('请选择课时')
        if self._running(project):
            raise ValueError('本课程已有全自动任务在运行')
        start_from = options.get('start_from') or 'textbook'
        if start_from not in ('textbook', 'lesson'):
            raise ValueError('请选择从教材开始还是从我的教案开始')
        lesson = self._latest(project, 'lesson')
        textbooks = [a['id'] for a in project['assets'] if a['kind'] == 'textbook']
        ids = options.get('asset_ids') or []
        if not isinstance(ids, list) or len(ids) > MAX_SOURCES or len(ids) != len(set(ids)):
            raise ValueError(f'请选择本次使用的教材页，最多 {MAX_SOURCES} 份')
        if any(i not in textbooks for i in ids):
            raise ValueError('教材页不属于本课程')
        materials = self._latest(project, 'materials')
        # From the textbook, pages are required. From the teacher's own plan they are optional: when present the
        # textbook is read first and the plan is checked against it (texts, tasks and characters follow the book).
        if start_from == 'textbook' and not textbooks:
            raise ValueError('请先放入教材页')
        text_source = 'ocr' if ids else options.get('text_source')
        if text_source not in ('ocr', 'saved'):
            text_source = 'saved' if materials else 'ocr'
        if start_from == 'lesson' and text_source == 'ocr' and not textbooks:
            text_source = 'saved' if materials else 'none'
        if text_source == 'saved' and not materials:
            raise ValueError('还没有保存过教材文字，请改为重新识别教材页')
        if text_source == 'ocr' and not ids:
            if len(textbooks) > MAX_SOURCES:
                raise ValueError(f'本课程有 {len(textbooks)} 份教材，超过 {MAX_SOURCES} 份；请先保存教材文字，再用「已保存的教材文字」启动')
            ids = textbooks
        if start_from == 'lesson':
            if not lesson:
                raise ValueError('请先放入你的教案')
            lesson_source = options.get('lesson_source') or 'expand'
            if lesson_source not in ('expand', 'existing'):
                raise ValueError('请选择教案怎么处理')
        else:
            lesson_source = options.get('lesson_source') or 'draft'
            if lesson_source not in ('draft', 'existing'):
                raise ValueError('请选择教案来源')
            if lesson_source == 'existing' and not lesson:
                raise ValueError('还没有保存过教案，请改为「自动起草教案」')
        template = options.get('template_asset_id') or None
        skill = options.get('skill_asset_id') or None
        if skill and str(skill).startswith('builtin:'):
            from web.lesson_skill import BUILTIN_SKILLS
            if skill not in BUILTIN_SKILLS:
                raise ValueError('内置 Skill 不存在，请重新选择')
            skill_ok = True
        else:
            skill_ok = not skill or any(a['id'] == skill and a['kind'] == 'skill' for a in project['assets'])
        if not skill_ok or (template and not any(a['id'] == template and a['kind'] == 'template' for a in project['assets'])):
            raise ValueError('所选模板或 Skill 不属于本课程')
        # Fail before any paid step instead of after a 20-minute Notebook run.
        if self.canva_ready:
            reason = self.canva_ready()
            if reason:
                raise ValueError('全自动需要 Canva 拆层：' + reason)
        if start_from == 'lesson' and lesson_source == 'existing' and period:
            # A whole-unit plan goes to Notebook one period at a time.
            from web.local_server import select_lesson_period
            text = select_lesson_period(lesson['payload']['text'], int(period))
            if text.strip() != lesson['payload']['text'].strip():
                lesson = self.store.save_version(project_id, 'lesson', {
                    'text': text, 'period': int(period), 'source_lesson_version_id': lesson['id'], 'uploaded': True})
        # The launch record fixes sources and the credit cap before any paid action.
        config = {'start_from': start_from, 'asset_ids': ids, 'text_source': text_source, 'lesson_source': lesson_source,
                  'lesson_version_id': lesson['id'] if start_from == 'lesson' else None,
                  'parts': parts, 'period': period, 'max_canva_pages': cap,
                  'template_asset_id': template, 'skill_asset_id': skill,
                  'notes': str(options.get('notes') or '')[:2000]}
        version = self.store.save_version(project_id, 'automation', config)
        task = self.store.create_task(project_id, 'automation', version['id'], version['id'])
        warnings = []
        if text_source == 'none':
            # Only the teacher's plan: nothing to read, so the run starts at the lesson station.
            phase, first = (('drafting', '从你的教案出发，正在按 Skill 扩写本课时详案') if lesson_source == 'expand'
                            else ('prompting', '直接使用你的教案，正在生成逐页提示词'))
            skipped = [0] if lesson_source == 'expand' else [0, 1]
            warnings = ['这次没有教材页，课文、任务和人物只按你的教案生成；放入本课时教材页会更准确']
        else:
            phase = 'extracting'
            skipped = [1] if start_from == 'lesson' and lesson_source == 'existing' else []
            first = '正在读取已保存的教材文字' if text_source == 'saved' else f'正在识别 {len(ids)} 份教材'
        self._save(task['id'], phase, first, force=True, config=config, warnings=warnings,
                   started=self.now(), stations=list(STATIONS), skipped=skipped)
        self._schedule(task['id'])
        return self.store.get_task(task['id'])

    def resume(self, task_id, options=None):
        """Teacher-confirmed continue from the station where the run stopped."""
        options = options or {}
        with self.lock:
            task = self._own(task_id)
            if task['status'] != 'paused':
                raise ValueError('只有已暂停的全自动任务可以继续')
            if self._running(self.store.get_project(task['project_id'])):
                raise ValueError('本课程已有全自动任务在运行')
            state = task['result']
            config = dict(state.get('config') or {})
            cap = options.get('max_canva_pages')
            if cap is not None:
                if not isinstance(cap, int) or not 1 <= cap <= 100:
                    raise ValueError('拆层上限须为 1–100 页')
                config['max_canva_pages'] = cap
            phase, extra = self._resume_phase(task, state.get('resume_phase') or 'extracting')
            self._save(task_id, phase, '已继续：' + STATIONS[STATION_OF.get(phase, 0)], force=True,
                       config=config, **extra)
            self._schedule(task_id)
            return self.store.get_task(task_id)

    def cancel(self, task_id):
        with self.lock:
            task = self._own(task_id)
            if task['status'] not in ('running', 'paused'):
                raise ValueError('这个全自动任务已经结束')
            state = dict(task['result'])
            if task['status'] == 'running':
                state['resume_phase'] = state.get('phase')
            state.update(phase='cancelled', updated=self.now(),
                         message='已停止全自动。已经提交到 Notebook 或 Canva 的任务不会被撤回，可在对应节点继续手动处理')
            return self.store.update_task(task_id, 'failed', state)

    def resume_pending(self):
        """On server start: continue safe phases, pause uncertain ones for the teacher."""
        with self.store.db() as db:
            rows = list(db.execute("SELECT id FROM tasks WHERE stage='automation' AND status='running'"))
        for row in rows:
            result = self.store.get_task(row['id'])['result']
            phase = result.get('phase')
            if phase in UNCERTAIN:
                self._pause(row['id'], '工作台重启时外部提交结果不确定；请检查对应账号后点「继续」')
            elif phase == 'notebook_waiting' and self.store.get_task(result['notebook_task_id'])['status'] == 'running':
                self._pause(row['id'], '工作台重启时 Notebook 仍在生成；请在专用 Chrome 检查。若已手动下载课件，请先上传到「Gemini Notebook 生成」节点，再点「继续」')
            else:
                self._schedule(row['id'])

    # ---------- state helpers ----------

    def _own(self, task_id):
        task = self.store.get_task(task_id)
        if task['stage'] != 'automation':
            raise ValueError('不是全自动任务')
        return task

    @staticmethod
    def _running(project):
        return any(t['stage'] == 'automation' and t['status'] == 'running' for t in project['tasks'])

    @staticmethod
    def _latest(project, stage):
        return next((v for v in project['versions'] if v['stage'] == stage
                     and isinstance(v['payload'].get('text'), str) and v['payload']['text'].strip()), None)

    def _save(self, task_id, phase, message, status='running', force=False, **extra):
        with self.lock:
            task = self.store.get_task(task_id)
            if not force and task['status'] != 'running':
                raise _Stopped()
            state = dict(task['result'], **extra)
            now = self.now()
            station = STATION_OF.get(phase)
            if station is not None:
                times = {k: dict(v) for k, v in (state.get('station_times') or {}).items()}
                for key, value in times.items():
                    if int(key) < station and 'end' not in value:
                        value['end'] = now
                times.setdefault(str(station), {'start': now})
                if phase == 'done':
                    times[str(station)]['end'] = now
                state.update(station=station, station_times=times)
            state.update(phase=phase, message=message, updated=now)
            return self.store.update_task(task_id, status, state)

    def _pause(self, task_id, message):
        with self.lock:
            task = self.store.get_task(task_id)
            if task['status'] != 'running':
                return task
            state = dict(task['result'])
            if state.get('phase') not in ('paused', 'cancelled', None):
                state['resume_phase'] = state['phase']
            state.update(phase='paused', message=message[:300], updated=self.now())
            return self.store.update_task(task_id, 'paused', state)

    def _warn(self, task_id, text):
        task = self.store.get_task(task_id)
        warnings = list(task['result'].get('warnings') or [])
        if text not in warnings:
            warnings.append(text)
        return warnings

    def _schedule(self, task_id):
        with self.lock:
            worker = self.workers.get(task_id)
            if worker and worker.is_alive():
                worker.join(timeout=2)  # A just-paused worker is on its way out.
            if worker and worker.is_alive():
                return
            worker = threading.Thread(target=self._run, args=(task_id,), daemon=True, name='lesson-auto')
            self.workers[task_id] = worker
            worker.start()

    def _resume_phase(self, task, phase):
        """Map the stopped phase to a safe re-entry point (never resubmits silently)."""
        state, extra = task['result'], {}
        if phase == 'draft_requesting':
            return 'drafting', extra
        if phase == 'prompt_requesting':
            return 'prompting', extra
        if phase in ('notebook_requesting', 'notebook_waiting'):
            child = None
            if state.get('notebook_task_id'):
                child = self.store.get_task(state['notebook_task_id'])
            else:
                project = self.store.get_project(task['project_id'])
                child = next((t for t in reversed(project['tasks']) if t['stage'] == 'notebook'
                              and t['created'] >= task['created']), None)
            if child and (child['status'] == 'succeeded' or (child['status'] == 'running' and self.notebook_busy())):
                return 'notebook_waiting', {'notebook_task_id': child['id']}
            if child and child['status'] == 'running':
                # The worker thread died with the old process; record that honestly.
                self.store.update_task(child['id'], 'failed', dict(child['result'], phase='failed',
                                       message='工作台重启，Notebook 自动生成中断'))
            adopted = self._uploaded_courseware(task)
            if adopted:
                return 'inspecting', {'courseware_asset_ids': adopted}
            return 'notebook_queue', {}
        if phase in ('inspecting', 'inspection_waiting', 'canva_starting'):
            inspection = state.get('inspection_task_id') and self.store.get_task(state['inspection_task_id'])
            if not inspection or inspection['status'] == 'failed':
                return 'inspecting', extra
            return 'inspection_waiting', extra
        if phase == 'canva_waiting':
            deck_id = state.get('deck_id')
            if deck_id:
                deck = self.store.get_task(deck_id)['result']
                network = deck.get('error_kind') == 'network' or is_network_error(deck.get('error'))
                if deck.get('error') and not network:
                    raise ValueError('Canva 合并出错：' + str(deck['error'])[:120] + '。请先在「课件拆层与合并」节点处理，再点继续')
                # A network error keeps every finished page, merge and export: 继续 retries from the saved step.
                if (deck.get('error') or not deck.get('auto_run')) and deck.get('phase') != 'done':
                    if not self.requeue:
                        raise ValueError('请在「课件拆层与合并」节点继续后台处理')
                    self.requeue(deck_id)
            return 'canva_waiting', extra
        if phase in STATION_OF and phase != 'done':
            return phase, extra
        raise ValueError('这个任务没有可以继续的步骤')

    def _uploaded_courseware(self, task):
        """Courseware the teacher downloaded by hand after this run started."""
        project = self.store.get_project(task['project_id'])
        return [a['id'] for a in project['assets'] if a['kind'] == 'courseware'
                and a['ext'] in ('.pptx', '.pdf') and a['created'] >= task['created']][-2:]

    # ---------- the run ----------

    def _run(self, task_id):
        try:
            while True:
                task = self.store.get_task(task_id)
                if task['status'] != 'running':
                    return
                if self._step(task) is False:
                    return
        except _Stopped:
            return
        except Exception as exc:
            self._pause(task_id, str(exc) or type(exc).__name__)

    def _step(self, task):
        task_id, state = task['id'], task['result']
        phase, project_id, config = state['phase'], task['project_id'], state['config']
        project = self.store.get_project(project_id)
        if phase == 'extracting':
            lesson_source = config.get('lesson_source', 'draft')
            nxt = (('drafting', '正在起草指定课时教案') if lesson_source == 'draft'
                   else ('drafting', '正在按 Skill 扩写你的教案，并用教材原文核对') if lesson_source == 'expand'
                   else ('prompting', '使用你的教案，正在生成逐页提示词'))
            if config.get('text_source') == 'saved':
                materials = self._latest(project, 'materials')
                if not materials or len(materials['payload']['text'].strip()) < 20:
                    raise ValueError('已保存的教材文字太少，请回到「教材」检查')
                self._save(task_id, *nxt, materials_version_id=materials['id'])
                return
            chunks, empty = [], []
            for index, asset_id in enumerate(config['asset_ids'], 1):
                asset = next((a for a in project['assets'] if a['id'] == asset_id), None)
                if not asset:
                    raise ValueError('有教材页已被删除，请重新启动全自动')
                self._save(task_id, 'extracting', f'正在识别教材 {index}/{len(config["asset_ids"])}：{asset["name"]}')
                parsed = parsers.parse_file(self.store.asset_path(project_id, asset_id))
                content = str(parsed.get('text') or '').strip()
                if content:
                    chunks.append(f'【{asset["name"]}】\n{content}')
                else:
                    empty.append(asset['name'])  # Picture-only pages are normal in lower grades.
            joined = '\n\n'.join(chunks)
            if len(joined) < 20:
                raise ValueError('教材几乎没有识别到文字，请在「教材」节点手动补充文字后，用「已保存的教材文字」重新启动')
            extra = {}
            if empty:
                extra['warnings'] = self._warn(task_id, f'{len(empty)} 页没有识别到文字，已跳过：' + '、'.join(empty[:5]) + ('…' if len(empty) > 5 else ''))
            version = self.store.save_version(project_id, 'materials', {'text': joined, 'auto_task_id': task_id})
            self._save(task_id, *nxt, materials_version_id=version['id'], **extra)
        elif phase == 'drafting':
            self._save(task_id, 'draft_requesting', 'DeepSeek 正在起草教案（约 1–3 分钟）')
        elif phase == 'draft_requesting':
            with self.prompt_lock:
                version = self.draft(self.store, project, {
                    'period': config['period'], 'notes': config['notes'],
                    'source_lesson_version_id': config.get('lesson_version_id') if config.get('lesson_source') == 'expand' else None,
                    'template_asset_id': config['template_asset_id'],
                    'skill_asset_id': config['skill_asset_id']})
            self._save(task_id, 'prompting', '教案已保存，正在生成逐页提示词和来源 PDF', lesson_version_id=version['id'])
        elif phase == 'prompting':
            self._save(task_id, 'prompt_requesting', 'DeepSeek 正在生成逐页大纲')
        elif phase == 'prompt_requesting':
            with self.prompt_lock:
                version = self.prompt(self.store, project, {'parts': config['parts'], 'period': config.get('period')})
            self._save(task_id, 'notebook_queue', '提示词已生成，准备启动专用 Chrome', outline_version_id=version['id'])
        elif phase == 'notebook_queue':
            if self.notebook_busy():
                self._save(task_id, 'notebook_queue', '另一个 Notebook 任务正在生成，排队等待中')
                time.sleep(self.poll_notebook)
                return
            self._save(task_id, 'notebook_requesting', '正在启动专用 Chrome，上传来源并生成课件')
        elif phase == 'notebook_requesting':
            notebook_task = self.notebook(self.store, project_id)
            self._save(task_id, 'notebook_waiting', 'Notebook 正在生成课件（通常 5–15 分钟）', notebook_task_id=notebook_task['id'])
        elif phase == 'notebook_waiting':
            child = self.store.get_task(state['notebook_task_id'])
            if child['status'] == 'failed':
                raise ValueError('Notebook 中断：' + child['result'].get('message', '请检查专用 Chrome'))
            if child['status'] == 'succeeded':
                self._save(task_id, 'inspecting', '课件已下载，正在检测页数和可编辑性', courseware_asset_ids=child['result']['asset_ids'])
            else:
                if child['result'].get('message') != state.get('message'):
                    self._save(task_id, 'notebook_waiting', child['result'].get('message', 'Notebook 正在生成'))
                time.sleep(self.poll_notebook)
        elif phase == 'inspecting':
            service = self.unified(False)
            inspection = service.inspect(project_id, state['courseware_asset_ids'], '', project['metadata']['title'])
            self._save(task_id, 'inspection_waiting', '正在逐页检测课件', inspection_task_id=inspection['id'])
        elif phase == 'inspection_waiting':
            self.unified(False).resume_pending()
            inspection = self.store.get_task(state['inspection_task_id'])
            result = inspection['result']
            if inspection['status'] == 'failed':
                raise ValueError('课件检测失败：' + result.get('message', ''))
            if result.get('phase') in ('processing', 'done'):
                # Already handed to Canva (e.g. resumed after a restart): never start twice.
                self._save(task_id, 'canva_waiting', 'Canva 正在拆层、合并及导出', deck_id=result.get('deck_id'))
            elif result.get('phase') == 'review':
                pages = result['pages']
                flat = [i for i, page in enumerate(pages) if page['kind'] != 'editable']
                if len(flat) > config['max_canva_pages']:
                    raise ValueError(f'需要拆层 {len(flat)} 页，超过预设上限 {config["max_canva_pages"]} 页；可调高上限后点「继续」')
                extra = {}
                expected = config['parts'] * 15
                if len(pages) != expected:
                    extra['warnings'] = self._warn(task_id, f'Notebook 实际生成 {len(pages)} 页（预期约 {expected} 页），已按实际页数继续')
                self._save(task_id, 'canva_starting', f'共 {len(pages)} 页，正在提交 {len(flat)} 页 Canva 拆层',
                           flat_pages=flat, page_count=len(pages), **extra)
            else:
                time.sleep(self.poll_inspect)
        elif phase == 'canva_starting':
            inspection = self.store.get_task(state['inspection_task_id'])
            pages = inspection['result']['pages']
            flat = state['flat_pages']
            needs_canva = bool(flat) or len(inspection['result']['sources']) != 1
            service = self.unified(needs_canva)
            started = service.start(inspection['id'], list(range(len(pages))), flat, len(flat))
            self._save(task_id, 'canva_waiting', 'Canva 正在拆层、合并及导出', deck_id=started['result'].get('deck_id'))
        elif phase == 'canva_waiting':
            inspection = self.store.get_task(state['inspection_task_id'])
            deck_id = state.get('deck_id')
            if deck_id:
                self.connect()
                deck = self.store.get_task(deck_id)['result']
                if deck.get('phase') == 'done':
                    self._save(task_id, 'done', '完整可编辑 PPTX 和字体修复版已保存', status='succeeded',
                               result_asset_id=deck.get('result_asset_id'), fixed_asset_id=deck.get('fixed_asset_id'))
                    return False
                if deck.get('error') or not deck.get('auto_run'):
                    raise ValueError('Canva 已暂停：' + (deck.get('error') or deck.get('auto_message') or '请检查逐页任务'))
                progress = deck.get('auto_message') or 'Canva 正在拆层、合并及导出'
                if progress != state.get('message'):
                    self._save(task_id, 'canva_waiting', progress)
            elif inspection['result'].get('phase') == 'done':
                self._save(task_id, 'done', '可编辑 PPTX 已保存', status='succeeded',
                           result_asset_id=inspection['result'].get('result_asset_id'))
                return False
            time.sleep(self.poll_canva)
        else:
            return False
