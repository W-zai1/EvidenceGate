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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api
import auth
import db
import http_base
import runs
import sse

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
VIEWER_DIR = os.path.join(PROJECT_ROOT, "viewer")
PORT = int(os.getenv("PORT", "8123"))
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
    from http.server import ThreadingHTTPServer

    class Srv(ThreadingHTTPServer):
        # Windows 上 SO_REUSEADDR 近似 SO_REUSEPORT，会让两个进程绑同一端口，
        # 于是"端口已占用"永远探测不到。关掉它，让重复启动明确失败。
        allow_reuse_address = False
        daemon_threads = True

    return Srv((HOST, PORT), Handler)


def main():
    url = "http://%s:%d/viewer/index.html" % (
        "localhost" if HOST in ("127.0.0.1", "0.0.0.0") else HOST, PORT)

    db.init(verbose=True)
    auth.cleanup_sessions()
    runs.reap_stale()

    # 启动时检查备份新鲜度。不是"每天定时备份"，而是"启动时发现太旧才补一份"——
    # 这个工具是本地单机工具，没有常驻守护进程，定时任务根本不会触发，
    # 只有在有人真的用它的时候才有机会备份。
    try:
        import backup
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
        print("[server] 无法绑定 %s:%d —— %s" % (HOST, PORT, e))
        print("[server] 可能已有一个控制台在运行。换端口：set PORT=8124 && python server.py")
        sys.exit(1)

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
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            print("[server] 自动打开浏览器失败：%s（请手动访问 %s）" % (e, url))

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
