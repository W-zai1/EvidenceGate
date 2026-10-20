"""
通用供应商探测层：对**任意** OpenAI 兼容供应商做「列出可用模型」与「连通性测试」。

设计目标：
  · 一键读取可用模型 —— 不用手敲模型名；
  · 测试只做连通性判断 —— 明确告诉你这个 API 现在能不能用，
    **不触发人才库筛选流程**；
  · 各供应商行为一致（WorkBuddy / DeepSeek / 通义 / Ollama / 自定义 OpenAI 兼容）。

与 `llm_client.chat()` 的关键区别：这里**绝不降级**。
探测的意义就是回答「真的通不通」，一旦静默兜底成假数据，这个答案就没价值了。

只依赖标准库（urllib），与项目其余部分保持一致。
"""
import json
import ssl
import time
import urllib.error
import urllib.request

# 与 llm_client.PROVIDER_PRESETS 保持同源，避免两处默认值漂移
try:
    from engine.api.llm_client import PROVIDER_PRESETS
except Exception:  # 允许单独导入本模块做诊断
    PROVIDER_PRESETS = {}

DEFAULT_TIMEOUT = 60
PROBE_PROMPT = "ping，请只回复 pong"
PROBE_MAX_TOKENS = 8


def _preset(provider):
    return PROVIDER_PRESETS.get(provider or "") or {}


def _is_workbuddy(provider, base_url):
    if provider == "workbuddy":
        return True
    bu = (base_url or "").lower()
    return "copilot.tencent.com" in bu or "workbuddy" in bu or "tencentbuddy" in bu


def _is_local(provider, base_url):
    """本地推理服务（Ollama 等）不需要 API Key，甚至会拒绝空 Authorization。"""
    if provider == "ollama":
        return True
    bu = (base_url or "").lower()
    return "localhost" in bu or "127.0.0.1" in bu or "11434" in bu


