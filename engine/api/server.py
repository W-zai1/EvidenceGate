"""
本地后端控制台（零依赖，仅标准库）。

职责边界（P2-12 拆分后）
------------------------
  http_base.py   ← 请求解析 / 响应 / Cookie / CSRF / 静态文件（不懂业务）
  api.py         ← 路由表 + 各接口实现（业务）
  jobs_runner.py ← 评审执行线程（长任务）
  sse.py         ← 事件流推送
  server.py      ← 本文件：只做启动、分发、SSE 挂载

为什么拆
--------
原先是 806 行单文件，路由是 `if path.startswith(...)` 的瀑布，
权限点藏在分支里，没法审计。现在每个接口在 `api.py` 注册时
就要显式声明所需权限，漏声明的接口在注册表里一眼可见。

安全基线（相对旧版的变化）
--------------------------
  · 移除 `Access-Control-Allow-Origin: *`：本地控制台不该对任意网页开放。
  · 所有 /api 写操作强制 CSRF token（双提交 cookie）。
  · 新增登录；未登录访问业务接口一律 401。
  · 静态文件路径做前缀校验，挡住 `../` 穿越。
"""
import os
import sys

# 直接 `python engine/api/server.py` 时，sys.path[0] 是 engine/api，
# 而下面的导入都是包路径（engine.api.xxx），找不到 engine 包。
# 这里把项目根补进去，让「直接运行」和「python -m engine.api.server」都能用。
_HERE = os.path.dirname(os.path.abspath(__file__))        # engine/api
_ROOT = os.path.dirname(os.path.dirname(_HERE))           # 项目根
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine.api import api
from   engine.api import auth
from   engine.storage import db
from   engine.api import http_base
from   engine.orchestrator import runs
from   engine.api import sse

ROOT = os.path.dirname(os.path.abspath(__file__))          # engine/api
PROJECT_ROOT = os.path.dirname(os.path.dirname(ROOT))     # 项目根（engine/api 上两级）
VIEWER_DIR = os.path.join(PROJECT_ROOT, "viewer")
PORT = int(os.getenv("PORT", "8123"))
PORT_FALLBACK = 6                              # 8123 忙则试 8124..8128
HOST = os.getenv("HOST", "127.0.0.1")     # 默认只监听本机


class Handler(http_base.HttpBase):
    """把请求分发到 api 路由，或者吐静态文件。"""

    def route(self, method, path, query, body):
        # ---- SSE：需要长连接，单独处理，不走普通 JSON 响应路径 ----
        if method == "GET" and path.startswith("/api/runs/") and path.endswith("/stream"):
            rid = path[len("/api/runs/"):-len("/stream")].strip("/")
            user = self.current_user()
            if not user:
                self.send_error_json("未登录或会话已过期", 401)
                return
            auth.require(user, "run.read")
            sse.stream(self, rid, int(query.get("after") or 0))
            return

        # ---- 静态资源 ----
        # 注意 root 必须是 VIEWER_DIR，不能是 PROJECT_ROOT。
        # 踩过的坑：曾经传 PROJECT_ROOT，于是 `/viewer/../engine/server.py`
        # 归一化成 `engine/server.py`，它"确实在 root 之下"，校验通过，
        # 整个 engine/ 目录（含数据库、密钥）可被匿名下载。
        # 现在 root 锁死在 viewer/，引擎目录从物理上不可能被这段代码命中。
        if not path.startswith("/api/"):
            if path in ("/", "/index.html", "/viewer", "/viewer/"):
                rel = "/index.html"
            elif path.startswith("/viewer/"):
                rel = path[len("/viewer"):]
            else:
                # 允许 /app.js 这类同目录直连（旧引用方式），但只认确实存在的文件
                rel = path
                if not os.path.isfile(os.path.join(VIEWER_DIR, path.lstrip("/"))):
                    rel = "/index.html"
            http_base.serve_static(self, VIEWER_DIR, rel)
            return

        # ---- 业务 API ----
        r, params = api.match(method, path)
        if not r:
            self.send_error_json("接口不存在：%s %s" % (method, path), 404)
            return

        user = self.current_user()
        if r["auth"]:
            if not user:
                self.send_error_json("未登录或会话已过期，请重新登录", 401)
                return
            auth.require(user, r["auth"])
            # 写操作强制 CSRF
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                sent = self.headers.get("X-CSRF-Token") or ""
                want = self._cookies().get(auth.CSRF_COOKIE) or ""
                if not want or not sent or not http_base._consteq(sent, want):
                    self.send_error_json("CSRF 校验失败，请刷新页面后重试", 403)
                    return

        ctx = {
            "method": method, "path": path, "query": query, "body": body,
            "params": params or {}, "user": user,
            "cookies": self._cookies(), "ip": self._client_ip(),
            "ua": self.headers.get("User-Agent") or "",
        }
        out = r["fn"](ctx)
        if out is None:
            return
        cookies = out.pop("cookies", None) if isinstance(out, dict) else None
        status = out.pop("_status", 200) if isinstance(out, dict) else 200
        self.send_json(out, status=status, cookies=cookies)


