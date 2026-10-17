"""
HTTP 基础设施：请求解析、响应输出、Cookie、CSRF、鉴权闸门。

为什么要把这部分从 server.py 拆出来
----------------------------------
原来的 `server.py` 一个文件 806 行，把路由、静态文件、业务逻辑、SSE 全揉在
`Handler` 的一个类里。具体后果：

  · 想给某个接口加鉴权，得在 do_GET/do_POST 的一堆 if 里找位置插；
  · 想给写操作加 CSRF 校验，没有钩子可挂；
  · 任何一个业务函数改动都要碰这个巨型类，冲突率高、review 困难。

这里只负责"把 HTTP 翻译成 Python 值"和"把 Python 值翻译成 HTTP"，
不含任何业务判断。业务在 api.py，流程在 jobs_runner.py。
"""
import base64
import json
import os
import posixpath
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler

import auth

MAX_BODY = 64 * 1024 * 1024      # 单个请求体上限 64MB（一次传 20 份 PDF 够用）
MAX_JSON = 8 * 1024 * 1024


class ApiError(Exception):
    """业务层主动抛出的错误（带 HTTP 状态码）。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class HttpBase(BaseHTTPRequestHandler):
    """请求/响应基础设施。

    子类需实现 `route(method, path, query, body)`。
    """

    server_version = "talent-screen"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ---- 解析 ----------------------------------------------------------
    def _split(self):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        return path, query

    def _cookies(self):
        raw = self.headers.get("Cookie") or ""
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return {}
        return {k: m.value for k, m in jar.items()}

    def _read_body(self):
        """读请求体并尽量解析为 dict。

        Content-Length 缺失或超限都拒绝——否则一个畸形请求能把内存吃光。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("请求体过大（上限 %d MB）" % (MAX_BODY // 1024 // 1024), 413)
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" in ctype or not ctype:
            try:
                obj = json.loads(raw.decode("utf-8"))
                return obj if isinstance(obj, dict) else {"_raw": obj}
            except Exception:
                raise ApiError("请求体不是合法 JSON", 400)
        if "form-urlencoded" in ctype:
            return {k: v[0] for k, v in urllib.parse.parse_qs(
                raw.decode("utf-8", "replace")).items()}
        # multipart 不支持：前端统一用 base64 JSON 上传（零依赖）
        raise ApiError("不支持的 Content-Type：%s（请用 application/json 传 base64）" % ctype,
                       415)

    # ---- 会话 ----------------------------------------------------------
    def current_user(self):
        """从 Cookie 解析当前用户（无则 None）。"""
        token = self._cookies().get(auth.SESSION_COOKIE)
        if not token:
            return None
        try:
            return auth.resolve_session(token)
        except Exception:
            return None

    def _client_ip(self):
        return self.client_address[0] if self.client_address else ""

    def require(self, action):
        """校验登录 + 角色权限；写操作额外校验 CSRF。"""
        user = self.current_user()
        if not user:
            raise ApiError("未登录或会话已过期，请重新登录", 401)
        auth.require(user, action)
        if self.command in ("POST", "PUT", "PATCH", "DELETE"):
            sent = self.headers.get("X-CSRF-Token") or ""
            want = self._cookies().get(auth.CSRF_COOKIE) or ""
            if not want or not sent or not _consteq(sent, want):
                raise ApiError("CSRF 校验失败，请刷新页面后重试", 403)
        return user

    # ---- 响应 ----------------------------------------------------------
    def send_json(self, obj, status=200, cookies=None, headers=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for c in (cookies or []):
            self.send_header("Set-Cookie", c)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error_json(self, message, status=400, extra=None):
        payload = {"ok": False, "error": message}
        if extra:
            payload.update(extra)
        self.send_json(payload, status=status)

    # ---- 分发 ----------------------------------------------------------
    def _dispatch(self, method):
        path, query = self._split()
        try:
            body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
            self.route(method, path, query, body)
        except ApiError as e:
            self.send_error_json(e.message, e.status)
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except Exception as e:
            # 把"用户能自己修"的错误和"程序坏了"分开：
            # 凭证缺失/上游模型报错是前者，不该以 500 出现——那会让用户
            # 以为是系统 bug 而无从下手。
            mapped = _map_engine_error(e)
            if mapped:
                self.send_error_json(mapped[0], mapped[1])
                return
            import traceback
            traceback.print_exc()
            self.send_error_json("服务器内部错误：%s" % e, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        """预检。同源部署下用不到，但显式拒绝比 501 更清晰。"""
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, PUT, PATCH, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        """默认日志把每个静态文件都打一行，噪音太大。只记 API 与错误。"""
        try:
            if "/api/" in (self.path or ""):
                import sys
                sys.stderr.write("[http] %s %s %s\n" % (
                    self.command, self.path.split("?")[0],
                    (args[1] if len(args) > 1 else "-")))
        except Exception:
            pass


def _consteq(a, b):
    """恒定时间字符串比较（防时序侧信道）。"""
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _map_engine_error(e):
    """把引擎/LLM 层的异常映射成合适的 HTTP 语义。

    返回 (message, status) 或 None（None 表示"这是真 bug，按 500 报"）。
    """
    name = type(e).__name__
    msg = str(e)
    if name in ("LLMConfigError", "LLMError"):
        # 402 更贴近"额度/凭证问题"，前端据此直接弹"去配模型"而不是报错崩溃
        return (msg, 400)
    if name in ("RunBusy",):
        return (msg, 409)
    if name in ("KeyError",):
        return (msg or "对象不存在", 404)
    if name == "AuthError":
        # ⚠ 登录失败必须是 401，不能是 403。
        # 踩过的坑：这里原本和"无权访问"合并成 403，于是前端收到
        # "用户名或口令不正确" 的 403 后提示"你没有权限"，用户会以为账号被停用。
        # 更糟的是前端把 401 当作"会话过期"去跳登录页，403 却不跳，
        # 于是输错口令时页面毫无反应。
        if "口令" in msg or "用户" in msg or "登录" in msg or "次数" in msg:
            return (msg, 401)
        return (msg, 403)
    if "无权" in msg:
        return (msg, 403)
    if "额度" in msg or "积分" in msg or "credits" in msg.lower():
        return (msg, 402)
    return None


# ---------------------------------------------------------------------------
# 静态文件
# ---------------------------------------------------------------------------
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


def serve_static(handler, root, url_path, strip_prefix=""):
    """把 url_path 映射到 root 下的文件并输出。

    安全要点（这里踩过坑，写清楚）：
    1. `posixpath.normpath` 归一化后，**任何含 `..` 的路径直接拒绝**，
       不做"看归一化结果前缀是否在 root 内"这种判断。
       踩坑记录：早期版本 root 取的是 PROJECT_ROOT（项目根），于是
       `/viewer/../engine/server.py` 归一化后变成 `engine/server.py`，
       它确实"在 root 内"——校验通过，源码被原样吐了出来。
       engine/ 里有数据库、加密密钥、.env，这是真实的源码泄露。
       正确做法是把 root 锁死在 viewer/，并且拒绝一切 `..`。
    2. 只允许白名单后缀，避免把奇怪的二进制/备份文件吐出去。

    返回 None；出错时已写好响应。
    """
    rel = urllib.parse.unquote(url_path)
    if strip_prefix and rel.startswith(strip_prefix):
        rel = rel[len(strip_prefix):]
    rel = rel.lstrip("/")
    if not rel or rel.endswith("/"):
        rel += "index.html"

    # 先把 URL 里的反斜杠统一成 /（Windows 上 `..\` 同样是穿越）
    rel = rel.replace("\\", "/")
    if ".." in rel.split("/"):
        handler.send_error_json("非法的路径", 400)
        return
    rel = posixpath.normpath(rel)
    if rel.startswith("..") or rel.startswith("/") or os.path.isabs(rel):
        handler.send_error_json("非法的路径", 400)
        return

    root_abs = os.path.normpath(os.path.abspath(root))
    full = os.path.normpath(os.path.join(root_abs, rel.replace("/", os.sep)))
    # 双保险：归一化后必须仍在 root 之下
    if not (full == root_abs or full.startswith(root_abs + os.sep)):
        handler.send_error_json("非法的路径", 400)
        return
    if not os.path.isfile(full):
        handler.send_error_json("文件不存在：%s" % rel, 404)
        return

    ext = os.path.splitext(full)[1].lower()
    if ext not in _MIME:
        handler.send_error_json("不支持的文件类型：%s" % ext, 404)
        return
    ctype = _MIME[ext]
    try:
        with open(full, "rb") as f:
            data = f.read()
    except OSError as e:
        handler.send_error_json("读取文件失败：%s" % e, 500)
        return

    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    # 前端资源不缓存，避免改了代码刷新还是旧的（本地工具，流量不值钱）
    handler.send_header("Cache-Control", "no-cache, must-revalidate")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def b64decode_strict(s):
    """严格 base64 解码（允许 data: URL 前缀）。"""
    if not s:
        return b""
    if isinstance(s, str) and s.startswith("data:"):
        comma = s.find(",")
        if comma > 0:
            s = s[comma + 1:]
    try:
        return base64.b64decode(s, validate=False)
    except Exception as e:
        raise ApiError("文件数据不是合法 base64：%s" % e, 400)
