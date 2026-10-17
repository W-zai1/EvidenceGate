"""
WorkBuddy 反代客户端 —— 让本项目复用 WorkBuddy（CodeBuddy 桌面端）的账号积分调用模型。

背景 / 为什么需要这一层
----------------------
WorkBuddy 桌面端登录后，模型调用走腾讯网关 `https://copilot.tencent.com/v2/chat/completions`，
请求消耗的是**账号积分**（而非用户自己的 API key）。凭证保存在
`%LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`，
且在 Windows 版里 `accessToken` / `refreshToken` / `nickname` 是**加密字段**
（形如 `{"$wbEncrypted":1,"envelope":"<base64>"}`），不是明文。

所以本模块做两件事：
  1. 拿到 at-rest 密钥并解密出 accessToken；
  2. 按网关要求（SSE 流式 + 一组固定网关请求头）发起补全调用。

与 WorkBuddy 官方工具链（dsh-connect-workbuddy）保持一致的实现要点
--------------------------------------------------------------
- **at-rest 密钥**：调用 WorkBuddy 自己的二进制，以 `ELECTRON_RUN_AS_NODE=1` 运行
  `process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()`
  拿到 `{"atRestSecretKey": "<base64 字符串>"}`。密钥 = `sha256(atRestSecretKey 的字符串本身)`
  （注意是对 base64 **字符串**取哈希，不是对其解码后的字节）。
- **信封解密**：AES-256-GCM，AAD 为固定分帧
  `"WB-AAD\\0" + 0x01 + lp("WBEV1") + lp("sym-v1") + u32be(suite) + lp(keyId) + 0x02 + 0x00 + 0x00`
  （`lp` = uint32be 长度前缀 + UTF-8 字节）。`keyId` 必须等于 `sha256(key)` 的前 16 位十六进制。
  GCM 认证标签不匹配时**直接报错**，绝不返回被截断的 token。
- **调用形态**：网关**只接受流式**请求（`"stream": true`）；非流式会返回
  `code 11101 Non-stream chat request is currently not supported`。因此本模块一律走 SSE，
  再把增量拼回完整文本，对上层保持非流式的 `chat()` 语义。
- **积分不足**：网关返回 HTTP 402（或正文含 "insufficient credit"/"积分不足" 等）时，
  转成可识别的 `InsufficientCreditError`，让上层能明确报错而不是静默降级。

安全与合规说明
--------------
- 只读取**当前登录用户自己**的 WorkBuddy 凭证，用于本机本项目的模型调用；
- 密钥只在进程内存中派生，**不落盘**、不打印、不写日志；
- 解密借助 WorkBuddy 自身可执行文件完成（与官方插件同一路径）；
- 严禁把 token 打印/记录，日志只输出脱敏后的诊断信息。
"""
import hashlib
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CN_GATEWAY = "https://copilot.tencent.com"
GLOBAL_GATEWAY = "https://www.workbuddy.ai"
CN_BILLING = "https://www.codebuddy.cn"
CN_CHAT_PATH = "/v2/chat/completions"
CN_MODELS_PATH = "/v2/enterprises/personal/models"
CN_CREDITS_PATH = "/v2/billing/meter/get-user-resource"

# 网关要求的一组固定请求头（与 WorkBuddy CLI 客户端一致）
GATEWAY_UA = "CLI/2.63.2 CodeBuddy/2.63.2"

# 网关模型清单（离线兜底；联网时以 /v2/enterprises/personal/models 为准）
FALLBACK_MODELS = [
    "deepseek-v4-pro", "deepseek-v4.1-flash", "glm-5.3", "glm-5.2",
    "kimi-k3-1", "step-5-preview", "space-bunny", "auto",
]
DEFAULT_MODEL = "deepseek-v4-pro"

