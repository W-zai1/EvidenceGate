"""同名歧义的人工绑定通道。"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp()
from engine.storage import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "bind.db")
db.reset_init_flag()
from engine.intake import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

from engine.guardrails import align
from   engine.storage import store

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


job = store.create_job("绑定测试", {"raw_text": "x"})
jid = job["id"] if isinstance(job, dict) else job
job2 = store.create_job("别的岗位", {"raw_text": "y"})
jid2 = job2["id"] if isinstance(job2, dict) else job2

with db.tx(db.connect()) as c:
    # 两个同名「张伟」，分数不同
    c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at,"
              " result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
              ("zw1", jid, "张伟", "done", db.now_iso(), db.now_iso(),
               db.jdump({"total": 9.0, "grade": "A"}), "rV"))
    c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at,"
              " result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
              ("zw2", jid, "张伟", "done", db.now_iso(), db.now_iso(),
               db.jdump({"total": 5.0, "grade": "C"}), "rV"))
    # 另一个岗位的同名者
    c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at,"
              " result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
              ("zw_other", jid2, "张伟", "done", db.now_iso(), db.now_iso(),
               db.jdump({"total": 7.0, "grade": "B"}), "rV"))
    # 一个正常人 + 一堆陪跑，凑够 MIN_SAMPLES
    c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at,"
              " result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
              ("ok1", jid, "李四", "done", db.now_iso(), db.now_iso(),
               db.jdump({"total": 8.0, "grade": "A"}), "rV"))
    for i in range(9):
        c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at,"
                  " result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
                  ("f%d" % i, jid, "陪跑%d" % i, "done", db.now_iso(), db.now_iso(),
                   db.jdump({"total": 6.0 - i * 0.1, "grade": "B"}), "rV"))

batch = align.create_batch(jid, label="同名测试")
bid = batch["id"]

text = "姓名,决定\n张伟,推进面试\n李四,淘汰\n" + "\n".join(
    "陪跑%d,%s" % (i, "推进面试" if i < 3 else "淘汰") for i in range(9))
rows, errs = align.parse_csv(text)
res = align.import_truth(bid, rows)
print("  导入：added=%d ambiguous=%d unmatched=%d"
      % (res["added"], len(res["ambiguous"]), len(res["unmatched"])))

print("\n--- 1. 同名不猜 ---")
check("张伟被标为 ambiguous", any(a["name"] == "张伟" for a in res["ambiguous"]),
      res["ambiguous"])
check("ambiguous 列出两个候选供人判断",
      len(next(a for a in res["ambiguous"] if a["name"] == "张伟")["candidates"]) == 2,
      res["ambiguous"])

print("\n--- 2. 待绑定清单可见 ---")
pend = align.pending_bindings(bid)
check("有 1 条 ambiguous", len(pend["ambiguous"]) == 1, pend["ambiguous"])
item = pend["ambiguous"][0]
check("待绑定项带候选人选项", len(item.get("options") or []) == 2, item)
check("选项里能看到各自分数线索（status/version）",
      all("status" in o and "rubric_version" in o for o in item["options"]), item["options"])
check("已绑定数 = 10", pend["bound"] == 10, pend["bound"])

print("\n--- 3. 报告里如实说明有未绑定项 ---")
rep = align.report(bid)
check("报告报出 1 条 ambiguous", rep["pending_bindings"]["ambiguous"] == 1,
      rep["pending_bindings"])
check("提示了未参与计算", "未参与计算" in rep["pending_bindings"]["note"],
      rep["pending_bindings"]["note"])

print("\n--- 4. 手工绑定 ---")
truth_id = item["truth_id"]
out = align.bind_truth(truth_id, "zw1", actor="hr01")
check("绑定成功", out["candidate_id"] == "zw1", out)
check("记录了绑定前的值（原来是空）", out["previous"] == "", out)
pend2 = align.pending_bindings(bid)
check("绑定后不再 pending", len(pend2["ambiguous"]) == 0, pend2["ambiguous"])
check("已绑定数 +1", pend2["bound"] == 11, pend2["bound"])

print("\n--- 5. 绑定改变回测结果（所以必须留痕） ---")
rep2 = align.report(bid)
check("张伟这条现在参与计算了", rep2["matched"] > rep["matched"],
      (rep["matched"], rep2["matched"]))
check("绑定进审计链", any(a["action"] == "align.bind"
                          for a in db.connect().execute(
                              "SELECT action FROM audit").fetchall()))

print("\n--- 6. 拒绝跨岗位绑定（作弊面） ---")
import sqlite3
# 再插一条 ambiguous 来测
rows2, _ = align.parse_csv("姓名,决定\n张伟,待定")
align.import_truth(bid, rows2)
pend3 = align.pending_bindings(bid)
# 待定那条会被 update 到同一个 name_key 上（同名覆盖），所以重新造一条
with db.tx(db.connect()) as c:
    c.execute("INSERT INTO align_truth(id, align_id, candidate_id, person_id,"
              " name_key, name_raw, decision, note, created_at)"
              " VALUES ('t_dup',?, '', '', 'zzz', '外岗某人', 'advance', '', ?)",
              (bid, db.now_iso()))
try:
    align.bind_truth("t_dup", "zw_other", actor="hr01")
    check("跨岗位绑定应被拒绝", False)
except ValueError as e:
    check("拒绝跨岗位绑定", "跨岗位" in str(e), e)

print("\n--- 7. 不存在的对象 ---")
try:
    align.bind_truth("nope", "zw1")
    check("不存在的 truth 应报错", False)
except ValueError as e:
    check("truth 不存在被拒", "真实决定" in str(e), e)
try:
    align.bind_truth(truth_id, "nope")
    check("不存在的候选人应报错", False)
except ValueError as e:
    check("候选人不存在被拒", "候选人不存在" in str(e), e)

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
