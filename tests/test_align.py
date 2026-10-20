"""对齐模块验证：precision@k 的算术必须能被手算核对。"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp()
from engine.storage import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "align.db")
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


job = store.create_job("回测演示", {"raw_text": "8年后端"})
jid = job["id"] if isinstance(job, dict) else job

# ---------------------------------------------------------------- 构造 20 人
# 分数故意设计成：前 5 名里有 4 个是 HR 真推的（80%），
# 全体正例率 6/20 = 30%，所以 precision@5 = 0.8，lift = 0.8/0.3 ≈ 2.67
POS = {1, 2, 3, 4, 9}     # 按分数排名（1 最高）真推的序号
EXTRA_POS = {13}          # 再放一个低分的正例，共 6 人
N = 20

conn = db.connect()
with db.tx(conn) as c:
    for i in range(1, N + 1):
        total = 10.0 - i * 0.3
        res = {"total": total, "grade": "A" if total >= 8 else "B",
               "rubric_version": "rTEST01", "scores": {}}
        c.execute(
            "INSERT INTO candidates(id, job_id, name, status, created_at, updated_at, "
            "result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
            ("c%02d" % i, jid, "候选人%02d" % i, "done", db.now_iso(), db.now_iso(),
             db.jdump(res), "rTEST01"))

batch = align.create_batch(jid, label="20 人小样本")
aid = batch["id"]

truth = []
for i in range(1, N + 1):
    dec = "advance" if (i in POS or i in EXTRA_POS) else "reject"
    truth.append({"name": "候选人%02d" % i, "decision": dec, "note": ""})
r = align.import_truth(aid, truth)
check("导入 20 条真实决定", r["added"] == 20, r)
check("全部匹配到候选人", not r["unmatched"], r["unmatched"])

# ---------------------------------------------------------------- 手算核对
rep = align.report(aid, ks=[5, 6, 10])
check("样本足够（20 个已决 ≥ 10）", rep["sufficient"] is True, rep.get("message"))
check("正例 6 人", rep["positives"] == 6, rep["positives"])
check("已决 20 人", rep["decided"] == 20, rep["decided"])

at5 = [x for x in rep["at"] if x["k"] == 5][0]
# 前 5 名序号 1,2,3,4,5 → POS 命中 1,2,3,4 共 4 个
check("precision@5 = 4/5 = 0.8", abs(at5["precision"] - 0.8) < 1e-9, at5)
check("recall@5 = 4/6 ≈ 0.667", abs(at5["recall"] - 4 / 6) < 1e-4, at5)
# 随机基线 = 6/20 = 0.3；lift = 0.8/0.3 = 2.667
check("lift@5 ≈ 2.67", abs(at5["lift"] - (0.8 / 0.3)) < 1e-2, at5)
check("随机期望命中 1.5 个", abs(at5["random_expected"] - 1.5) < 1e-9, at5)

at10 = [x for x in rep["at"] if x["k"] == 10][0]
# 前 10 名含 POS {1,2,3,4,9} = 5 个 → 5/10 = 0.5
check("precision@10 = 5/10 = 0.5", abs(at10["precision"] - 0.5) < 1e-9, at10)
check("recall@10 = 5/6 ≈ 0.833", abs(at10["recall"] - 5 / 6) < 1e-4, at10)

check("给出人话结论", bool(rep.get("verdict")), rep.get("verdict"))
print("      结论：%s" % rep.get("verdict"))
check("排序优于随机 → 结论应指出有信息量", "信息量" in (rep.get("verdict") or ""),
      rep.get("verdict"))

# ---------------------------------------------------------------- 样本不足
job2 = store.create_job("小样本", {"raw_text": "x"})
jid2 = job2["id"] if isinstance(job2, dict) else job2
with db.tx(conn) as c:
    for i in range(1, 6):
        c.execute(
            "INSERT INTO candidates(id, job_id, name, status, created_at, updated_at, "
            "result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
            ("d%02d" % i, jid2, "小%02d" % i, "done", db.now_iso(), db.now_iso(),
             db.jdump({"total": 9.0 - i, "rubric_version": "rX"}), "rX"))
b2 = align.create_batch(jid2, label="5 人")
align.import_truth(b2["id"], [{"name": "小%02d" % i,
                               "decision": "advance" if i == 1 else "reject"}
                              for i in range(1, 6)])
rep2 = align.report(b2["id"])
check("5 个样本 → 拒绝给结论", rep2["sufficient"] is False, rep2.get("message"))
check("明确告知还差几个", rep2.get("need_more") == 5, rep2.get("need_more"))
check("样本不足时不编造 precision", "at" not in rep2, list(rep2.keys()))

# ---------------------------------------------------------------- 决策理由
s, = (None,) if False else (None,)
r = align.decide("c01", "reject", "   " ) if False else None
try:
    align.decide("c01", "reject", "ok")
    check("理由太短应被拒", False, "居然通过了")
except ValueError as e:
    check("理由太短被拒（挡住敷衍）", "理由" in str(e), e)

try:
    align.decide("c01", "maybe", "这个人背景不错但经验偏浅")
    check("非法决定应被拒", False)
except ValueError as e:
    check("非法决定被拒", "advance" in str(e), e)

d = align.decide("c01", "advance", "清华本科+字节后端，与 JD 高度匹配", actor="hr1")
check("合法决策被记录", d["decision"] == "advance", d)
row = conn.execute("SELECT decision, decision_reason, decided_by FROM candidates "
                   "WHERE id='c01'").fetchone()
check("理由落库", row["decision_reason"].startswith("清华"), dict(row))
check("记录决策人", row["decided_by"] == "hr1", dict(row))

from engine.guardrails import audit
n = conn.execute("SELECT COUNT(*) AS n FROM audit WHERE action='candidate.decide'"
                 ).fetchone()["n"]
check("决策写入审计链", n == 1, n)

# ---------------------------------------------------------------- CSV 解析
rows, errs = align.parse_csv(
    "姓名,决定,备注\n张三,推进,背景强\n李四,淘汰,经验不符\n王五,待定,\n")
check("CSV 解析 3 行", len(rows) == 3 and not errs, (rows, errs))
check("识别「推进」", rows[0]["decision"] == "advance", rows[0])
check("识别「淘汰」", rows[1]["decision"] == "reject", rows[1])
check("识别「待定」", rows[2]["decision"] == "hold", rows[2])

rows2, _ = align.parse_csv("name\tdecision\n张三\tyes\n李四\tno\n")
check("制表符分隔（Excel 粘贴）", len(rows2) == 2 and rows2[0]["decision"] == "advance",
      rows2)

rows3, errs3 = align.parse_csv("姓名,决定\n张三,可能吧\n")
check("无法识别的决定被抓出来", rows3 == [] and errs3, errs3)

rows4, errs4 = align.parse_csv("只有一列\n张三\n")
check("缺列时明确报错并说明缺什么",
      errs4 and ("姓名" in errs4[0]["error"] or "决定" in errs4[0]["error"]), errs4)

# 有姓名列但缺决定列 —— 这时必须精确指出缺的是"决定"
rows5, errs5 = align.parse_csv("姓名,备注\n张三,背景强\n")
check("缺决定列时精确指出（而不是笼统报错）",
      errs5 and "决定" in errs5[0]["error"], errs5)

# 同名两人不能瞎猜
job3 = store.create_job("同名", {"raw_text": "x"})
jid3 = job3["id"] if isinstance(job3, dict) else job3
with db.tx(conn) as c:
    for k, cid in enumerate(("e1", "e2")):
        c.execute(
            "INSERT INTO candidates(id, job_id, name, status, created_at, updated_at, "
            "result_json) VALUES (?,?,?,?,?,?,?)",
            (cid, jid3, "张三", "done", db.now_iso(), db.now_iso(),
             db.jdump({"total": 8.0 - k})))
b3 = align.create_batch(jid3)
r3 = align.import_truth(b3["id"], [{"name": "张三", "decision": "advance", "note": ""}])
check("同名候选人 → 标记 ambiguous 不猜", len(r3["ambiguous"]) == 1, r3)

print()
print("失败 %d 项" % len(FAIL))
sys.exit(0 if not FAIL else 1)
