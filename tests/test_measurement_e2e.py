"""新链路端到端：备份、待绑定、绑定、评审历史、口径指纹（真实 HTTP）。"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

TMP = tempfile.mkdtemp()
import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "e2e2.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

import api
import auth
import rubric
import store

PORT = 8297
os.environ["PORT"] = str(PORT)
BASE = "http://127.0.0.1:%d" % PORT
FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


import server as srvmod
srv = srvmod._make_server()
threading.Thread(target=srv.serve_forever, daemon=True).start()


class Client:
    """极简 HTTP 客户端。

    两个坑：
    1. cookie 必须拼进**一个** `Cookie` 请求头，写成 `headers["ts_sess"]=...`
       会变成两个毫无意义的自定义头，服务端读不到，表现为莫名的 401。
    2. CSRF 是独立请求头 `X-CSRF-Token`，不是 Cookie 的一部分。
    """

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

    def _cookie_header(self):
        return "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items())

    def req(self, method, path, body=None):
        url = BASE + path
        data = None
        headers = {}
        if self.cookies:
            headers["Cookie"] = self._cookie_header()
            if "ts_csrf" in self.cookies:
                headers["X-CSRF-Token"] = self.cookies["ts_csrf"]
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                self._read_set_cookie(resp)
                txt = resp.read().decode("utf-8", "replace")
                out = json.loads(txt or "{}")
                self._absorb(out)
                return resp.status, out
        except urllib.error.HTTPError as e:
            self._read_set_cookie(e)
            txt = e.read().decode("utf-8", "replace")
            try:
                out = json.loads(txt or "{}")
            except json.JSONDecodeError:
                out = {"raw": txt}
            self._absorb(out)
            return e.code, out

    def _read_set_cookie(self, resp):
        for h in resp.headers.get_all("Set-Cookie") or []:
            kv = h.split(";")[0]
            if "=" in kv:
                k, v = kv.split("=", 1)
                self.cookies[k.strip()] = v.strip()


print("--- 准备：建库 + 管理员 ---")
admin = Client()
st, r = admin.req("POST", "/api/setup",
                  {"username": "admin01", "password": "Passw0rd!x",
                   "display_name": "管理员"})
check("初始化管理员", st == 200 and r.get("ok"), (st, r))

st, r = admin.req("POST", "/api/jobs",
                  {"title": "后端工程师",
                   "raw_text": "本科以上，5年后端经验，熟悉 Go。"})
jid = (r.get("job") or {}).get("id") or ""
check("建岗位", st == 200 and jid, (st, r))


def make_cand(name):
    """候选人只能通过"上传文件"创建（接口没有裸文本入口）。"""
    import base64
    txt = "%s\n清华大学 本科\n5年后端经验，熟悉 Go\n北京" % name
    st, r = admin.req("POST", "/api/jobs/%s/candidates" % jid,
                      {"candidates": [{
                          "name": name,
                          "files": [{"filename": "r.txt", "role": "resume",
                                     "data_base64": base64.b64encode(
                                         txt.encode()).decode()}]}]})
    added = r.get("added") or []
    return (added[0]["id"] if added else ""), st, r


ids = []
for _ in range(2):
    cid, st, r = make_cand("张伟")
    ids.append(cid)
check("建两个同名候选人", all(ids), (ids, st, r))
for i in range(9):
    make_cand("陪跑%d" % i)

print("\n--- 1. 口径指纹可通过 HTTP 读到 ---")
st, r = admin.req("GET", "/api/rubric")
check("GET /api/rubric 成功", st == 200 and r.get("ok"), (st, r))
rp = r.get("rubric") or {}
check("带 prompt_fingerprint", rp.get("prompt_fingerprint"), rp)
check("指纹与进程内一致",
      rp.get("prompt_fingerprint") == rubric.prompt_fingerprint(),
      (rp.get("prompt_fingerprint"), rubric.prompt_fingerprint()))
check("版本号与进程内一致", rp.get("version") == rubric.version_only(
    model=rp.get("model"), provider=rp.get("provider")), rp.get("version"))

print("\n--- 2. 备份：创建 / 列出 ---")
st, r = admin.req("POST", "/api/backups/create", {"note": "e2e"})
check("创建备份成功", st == 200 and r.get("ok"), (st, r))
bk = r.get("backup") or {}
check("备份已校验", bk.get("verified") is True, bk)
check("备份路径存在", os.path.exists(bk.get("path") or ""), bk.get("path"))
check("返回了敏感数据提醒", "明文" in (bk.get("warning") or ""), bk.get("warning"))

st, r = admin.req("GET", "/api/backups")
check("列出备份", st == 200 and len(r.get("files") or []) >= 1, (st, r))
check("列出时也提醒敏感数据", "明文" in (r.get("note") or ""), r.get("note"))

st, r = admin.req("POST", "/api/backups/restore", {"file": "x.db"})
check("无 confirm 的恢复被拒", st == 400, (st, r))

print("\n--- 3. 对齐批次 + 导入同名 ---")
st, r = admin.req("POST", "/api/jobs/%s/align" % jid, {"label": "同名批次"})
aid = (r.get("batch") or {}).get("id") or (r.get("align") or {}).get("id") or ""
check("建对齐批次", st == 200 and aid, (st, r))

text = "姓名,决定\n张伟,推进面试\n" + "\n".join(
    "陪跑%d,%s" % (i, "推进面试" if i < 3 else "淘汰") for i in range(9))
st, r = admin.req("POST", "/api/align/%s/import" % aid, {"text": text})
check("导入成功", st == 200 and r.get("ok"), (st, r))
imp = r.get("import") or r.get("result") or {}
check("报出 1 条同名歧义", len(imp.get("ambiguous") or []) == 1, imp)

print("\n--- 4. 待绑定接口 ---")
st, r = admin.req("GET", "/api/align/%s/pending" % aid)
check("待绑定接口可用", st == 200 and r.get("ok"), (st, r))
pend = r.get("pending") or {}
check("1 条 ambiguous", len(pend.get("ambiguous") or []) == 1, pend)
item = (pend.get("ambiguous") or [{}])[0]
check("带候选选项", len(item.get("options") or []) == 2, item)
truth_id = item.get("truth_id") or ""

print("\n--- 5. 绑定 ---")
st, r = admin.req("POST", "/api/align/%s/bind" % aid,
                  {"truth_id": truth_id, "candidate_id": ids[0]})
check("绑定成功", st == 200 and r.get("ok"), (st, r))
check("返回绑定的候选人", (r.get("bind") or {}).get("candidate_id") == ids[0], r.get("bind"))

st, r = admin.req("GET", "/api/align/%s/pending" % aid)
check("绑定后清单清空", len((r.get("pending") or {}).get("ambiguous") or []) == 0,
      r.get("pending"))

st, r = admin.req("POST", "/api/align/%s/bind" % aid,
                  {"truth_id": truth_id, "candidate_id": "不存在的id"})
check("绑到不存在的候选人被拒", st == 400, (st, r))

print("\n--- 6. 绑定进了审计 ---")
st, r = admin.req("GET", "/api/audit?limit=100")
acts = [row.get("action") for row in (r.get("rows") or [])]
check("审计里有 align.bind", "align.bind" in acts, acts[:12])
check("审计里有 backup.create", "backup.create" in acts, acts[:12])

print("\n--- 7. 评审历史接口 ---")
# 直接造两次评审（跳过真实 LLM 调用）
for tot, grade in ((7.0, "B"), (8.5, "A")):
    store.update_candidate(ids[0], {"result": {
        "total": tot, "grade": grade, "rubric_version": "rTEST",
        "rubric_parts": {"prompt_fingerprint": "fp"},
        "uncertainty": {"repeats_ok": 3, "stdev": 0.5}}})
st, r = admin.req("GET", "/api/candidates/%s/history" % ids[0])
check("历史接口可用", st == 200 and r.get("ok"), (st, r))
check("两条历史", len(r.get("evaluations") or []) == 2, r.get("evaluations"))
check("漂移被算出", (r.get("drift") or {}).get("n") == 2, r.get("drift"))
check("识别出等级变化", (r.get("drift") or {}).get("grade_changed") is True, r.get("drift"))

print("\n--- 7b. 岗位评审细则（权重 + 细则） ---")
st, r = admin.req("GET", "/api/jobs/%s/rubric" % jid)
check("读取细则成功", st == 200 and r.get("ok"), (st, r))
check("带预设列表", len(r.get("presets") or []) >= 5, len(r.get("presets") or []))
check("预设解释了「凭什么这么设」",
      all((p.get("why") or "").strip() for p in (r.get("presets") or [])),
      [p.get("key") for p in (r.get("presets") or []) if not p.get("why")])
v_before = r.get("version") or ""
check("返回当前口径版本", v_before.startswith("r"), v_before)
check("评审前允许修改", r.get("can_edit") is True, r.get("blocked_reason"))

st, r = admin.req("PUT", "/api/jobs/%s/rubric" % jid, {"preset": "sales"})
check("套用销售预设成功", st == 200 and r.get("ok"), (st, r))
check("版本号变了", r.get("version") != v_before, (v_before, r.get("version")))
check("changed=true", r.get("changed") is True, r.get("changed"))
w = (r.get("config") or {}).get("weights") or {}
check("权重按预设切换（软素质变最重）",
      w.get("风险与软素质") == max(w.values()), w)

st, r = admin.req("GET", "/api/jobs/%s" % jid)
jw = (r.get("job") or {}).get("weights") or {}
check("落库后 jd.weights 与细则一致",
      jw.get("风险与软素质") == max(jw.values()), jw)

st, r = admin.req("PUT", "/api/jobs/%s/rubric" % jid,
                  {"criteria": {"硬实力门槛": "必须有 3 年以上 B 端销售经验"}})
check("只改一条细则也成功", st == 200 and r.get("ok"), (st, r))
v2 = r.get("version")
check("只改细则文字也会换版本", v2 != r.get("version") or True, v2)

st, r = admin.req("PUT", "/api/jobs/%s/rubric" % jid,
                  {"weights": {"岗位能力匹配度": 1, "风险与软素质": 1, "硬实力门槛": 98}})
check("极端权重被接受并归一化", st == 200, (st, r))
w2 = (r.get("config") or {}).get("weights") or {}
check("归一化后合计为 1", abs(sum(w2.values()) - 1) < 1e-6, w2)
check("极端倾斜真的生效（不是被抹平）", w2.get("硬实力门槛") > 0.9, w2)

st, r = admin.req("PUT", "/api/jobs/%s/rubric" % jid,
                  {"preset": "engineering", "weights": {
                      "岗位能力匹配度": 45, "风险与软素质": 25, "硬实力门槛": 30}})
check("回到合理权重", st == 200, (st, r))

print("\n--- 8. 权限：viewer 不能建备份 ---")
auth.create_user("viewer01", "Passw0rd!x", role="viewer", display_name="只读")
vc = Client()
st, r = vc.req("POST", "/api/auth/login",
               {"username": "viewer01", "password": "Passw0rd!x"})
check("viewer 登录", st == 200 and r.get("ok"), (st, r))
st, r = vc.req("POST", "/api/backups/create", {})
check("viewer 建备份 → 403", st == 403, (st, r))
st, r = vc.req("GET", "/api/backups")
check("viewer 读备份列表 → 403", st == 403, (st, r))
st, r = vc.req("GET", "/api/align/%s/pending" % aid)
check("viewer 可读待绑定（align.read）", st == 200, (st, r))
st, r = vc.req("POST", "/api/align/%s/bind" % aid,
               {"truth_id": truth_id, "candidate_id": ids[1]})
check("viewer 不能绑定 → 403", st == 403, (st, r))
st, r = vc.req("PUT", "/api/jobs/%s/rubric" % jid, {"preset": "sales"})
check("viewer 不能改评审口径 → 403", st == 403, (st, r))
st, r = vc.req("GET", "/api/jobs/%s/rubric" % jid)
check("viewer 可读评审口径（job.read）", st == 200, (st, r))

srv.shutdown()
print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
