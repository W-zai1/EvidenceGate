"""200 人规模验证：并发耗时、事件完整性、对齐报告在真实规模下的表现。

这不是性能基准测试，而是回答一个具体问题：
"一个岗位 200 份简历，这套流程能不能在可接受时间内跑完并给出可信的对齐结论。"
"""
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

TMP = tempfile.mkdtemp()
os.environ["PORT"] = "8294"
import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "scale.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

import agents
import align
import jobs_runner
import runs
import store

N = 200
FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


# 打桩：每个候选人固定消耗 40ms（真实小模型约 20-40s，这里缩比以缩短测试）
WORK = 0.04
_calls = []
_lock = threading.Lock()
_inflight = [0]
_peak = [0]


def fake_run_candidate(c, job, api=None, on_event=None):
    with _lock:
        _inflight[0] += 1
        _peak[0] = max(_peak[0], _inflight[0])
    try:
        time.sleep(WORK)
        with _lock:
            _calls.append(c["id"])
        # 让分数有区分度，便于验证对齐报告
        h = abs(hash(c["id"])) % 1000
        total = round(3.0 + h / 1000.0 * 7.0, 2)
        return {"total": total, "grade": "A" if total >= 8 else "B",
                "rubric_version": "rSCALE1", "scores": {}, "spread": {},
                "debate": {"adjudication": {}}}
    finally:
        with _lock:
            _inflight[0] -= 1


agents.run_candidate = fake_run_candidate

job = store.create_job("200人规模测试", {"raw_text": "后端"})
jid = job["id"] if isinstance(job, dict) else job

conn = db.connect()
ids = []
with db.tx(conn) as c:
    for i in range(N):
        cid = "s%03d" % i
        ids.append(cid)
        c.execute(
            "INSERT INTO candidates(id, job_id, name, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (cid, jid, "候选人%03d" % i, "pending", db.now_iso(), db.now_iso()))

print("=" * 64)
print(" 200 人规模：并发耗时 / 事件完整性")
print("=" * 64)

results = {}
for w in (1, 4, 8):
    os.environ["REFINE_CONCURRENCY"] = str(w)
    _calls.clear()
    _peak[0] = 0
    with db.tx(conn) as c:
        c.execute("UPDATE candidates SET status='pending', result_json=NULL "
                  "WHERE job_id=?", (jid,))
        rid = "run-scale-%d" % w
        c.execute("INSERT OR REPLACE INTO runs(id, job_id, status, started_at, "
                  "rubric_version, candidate_ids_json) VALUES (?,?,?,?,?,?)",
                  (rid, jid, "running", db.now_iso(), "rSCALE1", db.jdump(ids)))
    t0 = time.time()
    jobs_runner.execute(rid, {"provider": "openai", "model": "m", "api_key": "k"})
    el = time.time() - t0
    ok_n = conn.execute("SELECT COUNT(*) AS n FROM candidates WHERE job_id=? "
                        "AND status='done'", (jid,)).fetchone()["n"]
    evs = conn.execute("SELECT seq FROM run_events WHERE run_id=? ORDER BY seq",
                       (rid,)).fetchall()
    seqs = [e["seq"] for e in evs]
    results[w] = {"elapsed": el, "done": ok_n, "events": len(seqs),
                  "contiguous": seqs == list(range(1, len(seqs) + 1)),
                  "peak_inflight": _peak[0], "calls": len(_calls)}
    print("  并发 %d 路：%.2fs  done=%d/%d  事件=%d  序号连续=%s  峰值在飞=%d"
          % (w, el, ok_n, N, len(seqs), results[w]["contiguous"], _peak[0]))

r1, r4, r8 = results[1], results[4], results[8]
check("串行 200 人耗时 ≈ 200×40ms", abs(r1["elapsed"] - N * WORK) < 1.5,
      r1["elapsed"])
check("并发 4 路显著加速（≥2.5×）", r1["elapsed"] / r4["elapsed"] >= 2.5,
       round(r1["elapsed"] / r4["elapsed"], 2))
check("并发 8 路比 4 路更快", r8["elapsed"] < r4["elapsed"],
      (r4["elapsed"], r8["elapsed"]))
for w in (1, 4, 8):
    check("并发 %d 路全部跑完 %d 人" % (w, N), results[w]["done"] == N,
          results[w]["done"])
    check("并发 %d 路事件序号连续（无冲突丢失）" % w, results[w]["contiguous"],
          results[w]["events"])
    check("并发 %d 路每人只跑一次（无重复计费）" % w, results[w]["calls"] == N,
          results[w]["calls"])
check("并发 4 路峰值在飞 ≤ 4", r4["peak_inflight"] <= 4, r4["peak_inflight"])
check("并发 8 路峰值在飞 > 4（确实吃满了）", r8["peak_inflight"] > 4,
      r8["peak_inflight"])

print()
print("=" * 64)
print(" 200 人规模：对齐报告")
print("=" * 64)
os.environ.pop("REFINE_CONCURRENCY", None)

# HR 真实决定：按分数取前 30 名（15%）推进，其余淘汰
scored = sorted(
    [(r["id"], (db.jload(r["result_json"] or "{}", {}) or {}).get("total") or 0)
     for r in conn.execute("SELECT id, result_json FROM candidates WHERE job_id=?",
                           (jid,))],
    key=lambda x: (-x[1], x[0]))
top = {cid for cid, _ in scored[:30]}

batch = align.create_batch(jid, label="200人真实筛选")
rows = [{"name": "候选人%03d" % i,
         "decision": "advance" if ids[i] in top else "reject", "note": ""}
        for i in range(N)]
imp = align.import_truth(batch["id"], rows)
check("导入 200 条真实决定", imp["added"] == 200, imp["added"])
check("全部匹配", not imp["unmatched"], len(imp["unmatched"]))

rep = align.report(batch["id"], ks=[30, 50, 100])
check("样本足够", rep["sufficient"] is True, rep.get("message"))
check("正例 30 人", rep["positives"] == 30, rep["positives"])
at30 = [x for x in rep["at"] if x["k"] == 30][0]
check("HR 与系统排序一致时 precision@30 = 1.0",
      abs(at30["precision"] - 1.0) < 1e-9, at30)
check("曲线长度 = 200", len(rep["curve"]) == N, len(rep["curve"]))
check("曲线末点 recall = 1.0", abs(rep["curve"][-1]["recall"] - 1.0) < 1e-9,
      rep["curve"][-1])
print("      %s" % rep.get("verdict"))

# 反向场景：HR 的决定与系统完全相反，结论应当说"比随机还差"
inv_batch = align.create_batch(jid, label="反向对照")
inv_rows = [{"name": "候选人%03d" % i,
             "decision": "reject" if ids[i] in top else "advance", "note": ""}
            for i in range(N)]
align.import_truth(inv_batch["id"], inv_rows)
inv = align.report(inv_batch["id"], ks=[30])
check("反向场景样本足够", inv["sufficient"] is True, inv.get("message"))
check("反向场景 lift < 1", inv["at"][0]["lift"] < 1.0, inv["at"][0])
check("反向场景结论指出'比随机差'", "比随机" in (inv.get("verdict") or ""),
      inv.get("verdict"))
print("      %s" % inv.get("verdict"))

print()
print("=" * 64)
print(" 失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
