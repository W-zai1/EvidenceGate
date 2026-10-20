"""启动自检：四件事说得清、探测不偷偷花钱、匿名时不给细节。

现场最怕的不是功能不好用，而是"配好了但跑不起来"且不知道卡在哪。
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp()
from engine.storage import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "self.db")
db.reset_init_flag()
from engine.intake import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

from engine.api import api
from   engine.guardrails import backup
from   engine.api import provider_probe

PORT = 8298
os.environ["PORT"] = str(PORT)
BASE = "http://127.0.0.1:%d" % PORT
FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


from engine.api import server as srvmod
srv = srvmod._make_server()
threading.Thread(target=srv.serve_forever, daemon=True).start()


class Client:
    def __init__(self):
        self.cookies = {}

    def _absorb(self, payload):
        if not isinstance(payload, dict):
            return
        for c in payload.get("cookies") or []:
            kv = str(c).split(";")[0]
            if "=" in kv:
                k, v = kv.split("=", 1)
                self.cookies[k.strip()] = v.strip()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {}
        if self.cookies:
            headers["Cookie"] = "; ".join("%s=%s" % (k, v)
                                          for k, v in self.cookies.items())
            if "ts_csrf" in self.cookies:
                headers["X-CSRF-Token"] = self.cookies["ts_csrf"]
        if body is not None:
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(BASE + path, data=data, headers=headers,
                                   method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                for h in resp.headers.get_all("Set-Cookie") or []:
                    kv = h.split(";")[0]
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        self.cookies[k] = v
                txt = resp.read().decode("utf-8", "replace")
                out = json.loads(txt or "{}")
                self._absorb(out)
                return resp.status, out
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            try:
                out = json.loads(txt or "{}")
            except json.JSONDecodeError:
                out = {"raw": txt}
            self._absorb(out)
            return e.code, out


print("--- 1. 未登录也能看，但不给细节 ---")
anon = Client()
st, r = anon.req("GET", "/api/selfcheck")
check("匿名可访问", st == 200 and r.get("ok"), (st, r))
check("标了 anonymous", r.get("anonymous") is True, r.get("anonymous"))
check("库可写", (r.get("db") or {}).get("ok") is True, r.get("db"))
check("库路径只留文件名",
      "/" not in (r.get("db") or {}).get("path", "")
      and "\\" not in (r.get("db") or {}).get("path", ""),
      (r.get("db") or {}).get("path"))
check("匿名时 backup.dir 为空", (r.get("backup") or {}).get("dir") == "",
      (r.get("backup") or {}).get("dir"))
check("匿名时不给模型名", "model" not in (r.get("provider") or {}),
      r.get("provider"))
check("匿名时不暴露并发上限", r.get("concurrency") == {}, r.get("concurrency"))
check("带上了 schema 版本", r.get("schema_version"), r.get("schema_version"))

print("\n--- 2. 备份新鲜度说得清 ---")
bk = r.get("backup") or {}
check("有 count 字段", isinstance(bk.get("count"), int), bk)
check("过期标记是布尔", isinstance(bk.get("stale"), bool), bk)
check("全新库还没有备份 → 标记为过期", bk.get("stale") is True and bk.get("count") == 0,
      (bk.get("stale"), bk.get("count")))
check("带 WAL 手动拷贝警告", "-wal" in (bk.get("note") or ""), bk.get("note"))

# 建一份备份，看它是否立刻从"过期"变成"新鲜"
_bk = backup.create(keep=3, actor="selfcheck-test")
check("备份创建成功", _bk.get("ok") is True, _bk.get("error"))
st, rb = anon.req("GET", "/api/selfcheck")
bkb = rb.get("backup") or {}
check("建完备份后不过期", bkb.get("stale") is False, bkb)
check("计数变成 1", bkb.get("count") == 1, bkb.get("count"))
check("给了最新文件名", bool(bkb.get("latest")), bkb.get("latest"))
check("给了距今小时数", isinstance(bkb.get("age_hours"), (int, float)),
      bkb.get("age_hours"))

print("\n--- 3. 默认不探测（不偷偷花钱） ---")
check("默认 probed=false", r.get("probed") is False, r.get("probed"))
check("默认 connectivity 为空", r.get("connectivity") is None, r.get("connectivity"))
check("默认 credits 为空", r.get("credits") is None, r.get("credits"))

print("\n--- 4. probe=1 才真探测，并且结果被缓存 ---")
CALLS = {"n": 0}
orig_probe = provider_probe.test_connection


def fake_probe(*a, **kw):
    CALLS["n"] += 1
    return {"ok": True, "model": "m", "latency_ms": 12, "reply": "pong",
            "error": None, "kind": "ok"}


provider_probe.test_connection = fake_probe
try:
    st, r2 = anon.req("GET", "/api/selfcheck?probe=1")
    check("probe=1 时有探测结果", (r2.get("connectivity") or {}).get("ok") is True,
          r2.get("connectivity"))
    check("真的调用了一次探测", CALLS["n"] == 1, CALLS["n"])
    st, r3 = anon.req("GET", "/api/selfcheck?probe=1")
    check("第二次命中缓存，没有再打一次", CALLS["n"] == 1, CALLS["n"])
    check("返回剩余缓存时间", r3.get("connectivity_cached_for", 0) > 0,
          r3.get("connectivity_cached_for"))
finally:
    provider_probe.test_connection = orig_probe

print("\n--- 5. 登录后给全量细节 ---")
admin = Client()
st, r = admin.req("POST", "/api/setup",
                  {"username": "admin01", "password": "Passw0rd!x",
                   "display_name": "管理员"})
check("初始化管理员", st == 200 and r.get("ok"), (st, r))
st, r = admin.req("GET", "/api/selfcheck")
check("登录后可访问", st == 200, (st, r))
check("不再标 anonymous", not r.get("anonymous"), r.get("anonymous"))
check("给了完整库路径",
      os.path.sep in (r.get("db") or {}).get("path", ""),
      (r.get("db") or {}).get("path"))
check("给了备份目录", bool((r.get("backup") or {}).get("dir")),
      (r.get("backup") or {}).get("dir"))
check("给了 provider 结构", "provider" in (r.get("provider") or {}),
      r.get("provider"))
check("给了并发上限", bool(r.get("concurrency")), r.get("concurrency"))

print("\n--- 6. 探测失败也要如实报告（不假装成功） ---")
provider_probe.test_connection = lambda *a, **kw: {
    "ok": False, "model": "m", "latency_ms": 0, "reply": None,
    "error": "未填写 API Key", "kind": "auth"}
api._SELFCACHE.clear()
try:
    st, r4 = admin.req("GET", "/api/selfcheck?probe=1")
    cc = r4.get("connectivity") or {}
    check("失败时 ok=false", cc.get("ok") is False, cc)
    check("带着失败的 kind", cc.get("kind") == "auth", cc)
    check("带着可读的错误", "API Key" in (cc.get("error") or ""), cc)
finally:
    provider_probe.test_connection = orig_probe
    api._SELFCACHE.clear()

srv.shutdown()
print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
