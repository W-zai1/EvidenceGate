"""
LLM 客户端 —— 兼容 OpenAI Chat Completions 协议。

设计底线（不要退回）：
- **必须**有真实凭证；未配置、调用失败一律抛错，不做任何本地兜底。
  评审结论必须来自真实模型——宁可跑失败，也不能返回本地编造的内容，
  否则「LLM 认为某某很强」可能是脚本自己编的，整个筛选结论就失去意义。
- 仅依赖标准库，零 pip 安装。
- 真实用量必须记账（见 usage.py）。原实现完全没读 `usage` 字段，
  导致"这次花了多少钱"只能靠手编系数猜——这是"落地"环节最说不清的一件事。

调用入口
--------
    complete(...) -> {"text": str, "usage": {...}, "latency_ms": int, ...}   完整信息
    chat(...)     -> str                                                    只要文本（兼容旧调用）

两者都会在拿到 `usage` 后写一行 llm_usage（拿不到则标 estimated=1）。
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request


# ---------------------------------------------------------------------------
# 全局并发闸门
#
# 为什么需要：`REFINE_CONCURRENCY` 是**每个 run** 的并发数。同时跑 3 个岗位
# （各 4 路）就是 12 路并发打同一个 API Key，而每个候选人内部还有 3 个评委
# 并发，瞬时并发可达 36 路——这必然触发限流，而现有的重试只有
# `min(2**attempt, 8)` 三次，大规模下会成片失败。
#
# 解法：按 provider 设一个**进程级**信号量，所有 run 共享。
# 单个 run 的并发数仍然控制"一次处理几个候选人"，但真正打到 API 的
# 请求数由这个闸门统一封顶。
# ---------------------------------------------------------------------------
_GLOBAL_LIMITS = {
    "openai": 8,
    "deepseek": 8,
    "qwen": 6,
    "workbuddy": 2,     # 反代通道对并发敏感，压得很低
    "ollama": 2,        # 本地模型，并发高只会互相拖慢
}
_DEFAULT_GLOBAL_LIMIT = 6

_sems = {}
_sems_lock = threading.Lock()


def _global_limit(provider_key):
    """读环境变量 REFINE_GLOBAL_CONCURRENCY 覆盖默认值。"""
    raw = os.getenv("REFINE_GLOBAL_CONCURRENCY")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _GLOBAL_LIMITS.get(provider_key, _DEFAULT_GLOBAL_LIMIT)


def _global_sem(provider_key):
    with _sems_lock:
        s = _sems.get(provider_key)
        if s is None:
            s = threading.BoundedSemaphore(_global_limit(provider_key))
            _sems[provider_key] = s
        return s


def concurrency_snapshot():
    """当前各 provider 的全局并发上限（供界面与诊断使用）。"""
    return {k: _global_limit(k) for k in
            set(list(_GLOBAL_LIMITS) + list(_sems.keys()))}


class LLMError(RuntimeError):
    """模型调用失败（含重试后仍失败）。"""


class LLMConfigError(LLMError):
    """缺少必要配置（未提供凭证 / 参数不合法），属于"还没开始调用就注定失败"。"""


# 开箱即用的兼容网关预设（均兼容 OpenAI Chat Completions 协议）。
# 每个预设含：base_url / 默认 model / json_mode(是否支持 response_format=json_object) / stream(是否支持流式)。
PROVIDER_PRESETS = {
    "openai":   {"base_url": "https://api.openai.com/v1",                "model": "gpt-4o-mini",  "json_mode": True,  "stream": True},
    "deepseek": {"base_url": "https://api.deepseek.com/v1",             "model": "deepseek-chat", "json_mode": True,  "stream": True},
    "qwen":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "json_mode": True, "stream": True},
    "ollama":   {"base_url": "http://localhost:11434/v1",                "model": "qwen2.5:7b",   "json_mode": False, "stream": True},
    "workbuddy": {"base_url": "https://copilot.tencent.com/v2",         "model": "deepseek-v4-pro", "json_mode": True, "stream": True},
}

# 支持结构化 JSON 输出的供应商（不满足则退化为纯 prompt 约束 + 解析兜底，不强行 json_mode）
JSON_MODE_SUPPORTED = {"openai", "deepseek", "qwen", "workbuddy"}

# 已知单价（每 100 万 token 的美元价），仅用于"能算就算"的成本展示。
# 没在这里登记的模型 → 只报 token 数，不编造金额。
PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "deepseek-chat": (0.27, 1.10),
    "qwen-plus": (0.40, 1.20),
}

# 当前线程的调用上下文（角色/岗位/候选人），由 agents 在调用前设置。
# 用 threading.local 而非全局：并发跑多候选人时不会互相串味。
_local = threading.local()


def set_context(**kw):
    """设置本次调用的记账上下文（role / job_id / candidate_id）。"""
    ctx = getattr(_local, "ctx", None) or {}
    ctx.update({k: v for k, v in kw.items() if v is not None})
    _local.ctx = ctx
    return ctx


def clear_context():
    _local.ctx = {}


def _context():
    return getattr(_local, "ctx", None) or {}


def _provider_key(provider, base_url):
    """把 provider 名或 base_url 映射到内部供应商键（用于能力判定）。"""
    if provider in PROVIDER_PRESETS:
        return provider
    bu = (base_url or "").lower()
    if "copilot.tencent.com" in bu or "workbuddy" in bu or "tencentbuddy" in bu:
        return "workbuddy"
    if "deepseek" in bu:
        return "deepseek"
    if "dashscope" in bu or "qwen" in bu:
        return "qwen"
    if "localhost" in bu or "11434" in bu:
        return "ollama"
    if "openai" in bu:
        return "openai"
    return "openai"  # 默认按 OpenAI 协议处理


def estimate_cost(usage, model):
    """按单价估算金额（美元）。没登记单价的模型返回 None——不编数字。"""
    price = PRICING.get((model or "").strip().casefold())
    if not price or not usage:
        return None
    pt = usage.get("prompt_tokens") or 0
    ct = usage.get("completion_tokens") or 0
    return round(pt / 1e6 * price[0] + ct / 1e6 * price[1], 6)


def _record_usage(provider_key, model, usage, latency_ms, ok=True, error=""):
    """把一次调用的用量写库。记账失败不能影响评审本身。"""
    ctx = _context()
    try:
        import usage as usage_mod
        usage_mod.record(
            provider=provider_key, model=model,
            role=ctx.get("role", ""), job_id=ctx.get("job_id", ""),
            candidate_id=ctx.get("candidate_id", ""),
            usage=usage, latency_ms=latency_ms, ok=ok, error=error,
        )
    except Exception as e:      # 库锁、表缺失都不应让评审崩掉
        print("[usage warn] 用量记账失败：%s" % e)


def chat(messages, model=None, api_key=None, base_url=None, temperature=0.7,
         provider=None, json_mode=False, timeout=90, retries=2):
    """与 OpenAI 兼容接口对话，返回模型文本（兼容旧调用）。

    需要 usage/延迟等信息时用 `complete()`。
    """
    return complete(messages, model=model, api_key=api_key, base_url=base_url,
                    temperature=temperature, provider=provider, json_mode=json_mode,
                    timeout=timeout, retries=retries)["text"]


def complete(messages, model=None, api_key=None, base_url=None, temperature=0.7,
             provider=None, json_mode=False, timeout=90, retries=2, actor="system"):
    """执行一次补全，返回 {text, usage, model, provider, latency_ms, attempt}。

    失败语义：未配置凭证抛 LLMConfigError；重试耗尽仍失败抛 LLMError。
              **绝不降级为本地占位内容**——调用方必须显式处理失败。
    """
    provider_key = _provider_key(provider, base_url)
    # model 不写死默认值：否则 --provider workbuddy 会把 gpt-4o-mini 发到腾讯网关（400）。
    # 优先级：调用方显式指定 > 供应商预设 > 环境变量 > 通用兜底。
    if not model:
        model = (PROVIDER_PRESETS.get(provider_key) or {}).get("model") \
            or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

    if provider_key == "workbuddy":
        return _complete_workbuddy(messages, model, temperature, json_mode,
                                   timeout, retries, provider_key)

    api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "未配置模型凭证：请在「模型设置」里填入 API Key，"
            "或设置 OPENAI_API_KEY 环境变量；"
            "也可以 --provider workbuddy 复用 WorkBuddy 本机登录积分。"
        )
    base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    use_json = bool(json_mode) and provider_key in JSON_MODE_SUPPORTED

    last_err = None
    for attempt in range(max(1, retries + 1)):
        t0 = time.time()
        # 全局闸门：跨 run 共享，封住打到供应商的总并发。
        # 放在循环内部而不是外面：等待重试的时间不该占着名额。
        sem = _global_sem(provider_key)
        sem.acquire()
        try:
            text, usage = _raw_completion(messages, model, api_key, base_url,
                                          temperature, use_json, timeout)
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            last_err = e
            print(f"[llm warn] 第 {attempt + 1}/{retries + 1} 次调用失败（{provider_key}）：{e}")
            _record_usage(provider_key, model, None, latency, ok=False, error=str(e))
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))  # 指数退避，封顶 8s
            continue
        finally:
            sem.release()

        latency = int((time.time() - t0) * 1000)
        _record_usage(provider_key, model, usage, latency)
        return {"text": text, "usage": usage or {}, "model": model,
                "provider": provider_key, "latency_ms": latency, "attempt": attempt + 1}
    raise LLMError(f"模型调用失败（已重试 {retries + 1} 次）：{last_err}") from last_err


def _complete_workbuddy(messages, model, temperature, json_mode, timeout, retries,
                        provider_key):
    """WorkBuddy 反代调用（复用本机 WorkBuddy 桌面端账号积分）。

    与普通供应商的差别：凭证不是 api_key，而是本机 WorkBuddy 登录态
    （由 workbuddy_client 解密取得）。失败语义与 complete() 一致。
    """
    import workbuddy_client as wb

    last_err = None
    for attempt in range(max(1, retries + 1)):
        t0 = time.time()
        sem = _global_sem(provider_key)
        sem.acquire()
        try:
            text, usage = wb.chat(messages, model=model, temperature=temperature,
                                  json_mode=json_mode, timeout=timeout, with_usage=True)
        except wb.InsufficientCreditError as e:
            # 积分不足重试没有意义，直接失败，避免无谓等待
            _record_usage(provider_key, model, None, int((time.time() - t0) * 1000),
                          ok=False, error=str(e))
            raise LLMError(str(e)) from e
        except wb.WorkBuddyError as e:
            last_err = e
            print(f"[llm warn] 第 {attempt + 1}/{retries + 1} 次 WorkBuddy 调用失败：{e}")
            _record_usage(provider_key, model, None, int((time.time() - t0) * 1000),
                          ok=False, error=str(e))
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
            continue
        finally:
            sem.release()

        latency = int((time.time() - t0) * 1000)
        _record_usage(provider_key, model, usage, latency)
        return {"text": text, "usage": usage or {}, "model": model,
                "provider": provider_key, "latency_ms": latency, "attempt": attempt + 1}
    raise LLMError(f"WorkBuddy 调用失败（已重试 {retries + 1} 次）：{last_err}") from last_err


def _raw_completion(messages, model, api_key, base_url, temperature, use_json, timeout):
    """单次真实补全调用（无重试）。返回 (文本, usage)。"""
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if use_json:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise urllib.error.HTTPError(base_url, resp.status, resp.reason, resp.headers, None)
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    return text, _normalize_usage(data.get("usage"))


def _normalize_usage(u):
    """把不同网关的 usage 字段统一成 OpenAI 命名。

    有的网关返回 input_tokens/output_tokens，有的是 prompt_tokens/completion_tokens，
    有的干脆不返回。不返回就返回 None——**不要用字符数估算冒充真实用量**。
    """
    if not isinstance(u, dict) or not u:
        return None
    p = u.get("prompt_tokens", u.get("input_tokens"))
    c = u.get("completion_tokens", u.get("output_tokens"))
    t = u.get("total_tokens")
    if p is None and c is None and t is None:
        return None
    out = {}
    if p is not None:
        out["prompt_tokens"] = int(p)
    if c is not None:
        out["completion_tokens"] = int(c)
    if t is not None:
        out["total_tokens"] = int(t)
    elif p is not None and c is not None:
        out["total_tokens"] = int(p) + int(c)
    return out


def parse_sse_deltas(lines):
    """把 OpenAI 兼容的 SSE 行流解析成 (增量列表, usage)。

    单独抽出来是为了可测：不用起 HTTP 服务就能验证
    "带 data: 前缀 / [DONE] / 空行 / 半截 JSON / 非 data 行" 这些真实形态。
    解析失败的行直接跳过——一个坏块不该让整段发言丢掉。
    """
    chunks, usage = [], None
    for raw in lines:
        line = (raw or "").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
        try:
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
        except (AttributeError, IndexError, TypeError):
            continue
        piece = delta.get("content")
        if piece:
            chunks.append(piece)
    return chunks, usage


def _stream_raw(messages, model, api_key, base_url, temperature, use_json, timeout,
                on_delta):
    """单次真实流式调用（无重试）。逐块回调 on_delta，返回 (全文, usage)。

    `stream_options.include_usage` 只在部分网关生效，拿不到 usage 就返回 None
    ——上层会把它记成 estimated，而不是编一个数出来。
    """
    payload = {"model": model, "messages": messages,
               "temperature": temperature, "stream": True,
               "stream_options": {"include_usage": True}}
    if use_json:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    parts = []
    usage = None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            body = resp.read(2000).decode("utf-8", "replace")
            raise LLMError("流式调用返回 HTTP %s：%s" % (resp.status, body[:300]))
        # 逐行读：不能用 resp.read()，否则又是"等全部结束才拿到"，
        # 那就等于没做流式。
        for raw in resp:
            line = raw.decode("utf-8", "replace")
            pieces, u = parse_sse_deltas([line])
            if u:
                usage = u
            for p in pieces:
                parts.append(p)
                if on_delta:
                    try:
                        on_delta(p)
                    except Exception:
                        pass
            if line.strip().startswith("data:") and line.strip()[5:].strip() == "[DONE]":
                break
    return "".join(parts), usage


def complete_stream(messages, on_delta=None, model=None, api_key=None, base_url=None,
                    temperature=0.7, provider=None, json_mode=False, timeout=90,
                    retries=2, actor="system"):
    """流式补全，语义与 `complete()` 一致，只是多了增量回调。

    返回结构与 complete() 相同，另有 `streamed` 字段说明这一次是否真的走了流式。
    失败兜底策略：
      · 一个增量都没收到就失败 → 按 complete() 的重试语义重试；
      · 已经吐出内容之后断了 → **不重试同一路**，改用非流式再要一次完整结果。
        因为重试会把同一段话重新吐一遍，前端看到的就是重复内容；
        而非流式拿回全文后，前端会用 agent_message 的全文替换掉气泡里的半截，
        用户看到的最终结果是完整且一致的。
    """
    on_delta = on_delta or (lambda _p: None)
    provider_key = _provider_key(provider, base_url)
    want_stream = (PROVIDER_PRESETS.get(provider_key) or {}).get("stream", True)
    if not want_stream or os.getenv("REFINE_STREAM", "1") == "0":
        out = complete(messages, model=model, api_key=api_key, base_url=base_url,
                       temperature=temperature, provider=provider, json_mode=json_mode,
                       timeout=timeout, retries=retries, actor=actor)
        out["streamed"] = False
        return out

    if not model:
        model = (PROVIDER_PRESETS.get(provider_key) or {}).get("model") \
            or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

    emitted = {"n": 0}

    def _tap(piece):
        emitted["n"] += 1
        on_delta(piece)

    # WorkBuddy 网关的凭证与路径都不同，交给它自己的实现
    if provider_key == "workbuddy":
        return _complete_stream_workbuddy(
            messages, model, temperature, json_mode, timeout, retries,
            provider_key, _tap, emitted, on_delta)

    api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "未配置模型凭证：请在「模型设置」里填入 API Key，"
            "或设置 OPENAI_API_KEY 环境变量；"
            "也可以 --provider workbuddy 复用 WorkBuddy 本机登录积分。"
        )
    base_url = (base_url or os.getenv("OPENAI_BASE_URL")
                or "https://api.openai.com/v1").rstrip("/")
    use_json = bool(json_mode) and provider_key in JSON_MODE_SUPPORTED

    last_err = None
    for attempt in range(max(1, retries + 1)):
        t0 = time.time()
        sem = _global_sem(provider_key)
        sem.acquire()
        before = emitted["n"]
        try:
            text, usage = _stream_raw(messages, model, api_key, base_url,
                                      temperature, use_json, timeout, _tap)
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            last_err = e
            _record_usage(provider_key, model, None, latency, ok=False, error=str(e))
            if emitted["n"] > before:
                # 已经吐过字了：不能再重试同一路（会重复输出），
                # 改用非流式拿一次完整结果，前端会用它替换半截内容。
                print("[llm warn] 流式中断（已输出 %d 段），改为非流式重取全文：%s"
                      % (emitted["n"] - before, e))
                out = complete(messages, model=model, api_key=api_key,
                               base_url=base_url, temperature=temperature,
                               provider=provider, json_mode=json_mode,
                               timeout=timeout, retries=retries, actor=actor)
                out["streamed"] = False
                return out
            print(f"[llm warn] 第 {attempt + 1}/{retries + 1} 次流式调用失败（{provider_key}）：{e}")
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
            continue
        finally:
            sem.release()

        latency = int((time.time() - t0) * 1000)
        _record_usage(provider_key, model, usage, latency)
        return {"text": text, "usage": usage or {}, "model": model,
                "provider": provider_key, "latency_ms": latency,
                "attempt": attempt + 1, "streamed": True}
    raise LLMError(f"流式调用失败（已重试 {retries + 1} 次）：{last_err}") from last_err


def _complete_stream_workbuddy(messages, model, temperature, json_mode, timeout,
                               retries, provider_key, tap, emitted, on_delta):
    """WorkBuddy 的流式路径。网关只接受流式，所以它本来就有增量。"""
    import workbuddy_client as wb

    last_err = None
    for attempt in range(max(1, retries + 1)):
        t0 = time.time()
        sem = _global_sem(provider_key)
        sem.acquire()
        before = emitted["n"]
        try:
            text, usage = wb.chat_stream(messages, model=model,
                                         temperature=temperature, json_mode=json_mode,
                                         timeout=timeout, on_delta=tap)
        except wb.InsufficientCreditError as e:
            _record_usage(provider_key, model, None, int((time.time() - t0) * 1000),
                          ok=False, error=str(e))
            raise LLMError(str(e)) from e
        except Exception as e:
            last_err = e
            _record_usage(provider_key, model, None, int((time.time() - t0) * 1000),
                          ok=False, error=str(e))
            if emitted["n"] > before:
                print("[llm warn] WorkBuddy 流式中断，改为一次性取回全文：%s" % e)
                out = complete(messages, model=model, temperature=temperature,
                               provider=provider_key, json_mode=json_mode,
                               timeout=timeout, retries=retries)
                out["streamed"] = False
                return out
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
            continue
        finally:
            sem.release()

        latency = int((time.time() - t0) * 1000)
        _record_usage(provider_key, model, usage, latency)
        return {"text": text, "usage": usage or {}, "model": model,
                "provider": provider_key, "latency_ms": latency,
                "attempt": attempt + 1, "streamed": True}
    raise LLMError(f"WorkBuddy 流式调用失败（已重试 {retries + 1} 次）：{last_err}") from last_err


def chat_stream(messages, model=None, api_key=None, base_url=None, temperature=0.7,
                provider=None, timeout=120):
    """流式补全的**生成器**写法（保留给脚本/CLI 用）。

    内部复用 complete_stream，因此供应商预设、并发闸门、用量记账与重试
    都和正常调用一致。原来这里是一份平行的实现：写死了 OPENAI_API_KEY、
    不认 provider、不计用量、失败即抛——等于两套语义。
    """
    import queue as _queue
    import threading as _threading

    q = _queue.Queue()
    box = {}

    def worker():
        def push(piece):
            q.put(("delta", piece))
        try:
            out = complete_stream(messages, on_delta=push, model=model, api_key=api_key,
                                  base_url=base_url, temperature=temperature,
                                  provider=provider, timeout=timeout)
            box["out"] = out
        except Exception as e:
            box["err"] = e
        finally:
            q.put(("end", None))

    t = _threading.Thread(target=worker, daemon=True)
    t.start()
    while True:
        kind, payload = q.get()
        if kind == "delta":
            yield payload
        else:
            break
    t.join(timeout=0.1)
    if "err" in box:
        raise box["err"]
