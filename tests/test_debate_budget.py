"""辩论历史预算守卫：超长时怎么裁、裁了有没有留痕、裁判与评委是否看到同一份。

背景：每轮互驳都会把「材料全文 + 完整辩论历史」重新塞一遍，历史随轮次线性增长。
单次调用很容易过万 token，超出窗口的模型会被供应商**静默截断**，
而截断位置由供应商决定——可能正好砍掉反方最关键的质疑，而结论看不出发生过。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp()
from engine.storage import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "debate.db")
db.reset_init_flag()
from engine.intake import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

from engine.orchestrator import agents
from   engine.orchestrator import context
from   engine.api import llm_client
from   engine.storage import store

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


def make_mem(rounds=2, seg_len=800):
    """造一段"很长的"辩论。每段 seg_len 字。"""
    m = context.DebateMemory()
    m.pro = "正" * seg_len
    m.con = "反" * seg_len
    for i in range(rounds):
        m.add_round("正驳%d" % (i + 1) + "x" * (seg_len - 3),
                    "反驳%d" % (i + 1) + "y" * (seg_len - 3))
    return m


print("--- 1. 不超预算时一字不动 ---")
m = make_mem(rounds=1, seg_len=200)
full = m.history_text()
info = m.history_text(with_info=True)[1]
check("默认返回全文", info["truncated"] is False, info)
check("全文包含四个段落", full.count("：\n") >= 4, full.count("：\n"))
trim, info2 = m.history_text(budget=10000, with_info=True)
check("预算充裕时不裁剪", trim == full, (len(trim), len(full)))
check("final == original", info2["final_tokens"] == info2["original_tokens"], info2)

print("\n--- 2. 超预算时按「最近优先」裁剪 ---")
big = make_mem(rounds=2, seg_len=1500)      # 6 段 × 1500 字 ≈ 6000 字
raw_len = len(big.history_text())
raw_tokens = big.history_text(with_info=True)[1]["original_tokens"]
trim, info3 = big.history_text(budget=context.HISTORY_TOKEN_BUDGET, with_info=True)
print("      原文 %d 字 / %d token → 裁剪后 %d 字 / %d token"
      % (raw_len, raw_tokens, len(trim), info3["final_tokens"]))
check("确实超了预算（测试前提成立）", raw_tokens > context.HISTORY_TOKEN_BUDGET,
      (raw_tokens, context.HISTORY_TOKEN_BUDGET))
check("裁剪后落在预算内", info3["final_tokens"] <= context.HISTORY_TOKEN_BUDGET,
      info3["final_tokens"])
check("truncated 标记为真", info3["truncated"] is True, info3)
check("记录了丢弃的段数", info3["dropped_segments"] >= 1, info3)
check("记录了丢弃的字数", info3["dropped_chars"] > 0, info3)
check("最新的那一段（第2轮反方驳斥）完整保留",
      ("反驳2" + "y" * 100) in trim, trim[:80])
check("开头有省略说明", "更早的" in trim[:40] and "已省略" in trim[:60], trim[:60])
check("被截断处有明确标记",
      ("此处已截断" in trim) or info3["dropped_segments"] == info3["kept_segments"],
      trim[-40:])

print("\n--- 3. 存档不受影响（报告/导出仍拿全文） ---")
d = big.to_dict()
check("to_dict 里 pro 是完整原文", len(d["pro"]) == 1500, len(d["pro"]))
check("to_dict 里 rounds 是完整原文",
      all(len(r["pro"]) == 1500 and len(r["con"]) == 1500 for r in d["rounds"]),
      [(len(r["pro"]), len(r["con"])) for r in d["rounds"]])
check("预算只作用于 prompt，不改记忆体本身",
      len(big.history_text()) == raw_len, len(big.history_text()))

print("\n--- 4. 确定性：同样输入给同样结果 ---")
a, ia = big.history_text(budget=1500, with_info=True)
b, ib = big.history_text(budget=1500, with_info=True)
check("同一份历史裁剪两次结果相同", a == b)
check("统计信息也相同", ia == ib)

print("\n--- 5. 极端预算不崩、不返回空壳 ---")
for bud in (1, 10, 60, 200):
    out, inf = big.history_text(budget=bud, with_info=True)
    check("预算 %s 时可返回且不抛错" % bud, isinstance(out, str), out[:20])
tiny = big.history_text(budget=1)
check("预算过小时给出说明而不是空白", len(tiny) > 0 and "省略" in tiny, tiny)

print("\n--- 6. 真实 run_candidate：裁判与评委看到同一份历史 ---")
CAP = {"user": [], "audit": None, "system": []}


def fake_stream(messages, model, api_key, base_url, temperature, use_json,
                timeout, on_delta):
    text = "辩" * 1500
    for ch in text[:20]:
        on_delta(ch)
    return text, {"total_tokens": 10}


def fake_raw(messages, model, api_key, base_url, temperature, use_json, timeout):
    from engine.orchestrator.util import DIMENSIONS as D
    sysmsg = messages[0].get("content") or ""
    user = messages[-1].get("content") or ""
    CAP["system"].append(sysmsg)
    CAP["user"].append(user)
    if "评委" in sysmsg:
        return (json.dumps({D[0]: 8.0, D[1]: 8.0, D[2]: 8.0, "comment": "「原文」"}),
                {"total_tokens": 5})
    if "裁定裁判" in sysmsg:
        return (json.dumps({"pro_evidence": ["「原文」"], "con_challenges": ["「原文」"],
                            "disputes": ["争议"], "excluded": []}), {"total_tokens": 5})
    # 辩论发言（走非流式时的兜底）：同样给足够长的文本
    return "辩" * 1500, {"total_tokens": 10}


import _llmstub
_llmstub.install(fake_raw)
llm_client._stream_raw = fake_stream
try:
    job = store.create_job("预算岗", {"requirements": []})
    ej = {"id": job["id"], "title": "岗位", "requirements": [], "preferred": [],
          "red_flags": [], "weights": None, "rubric": None}
    cand = {"id": "c1", "job_id": job["id"], "name": "测试人",
            "resume_text": "材料" * 100, "assessment_text": "",
            "transcript_text": "", "job": ej}
    # 必须传 on_event：不传的话 _agent_turn 走非流式路径，
    # 那样测不到"每轮只算一次 hist"的流式分支。
    res = agents.run_candidate(cand, ej,
                               api={"model": "m", "provider": "openai", "api_key": "k"},
                               on_event=lambda e: None, repeats=1)
finally:
    _llmstub.restore(fake_raw, fake_stream)

audit = res.get("audit") or {}
db_info = audit.get("debate_budget") or {}
check("结果里记录了辩论预算", isinstance(db_info.get("budget_tokens"), int), db_info)
check("结果里有 debate_truncated 布尔",
      isinstance(audit.get("debate_truncated"), bool), audit.get("debate_truncated"))
check("辩论原文仍完整存档（裁剪只作用于 prompt）",
      len(res["debate"]["pro"]) == 1500, len(res["debate"]["pro"]))

# 用存档的辩论重建一次期望的历史，它必须**原样出现**在裁判与每位评委的输入里。
# 这同时证明两件事：①预算只裁 prompt 不裁存档 ②裁判和评委拿到的是同一份字符串
mem2 = context.DebateMemory()
mem2.pro = res["debate"]["pro"]
mem2.con = res["debate"]["con"]
mem2.rounds = res["debate"]["rounds"]
expected_hist = mem2.history_text(budget=context.HISTORY_TOKEN_BUDGET)

judge_users = [u for u in CAP["user"] if "【原始事实" in u]
adj_users = [u for u in CAP["user"] if "【原始事实" not in u and "评委" not in u]
check("抓到了 3 位评委", len(judge_users) == 3, len(CAP["user"]))
check("评委看到的辩论历史与裁判完全一致",
      all(expected_hist in u for u in judge_users),
      [expected_hist in u for u in judge_users])
check("裁定看到的也是同一份",
      any(expected_hist in u for u in adj_users), len(adj_users))
check("辩论确实被裁剪过（测试前提成立）",
      audit.get("debate_truncated") is True, db_info)

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
