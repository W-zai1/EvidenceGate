"""批量身份确认：部分成功照实返回、超限拒绝、每条都进审计。

批量合并身份是**不可逆**的（confirm_identity 会改写 person_id 并合并档案），
所以这一组测试盯的是"出错时的行为"，而不只是"成功时能用"。
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp()
from engine.storage import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "batch.db")
db.reset_init_flag()
from engine.intake import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

from engine.api import api
from   engine.storage import store

PORT = 8299
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


admin = Client()
st, r = admin.req("POST", "/api/setup",
                  {"username": "admin01", "password": "Passw0rd!x",
                   "display_name": "管理员"})
check("初始化管理员", st == 200 and r.get("ok"), (st, r))
st, r = admin.req("POST", "/api/jobs", {"title": "身份岗", "raw_text": "本科"})
jid = (r.get("job") or {}).get("id")
check("建岗位", bool(jid), (st, r))

# 造一批候选人。同名同校会让 identity 判定给出"疑似同一人"待确认状态
ids = []
for i, name in enumerate(["张三", "张三", "李四", "王五"]):
    c = store.add_candidate(jid, name,
                            resume_text="清华大学 本科\n字节跳动\n北京",
                            actor="seed")
    ids.append(c["id"])
check("4 位候选人已建", len(ids) == 4, len(ids))

print("\n--- 1. 参数校验 ---")
st, r = admin.req("POST", "/api/identity/batch-confirm", {})
check("缺 items → 400", st == 400, (st, r))
st, r = admin.req("POST", "/api/identity/batch-confirm", {"items": []})
check("空列表 → 400", st == 400, (st, r))
too_many = [{"cid": ids[0], "is_same": True}] * (api.IDENTITY_BATCH_MAX + 1)
st, r = admin.req("POST", "/api/identity/batch-confirm", {"items": too_many})
check("超过上限 → 400（不静默截断）", st == 400, (st, r))
check("错误里说明了上限", str(api.IDENTITY_BATCH_MAX) in (r.get("error") or ""),
      r.get("error"))

print("\n--- 2. 部分成功照实返回 ---")
items = [
    {"cid": ids[0], "is_same": False},          # 合法
    {"cid": ids[1], "is_same": False},          # 合法
    {"cid": "不存在的id", "is_same": True},      # 会失败
    {"cid": ids[2]},                            # 缺 is_same
    {"is_same": True},                          # 缺 cid
]
st, r = admin.req("POST", "/api/identity/batch-confirm", {"items": items})
check("整体不因部分失败而 500", st == 200, (st, r))
check("成功 2 条", r.get("succeeded") == 2, r.get("succeeded"))
check("失败 3 条", r.get("failed") == 3, r.get("failed"))
res = r.get("results") or []
check("逐条都有结果", len(res) == 5, len(res))
check("失败项带原因", all(("error" in x) for x in res if not x["ok"]),
      [x for x in res if not x["ok"]])
check("坏 cid 的失败原因可读",
      any("不存在" in (x.get("error") or "") for x in res if not x["ok"]),
      [x.get("error") for x in res if not x["ok"]])

print("\n--- 3. 每条都进审计 ---")
conn = db.connect()
rows = conn.execute(
    "SELECT action, target_id, actor, detail_json FROM audit"
    " WHERE action LIKE 'candidate.identity%' ORDER BY rowid DESC LIMIT 20").fetchall()
targets = [x["target_id"] for x in rows]
check("审计里有身份操作记录", len(rows) >= 2, len(rows))
check("成功的那两条都有审计", ids[0] in targets and ids[1] in targets, targets)
check("审计带 actor", all(x["actor"] for x in rows), [x["actor"] for x in rows][:3])

print("\n--- 4. 无权限角色被拒 ---")
from engine.api import auth
auth.create_user("viewer01", "Passw0rd!x", role="viewer", display_name="只读")
vc = Client()
st, r = vc.req("POST", "/api/auth/login",
               {"username": "viewer01", "password": "Passw0rd!x"})
check("viewer 登录", st == 200 and r.get("ok"), (st, r))
st, r = vc.req("POST", "/api/identity/batch-confirm",
               {"items": [{"cid": ids[3], "is_same": False}]})
check("viewer 不能批量确认 → 403", st == 403, (st, r))

print("\n--- 5. 单条接口仍然可用（没有被批量替换掉） ---")
st, r = admin.req("POST", "/api/candidates/%s/identity" % ids[3],
                  {"is_same": False})
check("单条确认可用", st == 200 and r.get("ok"), (st, r))

srv.shutdown()
print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
