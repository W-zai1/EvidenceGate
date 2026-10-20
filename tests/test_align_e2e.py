"""对齐功能的端到端验证：真实 HTTP 栈，走一遍 HR 的实际用法。"""
import base64
import http.cookiejar
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
os.environ["PORT"] = "8293"
from engine.storage import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "e2e_align.db")
db.reset_init_flag()
from engine.intake import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
from engine.api import llm_client
import _llmstub

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


def fake_raw(messages, model, api_key, base_url, temperature, use_json, timeout):
    sysp = messages[0]["content"]
    if "评委" in sysp:
        return ('{"岗位能力匹配度":8.0,"风险与软素质":7.0,"硬实力门槛":9.0,'
                '"comment":"ok"}'), {"prompt_tokens": 100, "completion_tokens": 50,
                                     "total_tokens": 150}
    if "裁定裁判" in sysp:
        return ('{"pro_evidence":["8年后端"],"con_challenges":["无带团队"],'
                '"disputes":["是否真的带过团队"],"excluded":[]}'), \
               {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    if "JD" in sysp or "岗位描述" in sysp:
        return ('{"summary":"后端","requirements":["8年后端"],"preferred":[],'
                '"red_flags":[],"weights":{"岗位能力匹配度":0.4,'
                '"风险与软素质":0.3,"硬实力门槛":0.3}}'), \
               {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    return "发言" * 30, {"prompt_tokens": 100, "completion_tokens": 50,
                         "total_tokens": 150}


_llmstub.install(fake_raw)
from engine.api import server as srvmod
srv = srvmod._make_server()
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:8293"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def call(m, p, b=None, timeout=60):
    req = urllib.request.Request(
        BASE + p, data=json.dumps(b).encode() if b is not None else None, method=m)
    req.add_header("Content-Type", "application/json")
    c = [x.value for x in cj if x.name == "ts_csrf"]
    if c:
        req.add_header("X-CSRF-Token", c[0])
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


API = {"api": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-t"}}

call("POST", "/api/setup", {"username": "hr1", "password": "Str0ng-Pass!2026"})
call("POST", "/api/jobs", {"title": "高级后端", "raw_text": "8年以上后端"})
s, b = call("GET", "/api/jobs")
jid = b["jobs"][0]["id"]

# 造 12 个候选人（人为控制名字，便于和 CSV 对上）
NAMES = ["张三", "李四", "王五", "赵六", "钱七", "孙八",
         "周九", "吴十", "郑一", "冯二", "陈三", "楚四"]
cids = {}
for n in NAMES:
    s, b = call("POST", "/api/jobs/%s/candidates" % jid, {"candidates": [{
        "name": n,
        "files": [{"filename": "r.txt", "role": "resume",
                   "data_base64": base64.b64encode(
                       ("%s\n清华大学 本科\n字节跳动\n北京" % n).encode()).decode()}]}]})
    cids[n] = b["added"][0]["id"]

print("\n--- 1. 跑评审（并发 4 路） ---")
import time
t0 = time.time()
s, b = call("POST", "/api/jobs/%s/run" % jid, API)
check("启动评审", s == 200, b)
rid = b["run"]["id"]
for _ in range(600):
    s, b = call("GET", "/api/jobs/%s/run" % jid)
    if not b.get("active"):
        break
    time.sleep(0.1)
el = time.time() - t0
check("评审完成", not b.get("active"), b)
s, b = call("GET", "/api/runs/%s/events?after=0" % rid)
evs = b.get("events") or []
plan = [e for e in evs if e.get("type") == "run_plan"]
check("事件里记录了并发路数", plan and plan[0].get("concurrency", 0) > 1,
      plan[0] if plan else None)
print("      12 人并发 4 路耗时 %.2fs，并发=%s" %
      (el, plan[0].get("concurrency") if plan else "?"))

s, b = call("GET", "/api/jobs/%s" % jid)
done = [c for c in b["candidates"] if c["status"] == "done"]
check("12 人全部出结果", len(done) == 12, len(done))
check("候选人带出了决策字段", "decision" in done[0], sorted(done[0].keys()))

# 分数在 result 下面，不在顶层（顶层 total 是 None，早期测试在这里读错过）。
# 排序必须和 align.report 用同一个全序：(分数降序, 候选人 id 升序)。
# 否则同分时"前 5 名"有歧义，测试会时对时错——这类不稳定测试比没有测试更糟。
scored = sorted(done, key=lambda c: (-((c.get("result") or {}).get("total") or 0),
                                     c["id"]))
check("候选人都有有效分数（不是全 0）",
      ((scored[0].get("result") or {}).get("total") or 0) > 0,
      (scored[0].get("result") or {}).get("total"))
_lo = (scored[-1].get("result") or {}).get("total")
_hi = (scored[0].get("result") or {}).get("total")
print("      分数区间 %.1f ~ %.1f（%s）" % (_lo or 0, _hi or 0,
                                            "全部同分" if _lo == _hi else "有区分"))

print("\n--- 2. 建对齐批次 + 预览 CSV ---")
s, b = call("POST", "/api/jobs/%s/align" % jid, {"label": "12人实测"})
check("建批次成功", s == 200 and b["batch"]["id"], b)
aid = b["batch"]["id"]
check("批次锁定口径版本", bool(b["batch"]["rubric_version"]),
      b["batch"].get("rubric_version"))

# HR 的真实决定：按分数取前 5 名。这样 precision@5 必然 = 1.0，
# 是唯一能用来验证"计算是否正确"的确定性场景。
# 同时也验证了当 HR 和系统排序一致时，指标会如实反映（而不是被写死成好看）。
TOP5 = [c["name"] for c in scored[:5]]
CSV = "姓名,决定,备注\n" + "\n".join(
    "%s,%s,%s" % (c["name"], "推进" if c["name"] in TOP5 else "淘汰",
                  "备注" if i == 0 else "")
    for i, c in enumerate(done))
s, b = call("POST", "/api/align/preview", {"text": CSV})
check("预览不落库（不需要先有批次）", s == 200, b)
check("预览统计推进 5 人", b["summary"]["advance"] == 5, b["summary"])
check("预览统计淘汰 7 人", b["summary"]["reject"] == 7, b["summary"])

# 预览不该产生任何批次。早先的实现让前端为预览先建批次，
# 用户点一次预览就多一个空批次 —— 预览是只读操作，不能有副作用。
s, b = call("GET", "/api/jobs/%s/align" % jid)
check("预览没有留下垃圾批次", len(b["batches"]) == 1, len(b["batches"]))

print("\n--- 3. 导入 + 出报告 ---")
s, b = call("POST", "/api/align/%s/import" % aid, {"text": CSV})
check("导入成功", s == 200 and b["result"]["added"] == 12, b.get("result"))
check("全部匹配到候选人", not b["result"]["unmatched"], b["result"]["unmatched"])

s, b = call("GET", "/api/align/%s/report" % aid)
check("报告可获取", s == 200, b)
rep = b["report"]
check("样本足够（12 ≥ 10）", rep["sufficient"] is True, rep.get("message"))
check("正例 5 人", rep["positives"] == 5, rep["positives"])
at = {x["k"]: x for x in rep["at"]}
print("      %s" % rep.get("verdict"))
# 我们的 HR"s 决定 恰好是分数前 5 名 → precision@5 应为 1.0
if 5 in at:
    check("precision@5 = 1.0（前 5 名正好全被推）",
          abs(at[5]["precision"] - 1.0) < 1e-9, at[5])
    check("lift@5 > 1", at[5]["lift"] > 1, at[5])

print("\n--- 4. 样本不足时拒绝给结论 ---")
s, b = call("POST", "/api/jobs/%s/align" % jid, {"label": "小样本"})
aid2 = b["batch"]["id"]
CSV2 = "姓名,决定\n张三,推进\n李四,淘汰\n"
call("POST", "/api/align/%s/import" % aid2, {"text": CSV2})
s, b = call("GET", "/api/align/%s/report" % aid2)
check("2 个样本 → sufficient=false", b["report"]["sufficient"] is False,
      b["report"].get("message"))
check("明确告知还差几个", b["report"].get("need_more") == 8,
      b["report"].get("need_more"))

print("\n--- 5. 候选人决策（理由必填） ---")
s, b = call("POST", "/api/candidates/%s/decide" % cids["张三"],
            {"decision": "advance", "reason": ""})
check("空理由被拒 400", s == 400, (s, b))

s, b = call("POST", "/api/candidates/%s/decide" % cids["张三"],
            {"decision": "advance", "reason": "ok"})
check("敷衍理由被拒 400", s == 400, (s, b))

s, b = call("POST", "/api/candidates/%s/decide" % cids["张三"],
            {"decision": "advance", "reason": "清华本科+字节后端，与 JD 高度匹配"})
check("合规理由被接受", s == 200, b)
check("记录决策人", b["decision"]["decided_by"] == "hr1", b["decision"])

s, b = call("POST", "/api/candidates/%s/decide" % cids["李四"],
            {"decision": "reject", "reason": "工作经历与 JD 要求的后端方向不符"})
check("淘汰也能记录", s == 200, b)

s, b = call("GET", "/api/jobs/%s/align" % jid)
dec = b["decisions"]
check("决策列表可查", len(dec) == 2, len(dec))
check("决策带分数快照（复核时能对着看）",
      all("total" in d for d in dec), dec[:1])

print("\n--- 6. 审计链覆盖新动作 ---")
s, b = call("GET", "/api/audit?limit=200")
acts = {a["action"] for a in (b.get("rows") or [])}
check("审计接口可读", s == 200 and "rows" in b, list(b.keys()))
for need in ("align.create", "align.import", "candidate.decide"):
    check("审计记录了 %s" % need, need in acts, sorted(acts))

print("\n--- 7. 权限：viewer 不能写 ---")
s, b = call("POST", "/api/users", {"username": "viewer01",
                                   "password": "An0ther-Pass!26", "role": "viewer"})
check("创建 viewer 账号", s == 200, (s, b))
cj2 = http.cookiejar.CookieJar()
op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj2))


def call2(m, p, b=None):
    req = urllib.request.Request(
        BASE + p, data=json.dumps(b).encode() if b is not None else None, method=m)
    req.add_header("Content-Type", "application/json")
    c = [x.value for x in cj2 if x.name == "ts_csrf"]
    if c:
        req.add_header("X-CSRF-Token", c[0])
    try:
        with op2.open(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


call2("POST", "/api/auth/login", {"username": "viewer01", "password": "An0ther-Pass!26"})
s, _ = call2("GET", "/api/align/%s/report" % aid)
check("viewer 可读报告", s == 200, s)
s, _ = call2("POST", "/api/align/%s/import" % aid, {"text": CSV})
check("viewer 不能导入（403）", s == 403, s)
s, _ = call2("POST", "/api/candidates/%s/decide" % cids["王五"],
             {"decision": "reject", "reason": "测试权限是否拦住"})
check("viewer 不能做决策（403）", s == 403, s)

print()
print("=" * 60)
print(" 失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
srv.shutdown()
sys.exit(0 if not FAIL else 1)