def _make_server():
    """绑定并返回服务器。端口回退时，实际端口看 srv.server_address[1]。

    只返回 server（不返回 (server, port) 元组），这样老的调用点
    `srv = _make_server()` 不用改；需要端口的地方从 server_address 取，
    服务器本来就知道自己绑在哪。
    """
    from http.server import ThreadingHTTPServer

    class Srv(ThreadingHTTPServer):
        # Windows 上 SO_REUSEADDR 近似 SO_REUSEPORT，会让两个进程绑同一端口，
        # 于是"端口已占用"永远探测不到。关掉它，让重复启动明确失败。
        allow_reuse_address = False
        daemon_threads = True

    # 端口被占用时依次往后试，而不是直接崩掉。
    # 常见场景：上一次的窗口没关干净，或者浏览器还连着旧进程。此时直接退出
    # 会表现为「双击 bat 什么都没发生」，用户完全不知道发生了什么。
    oserr = None
    tried = []
    for port in range(PORT, PORT + PORT_FALLBACK):
        tried.append(port)
        try:
            return Srv((HOST, port), Handler)
        except OSError as e:
            oserr = e
            continue
    raise OSError("端口 %s 都被占用（%s）" % (tried, oserr))


def _open_browser(url):
    """打开浏览器，失败时明确报错并把地址打出来。

    为什么不能只写 webbrowser.open(url)：它在 Windows 上可能「成功返回但什么
    都没打开」（没有默认浏览器、关联被改、在无 GUI 会话里）。此时用户只看到
    一个黑窗口不动，以为程序卡住了。所以这里检测返回值，并在失败时把 URL
    单独打成一行，让人能直接复制。
    """
    ok = False
    try:
        import webbrowser
        ok = bool(webbrowser.open(url))
    except Exception as e:
        print("[server] 自动打开浏览器失败：%s" % e)
    if not ok:
        print("[server] 没能自动打开浏览器。请手动复制下面的地址到浏览器：")
        print("         %s" % url)
    else:
        print("[server] 已在浏览器中打开：%s" % url)


def main():
    db.init(verbose=True)
    auth.cleanup_sessions()
    runs.reap_stale()

    # 启动时检查备份新鲜度。不是"每天定时备份"，而是"启动时发现太旧才补一份"——
    # 这个工具是本地单机工具，没有常驻守护进程，定时任务根本不会触发，
    # 只有在有人真的用它的时候才有机会备份。
    try:
        from engine.guardrails import backup
        bk = backup.auto_backup_if_stale(hours=backup.AUTO_HOURS)
        if bk:
            print(" 已自动备份（距上次超过 %d 小时）：%s"
                  % (backup.AUTO_HOURS, bk.get("path")))
    except Exception as e:
        # 备份失败不能阻止启动——否则一次磁盘满就会让人连工具都用不了。
        print(" ⚠ 自动备份失败（不影响启动）：%s" % e)

    try:
        srv = _make_server()
    except OSError as e:
        print("[server] 无法绑定 %s（起始端口 %d）—— %s" % (HOST, PORT, e))
        print("[server] 可能已有一个控制台在运行。关掉旧窗口，或换端口：")
        print("         set PORT=8300 && start.bat")
        sys.exit(1)

    port = srv.server_address[1]
    if port != PORT:
        print(" ⚠ 端口 %d 已被占用，改用 %d。" % (PORT, port))

    # url 必须在确定端口之后才拼，否则回退到别的端口时浏览器会打开错地址。
    url = "http://%s:%d/viewer/index.html" % (
        "localhost" if HOST in ("127.0.0.1", "0.0.0.0") else HOST, port)

    need = auth.needs_setup()
    print("=" * 66)
    print(" 本地控制台已启动 → %s" % url)
    if need:
        print(" ⚠ 首次启动：请在页面上创建管理员账号（系统当前无任何用户）")
    if HOST == "0.0.0.0":
        print(" ⚠ 正在监听所有网卡。本工具会处理真实简历（含身份证/手机号），")
        print("   非本机访问请务必先确认网络环境可信。")
    print(" 数据库：%s" % db.DB_PATH)
    print(" 按 Ctrl+C 停止。")
    print("=" * 66)

    if "--open" in sys.argv or os.getenv("OPEN_BROWSER") == "1":
        _open_browser(url)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 已停止。")
    finally:
        try:
            db.close_current_thread()
        except Exception:
            pass


if __name__ == "__main__":
    main()
