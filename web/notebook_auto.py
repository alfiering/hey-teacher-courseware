"""Drive Gemini Notebook in the workbench's own Chrome profile: new notebook → upload source PDF →
custom slide deck with our prompt → wait → download PPTX.

No language model is called here; it only clicks the web page like a teacher would. It uses the
teacher's own Notebook generation allowance. The Chrome window uses a separate profile
(data/notebook_profile) where the teacher signed in once; passwords never pass through this code.
Any unexpected page state stops the run with a readable message, and the teacher can finish by hand.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

CDP = 'http://127.0.0.1:9333'
HOME = 'https://notebook.google.com/'
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
GENERATE_TIMEOUT = 25 * 60
SOURCE_TIMEOUT = 4 * 60


class NotebookError(Exception):
    """A step failed; message is shown to the teacher."""


def _launch(profile: Path):
    """Start the dedicated Chrome; if it is already running, this hands it a new window and exits."""
    if not Path(CHROME).exists():
        raise NotebookError('没有找到 Google Chrome，无法自动操作 Notebook')
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([CHROME, f'--user-data-dir={profile}', '--remote-debugging-port=9333',
                      '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check', HOME],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _page_count():
    """Open tabs in the dedicated Chrome, or None when nothing answers on the debugging port."""
    try:
        targets = json.loads(urllib.request.urlopen(CDP + '/json/list', timeout=2).read())
    except (OSError, ValueError):
        return None
    return sum(1 for target in targets if target.get('type') == 'page')


def ensure_browser(profile: Path):
    """Make sure the dedicated Chrome is running WITH a window.

    On a Mac, closing Chrome's last window leaves Chrome running in the background with no profile loaded.
    The debugging port still answers, but connecting then fails with "Browser context management is not
    supported". So a running Chrome without a tab is handed a new window before we connect.
    """
    if _page_count():
        return
    _launch(profile)
    for _ in range(40):
        time.sleep(0.5)
        if _page_count():
            return
    raise NotebookError('专用 Chrome 没有打开窗口。请关掉所有「专用 Chrome」窗口后重试；还不行就重启电脑再试')


def _connect(p, profile: Path):
    try:
        return p.chromium.connect_over_cdp(CDP)
    except Exception as exc:
        if 'context management' not in str(exc):
            raise
        _launch(profile)  # the window was closed between our check and the connect: reopen and try once more
        for _ in range(20):
            time.sleep(0.5)
            if _page_count():
                break
        return p.chromium.connect_over_cdp(CDP)


def run(source_pdf: Path, prompts: list[str], out_dir: Path, profile: Path, *, language='', slide_format='演示用幻灯片',
        title='', progress=lambda phase, message, **extra: None) -> list[Path]:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    ensure_browser(profile)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    with sync_playwright() as p:
        browser = _connect(p, profile)
        cdp = browser.new_browser_cdp_session()
        page = browser.contexts[0].new_page()
        page.bring_to_front()
        try:
            progress('opening', '正在打开 Gemini Notebook…')
            page.goto(HOME, wait_until='domcontentloaded')
            page.wait_for_timeout(3000)
            if 'trynow' in page.url or 'accounts.google' in page.url:
                raise NotebookError('专用浏览器还没有登录 Google，请在弹出的 Chrome 窗口里登录后重试')
            progress('creating', '正在新建笔记本…')
            page = _open_new_notebook(browser.contexts[0], page)
            notebook_url = page.url
            progress('uploading', '正在上传来源 PDF…', notebook_url=notebook_url)
            _upload(page, source_pdf)
            _wait_source(page, source_pdf.name)
            if title:
                _rename(page, title)
            for index, prompt in enumerate(prompts, 1):
                label = f'第 {index}/{len(prompts)} 份' if len(prompts) > 1 else ''
                close = page.get_by_role('button', name='关闭演示文稿').locator('visible=true')
                if close.count():
                    close.first.click()
                    page.wait_for_timeout(1000)
                before = _artifact_count(page)
                progress('requesting', f'正在提交演示文稿生成{label}…', notebook_url=notebook_url)
                _request_deck(page, prompt, language, slide_format)
                progress('generating', f'Notebook 正在生成演示文稿{label}（通常 5–15 分钟）…', notebook_url=notebook_url)
                _wait_new_artifact(page, before)
                target = out_dir / f'{(title or source_pdf.stem)[:80]}{"-第" + str(index) + "部分" if len(prompts) > 1 else ""}.pptx'
                progress('downloading', f'正在下载 PPTX{label}…', notebook_url=notebook_url)
                files.append(_download_pptx(page, target, cdp))
            progress('done', '已下载 Notebook 生成的课件', notebook_url=notebook_url)
            return files
        except PwTimeout as exc:
            raise NotebookError('Notebook 页面长时间没有响应或页面结构变了，请在 Chrome 窗口里手动完成这一步') from exc
        finally:
            try:
                page.close()
            except Exception:
                pass


def _open_new_notebook(context, page):
    """「新建笔记本」 opens the notebook in a new tab on some accounts and in place on others."""
    pages_before = set(context.pages)
    page.get_by_role('button', name='新建笔记本').locator('visible=true').first.click()
    for _ in range(240):  # creating a notebook can take over a minute
        page.wait_for_timeout(500)
        if '/notebook/' in page.url:
            break
        fresh = [pg for pg in context.pages if pg not in pages_before and '/notebook/' in pg.url]
        if fresh:
            page.close()
            page = fresh[0]
            break
    else:
        raise NotebookError('新建笔记本没有打开，请在 Chrome 窗口里查看')
    page.bring_to_front()
    page.wait_for_timeout(3000)
    return page


def _dialog(page):
    return page.locator('mat-dialog-container').last


def _upload(page, pdf: Path):
    button = page.locator('mat-dialog-container button:has-text("上传文件")')
    for _ in range(3):
        if not button.first.is_visible():
            page.get_by_role('button', name='添加来源').locator('visible=true').first.click()
        try:
            button.first.wait_for(state='visible', timeout=10000)
            break
        except Exception:
            page.keyboard.press('Escape')
            page.wait_for_timeout(1000)
    else:
        raise NotebookError('没有打开「添加来源」窗口，请在 Chrome 窗口里查看')
    with page.expect_file_chooser(timeout=15000) as chooser:
        button.first.click()
    chooser.value.set_files(str(pdf))


def _wait_source(page, name):
    deadline = time.time() + SOURCE_TIMEOUT
    page.get_by_role('button', name=name).first.wait_for(timeout=60000)
    while time.time() < deadline:
        busy = page.evaluate("""() => [...document.querySelectorAll('mat-progress-spinner, .mat-mdc-progress-spinner, [role=progressbar]')]
                                 .filter(e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().x < window.innerWidth * 0.35).length""")
        if not busy:
            page.wait_for_timeout(2000)
            return
        page.wait_for_timeout(2000)
    raise NotebookError('来源 PDF 处理超时，请在 Chrome 窗口里查看')


def _rename(page, title):
    try:
        heading = page.locator('.title-input, input[aria-label*="标题"], editable-project-title input').first
        if heading.count():
            heading.fill(title[:100])
            heading.press('Enter')
    except Exception:
        pass  # a cosmetic step; never fail the run for it


def _artifact_count(page):
    return page.locator('artifact-library-item').count()


def _newest_ready(page):
    """The newest Studio item is first; it is ready once it shows its "更多" menu and no progress indicator."""
    item = page.locator('artifact-library-item').first
    if not item.count():
        return False
    text = item.inner_text()
    busy = item.locator('mat-progress-spinner, .mat-mdc-progress-spinner, [role=progressbar], mat-progress-bar').count()
    return bool(item.locator('[aria-label="更多"]').count()) and not busy and '正在生成' not in text and 'Generating' not in text


def _request_deck(page, prompt, language, slide_format):
    page.locator('[data-create-button-type="8"] [role=button]').first.click()
    dlg = _dialog(page)
    dlg.locator('textarea').wait_for(timeout=20000)
    if slide_format:
        dlg.locator('mat-radio-button').filter(has_text=slide_format).first.click()
    if language:
        dlg.locator('mat-select').click()
        page.locator('mat-option').filter(has_text=language).first.click()
        page.wait_for_timeout(400)
    toggle = dlg.locator('button[role=switch]')
    if toggle.count() and toggle.first.get_attribute('aria-checked') == 'true':
        toggle.first.click()  # skip the outline review step
    dlg.locator('textarea').fill(prompt)
    dlg.get_by_role('button', name='立即生成').click()
    page.wait_for_timeout(3000)


def _wait_new_artifact(page, before):
    deadline = time.time() + GENERATE_TIMEOUT
    while time.time() < deadline:
        page.wait_for_timeout(15000)
        if _artifact_count(page) > before and _newest_ready(page):
            return
        if page.get_by_role('button', name='关闭演示文稿').locator('visible=true').count():
            return  # Notebook opened the finished deck by itself
        text = page.locator('artifact-library').first.inner_text() if page.locator('artifact-library').count() else ''
        if '无法生成' in text or '生成失败' in text:
            raise NotebookError('Notebook 提示生成失败，请在 Chrome 窗口里查看原因')
    raise NotebookError('等待生成超过 25 分钟，请稍后在 Chrome 窗口里查看并手动下载')


def _download_pptx(page, target: Path, cdp=None):
    """Downloads from a CDP-attached Chrome must go through Browser.setDownloadBehavior."""
    folder = target.parent / ('dl-' + target.stem)
    folder.mkdir(parents=True, exist_ok=True)
    cdp.send('Browser.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': str(folder), 'eventsEnabled': True})
    viewer_menu = page.get_by_role('button', name='更多选项').locator('visible=true')
    if viewer_menu.count():  # the finished deck is often opened in Studio automatically
        viewer_menu.last.click()
    else:
        page.locator('artifact-library-item').first.locator('[aria-label="更多"]').click()
    page.wait_for_timeout(800)
    page.get_by_role('menuitem').filter(has_text='PowerPoint').first.click()
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(2)
        done = [f for f in folder.iterdir() if f.suffix.lower() == '.pptx']
        if done and not any(f.suffix == '.crdownload' for f in folder.iterdir()):
            done[0].replace(target)
            break
    else:
        cdp.send('Browser.setDownloadBehavior', {'behavior': 'default'})
        raise NotebookError('下载 PPTX 超时，请在 Chrome 窗口里手动下载')
    cdp.send('Browser.setDownloadBehavior', {'behavior': 'default'})
    if target.stat().st_size < 1000:
        raise NotebookError('下载的 PPTX 不完整')
    return target
