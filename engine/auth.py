"""
本地鉴权 —— 账号、口令、会话、角色、CSRF。

为什么原本"没有鉴权"是个问题
----------------------------
原实现只监听 127.0.0.1，靠"本机"当唯一防线。但这在真实场景站不住：
  · 为了团队共用把监听改成 0.0.0.0（几乎是必然下一步）；
  · 本机有多个用户账户 / 远程桌面 / 共享电脑；
  · 任何人都能 `curl http://127.0.0.1:8123/api/jobs` 把全部简历拖走。

所以现在有账号体系，且**首次启动强制设置管理员口令**——
不给"默认密码 admin/admin"这种等于没有的门。

口令存储
--------
PBKDF2-HMAC-SHA256 + 每用户随机盐 + 240000 轮（OWASP 2023 建议量级）。
标准库 `hashlib.pbkdf2_hmac`，零依赖。
校验用 `hmac.compare_digest`，避免时序侧信道。

会话
----
随机 32 字节令牌，库里只存 `sha256(token)`——库泄露也拿不到可用会话。
Cookie 为 HttpOnly + SameSite=Lax。
写操作（POST/PUT/DELETE）额外要求 `X-CSRF-Token`，与 Cookie 里的值比对，
防跨站伪造请求。

角色
----
  admin —— 用户管理、密钥、导出/删除 PII、标定注入
  hr    —— 岗位/候选人/评审/人才库/结果回填
  viewer —— 只读（用人经理查看结论）
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

import audit
import crypto
import db

ROLES = ("admin", "hr", "viewer")
ROLE_LABEL = {"admin": "管理员", "hr": "招聘", "viewer": "只读"}

PBKDF2_ITERATIONS = 240000
SESSION_COOKIE = "ts_session"
CSRF_COOKIE = "ts_csrf"
SESSION_HOURS = 12

# 登录失败限流：同一用户名连续失败到阈值后锁定一段时间
MAX_FAILS = 8
LOCK_MINUTES = 15

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")


class AuthError(RuntimeError):
    """认证/授权失败。"""


class NotInitialized(AuthError):
    """还没有任何用户——需要先设置管理员口令。"""


# ---------------------------------------------------------------------------
# 口令
# ---------------------------------------------------------------------------
def _hash_password(password, salt_hex, iterations=PBKDF2_ITERATIONS):
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt_hex), iterations)
    return dk.hex()


def hash_password(password, iterations=PBKDF2_ITERATIONS):
    if not password or len(password) < 8:
        raise AuthError("口令至少 8 位")
    salt = secrets.token_hex(16)
    return _hash_password(password, salt, iterations), salt, iterations


def check_password(password, pw_hash, salt, iterations):
    if not password or not pw_hash or not salt:
        return False
    try:
        it = int(iterations or PBKDF2_ITERATIONS)
    except (TypeError, ValueError):
        it = PBKDF2_ITERATIONS
    calc = _hash_password(password, salt, it)
    return hmac.compare_digest(calc, pw_hash)


def password_strength(password):
    """给界面用的强度提示（不阻断，只提示）。"""
    p = password or ""
    score = 0
    if len(p) >= 8:
        score += 1
    if len(p) >= 12:
        score += 1
    if re.search(r"[a-z]", p) and re.search(r"[A-Z]", p):
        score += 1
    if re.search(r"\d", p):
        score += 1
    if re.search(r"[^\w]", p):
        score += 1
    label = ["很弱", "弱", "一般", "较强", "强", "很强"][min(score, 5)]
    return {"score": score, "label": label, "ok": len(p) >= 8}


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------
def count_users(conn=None):
    conn = conn or db.connect()
    return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def needs_setup():
    """是否还没有任何管理员（首次启动引导）。"""
    db.init()
    return count_users() == 0


def create_user(username, password, display_name="", role="hr", actor="system"):
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise AuthError("用户名只能包含字母、数字、下划线、点、横线，3~32 位")
    if role not in ROLES:
        raise AuthError("角色不合法：%s" % role)
    pw_hash, salt, iters = hash_password(password)

    conn = db.connect()
    now = db.now_iso()
    uid = "u_" + secrets.token_hex(8)
    with db.tx(conn) as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise AuthError("用户名已存在：%s" % username)
        c.execute(
            "INSERT INTO users(id, username, display_name, role, pw_hash, salt, iterations,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, username, (display_name or username).strip(), role, pw_hash, salt, iters, now),
        )
        audit.record(c, "user.create", actor=actor, target_type="user", target_id=uid,
                     detail={"username": username, "role": role})
    return get_user(uid)


def get_user(uid=None, username=None):
    conn = db.connect()
    if uid:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return _public(row) if row else None


def list_users():
    conn = db.connect()
    return [_public(r) for r in
            conn.execute("SELECT * FROM users ORDER BY created_at ASC")]


def _public(row):
    if not row:
        return None
    d = dict(row)
    d.pop("pw_hash", None)
    d.pop("salt", None)
    d.pop("iterations", None)
    d["role_label"] = ROLE_LABEL.get(d.get("role"), d.get("role"))
    return d


def set_password(uid, new_password, actor="system"):
    pw_hash, salt, iters = hash_password(new_password)
    conn = db.connect()
    with db.tx(conn) as c:
        c.execute("UPDATE users SET pw_hash=?, salt=?, iterations=? WHERE id=?",
                  (pw_hash, salt, iters, uid))
        # 改密后踢掉所有旧会话
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit.record(c, "user.password", actor=actor, target_type="user", target_id=uid)
    return True


def set_disabled(uid, disabled, actor="system"):
    conn = db.connect()
    with db.tx(conn) as c:
        c.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, uid))
        if disabled:
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit.record(c, "user.disable", actor=actor, target_type="user", target_id=uid,
                     detail={"disabled": bool(disabled)})
    return True


# ---------------------------------------------------------------------------
# 登录 / 会话
# ---------------------------------------------------------------------------
_fail_lock = {}
_fail_state = {}


def _throttle_key(username, ip):
    return "%s|%s" % ((username or "").strip().casefold(), ip or "")


def verify_password(uid, password):
    """校验某用户的当前口令（改密前验证身份用）。

    不返回 pw_hash/salt——那属于内部凭证，任何 API 层都不该拿得到。
    """
    conn = db.connect()
    row = conn.execute("SELECT pw_hash, salt, iterations, disabled FROM users WHERE id=?",
                       (uid,)).fetchone()
    if not row or row["disabled"]:
        return False
    return check_password(password or "", row["pw_hash"], row["salt"], row["iterations"])


def login(username, password, ip="", ua=""):
    """校验口令并建立会话。失败抛 AuthError（信息不区分"无此用户/密码错"）。"""
    username = (username or "").strip()
    key = _throttle_key(username, ip)
    st = _fail_state.get(key) or {"fails": 0, "until": 0}
    now_ts = datetime.now().timestamp()
    if st["until"] > now_ts:
        wait = int(st["until"] - now_ts)
        raise AuthError("尝试次数过多，请 %d 秒后再试" % wait)

    conn = db.connect()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    ok = bool(row) and not row["disabled"] and check_password(
        password, row["pw_hash"], row["salt"], row["iterations"]
    )
    if not ok:
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] >= MAX_FAILS:
            st["until"] = now_ts + LOCK_MINUTES * 60
            st["fails"] = 0
        _fail_state[key] = st
        with db.tx(conn) as c:
            audit.record(c, "auth.login_failed", actor=username or "?",
                         detail={"ip": ip, "ua": (ua or "")[:120]})
        raise AuthError("用户名或口令不正确")

    _fail_state.pop(key, None)
    token, csrf = _new_session(conn, row["id"], ip, ua)
    with db.tx(conn) as c:
        c.execute("UPDATE users SET last_login=? WHERE id=?", (db.now_iso(), row["id"]))
        audit.record(c, "auth.login", actor=row["id"],
                     detail={"ip": ip, "username": username})
    return {"user": _public(row), "token": token, "csrf": csrf}


def _token_hash(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _new_session(conn, user_id, ip="", ua=""):
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = datetime.now()
    expires = now + timedelta(hours=SESSION_HOURS)
    with db.tx(conn) as c:
        c.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, ip, ua)"
            " VALUES (?,?,?,?,?,?)",
            (_token_hash(token), user_id, now.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds"), ip or "", (ua or "")[:200]),
        )
        # 顺手清掉过期会话
        c.execute("DELETE FROM sessions WHERE expires_at < ?",
                  (now.isoformat(timespec="seconds"),))
    return token, csrf


def resolve_session(token):
    """令牌 → 用户。过期/不存在返回 None。"""
    if not token:
        return None
    conn = db.connect()
    row = conn.execute(
        "SELECT s.*, u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ?", (_token_hash(token),)
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < db.now_iso():
        with db.tx(conn) as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
        return None
    if row["disabled"]:
        return None
    d = _public(row)
    d["session_created"] = row["created_at"]
    d["session_expires"] = row["expires_at"]
    return d


def logout(token):
    if not token:
        return False
    conn = db.connect()
    with db.tx(conn) as c:
        cur = c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
        audit.record(c, "auth.logout", actor="session")
    return cur.rowcount > 0


def cleanup_sessions():
    conn = db.connect()
    with db.tx(conn) as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now_iso(),))


# ---------------------------------------------------------------------------
# 授权
# ---------------------------------------------------------------------------
# 角色 → 允许的动作前缀/精确动作
_PERMISSIONS = {
    "admin": {"*"},
    "hr": {
        "job.read", "job.write", "candidate.read", "candidate.write",
        "run.read", "run.write", "talent.read", "talent.write",
        "outcome.read", "outcome.write", "usage.read", "audit.read",
        "calibration.read", "calibration.write", "settings.write",
        # 回测对齐：HR 要能导入真实决定、并做出"推/拒"的决策记录。
        # 这本身就是 HR 的职责，不该只有 admin 能做——否则实际操作里
        # 就会绕开系统用 Excel，反而丢了留痕。
        "align.read", "align.write",
    },
    "viewer": {
        "job.read", "candidate.read", "run.read", "talent.read",
        "outcome.read", "usage.read", "align.read",
    },
}


def can(user, action):
    """判断角色是否能执行某动作。admin 恒真。"""
    role = (user or {}).get("role") or "viewer"
    perms = _PERMISSIONS.get(role, set())
    return "*" in perms or action in perms


def require(user, action):
    if not can(user, action):
        # 复用 http_base 的状态码语义：已登录但没权限是 403，不是 401。
        # 直接抛 AuthError(401) 会让前端误判成"掉登录"而跳转登录页，
        # 用户就会看到自己明明登录着却被踢回登录框。
        from http_base import ApiError
        raise ApiError("当前角色（%s）无权执行该操作：%s"
                       % (ROLE_LABEL.get((user or {}).get("role"), "?"), action), 403)
    return True


# ---------------------------------------------------------------------------
# 模型配置（按用户隔离，密钥加密落库）
# ---------------------------------------------------------------------------
def get_settings(user_id):
    conn = db.connect()
    row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return {"provider": "", "model": "", "base_url": "", "has_key": False}
    key = ""
    if row["api_key_enc"]:
        try:
            key = crypto.decrypt_text(row["api_key_enc"])
        except crypto.PIICryptoError:
            key = ""
    return {
        "provider": row["provider"] or "",
        "model": row["model"] or "",
        "base_url": row["base_url"] or "",
        "has_key": bool(key),
        "api_key": key,          # 仅在服务端内部使用，API 返回前必须剥掉
        "updated_at": row["updated_at"],
    }


def public_settings(user_id):
    """给前端的版本：绝不回显密钥，只告诉"有没有配"。"""
    s = get_settings(user_id)
    s.pop("api_key", None)
    return s


def save_settings(user_id, provider=None, model=None, base_url=None, api_key=None,
                  actor="system"):
    conn = db.connect()
    cur = get_settings(user_id)
    new_key = cur.get("api_key", "")
    if api_key is not None:
        new_key = api_key            # 传空串 = 清除
    enc = crypto.encrypt_text(new_key) if new_key else ""
    now = db.now_iso()
    with db.tx(conn) as c:
        c.execute(
            "INSERT INTO user_settings(user_id, provider, model, base_url, api_key_enc, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET provider=excluded.provider,"
            " model=excluded.model, base_url=excluded.base_url,"
            " api_key_enc=excluded.api_key_enc, updated_at=excluded.updated_at",
            (user_id,
             provider if provider is not None else cur.get("provider", ""),
             model if model is not None else cur.get("model", ""),
             base_url if base_url is not None else cur.get("base_url", ""),
             enc, now),
        )
        audit.record(c, "model.config", actor=actor, target_type="user", target_id=user_id,
                     detail={"provider": provider, "model": model,
                             "key_set": bool(new_key)})
    return public_settings(user_id)
