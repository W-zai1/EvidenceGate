"""全局并发闸门 + 评审历史 + 漂移分组的验证。"""
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

TMP = tempfile.mkdtemp()
import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "g.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

import llm_client
import _llmstub
import outcome
import store

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


print("--- 1. 全局并发闸门真的封住了 ---")
os.environ["REFINE_GLOBAL_CONCURRENCY"] = "3"
llm_client._sems.clear()
inflight = [0]
peak = [0]
lock = threading.Lock()


def slow_raw(messages, model, api_key, base_url, temperature, use_json, timeout):
    with lock:
        inflight[0] += 1
        peak[0] = max(peak[0], inflight[0])
    try:
        time.sleep(0.05)
        return "ok", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    finally:
        with lock:
            inflight[0] -= 1


_llmstub.install(slow_raw)

errors = []


def worker():
    try:
        llm_client.complete([{"role": "user", "content": "hi"}], model="m",
                            api_key="k", provider="openai", retries=0)
    except Exception as e:
        errors.append(e)


ths = [threading.Thread(target=worker) for _ in range(20)]
t0 = time.time()
for t in ths:
    t.start()
for t in ths:
    t.join()
el = time.time() - t0
check("20 个并发请求全部成功", len(errors) == 0, errors[:1])
check("峰值在飞 ≤ 全局上限 3", peak[0] <= 3, peak[0])
check("确实用满了 3 路（不是压成串行）", peak[0] == 3, peak[0])
print("      20 请求 / 上限 3 / 每次 50ms → %.2fs，峰值 %d" % (el, peak[0]))

# 跨"run"共享：模拟两个岗位各开 4 路，闸门仍是 3
llm_client._sems.clear()
peak[0] = 0


def job_worker():
    for _ in range(3):
        worker()


ths = [threading.Thread(target=job_worker) for _ in range(4)]
for t in ths:
    t.start()
for t in ths:
    t.join()
check("多岗位同时跑时闸门仍生效（跨 run 共享）", peak[0] <= 3, peak[0])

os.environ.pop("REFINE_GLOBAL_CONCURRENCY", None)
check("快照能报告上限", llm_client.concurrency_snapshot().get("openai") == 8,
      llm_client.concurrency_snapshot())

print("\n--- 2. 评审历史只追加不覆盖 ---")
job = store.create_job("历史测试", {"raw_text": "x"})
jid = job["id"] if isinstance(job, dict) else job
with db.tx(db.connect()) as c:
    c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at)"
              " VALUES ('h1',?,?,?,?,?)", (jid, "张三", "done", db.now_iso(), db.now_iso()))

store.update_candidate("h1", {"result": {"total": 7.0, "grade": "B",
                                        "rubric_version": "rA",
                                        "rubric_parts": {"prompt_fingerprint": "fpA"}}})
store.update_candidate("h1", {"result": {"total": 8.0, "grade": "A",
                                        "rubric_version": "rB",
                                        "rubric_parts": {"prompt_fingerprint": "fpB"}}})
store.update_candidate("h1", {"result": {"total": 7.5, "grade": "B",
                                        "rubric_version": "rB",
                                        "rubric_parts": {"prompt_fingerprint": "fpB"}}})

hist = store.evaluations_of("h1")
check("3 次评审留下 3 条历史", len(hist) == 3, len(hist))
check("最新在前", hist[0]["total"] == 7.5, hist[0])
check("第一次的 7.0 没被覆盖", any(h["total"] == 7.0 for h in hist), hist)
check("历史记录了口径版本", {h["rubric_version"] for h in hist} == {"rA", "rB"},
      {h["rubric_version"] for h in hist})
check("历史记录了提示词指纹", {h["prompt_fingerprint"] for h in hist} == {"fpA", "fpB"},
      {h["prompt_fingerprint"] for h in hist})

dr = store.evaluation_drift("h1")
check("漂移：3 次", dr["n"] == 3, dr)
check("漂移：极差 1.0", abs(dr["spread"] - 1.0) < 1e-9, dr["spread"])
check("漂移：等级发生过变化", dr["grade_changed"] is True, dr)

print("\n--- 3. 只有一次评审时不谎报漂移 ---")
with db.tx(db.connect()) as c:
    c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at)"
              " VALUES ('h2',?,?,?,?,?)", (jid, "李四", "done", db.now_iso(), db.now_iso()))
store.update_candidate("h2", {"result": {"total": 9.0, "grade": "A"}})
check("只评过 1 次 → drift 为 None（没测就是没测）",
      store.evaluation_drift("h2") is None, store.evaluation_drift("h2"))

print("\n--- 4. 漂移按岗位分组（不再跨岗位混算） ---")
job2 = store.create_job("另一个岗位", {"raw_text": "y"})
jid2 = job2["id"] if isinstance(job2, dict) else job2
with db.tx(db.connect()) as c:
    # 岗位 1 清一色 9 分，岗位 2 清一色 4 分。
    # 旧实现（无 job 过滤）会把两者混在一起算出一个中间的均值。
    for i in range(6):
        c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at,"
                  " result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
                  ("p1_%d" % i, jid, "甲%d" % i, "done", db.now_iso(), db.now_iso(),
                   db.jdump({"total": 9.0, "grade": "A"}), "rV1"))
        c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at,"
                  " result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
                  ("p2_%d" % i, jid2, "乙%d" % i, "done", db.now_iso(), db.now_iso(),
                   db.jdump({"total": 4.0, "grade": "C"}), "rV1"))

d = outcome.drift(limit_versions=20)
check("返回里标明按岗位分组", d.get("grouped_by") == "job_id", d.get("grouped_by"))
buckets = {(b["job_id"], b["version"]): b for b in d["versions"]}
check("岗位 1 的桶均值 9.0（没被稀释）",
      buckets.get((jid, "rV1"), {}).get("mean") == 9.0,
      buckets.get((jid, "rV1")))
check("岗位 2 的桶均值 4.0", buckets.get((jid2, "rV1"), {}).get("mean") == 4.0,
      buckets.get((jid2, "rV1")))
check("两岗位分属不同桶（没被合并）",
      (jid, "rV1") in buckets and (jid2, "rV1") in buckets, list(buckets))

# 跨岗位不再产生"均分偏移"告警：同一版本号、两个不同岗位，不该报警
check("同版本但不同岗位不产生偏移告警", not d["alerts"], d["alerts"])

print("\n--- 5. 同岗位内跨版本仍会告警 ---")
with db.tx(db.connect()) as c:
    for i in range(6):
        c.execute("INSERT INTO candidates(id, job_id, name, status, created_at, updated_at,"
                  " result_json, rubric_version) VALUES (?,?,?,?,?,?,?,?)",
                  ("p1b_%d" % i, jid, "甲b%d" % i, "done", db.now_iso(), db.now_iso(),
                   db.jdump({"total": 6.0, "grade": "B"}), "rV2"))
d2 = outcome.drift(limit_versions=20)
check("同岗位跨版本大偏移 → 告警", len(d2["alerts"]) >= 1, d2["alerts"])

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
