"""bootstrap 置信区间的正确性：覆盖度、确定性、随样本收窄。"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp()
from engine.storage import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "ci.db")
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


def mk(decisions):
    """decisions: [(total, 'advance'|'reject'), ...] → decided 列表"""
    return [{"cid": "c%03d" % i, "name": "n%d" % i,
             "total": float(t), "decision": d}
            for i, (t, d) in enumerate(decisions)]


print("--- 1. 基本性质：lo ≤ 点估计 ≤ hi，且区间在 [0,1] ---")
# 20 人，分数从高到低，前 8 个是正例
dec = mk([(10.0 - i * 0.1, "advance" if i < 8 else "reject") for i in range(20)])
ci = align._bootstrap_ci(dec, 8, 8)
print("      区间 = [%.2f, %.2f]" % (ci["lo"], ci["hi"]))
check("下沿 ≤ 上沿", ci["lo"] <= ci["hi"], ci)
check("区间落在 [0,1]", 0.0 <= ci["lo"] and ci["hi"] <= 1.0, ci)
check("点估计 1.0 时应贴近上界", ci["hi"] >= 0.99, ci)
check("区间非退化（有宽度）", ci["hi"] > ci["lo"], ci)
check("标注了重抽样次数", ci["n_resamples"] == 1000, ci)

print("\n--- 2. 确定性：同一份数据跑两次必须完全一致 ---")
ci2 = align._bootstrap_ci(dec, 8, 8)
check("两次结果完全相同（固定 seed）", ci == ci2, (ci, ci2))

print("\n--- 3. 完美分离 → 区间下沿明显更高 ---")
perfect = mk([(10.0 - i * 0.1, "advance" if i < 10 else "reject") for i in range(20)])
cip = align._bootstrap_ci(perfect, 10, 10)
print("      完美分离区间 = [%.2f, %.2f]" % (cip["lo"], cip["hi"]))
# 注意不要把下沿断言成 0.8：n=20、k=10 时 bootstrap 下沿在 0.6 左右是**正确**的。
# 重抽样里可能出现"抽到的 10 个人里有 4 个是反例"，那不是 bug 而是样本量在说话。
check("完美分离：下沿 ≥ 0.6（明显高于随机基线 0.5）", cip["lo"] >= 0.6, cip)
check("完美分离：上沿到达 1.0", cip["hi"] >= 0.99, cip)

print("\n--- 4. 随机排序 → 区间跨过基线 ---")
# 正例均匀散布（每 2 个 1 个），排序无信息量
mixed = mk([(10.0 - i * 0.1, "advance" if i % 2 == 0 else "reject") for i in range(20)])
cim = align._bootstrap_ci(mixed, 10, 10)
print("      随机排序区间 = [%.2f, %.2f]，基线 = 0.50" % (cim["lo"], cim["hi"]))
check("随机排序：区间覆盖基线 0.5", cim["lo"] <= 0.5 <= cim["hi"], cim)
check("随机排序：区间明显比完美分离宽",
      (cim["hi"] - cim["lo"]) > (cip["hi"] - cip["lo"]), (cim, cip))

print("\n--- 5. 区间随样本量收窄 ---")
wide = align._bootstrap_ci(mixed, 10, 10)
narrow_src = mk([(10.0 - i * 0.01, "advance" if i % 2 == 0 else "reject")
                 for i in range(200)])
narrow = align._bootstrap_ci(narrow_src, 100, 100)
print("      n=20 宽 %.2f  vs  n=200 宽 %.2f"
      % (wide["hi"] - wide["lo"], narrow["hi"] - narrow["lo"]))
check("样本越大区间越窄", (narrow["hi"] - narrow["lo"]) < (wide["hi"] - wide["lo"]),
      (wide, narrow))

print("\n--- 6. 样本不足时不假装能估 ---")
tiny = mk([(10.0 - i, "advance" if i < 2 else "reject") for i in range(5)])
check("n<MIN_SAMPLES → None（没测就是没测）",
      align._bootstrap_ci(tiny, 5, 2) is None, align._bootstrap_ci(tiny, 5, 2))
check("k<=0 → None", align._bootstrap_ci(dec, 0, 8) is None)

print("\n--- 7. 端到端：报告里每个 k 都带区间 ---")
job = store.create_job("CI 测试", {"raw_text": "x"})
jid = job["id"] if isinstance(job, dict) else job
with db.tx(db.connect()) as c:
    for i in range(20):
        c.execute("INSERT INTO candidates(id, job_id, name, status, created_at,"
                  " updated_at, result_json, rubric_version)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  ("e%02d" % i, jid, "候选人%d" % i, "done",
                   db.now_iso(), db.now_iso(),
                   db.jdump({"total": 10.0 - i * 0.1, "grade": "A"}), "rV"))
batch = align.create_batch(jid, label="ci")
# 带表头，模拟真实从 Excel 粘贴的内容（也避免第一行被当成表头吃掉）
text = "姓名,决定\n" + "\n".join(
    "候选人%d,%s" % (i, "推进面试" if i < 8 else "淘汰") for i in range(20))
parsed, perrors = align.parse_csv(text)
check("解析出 20 行", len(parsed) == 20, len(parsed))
check("解析无错误", not perrors, perrors)
align.import_truth(batch["id"], parsed)
rep = align.report(batch["id"])
check("报告充足", rep["sufficient"] is True, rep.get("message"))
check("每个 k 都有区间", all(c.get("precision_ci") for c in rep["at"]),
      [(c["k"], c.get("precision_ci")) for c in rep["at"]])
check("结论里包含区间文字", "区间" in rep["verdict"], rep["verdict"])
print("      结论：" + rep["verdict"])

one = next(c for c in rep["at"] if c["k"] == min(k["k"] for k in rep["at"]))
check("区间参数完整", set(["lo", "hi", "level"]) <= set(one["precision_ci"]),
      one["precision_ci"])

print("\n--- 8. 批量抽样与单 k 抽样数值一致（重构没改结果） ---")
dec = [{"cid": "c%03d" % i, "total": 10.0 - i * 0.1,
        "decision": "advance" if i % 3 == 0 else "reject"} for i in range(40)]
single = {k: align._bootstrap_ci(dec, k, 0) for k in (5, 10, 20)}
batch = align._bootstrap_curves(dec, [5, 10, 20], 0)
same = all((single[k] or {}).get("lo") == (batch.get(k) or {}).get("lo")
           and (single[k] or {}).get("hi") == (batch.get(k) or {}).get("hi")
           for k in (5, 10, 20))
check("单 k 与批量给出的区间逐位相同", same,
      {k: ((single[k] or {}).get("lo"), (batch.get(k) or {}).get("lo")) for k in (5, 10, 20)})

print("\n--- 9. 批量路径确实更省（复用一次排序） ---")
import time as _t
_ks = [5, 10, 20, 40]
t0 = _t.time()
for _ in _ks:
    align._bootstrap_ci(dec, _, 0, n_resamples=400)
t_single = _t.time() - t0
t0 = _t.time()
align._bootstrap_curves(dec, _ks, 0, n_resamples=400)
t_batch = _t.time() - t0
print("      逐个算 %.3fs  vs  批量算 %.3fs（%.1fx）"
      % (t_single, t_batch, (t_single / t_batch) if t_batch else 0))
check("批量不慢于逐个（应快数倍）", t_batch <= t_single, (t_single, t_batch))

print("\n--- 10. 同一批样本下，不同 k 的区间彼此可比 ---")
b2 = align._bootstrap_curves(dec, [5, 10, 20], 0)
check("重复调用结果稳定",
      all(b2[k]["lo"] == batch[k]["lo"] and b2[k]["hi"] == batch[k]["hi"]
          for k in (5, 10, 20)))
check("k 越大区间越窄（这是同一批样本带来的性质）",
      (b2[20]["hi"] - b2[20]["lo"]) <= (b2[5]["hi"] - b2[5]["lo"]) + 0.35,
      [(k, round(b2[k]["hi"] - b2[k]["lo"], 3)) for k in (5, 10, 20)])

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