def _resolve(provider, base_url, model, api_key):
    """补齐 base_url / model / api_key 的缺省值（前端没填就落到预设）。"""
    preset = _preset(provider)
    base = (base_url or preset.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    mdl = model or preset.get("model") or "gpt-4o-mini"
    key = (api_key or "").strip()
    if not key and _is_local(provider, base):
        key = "ollama"  # 占位：本地服务忽略该值，但请求头需要非空
    return base, mdl, key


def _ssl_ctx():
    return ssl.create_default_context()


def _http(url, headers, data=None, method=None, timeout=DEFAULT_TIMEOUT):
    """发一个请求，返回 (status, body_text)。HTTP 错误也返回而非抛出。"""
    req = urllib.request.Request(
        url, data=data, headers=headers,
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, "无法连接：%s" % (e.reason,)
    except Exception as e:
        return 0, "请求失败：%s" % e


def _extract_ids(body):
    """从 /models 响应里取出模型 id 列表（兼容 OpenAI 与 Ollama 两种形状）。"""
    try:
        obj = json.loads(body)
    except Exception:
        return []
    items = None
    if isinstance(obj, dict):
        for key in ("data", "models"):
            if isinstance(obj.get(key), list):
                items = obj[key]
                break
    if items is None:
        return []
    out = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for key in ("id", "name", "model"):
                v = it.get(key)
                if isinstance(v, str) and v:
                    out.append(v)
                    break
    # 去重且保持原序
    seen, uniq = set(), []
    for m in out:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def list_models(provider, base_url=None, api_key=None, timeout=DEFAULT_TIMEOUT):
    """列出该供应商可用的模型。

    返回 {"ok": bool, "models": [{"id","name","note"}], "error": str|None}
    note 用于附注倍率等额外信息（WorkBuddy 有积分倍率，其他供应商没有）。
    """
    if _is_workbuddy(provider, base_url):
        try:
            from engine.api.workbuddy_client import describe_models
            ms = describe_models(timeout=timeout)
            return {"ok": True, "error": None, "models": [
                {"id": m["id"], "name": m.get("name") or m["id"],
                 "note": "免费" if m.get("free") else (
                     "倍率未知" if m.get("multiplier") is None else "x%s 积分" % m["multiplier"])}
                for m in ms]}
        except Exception as e:
            return {"ok": False, "models": [], "error": str(e)}

    base, _, key = _resolve(provider, base_url, None, api_key)

    if _is_local(provider, base):
        # Ollama：原生 /api/tags 最可靠，再兜底 OpenAI 兼容的 /v1/models
        root = base[:-3].rstrip("/") if base.endswith("/v1") else base
        for url, hdr in ((root + "/api/tags", {}),
                         (base + "/models", {"Authorization": "Bearer " + (key or "ollama")})):
            status, body = _http(url, hdr, timeout=timeout)
            if status == 200:
                ids = _extract_ids(body)
                if ids:
                    return {"ok": True, "error": None,
                            "models": [{"id": i, "name": i, "note": ""} for i in ids]}
        return {"ok": False, "models": [],
                "error": "本地推理服务未响应（确认服务已启动，如 Ollama 默认 11434 端口）"}

    if not key:
        return {"ok": False, "models": [], "error": "未填写 API Key，无法读取模型列表"}

    status, body = _http(base + "/models",
                         {"Authorization": "Bearer " + key, "Accept": "application/json"},
                         timeout=timeout)
    if status == 200:
        ids = _extract_ids(body)
        if ids:
            return {"ok": True, "error": None,
                    "models": [{"id": i, "name": i, "note": ""} for i in ids]}
        return {"ok": False, "models": [], "error": "接口返回 200，但没解析出模型列表"}
    return {"ok": False, "models": [], "error": _friendly(status, body)}


def test_connection(provider, base_url=None, api_key=None, model=None,
                    timeout=DEFAULT_TIMEOUT):
    """只发一个最小补全请求，判断该 API 此刻能不能用。

    返回 {"ok", "model", "latency_ms", "reply", "error", "kind"}
    kind: not-signed-in / out-of-credit / rate-limited / network / server /
          auth / model-unavailable / unknown
    """
    if _is_workbuddy(provider, base_url):
        try:
            from engine.api.workbuddy_client import test_connection as wb_test
            return wb_test(model, timeout=timeout)
        except Exception as e:
            return {"ok": False, "model": model or "", "latency_ms": 0,
                    "reply": None, "error": str(e), "kind": "unknown"}

    base, mdl, key = _resolve(provider, base_url, model, api_key)
    if not key:
        return {"ok": False, "model": mdl, "latency_ms": 0, "reply": None,
                "error": "未填写 API Key", "kind": "auth"}

    payload = json.dumps({
        "model": mdl,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
    }).encode("utf-8")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
               "Accept": "application/json"}

    t0 = time.time()
    status, body = _http(base + "/chat/completions", headers, data=payload,
                         timeout=timeout)
    ms = int((time.time() - t0) * 1000)

    if status == 200:
        reply = ""
        try:
            obj = json.loads(body)
            msg = ((obj.get("choices") or [{}])[0].get("message") or {})
            # 思维链模型（如本地 deepseek-r1 / qwen-thinking）可能把内容全放进
            # reasoning_content，正文为空；兜底取它，确保能看出「确实活着」。
            reply = msg.get("content") or msg.get("reasoning_content") or ""
        except Exception:
            reply = ""
        return {"ok": True, "model": mdl, "latency_ms": ms,
                "reply": reply.strip()[:60], "error": None, "kind": None}

    return {"ok": False, "model": mdl, "latency_ms": ms, "reply": None,
            "error": _friendly(status, body), "kind": _kind_of(status, body)}


def _kind_of(status, body):
    low = (body or "").lower()
    if status in (401, 403):
        return "auth"
    if status == 402 or "insufficient" in low or "quota" in low or "余额不足" in body \
            or "积分不足" in body or "额度不足" in body:
        return "out-of-credit"
    if status == 404 or ("model" in low and ("not found" in low or "not exist" in low
                                             or "unavailable" in low)):
        return "model-unavailable"
    if status == 429:
        return "rate-limited"
    if status >= 500:
        return "server"
    if status == 0:
        return "network"
    return "unknown"


def _friendly(status, body):
    """把上游错误翻成一句人话，同时保留原始片段便于排查。"""
    snippet = (body or "").strip().replace("\n", " ")[:180]
    if status == 0:
        return snippet or "网络不通"
    table = {
        401: "认证失败（API Key 无效或已过期）",
        403: "无权限（Key 无权访问该模型）",
        402: "余额/积分不足",
        404: "接口或模型不存在",
        429: "请求过于频繁（被限流）",
    }
    head = table.get(status) or ("上游返回 HTTP %s" % status)
    return head + ("：" + snippet if snippet else "")
