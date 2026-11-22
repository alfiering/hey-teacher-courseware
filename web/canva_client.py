"""Canva REST boundary: PKCE user authorization, never app-only AI credits."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, quote

API = 'https://api.canva.com/rest/v1'
SCOPES = 'profile:read asset:read asset:write design:content:write design:content:read design:meta:read folder:read'


class CanvaError(Exception):
    def __init__(self, status, code, retry_after=30):
        self.status = status
        self.code = code if re.fullmatch(r'[a-zA-Z0-9_]{1,80}', str(code)) else 'request_failed'
        self.retry_after = max(5, min(300, int(retry_after)))
        super().__init__(f'Canva 请求失败：HTTP {status} ({self.code})')


def transport(method, url, headers, data):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # Never resend a mutating request on network failure or follow a redirect with credentials.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=35) as response:
            return json.loads(response.read(4_000_000))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(10000))
            code = body.get('code') or body.get('error', {}).get('code') or 'request_failed'
        except Exception:
            code = 'request_failed'
        try:
            retry = int(exc.headers.get('Retry-After', '30'))
        except ValueError:
            retry = 30
        raise CanvaError(exc.code, code, retry) from None


class KeychainStore:
    """macOS Security framework keeps secrets out of files, argv and logs."""
    def __init__(self, account):
        self.account = account.encode()
        self.service = b'HEY_TEACHER_Local_Canva'

    def _frameworks(self):
        if sys.platform != 'darwin':
            raise RuntimeError('本版本使用 macOS 钥匙串保存 Canva 授权')
        sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        sec.SecKeychainFindGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
        sec.SecKeychainAddGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p]
        sec.SecKeychainItemModifyAttributesAndData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p]
        sec.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        sec.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        return sec, cf

    def _find(self, sec):
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = sec.SecKeychainFindGenericPassword(None, len(self.service), self.service, len(self.account), self.account, ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        if status == -25300:
            return None, None
        if status:
            raise RuntimeError('无法读取系统钥匙串，请检查钥匙串访问授权')
        try:
            value = json.loads(ctypes.string_at(data, length.value))
        finally:
            sec.SecKeychainItemFreeContent(None, data)
        return item, value

    def load(self):
        sec, cf = self._frameworks()
        item, value = self._find(sec)
        if item:
            cf.CFRelease(item)
        return value

    def save(self, value):
        sec, cf = self._frameworks()
        payload = json.dumps(value).encode()
        item, _old = self._find(sec)
        if item:
            try:
                status = sec.SecKeychainItemModifyAttributesAndData(item, None, len(payload), payload)
            finally:
                cf.CFRelease(item)
        else:
            status = sec.SecKeychainAddGenericPassword(None, len(self.service), self.service, len(self.account), self.account, len(payload), payload, None)
        if status:
            raise RuntimeError('无法保存系统钥匙串，授权未落盘')

    def delete(self):
        sec, cf = self._frameworks()
        item, _value = self._find(sec)
        if item:
            try:
                status = sec.SecKeychainItemDelete(item)
                if status:
                    raise RuntimeError('无法移除本地授权')
            finally:
                cf.CFRelease(item)


class CanvaClient:
    def __init__(self, settings, token_store, request_transport=transport):
        self.settings, self.tokens, self.transport = settings, token_store, request_transport
        self.pending = None
        self.lock = threading.RLock()

    def status(self):
        configured = bool(self.settings.get('client_id') and self.settings.get('client_secret'))
        tokens = self.tokens.load() if configured else None
        return {'configured': configured, 'authorized': bool(tokens and tokens.get('refresh_token')),
                'redirect_uri': self.settings.get('redirect_uri', ''), 'scopes': SCOPES.split()}

    def begin_authorization(self):
        if not self.status()['configured']:
            raise ValueError('请先配置 Canva Developer 应用')
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        with self.lock:
            self.pending = {'verifier': verifier, 'state': state, 'created': time.time()}
        return {'url': 'https://www.canva.com/api/oauth/authorize?' + urlencode({
            'client_id': self.settings['client_id'], 'redirect_uri': self.settings['redirect_uri'],
            'response_type': 'code', 'scope': SCOPES, 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256'})}

    def _token_request(self, form):
        auth = base64.b64encode((self.settings['client_id'] + ':' + self.settings['client_secret']).encode()).decode()
        result = self.transport('POST', API + '/oauth/token', {'Authorization': 'Basic ' + auth, 'Content-Type': 'application/x-www-form-urlencoded'}, urlencode(form).encode())
        if not result.get('access_token') or not result.get('refresh_token'):
            raise ValueError('Canva 授权响应缺少用户令牌')
        result['expires_at'] = time.time() + int(result.get('expires_in', 3600))
        self.tokens.save(result)
        return result

    def complete_authorization(self, state, code):
        with self.lock:
            p = self.pending
            if not p or time.time()-p['created'] > 600 or not secrets.compare_digest(str(state), p['state']):
                raise ValueError('登录流程已失效，请返回工作台重新点击连接 Canva 账号')
            if not code:
                raise ValueError('Canva 未返回授权码，请检查应用权限后重新连接')
            self.pending = None
            self._token_request({'grant_type': 'authorization_code', 'code': code, 'code_verifier': p['verifier'], 'redirect_uri': self.settings['redirect_uri']})

    def access_token(self):
        with self.lock:
            value = self.tokens.load()
            if not value or not value.get('refresh_token'):
                raise ValueError('请先连接 Canva 账号')
            if float(value.get('expires_at', 0)) < time.time() + 60:
                value = self._token_request({'grant_type': 'refresh_token', 'refresh_token': value['refresh_token']})
            return value['access_token']

    def request(self, method, path, body=None, headers=None, binary=None):
        h = {'Authorization': 'Bearer ' + self.access_token(), 'Content-Type': 'application/json'}
        h.update(headers or {})
        payload = binary if binary is not None else json.dumps(body).encode() if body is not None else None
        return self.transport(method, API + path, h, payload)

    def capabilities(self):
        return self.request('GET', '/users/me/capabilities')

    def upload_image(self, path: Path):
        if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            raise ValueError('单页测试请上传 PNG、JPG 或 WEBP')
        metadata = json.dumps({'name_base64': base64.b64encode(path.name.encode()).decode()})
        return self.request('POST', '/asset-uploads', headers={'Content-Type': 'application/octet-stream', 'Asset-Upload-Metadata': metadata}, binary=path.read_bytes())

    def get_upload(self, job_id):
        return self.request('GET', '/asset-uploads/' + quote(job_id, safe=''))

    def start_conversion(self, asset_id, title):
        return self.request('POST', '/image-to-design-imports', {'image': {'asset_id': asset_id}, 'title': title})

    def get_conversion(self, job_id):
        return self.request('GET', '/image-to-design-imports/' + quote(job_id, safe=''))

    def start_export(self, design_id, export_type='pptx'):
        if export_type != 'pptx':
            raise ValueError('本次验证仅导出 PPTX')
        return self.request('POST', '/exports', {'design_id': design_id, 'format': {'type': 'pptx'}})

    def get_export(self, job_id):
        return self.request('GET', '/exports/' + quote(job_id, safe=''))

    @staticmethod
    def download(url, limit=500 * 1024 * 1024, on_progress=None, attempts=6, read_timeout=60, sleep=time.sleep):
        """Fetch a time-limited export URL. No Canva credentials are sent to the file host.

        A layered deck can be ~90 MB. When the connection stalls or drops, reconnect and ask only for
        the missing bytes (HTTP Range) instead of discarding what already arrived. Attempts count
        consecutive failures without progress; an HTTP error such as an expired link is not retried.
        """
        import http.client
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.username or parts.password or not parts.hostname:
            raise ValueError('导出下载地址不合法')
        data, total, failures = bytearray(), None, 0
        while total is None or len(data) < total:
            headers = {'Range': f'bytes={len(data)}-'} if data else {}
            req = urllib.request.Request(url, method='GET', headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=read_timeout) as response:
                    if getattr(response, 'status', 200) == 206:
                        match = re.fullmatch(r'bytes (\d+)-\d+/(\d+|\*)', response.headers.get('Content-Range', '').strip())
                        if not match or int(match.group(1)) != len(data):
                            raise ValueError('导出文件续传位置不一致')
                        total = int(match.group(2)) if match.group(2).isdigit() else total
                    else:
                        data = bytearray()  # first request, or the host ignored Range: take the whole file
                        length = response.headers.get('Content-Length')
                        total = int(length) if length and length.isdigit() else None
                    if total is not None and total > limit:
                        raise ValueError('导出文件超过大小上限')
                    if on_progress:
                        on_progress(len(data), total)
                    while True:
                        chunk = response.read(min(1024 * 1024, limit + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                        failures = 0
                        if len(data) > limit:
                            raise ValueError('导出文件超过大小上限')
                        if on_progress:
                            on_progress(len(data), total)
                if total is None:
                    break  # no length announced: the server closing the stream is the end
                if len(data) < total:
                    raise ConnectionError('导出文件下载中断')  # closed early: resume from here
            except urllib.error.HTTPError:
                raise
            except (OSError, http.client.HTTPException):
                failures += 1
                if failures >= attempts:
                    raise
                sleep(min(30, 2 ** failures))
        if not data or len(data) > limit:
            raise ValueError('导出文件为空或超过大小上限')
        return bytes(data)


def _merge_api(cls):
    def start_merge(self, body):
        return self.request('POST', '/merges', body)

    def get_merge(self, job_id):
        return self.request('GET', '/merges/' + quote(job_id, safe=''))

    def get_design(self, design_id):
        return self.request('GET', '/designs/' + quote(design_id, safe=''))
    def list_folder_designs(self, folder_id):
        items, continuation = [], None
        for _ in range(20):
            path = '/folders/' + quote(folder_id, safe='') + '/items?item_types=design&limit=100&sort_by=title_ascending'
            if continuation:
                path += '&continuation=' + quote(continuation, safe='')
            response = self.request('GET', path)
            items.extend(item for item in response.get('items', []) if item.get('type') == 'design')
            continuation = response.get('continuation')
            if not continuation:
                return items
        raise ValueError('Canva 文件夹超过 2000 个设计，请拆成较小文件夹')
    def start_import(self, path, title):
        metadata = json.dumps({'title_base64': base64.b64encode(title[:50].encode()).decode()})
        return self.request('POST', '/imports', headers={'Content-Type': 'application/octet-stream',
                            'Import-Metadata': metadata}, binary=Path(path).read_bytes())
    def get_import(self, job_id):
        return self.request('GET', '/imports/' + quote(job_id, safe=''))
    cls.start_merge, cls.get_merge, cls.get_design = start_merge, get_merge, get_design
    cls.list_folder_designs, cls.start_import, cls.get_import = list_folder_designs, start_import, get_import
    return cls


_merge_api(CanvaClient)
