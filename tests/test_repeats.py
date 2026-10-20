"""重复评审（repeats）的端到端验证：中位数、方差、跨档告警、失败容忍。

用桩函数模拟"LLM 有随机性"，检查 repeats>1 时是否正确聚合，
以及 repeats=1 时不制造虚假的稳定性结论。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp()
from engine.storage import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "rep.db")
db.reset_init_flag()
from engine.intake import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

from engine.orchestrator import agents
from   engine.api import llm_client
import _llmstub

FAIL = []
CHECKS = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


SYS_FAKE = {"calls": [], "score_seq": None, "n": 0, "judge_calls": 0}


def fake_raw(messages, model, api_key, base_url, temperature, use_json, timeout):
    """模拟模型。评委调用返回变化的分数，用来模拟随机性。

    关键：分数按"第几次评审"轮转，而不是按绝对调用序号。
    整场一次评审会调用 3 个评委（并发，顺序不定），所以第 r 次评审
    落在 seq[r]。原来用 `n // 3` 对全局计数取整，reset 后仍会跨评审累加，
    结果三次评审拿到同一个分数——测试会误以为"重复评审没生效"。
    """
    sysmsg = messages[0]["content"]
    SYS_FAKE["n"] += 1
    SYS_FAKE["calls"].append({"temperature": temperature, "json": use_json,
                              "is_judge": "评委" in sysmsg,
                              "is_adjudicator": "裁定裁判" in sysmsg,
                              "is_debate": ("正方Agent" in sysmsg
                                            or "反方Agent" in sysmsg)})
    if "评委" in sysmsg:
        seq = SYS_FAKE["score_seq"] or [8.0]
        import json
        # 第几次评审 = 已完成的评委调用数 // 每场评委数(3)
        r = (SYS_FAKE["judge_calls"] // 3) % len(seq)
        SYS_FAKE["judge_calls"] += 1
        v = seq[r]
        return json.dumps({"岗位能力匹配度": v, "风险与软素质": v,
                           "硬实力门槛": v, "comment": "证据「原文」"}), \
            {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
    if use_json:
        import json
        return json.dumps({"pro_evidence": ["「原文」支撑"], "con_challenges": ["「原文」缺口"],
                           "disputes": ["争议点"], "excluded": []}), \
            {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
    return "正方/反方论点，引用「原文」。", \
        {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}


_llmstub.install(fake_raw)

CAND = {"id": "x1", "job_id": "j1", "name": "测试人",
        "resume_text": "本科，5年后端经验。", "assessment_text": "", "transcript_text": "",
        "job": {"id": "j1", "title": "后端", "requirements": ["5年经验"],
                "preferred": [], "red_flags": [], "weights": None}}


def reset(seq=None):
    SYS_FAKE["calls"].clear()
    SYS_FAKE["n"] = 0
    SYS_FAKE["judge_calls"] = 0
    SYS_FAKE["score_seq"] = seq


print("--- 1. 打分与裁定是 temperature=0，辩论是 0.7 ---")
reset([8.0])
agents.run_candidate(CAND, CAND["job"], api={"model": "m", "provider": "openai",
                                            "api_key": "k"}, repeats=1)
judge_temps = {c["temperature"] for c in SYS_FAKE["calls"] if c["is_judge"]}
adj_temps = {c["temperature"] for c in SYS_FAKE["calls"] if c["is_adjudicator"]}
debate_temps = {c["temperature"] for c in SYS_FAKE["calls"] if c["is_debate"]}
check("所有评委调用 temperature=0", judge_temps == {0.0}, judge_temps)
check("裁定裁判 temperature=0（裁定要可复现）", adj_temps == {0.0}, adj_temps)
check("辩论阶段仍有温度 0.7（要多样性）", debate_temps == {0.7}, debate_temps)
check("辩论确实发生了（不是空跑）",
      sum(1 for c in SYS_FAKE["calls"] if c["is_debate"]) == 6,
      sum(1 for c in SYS_FAKE["calls"] if c["is_debate"]))

print("\n--- 2. repeats=1 时不制造虚假稳定性 ---")
reset([8.0])
r1 = agents.run_candidate(CAND, CAND["job"], api={"model": "m", "provider": "openai",
                                                 "api_key": "k"}, repeats=1)
check("repeats=1 → uncertainty 仍在（说明测了几次）",
      r1.get("uncertainty", {}).get("repeats_planned") == 1, r1.get("uncertainty"))
check("只评 1 次 → stdev 为 None（不是 0）",
      r1["uncertainty"]["stdev"] is None, r1["uncertainty"])
check("只评 1 次 → 没有跨档告警", r1["audit"]["stability_warning"] == "",
      r1["audit"]["stability_warning"])

print("\n--- 3. repeats=3：取中位数、记录极差 ---")
reset([9.0, 5.0, 7.0])       # 三次分别为 9 / 5 / 7 → 中位数 7
r3 = agents.run_candidate(CAND, CAND["job"], api={"model": "m", "provider": "openai",
                                                 "api_key": "k"}, repeats=3)
u = r3["uncertainty"]
print("      totals=%s median=%s stdev=%s" % (u["totals"], r3["total"], u["stdev"]))
check("计划 3 次", u["repeats_planned"] == 3, u)
check("成功 3 次", u["repeats_ok"] == 3, u)
check("记录了 3 个分数", len(u["totals"]) == 3, u["totals"])
check("最终分 = 中位数 7.0", abs(r3["total"] - 7.0) < 1e-6, r3["total"])
check("极差 = 4.0", abs(u["spread"] - 4.0) < 1e-6, u["spread"])
check("stdev 已计算", u["stdev"] is not None and u["stdev"] > 0, u["stdev"])
check("分数跨档 → 有稳定性告警",
      bool(r3["audit"]["stability_warning"]), r3["audit"]["stability_warning"])
check("告警说明了这是档位边界", "档位边界" in r3["audit"]["stability_warning"],
      r3["audit"]["stability_warning"])
print("      告警：" + r3["audit"]["stability_warning"])

print("\n--- 4. 稳定分数不触发告警 ---")
reset([7.0, 7.0, 7.0])
r4 = agents.run_candidate(CAND, CAND["job"], api={"model": "m", "provider": "openai",
                                                 "api_key": "k"}, repeats=3)
check("三次同分 → 无告警", r4["audit"]["stability_warning"] == "",
      r4["audit"]["stability_warning"])
check("stdev = 0", r4["uncertainty"]["stdev"] == 0.0, r4["uncertainty"]["stdev"])
check("极差 = 0", r4["uncertainty"]["spread"] == 0.0, r4["uncertainty"]["spread"])

print("\n--- 5. 代表结果与最终分一致（不能显示 A 却有 7 分详情） ---")
reset([9.0, 5.0, 7.0])
r5 = agents.run_candidate(CAND, CAND["job"], api={"model": "m", "provider": "openai",
                                                 "api_key": "k"}, repeats=3)
check("grade 由最终总分重新计算", r5["grade"] == agents._grade(r5["total"]),
      (r5["grade"], r5["total"]))
check("total 与 grade 自洽",
      (r5["total"] >= 8.0) == (r5["grade"] == "A"), (r5["total"], r5["grade"]))

print("\n--- 6. 单次失败不影响整体 ---")
# 注意：不能在 _raw_completion 层注入失败——llm_client.complete 自带 retries=2，
# 前两次失败会被重试悄悄吃掉，第三次成功，于是"失败"根本不会发生。
# 要模拟真正的失败，必须让重试之后仍然失败，所以我在这里直接替换
# agents 使用的完整调用路径，并让指定次数全部抛错。
orig_complete = llm_client.complete
state = {"judge": 0}


def flaky_complete(messages, **kw):
    """让**第一次评审**的三个评委（含重试）全部失败，后两次正常。"""
    is_judge = "评委" in (messages[0].get("content") or "")
    if is_judge:
        state["judge"] += 1
        if state["judge"] <= 3:
            raise llm_client.LLMError("模拟网络失败（重试后仍失败）")
    return orig_complete(messages, **kw)


llm_client.complete = flaky_complete
reset([7.5, 7.5, 7.5])
state["judge"] = 0
try:
    r6 = agents.run_candidate(CAND, CAND["job"], api={"model": "m", "provider": "openai",
                                                     "api_key": "k"}, repeats=3)
    u6 = r6["uncertainty"]
    check("部分失败仍能给出结果", r6["total"] is not None, r6.get("total"))
    check("如实记录失败的次数", u6["repeats_failed"] >= 1, u6)
    check("repeats_ok + failed == planned",
          u6["repeats_ok"] + u6["repeats_failed"] == u6["repeats_planned"], u6)
    check("失败详情有重复序号", all("repeat" in e for e in u6["errors"]), u6["errors"])
except Exception as e:
    check("部分失败仍能给出结果（不应整体抛错）", False, e)
finally:
    llm_client.complete = orig_complete

print("\n--- 7. 上限保护 ---")
reset([7.0])
r7 = agents.run_candidate(CAND, CAND["job"], api={"model": "m", "provider": "openai",
                                                 "api_key": "k"}, repeats=99)
check("repeats 被限制在 7 以内", r7["uncertainty"]["repeats_planned"] <= 7,
      r7["uncertainty"]["repeats_planned"])

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
