"""DeepSeek 客户端（stdlib 实现，零第三方依赖）。

设计要点：
1. 只用 urllib，不引 requests / openai，与两个现有工具的风格一致。
2. 强制 JSON 输出：response_format + prompt 含「json」字样。
3. 默认关闭思考模式（thinking=disabled）——填槽位要的是直出 JSON，
   不是链式思考（思考模式会把 max_tokens 烧光、正文一个字都不给）。
4. key 只从环境变量 DEEPSEEK_API_KEY 读，绝不写进代码或文件。
5. 返回结构化结果 + token 用量，方便做费用监控与「重试」决策。

关键事实（2026-09 已核实）：
- base_url = https://api.deepseek.com（OpenAI 兼容）
- 模型 deepseek-v4-flash（API 归一化为 deepseek-flash）；deepseek-chat/-reasoner 已退役
- 思考模式默认开启，需显式 {"thinking":{"type":"disabled"}} 关闭
- 思考模式不支持 temperature；关闭后可正常使用 temperature
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"

# v4-flash 定价（USD / 1M tokens），用于粗略费用估算
_PRICE = {"in_miss": 0.14, "in_hit": 0.0028, "out": 0.28}


class DeepSeekError(RuntimeError):
    """调用失败时抛出，message 里带上可读原因，方便上层做「重试 / 降级」决策。"""


def api_key() -> str:
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not k:
        # 兜底：本地服务常以非登录 shell 启动，拿不到 profile 里 export 的变量，
        # 这里直接去用户自己的 ~/.bash_profile / ~/.zshrc 里找（个人本地工具，安全）。
        for p in (Path.home() / ".bash_profile", Path.home() / ".zshrc"):
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    m = re.match(r'^\s*export\s+DEEPSEEK_API_KEY="([^"]+)"', line)
                    if m:
                        k = m.group(1).strip()
                        break
            except OSError:
                continue
            if k:
                break
    if not k:
        raise DeepSeekError(
            "还没有设置 DeepSeek API key。请双击工作台文件夹里的「设置DeepSeek密钥.command」填写，"
            "然后关掉工作台窗口、重新双击「启动工作台.command」。"
        )
    return k


def _estimate_cost(usage: dict) -> float:
    """估算本次调用花费（USD）。"""
    pt = int(usage.get("prompt_tokens", 0))
    ct = int(usage.get("completion_tokens", 0))
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(pt - int(hit), 0)
    return (int(miss) * _PRICE["in_miss"] + int(hit) * _PRICE["in_hit"]
            + ct * _PRICE["out"]) / 1_000_000


def _parse_json(content: str):
    """从模型输出里稳健地解析 JSON（允许模型多吐了 ```json 围栏或前后空行）。"""
    s = content.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s.replace("json", "", 1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 兜底：截取第一个 { 到最后一个 }
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b > a:
            return json.loads(s[a:b + 1])
        raise


def _post(system: str, user: str, *, json_mode: bool, temperature: float,
          max_tokens: int, retries: int, timeout: int, model: str) -> tuple[str, dict]:
    """底层 HTTP 调用。返回 (content, payload)。失败重试 retries 次后抛 DeepSeekError。"""
    payload_body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "thinking": {"type": "disabled"},
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload_body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key()}",
        },
    )

    last_err = None
    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:  # noqa: BLE001
                pass
            last_err = DeepSeekError(f"HTTP {e.code}: {detail or e.reason}")
            continue
        except urllib.error.URLError as e:
            last_err = DeepSeekError(f"网络错误：{e.reason}")
            continue

        if "error" in payload:
            last_err = DeepSeekError(f"API 返回错误：{payload['error']}")
            continue

        try:
            msg = payload["choices"][0]["message"]
            content = msg.get("content") or ""
        except (KeyError, IndexError):
            last_err = DeepSeekError("响应里没有 choices[0].message.content")
            continue

        if not content.strip():
            last_err = DeepSeekError("模型返回了空内容（可能烧在了思考上）")
            continue

        return content, payload

    raise last_err or DeepSeekError("调用失败，未知原因")


def _usage_meta(payload: dict, model: str) -> dict:
    return {
        "usage": payload.get("usage", {}),
        "cost": _estimate_cost(payload.get("usage", {})),
        "model": payload.get("model", model),
    }


def chat_json(system: str, user: str, *, model: str = DEFAULT_MODEL,
              temperature: float = 0.7, max_tokens: int = 3000,
              retries: int = 1, timeout: int = 60) -> dict:
    """调用 chat/completions 并强制 JSON 输出。

    返回 dict：
        data   —— 解析后的 JSON 对象
        raw    —— 模型原始文本
        + usage / cost / model
    """
    content, payload = _post(system, user, json_mode=True, temperature=temperature,
                             max_tokens=max_tokens, retries=retries, timeout=timeout,
                             model=model)
    try:
        data = _parse_json(content)
    except json.JSONDecodeError as e:
        raise DeepSeekError(f"JSON 解析失败：{e}；原始输出 {content[:200]!r}") from e
    return {"data": data, "raw": content, **_usage_meta(payload, model)}


def chat_text(system: str, user: str, *, model: str = DEFAULT_MODEL,
              temperature: float = 0.7, max_tokens: int = 3000,
              retries: int = 1, timeout: int = 60) -> dict:
    """调用 chat/completions，返回纯文本（不强制 JSON）。

    用于听力原稿这类非结构化输出。返回 dict：text / raw + usage / cost / model。
    """
    content, payload = _post(system, user, json_mode=False, temperature=temperature,
                             max_tokens=max_tokens, retries=retries, timeout=timeout,
                             model=model)
    return {"text": content, "raw": content, **_usage_meta(payload, model)}