# 积分不足的错误标记（ASCII 小写 + 原始中文），命中即判定为「没积分了」
CREDIT_MARKERS = (
    "insufficient credit", "no credit", "credit exhausted", "out of credit",
    "quota exceeded", "payment required", "credit not enough", "not enough credit",
    "积分不足", "额度不足", "余额不足", "积分用完", "额度用尽", "没有积分",
)
# 会话失效标记：需要回 WorkBuddy 重新登录
SESSION_DEAD_MARKERS = ("Offline user session not found", "12153")

KEY_FETCH_TIMEOUT_S = 30


class WorkBuddyError(Exception):
    """WorkBuddy 接入层通用异常。"""


class WorkBuddyNotInstalled(WorkBuddyError):
    """未找到 WorkBuddy 桌面端可执行文件。"""


class WorkBuddyNotSignedIn(WorkBuddyError):
    """未找到可用的 WorkBuddy 登录凭证。"""


class InsufficientCreditError(WorkBuddyError):
    """账号积分不足（HTTP 402 或网关积分错误标记）。"""


# ---------------------------------------------------------------------------
# 凭证读取与解密
# ---------------------------------------------------------------------------
def _auth_file_candidates():
    """WorkBuddy 桌面端 auth 文件的候选路径（按优先级）。

    `WORKBUDDY_AUTH_FILE` 是**显式指定**：一旦设置就只用它，不再回退到默认路径，
    这样「只想用这份凭证」的场景不会被默认登录态覆盖。
    """
    override = (os.getenv("WORKBUDDY_AUTH_FILE") or "").strip()
    if override:
        return [override]
    local = os.getenv("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    roaming = os.getenv("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return [
        os.path.join(local, "CodeBuddyExtension", "Data", "Public", "auth",
                     "workbuddy-desktop.info"),
        os.path.join(roaming, "CodeBuddyExtension", "Data", "Public", "auth",
                     "workbuddy-desktop.info"),
    ]


def _app_executable_candidates():
    """WorkBuddy 桌面端可执行文件的候选路径（Windows 优先 Program Files）。

    `WORKBUDDY_APP_EXECUTABLE` 同为显式指定：设置后只尝试该路径，
    不再自动探测默认安装位置（方便在非标准安装/测试环境下精确控制）。
    """
    override = (os.getenv("WORKBUDDY_APP_EXECUTABLE") or "").strip()
    if override:
        return [override]
    cands = []
    local = os.getenv("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.getenv(env)
        if base:
            cands.append(os.path.join(base, "WorkBuddy", "WorkBuddy.exe"))
    cands.append(os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe"))
    cands.append(os.path.join(local, "WorkBuddy", "WorkBuddy.exe"))
    return [c for c in cands if c]


def _find_first(paths, kind="file"):
    for p in paths:
        try:
            if kind == "file" and os.path.isfile(p):
                return p
            if kind == "dir" and os.path.isdir(p):
                return p
        except OSError:
            continue
    return None


# 一次性执行的小程序：借 WorkBuddy 自身运行时取 at-rest 密钥 + 解密字段。
# 之所以借助其二进制，是因为它才是写入该加密格式的那一版构建，
# 密钥只有它能给（与官方 dsh-connect-workbuddy 插件同一路径）。
_UNLOCK_JS = r"""
const fs = require('fs'), crypto = require('crypto');
const authPath = process.argv[1];
const out = { ok: false };

function deriveKey() {
  const payload = process._linkedBinding('electron_browser_workbuddy_storage').loggerGet();
  return crypto.createHash('sha256').update(JSON.parse(payload).atRestSecretKey, 'utf8').digest();
}
const u32 = n => { const b = Buffer.allocUnsafe(4); b.writeUInt32BE(n); return b; };
const lp = s => { const b = Buffer.from(s, 'utf8'); return Buffer.concat([u32(b.length), b]); };
const fieldAad = (kid, suite) => Buffer.concat([
  Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
  lp('WBEV1'), lp('sym-v1'), u32(suite), lp(kid), Buffer.from([2, 0, 0])]);

function openField(field, key) {
  if (typeof field === 'string') return field;                 // 老版本：明文
  if (!field || typeof field !== 'object' || typeof field.envelope !== 'string') return undefined;
  const e = JSON.parse(Buffer.from(field.envelope, 'base64').toString('utf8'));
  const keyId = crypto.createHash('sha256').update(key).digest('hex').slice(0, 16);
  if (e.keyId !== keyId) throw new Error('envelope belongs to another key');
  const d = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(e.nonce, 'base64'), { authTagLength: 16 });
  d.setAAD(fieldAad(e.keyId, e.suite));
  d.setAuthTag(Buffer.from(e.authTag, 'base64'));
  return Buffer.concat([d.update(Buffer.from(e.ciphertext, 'base64')), d.final()]).toString('utf8');
}

try {
  const doc = JSON.parse(fs.readFileSync(authPath, 'utf8'));
  const auth = (doc.auth && typeof doc.auth === 'object') ? doc.auth : doc;
  const acct = (doc.account && typeof doc.account === 'object') ? doc.account : doc;
  const key = deriveKey();
  out.ok = true;
  out.accessToken = openField(auth.accessToken, key);
  out.domain = typeof auth.domain === 'string' ? auth.domain : '';
  out.uid = typeof acct.uid === 'string' ? acct.uid : '';
  out.nickname = openField(acct.nickname, key) || '';
  out.expiresAt = typeof auth.expiresAt === 'number' ? auth.expiresAt : 0;
  if (!out.accessToken) { out.ok = false; out.error = 'no accessToken in credential file'; }
} catch (e) {
  out.ok = false;
  out.error = String((e && e.message) || e);
}
process.stdout.write(JSON.stringify(out));
"""

# 进程内缓存：一次运行只解密一次（密钥绝不落盘，仅存在于内存）
_CREDENTIAL_CACHE = None


def load_credential(force=False):
    """读取并解密 WorkBuddy 登录凭证。

    返回 dict：{access_token, domain, uid, nickname, expires_at, source}
    失败时抛 WorkBuddyNotInstalled / WorkBuddyNotSignedIn。
    """
    global _CREDENTIAL_CACHE
    if _CREDENTIAL_CACHE is not None and not force:
        return _CREDENTIAL_CACHE

    auth_path = _find_first(_auth_file_candidates())
    if not auth_path:
        raise WorkBuddyNotSignedIn(
            "未找到 WorkBuddy 登录凭证（%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\"
            "workbuddy-desktop.info）。请先在 WorkBuddy 桌面端登录。"
        )
    exe = _find_first(_app_executable_candidates())
    if not exe:
        raise WorkBuddyNotInstalled(
            "未找到 WorkBuddy 桌面端可执行文件，无法解密登录凭证。"
            "可用环境变量 WORKBUDDY_APP_EXECUTABLE 指定路径。"
        )

    try:
        proc = subprocess.run(
            [exe, "-e", _UNLOCK_JS, auth_path],
            env=dict(os.environ, ELECTRON_RUN_AS_NODE="1"),
            capture_output=True, timeout=KEY_FETCH_TIMEOUT_S,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise WorkBuddyError("WorkBuddy 密钥读取超时（%ds）。" % KEY_FETCH_TIMEOUT_S)
    except Exception as e:
        raise WorkBuddyError("无法调用 WorkBuddy 读取密钥：%s" % e)

    payload = (proc.stdout or "").strip()
    if not payload:
        raise WorkBuddyError(
            "WorkBuddy 未返回凭证解密结果（exit=%s）：%s"
            % (proc.returncode, (proc.stderr or "").strip()[:200])
        )
    try:
        data = json.loads(payload)
    except Exception:
        raise WorkBuddyError("WorkBuddy 凭证解密结果不是合法 JSON。")
    if not data.get("ok"):
        raise WorkBuddyNotSignedIn(
            "读取 WorkBuddy 登录凭证失败：%s；请在 WorkBuddy 桌面端重新登录。"
            % (data.get("error") or "未知原因")
        )

    _CREDENTIAL_CACHE = {
        "access_token": data.get("accessToken") or "",
        "domain": data.get("domain") or "",
        "uid": data.get("uid") or "",
        "nickname": data.get("nickname") or "",
        "expires_at": data.get("expiresAt") or 0,
        "source": auth_path,
    }
    if not _CREDENTIAL_CACHE["access_token"]:
        raise WorkBuddyNotSignedIn("WorkBuddy 凭证中没有 accessToken，请重新登录。")
    return _CREDENTIAL_CACHE


def clear_credential_cache():
    """清掉进程内缓存的凭证（诊断/登出场景用）。"""
    global _CREDENTIAL_CACHE
    _CREDENTIAL_CACHE = None


def _gateway_for(domain):
    """按登录域名选择网关：国际站走 workbuddy.ai，其余走国内网关。"""
    d = (domain or "").strip().lower()
    if d == "workbuddy.ai" or d.endswith(".workbuddy.ai") \
            or d == "codebuddy.ai" or d.endswith(".codebuddy.ai"):
        return GLOBAL_GATEWAY
    return CN_GATEWAY


def is_available():
    """探测 WorkBuddy 积分通道是否就绪（用于前端提示 / 预检）。"""
    try:
        cred = load_credential()
        return {"ok": True, "nickname": cred["nickname"], "domain": cred["domain"]}
    except WorkBuddyError as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 请求构造与错误分类
# ---------------------------------------------------------------------------
def _headers(cred, stream=True):
    """构造网关要求的请求头（含固定的 WorkBuddy 网关标识头）。"""
    h = {
        "Authorization": "Bearer " + cred["access_token"],
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": GATEWAY_UA,
        "X-Product": "SaaS",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-Private-Data": "true",
        "X-Agent-Intent": "craft",
        "X-Agent-Purpose": "conversation",
        "X-Conversation-ID": str(uuid.uuid4()),
        "X-Conversation-Message-ID": str(uuid.uuid4()),
        "X-Request-ID": str(uuid.uuid4()),
        "x-codebuddy-request": "1",
    }
    if cred.get("uid"):
        h["X-User-Id"] = cred["uid"]
    return h


def _raise_for_error(status, body):
    """把网关错误翻译成可识别的异常（积分不足 / 会话失效 / 其它）。"""
    text = body or ""
    lower = text.lower()
    if status == 402 or any(m in lower or m in text for m in CREDIT_MARKERS):
        raise InsufficientCreditError(
            "WorkBuddy 积分不足（HTTP %s）。请在 WorkBuddy 桌面端充值/领取积分后重试。" % status
        )
    if any(m in text for m in SESSION_DEAD_MARKERS):
        raise WorkBuddyNotSignedIn(
            "WorkBuddy 会话已失效，请在 WorkBuddy 桌面端重新登录后重试。"
        )
    raise WorkBuddyError("WorkBuddy 网关返回 HTTP %s：%s" % (status, text[:300]))


def _request(url, payload=None, cred=None, timeout=120, method=None):
    """执行一次网关请求，返回 (status, body_text)。"""
    cred = cred or load_credential()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    stream = bool(payload and payload.get("stream"))
    req = urllib.request.Request(
        url, data=data, headers=_headers(cred, stream=stream),
        method=method or ("POST" if data is not None else "GET"),
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        _raise_for_error(e.code, body)
        raise  # _raise_for_error 一定会抛，避免静默继续
    except urllib.error.URLError as e:
        raise WorkBuddyError("无法连接 WorkBuddy 网关：%s" % (e.reason,))


# ---------------------------------------------------------------------------
# 模型清单
# ---------------------------------------------------------------------------
def _parse_credit_multiplier(value):
    """解析 `credits` 字段，形如 "x0.51 credits" / "x0.05" / "x0.00 credits"。"""
    if not isinstance(value, str):
        return None
    m = re.search(r"x\s*([0-9]*\.?[0-9]+)", value, re.IGNORECASE)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    return n if n >= 0 else None


def describe_models(timeout=30):
    """返回账号可用模型的详细信息列表（含积分倍率、上下文窗口、是否免费）。

    每项：{"id", "name", "credits": 原文, "multiplier": float|None,
           "context": int|None, "max_output": int|None, "reasoning": bool, "free": bool}
    free=True 表示倍率为 0（调用不扣积分）。
    """
    try:
        cred = load_credential()
        base = _gateway_for(cred["domain"])
        status, body = _request(base + CN_MODELS_PATH, cred=cred, timeout=timeout)
        if status != 200:
            _raise_for_error(status, body)
        env = json.loads(body)
    except Exception:
        # 联网失败时退回内置清单（不带倍率信息）
        return [{"id": m, "name": m, "credits": None, "multiplier": None,
                 "context": None, "max_output": None, "reasoning": False, "free": False}
                for m in FALLBACK_MODELS]

    raw_models = ((env.get("data") or {}).get("models") or [])
    out = []
    seen = set()
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        mid = raw.get("id")
        if not isinstance(mid, str) or not mid or mid in seen or raw.get("disabled") is True:
            continue
        seen.add(mid)
        credits_raw = raw.get("credits")
        mult = _parse_credit_multiplier(credits_raw)
        ctx = raw.get("maxInputTokens")
        mo = raw.get("maxOutputTokens")
        out.append({
            "id": mid,
            "name": (raw.get("name") or mid) if isinstance(raw.get("name"), str) else mid,
            "credits": credits_raw if isinstance(credits_raw, str) else None,
            "multiplier": mult,
            "context": ctx if isinstance(ctx, int) else None,
            "max_output": mo if isinstance(mo, int) else None,
            "reasoning": bool(raw.get("reasoning")),
            "free": mult == 0,
        })
    if not out:
        out = [{"id": m, "name": m, "credits": None, "multiplier": None,
                "context": None, "max_output": None, "reasoning": False, "free": False}
               for m in FALLBACK_MODELS]
    return out


def list_models(timeout=30):
    """拉取账号可用的 WorkBuddy 模型 ID 清单；失败时回退到内置清单。"""
    try:
        return [m["id"] for m in describe_models(timeout=timeout)]
    except Exception:
        return list(FALLBACK_MODELS)


# ---------------------------------------------------------------------------
# 积分查询
# ---------------------------------------------------------------------------
def _billing_base(cred):
    """计费域名：国际站与网关同源，国内站走 codebuddy.cn。"""
    d = (cred.get("domain") or "").strip().lower()
    if d == "workbuddy.ai" or d.endswith(".workbuddy.ai") \
            or d == "codebuddy.ai" or d.endswith(".codebuddy.ai"):
        return GLOBAL_GATEWAY
    return CN_BILLING


def _billing_headers(cred):
    """计费接口请求头（与聊天网关分开：Origin/Referer 指向计费站，并带 X-Domain）。"""
    base = _billing_base(cred)
    h = {
        "Authorization": "Bearer " + cred["access_token"],
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": base,
        "Referer": base + "/",
        "User-Agent": GATEWAY_UA,
        "X-Product": "SaaS",
    }
    if cred.get("uid"):
        h["X-User-Id"] = cred["uid"]
    if cred.get("domain"):
        h["X-Domain"] = cred["domain"]
    return h


def _now_str(dt):
    return "%04d-%02d-%02d %02d:%02d:%02d" % (dt.year, dt.month, dt.day,
                                              dt.hour, dt.minute, dt.second)


def _parse_ts(raw):
    """把接口里的时间字段解析成毫秒时间戳（毫秒整数或可解析字符串）。"""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 1e12:
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(raw.strip(), fmt).timestamp() * 1000)
            except ValueError:
                continue
    return None


def fetch_credits(timeout=30):
    """查询账号剩余积分，返回结构化详情。

    返回 dict：{total_remain, total_size, total_used, packages[], expiring_soon[], error}
    查询失败不抛异常，返回 {"error": ...}，让调用方自行决定如何提示。
    """
    out = {"total_remain": 0, "total_size": 0, "total_used": 0,
           "packages": [], "expiring_soon": [], "error": None}
    try:
        cred = load_credential()
        base = _billing_base(cred)
        now = datetime.now()
        payload = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": _now_str(now),
            "PackageEndTimeRangeEnd": _now_str(now + timedelta(days=368)),
        }
        req = urllib.request.Request(
            base + CN_CREDITS_PATH,
            data=json.dumps(payload).encode("utf-8"),
            headers=_billing_headers(cred),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            if resp.status != 200:
                out["error"] = "计费接口返回 HTTP %s" % resp.status
                return out
            body = resp.read().decode("utf-8", "replace")
        env = json.loads(body)
        if env.get("code") != 0:
            out["error"] = env.get("msg") or env.get("message") or "计费接口返回 code %s" % env.get("code")
            return out
        inner = (((env.get("data") or {}).get("Response") or {}).get("Data") or {})
        accounts = inner.get("Accounts") or []
        SOON_MS = 3 * 24 * 3600 * 1000
        now_ms = int(time.time() * 1000)
        for raw in accounts:
            if not isinstance(raw, dict):
                continue

            def num(key):
                v = raw.get(key)
                return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

            monthly = num("CapacityType") == 4
            size = num("CycleCapacitySize") if monthly else num("CapacitySize")
            remain = num("CycleCapacityRemain") if monthly else num("CapacityRemain")
            remain = max(remain, 0)
            cycle_end = _parse_ts(raw.get("CycleEndTime"))
            # 月度包按周期刷新时间展示；一次性包按到期时间
            expires_ms = cycle_end if monthly else (_parse_ts(raw.get("ExpiredTime")) or cycle_end)
            refresh_ms = (cycle_end + 1000) if monthly else None
            pkg = {
                "name": raw.get("PackageName") or raw.get("SubProductName") or "积分包",
                "monthly": monthly,
                "size": size,
                "remain": remain,
                "used": max(size - remain, 0),
                "expires_at_ms": expires_ms,
                "refresh_at_ms": refresh_ms,
            }
            out["packages"].append(pkg)
            out["total_remain"] += remain
            out["total_size"] += size
            out["total_used"] += pkg["used"]
            if expires_ms and 0 < expires_ms - now_ms < SOON_MS:
                out["expiring_soon"].append(pkg)
    except InsufficientCreditError:
        raise
    except WorkBuddyError as e:
        out["error"] = str(e)
    except Exception as e:
        out["error"] = "积分查询失败：%s" % e
    return out


# ---------------------------------------------------------------------------
# 补全调用
# ---------------------------------------------------------------------------
def chat(messages, model=DEFAULT_MODEL, temperature=0.7, json_mode=False,
         max_tokens=None, timeout=120, credential=None, with_usage=False):
    """调用 WorkBuddy 网关，返回完整文本（上层拿到的是非流式语义）。

    网关只接受流式请求，因此内部走 SSE 并把增量拼回全文。
    json_mode=True 时下发 response_format=json_object（网关支持，实测有效）。
    with_usage=True 时返回 (文本, usage)——网关通常在最后一个 SSE 块里带 usage，
    拿不到就返回 None，不猜测、不估算。
    """
    cred = credential or load_credential()
    base = _gateway_for(cred["domain"])
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "stream": True,           # 网关强制要求
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    status, body = _request(base + CN_CHAT_PATH, payload=payload, cred=cred, timeout=timeout)
    if status != 200:
        _raise_for_error(status, body)
    text, usage = _join_sse(body, with_usage=True)
    return (text, usage) if with_usage else text


def _join_sse(sse_text, with_usage=False):
    """把 SSE 增量拼成完整文本（同时兜底处理非流式 JSON 响应）。

    with_usage=True 时返回 (text, usage|None)。usage 取自最后一个有效的 usage 块
    （流式响应里部分网关每块都带累计用量，取最后一个即总量）。
    """
    chunks = []
    saw_sse = False
    usage = None
    for line in sse_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        saw_sse = True
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if isinstance(obj.get("usage"), dict) and obj["usage"]:
            usage = obj["usage"]
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                chunks.append(piece)
    if saw_sse:
        text = "".join(chunks)
        return (text, usage) if with_usage else text
    # 兜底：万一网关改为返回标准 JSON
    try:
        data = json.loads(sse_text)
        text = data["choices"][0]["message"]["content"]
        return (text, data.get("usage")) if with_usage else text
    except Exception:
        return sse_text


def chat_stream(messages, model=DEFAULT_MODEL, temperature=0.7, json_mode=False,
                max_tokens=None, timeout=120, credential=None, on_delta=None):
    """流式调用：每收到一块增量就回调 on_delta(chunk)，返回 (全文, usage)。

    和 `chat()` 的区别只在"什么时候给"：chat() 会 `_request` 把整个响应体
    读完再拼（本质仍是等全部结束），这里逐行读 urllib 的响应流，
    所以文字是边生成边出来的。网关本来就强制流式，成本没有变化。
    """
    cred = credential or load_credential()
    base = _gateway_for(cred["domain"])
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "stream": True,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        base + CN_CHAT_PATH,
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(cred, stream=True),
        method="POST",
    )
    chunks, usage, saw_sse = [], None, False
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                _raise_for_error(resp.status, resp.read(4000).decode("utf-8", "replace"))
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                saw_sse = True
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if isinstance(obj.get("usage"), dict) and obj["usage"]:
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        chunks.append(piece)
                        if on_delta:
                            try:
                                on_delta(piece)
                            except Exception:
                                pass
    except urllib.error.HTTPError as e:
        _raise_for_error(e.code, e.read(4000).decode("utf-8", "replace"))

    if saw_sse:
        return "".join(chunks), usage
    # 兜底：网关若改成一次性 JSON 响应，chunks 会是空的，这里再判一次
    return "", usage


# ---------------------------------------------------------------------------
# 连通性测试（只探测，不跑业务）
# ---------------------------------------------------------------------------
def test_connection(model=None, timeout=60):
    """对指定模型发一个最小请求，判断该模型「此刻能不能用」。

    只做连通性判断，不返回业务内容；调用方据此告诉用户 API 可用与否。
    代价：**会真实消耗约 1 次调用的积分**（极小，`max_tokens=8`）。

    返回 dict：{"ok", "model", "latency_ms", "reply", "error", "kind"}
    kind: not-signed-in / out-of-credit / rate-limited / network / server / unknown
    """
    target = model or DEFAULT_MODEL
    t0 = time.time()
    try:
        text = chat([{"role": "user", "content": "ping，请只回复 pong"}],
                    model=target, max_tokens=8, timeout=timeout)
    except InsufficientCreditError as e:
        return {"ok": False, "model": target, "latency_ms": int((time.time() - t0) * 1000),
                "reply": None, "error": str(e), "kind": "out-of-credit"}
    except WorkBuddyNotSignedIn as e:
        return {"ok": False, "model": target, "latency_ms": int((time.time() - t0) * 1000),
                "reply": None, "error": str(e), "kind": "not-signed-in"}
    except WorkBuddyError as e:
        msg = str(e)
        kind = "unknown"
        if "429" in msg or "rate" in msg.lower() or "limit" in msg.lower():
            kind = "rate-limited"
        elif "无法连接" in msg:
            kind = "network"
        elif "HTTP 5" in msg:
            kind = "server"
        return {"ok": False, "model": target, "latency_ms": int((time.time() - t0) * 1000),
                "reply": None, "error": msg, "kind": kind}
    except Exception as e:
        return {"ok": False, "model": target, "latency_ms": int((time.time() - t0) * 1000),
                "reply": None, "error": "%s" % e, "kind": "unknown"}

    return {"ok": True, "model": target, "latency_ms": int((time.time() - t0) * 1000),
            "reply": (text or "").strip()[:60], "error": None, "kind": None}
