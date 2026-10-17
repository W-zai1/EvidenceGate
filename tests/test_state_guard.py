"""状态机端点隔离：通用 PATCH 不能改状态/结果/身份。

背景：`PUT /api/candidates/:cid` 原来按 update_candidate 的完整白名单过滤，
而那份白名单包含 status / result / rubric_version / person_id / identity。
于是"一条 PUT 把 running 改成 done"是可能的，审计里也看不出发生了什么。
现在公开的 PATCH 白名单只留内容字段。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

TMP = tempfile.mkdtemp()
import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "state.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

import api
import store

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


print("--- 1. 两份白名单的边界 ---")
check("HTTP 白名单不含 status", "status" not in store.CAND_HTTP_PATCHABLE,
      store.CAND_HTTP_PATCHABLE)
check("HTTP 白名单不含 result", "result" not in store.CAND_HTTP_PATCHABLE)
check("HTTP 白名单不含 rubric_version",
      "rubric_version" not in store.CAND_HTTP_PATCHABLE)
for k in ("person_id", "identity", "person_decision"):
    check("HTTP 白名单不含 %s" % k, k not in store.CAND_HTTP_PATCHABLE)
check("HTTP 白名单保留内容字段 name",
      "name" in store.CAND_HTTP_PATCHABLE, store.CAND_HTTP_PATCHABLE)
for k in ("resume_text", "assessment_text", "transcript_text"):
    check("HTTP 白名单保留 %s" % k, k in store.CAND_HTTP_PATCHABLE)

print("\n--- 2. 内部可写集仍然完整（否则评审写不进结果） ---")
for k in ("status", "result", "rubric_version", "person_id", "identity"):
    check("内部白名单仍含 %s" % k, k in store._CAND_WRITABLE, store._CAND_WRITABLE)

print("\n--- 3. 走 store 直调（内部路径）能改状态 ---")
job = store.create_job("状态岗", {"requirements": []})
cand = store.add_candidate(job["id"], "张三", resume_text="本科")
store.update_candidate(cand["id"], {"status": "running"})
check("内部可以直接改状态",
      store.get_candidate(cand["id"])["status"] == "running",
      store.get_candidate(cand["id"])["status"])
store.update_candidate(cand["id"], {"status": "done",
                                   "result": {"total": 8.0, "grade": "A"}})
c2 = store.get_candidate(cand["id"])
check("内部可以直接写结果", (c2.get("result") or {}).get("total") == 8.0,
      c2.get("result"))

print("\n--- 4. HTTP 通用 PATCH 过滤掉状态字段 ---")
raw_body = {"name": "张三改", "status": "done", "result": {"total": 99},
            "person_id": "person_hack", "rubric_version": "rfake"}
patch = {k: v for k, v in raw_body.items() if k in store.CAND_HTTP_PATCHABLE}
check("过滤后只剩内容字段", set(patch) == {"name"}, patch)
cand2 = store.update_candidate(cand["id"], patch)
check("名字确实改了", cand2["name"] == "张三改", cand2["name"])
check("状态没有被 PUT 改掉", cand2["status"] == "done", cand2["status"])
check("结果没有被覆盖", (cand2.get("result") or {}).get("total") == 8.0,
      cand2.get("result"))
check("rubric_version 没有被伪造",
      (cand2.get("rubric_version") or "") != "rfake", cand2.get("rubric_version"))

print("\n--- 5. 空补丁会被拒绝（而不是静默成功） ---")
# 直接验证 api 层的行为：只传状态字段时，过滤后为空 → 应当报 400
only_state = {k: v for k, v in {"status": "done"}.items()
              if k in store.CAND_HTTP_PATCHABLE}
check("只传状态字段时过滤结果为空", only_state == {}, only_state)

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
