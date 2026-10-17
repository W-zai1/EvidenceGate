"""评审细则与权重：规范化、指纹、落库、进提示词、API 行为。

这一组盯的是"用户改的东西到底有没有生效"——不是"代码里有没有这个词"。
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
db.DB_PATH = os.path.join(TMP, "rub.db")
db.reset_init_flag()
import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

import agents
import llm_client
import rubric
import store
from util import DIMENSIONS

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


D = DIMENSIONS

print("--- 1. 预设本身要自洽 ---")
check("至少 5 组预设（覆盖常见岗位类型）", len(rubric.PRESETS) >= 5,
      len(rubric.PRESETS))
bad_sum = []
for k, p in rubric.PRESETS.items():
    s = sum(p["weights"][d] for d in D)
    if abs(s - 1.0) > 1e-6:
        bad_sum.append((k, round(s, 4)))
check("每组预设权重都归一化到 1", not bad_sum, bad_sum)
missing = [k for k, p in rubric.PRESETS.items()
           if not all(p["criteria"].get(d) for d in D)]
check("每组预设三个维度都有细则文字", not missing, missing)
check("每组预设都解释了「凭什么这么设」",
      all((p.get("why") or "").strip() for p in rubric.PRESETS.values()),
      [k for k, p in rubric.PRESETS.items() if not p.get("why")])

print("\n--- 2. 规范化：脏值不该让页面打不开 ---")
c = rubric.normalize_config({"weights": {D[0]: -50, D[1]: 0, D[2]: 999}})
check("负权重被夹到最小正数", all(c["weights"][d] > 0 for d in D), c["weights"])
check("权重归一化后合计为 1", abs(sum(c["weights"].values()) - 1) < 1e-9, c["weights"])
c2 = rubric.normalize_config({"preset": "不存在的预设"})
check("未知预设退回默认", c2["preset"] == rubric.DEFAULT_PRESET, c2["preset"])
c3 = rubric.normalize_config({"criteria": {D[0]: "x" * 2000}})
check("超长细则被截断到上限", len(c3["criteria"][D[0]]) == rubric.CRITERIA_MAX,
      len(c3["criteria"][D[0]]))
c4 = rubric.normalize_config("不是字典")
check("非 dict 输入不抛错", isinstance(c4, dict) and c4["preset"], c4)
c5 = rubric.normalize_config(None, base_weights={D[0]: 0.5, D[1]: 0.3, D[2]: 0.2})
check("给了 base_weights 就用它（显示=算分）",
      abs(c5["weights"][D[0]] - 0.5) < 1e-6, c5["weights"])

# 回归：小数权重曾被当成百分比夹到最小 1，导致 0.45/0.25/0.30 全变成 1.0，
# 归一化后静默变成 33/33/33——用户设的权重被抹平，界面还显示着他填的值。
cf = rubric.normalize_config({"weights": {D[0]: 0.45, D[1]: 0.25, D[2]: 0.30}})
check("小数写法的权重不被抹平成三等分",
      abs(cf["weights"][D[0]] - 0.45) < 1e-6 and abs(cf["weights"][D[1]] - 0.25) < 1e-6,
      cf["weights"])
cp = rubric.normalize_config({"weights": {D[0]: 45, D[1]: 25, D[2]: 30}})
check("百分比写法与小数写法等价",
      all(abs(cf["weights"][d] - cp["weights"][d]) < 1e-6 for d in D),
      (cf["weights"], cp["weights"]))
check("不同岗位类型的预设权重确实不同",
      abs(rubric.normalize_config({"preset": "engineering"})["weights"][D[0]]
          - rubric.normalize_config({"preset": "sales"})["weights"][D[0]]) > 0.1)

print("\n--- 3. 指纹：改任何一项都要变 ---")
base = rubric.default_config()
f0 = rubric.config_fingerprint(base)
w2 = dict(base["weights"]); w2[D[0]] = 0.6
c2b = dict(base); c2b["weights"] = w2
cr = dict(base); cr["criteria"] = dict(base["criteria"]); cr["criteria"][D[0]] = "只看深度"
nt = dict(base); nt["notes"] = "团队硬规矩"
check("改权重 → 指纹变", rubric.config_fingerprint(c2b) != f0)
check("改细则文字 → 指纹变", rubric.config_fingerprint(cr) != f0)
check("改补充说明 → 指纹变", rubric.config_fingerprint(nt) != f0)
check("什么都不改 → 指纹不变", rubric.config_fingerprint(dict(base)) == f0)

print("\n--- 4. 版本号：两个岗位可以不是同一把尺子 ---")
v_eng = rubric.version_only(rubric_config={"preset": "engineering"})
v_sales = rubric.version_only(rubric_config={"preset": "sales"})
check("不同岗位类型 → 版本号不同", v_eng != v_sales, (v_eng, v_sales))
v_a = rubric.version_only(rubric_config=cr)
check("同类型但细则不同 → 版本号不同", v_a != v_eng, (v_a, v_eng))
check("不传细则与传默认细则一致（不能自相矛盾）",
      rubric.version_only() == rubric.version_only(rubric_config=rubric.default_config()),
      (rubric.version_only(), rubric.version_only(rubric_config=rubric.default_config())))
check("版本号仍以 r 开头且 9 位", v_eng.startswith("r") and len(v_eng) == 9, v_eng)

print("\n--- 5. 落库：细则与权重必须同源 ---")
job = store.create_job("细则测试岗", {"requirements": ["5 年经验"]})
store.update_job(job["id"], {"rubric": {
    "preset": "sales",
    "weights": {D[0]: 30, D[1]: 45, D[2]: 25},
    "criteria": {D[0]: "业绩要能拆到个人贡献", D[1]: "抗压与合规边界", D[2]: "行业资源"},
    "notes": "本科以下不看",
}})
j = store.get_job(job["id"])
check("jd.weights 被细则同步", abs(j["weights"][D[1]] - 0.45) < 1e-6, j["weights"])
check("rubric 一并返回", j["rubric"]["preset"] == "sales", j["rubric"].get("preset"))
check("带可读的预设名", j["rubric"].get("preset_label"), j["rubric"].get("preset_label"))
check("带预设理由", bool(j["rubric"].get("preset_why")))
check("细则文字被保存", "个人贡献" in j["rubric"]["criteria"][D[0]],
      j["rubric"]["criteria"][D[0]])

print("\n--- 6. 细则真的进了提示词（用假模型抓取） ---")
CAPTURE = {"system": [], "user": []}


def fake_complete(messages, **kw):
    """桩掉 llm_client.complete。

    返回形状必须是它文档里写的 {text, usage, model, provider, latency_ms, attempt}
    —— 只返回 (text, usage) 元组会让 agents 里 `out["text"]` 变成
    `tuple[k]` 并抛 TypeError，报错点离真正的原因很远。
    """
    CAPTURE["system"].append(messages[0].get("content") or "")
    CAPTURE["user"].append(messages[-1].get("content") or "")
    sysmsg = messages[0].get("content") or ""
    if "评委" in sysmsg:
        text = json.dumps({D[0]: 8.0, D[1]: 8.0, D[2]: 8.0, "comment": "证据「原文」"})
    elif "裁定裁判" in sysmsg:
        text = json.dumps({"pro_evidence": ["「原文」"], "con_challenges": ["「原文」"],
                           "disputes": ["争议"], "excluded": []})
    else:
        text = "论点，引用「原文」。"
    return {"text": text,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "model": "m", "provider": "openai", "latency_ms": 1, "attempt": 1}


llm_client.complete = fake_complete
cap_job = {
    "id": "j1", "title": "销售", "requirements": [], "preferred": [], "red_flags": [],
    "weights": j["weights"],
    "rubric": {"preset": "sales",
               "weights": j["weights"],
               "criteria": {D[0]: "业绩必须能拆到个人贡献占比",
                            D[1]: "合规边界是否愿意为签单越线",
                            D[2]: "行业资源与语言资质"},
               "notes": "没有可验证的业绩数字一律存疑"},
}
CAPTURE["system"].clear(); CAPTURE["user"].clear()
cand = {"id": "c1", "job_id": "j1", "name": "测试人", "resume_text": "本科，5 年销售。",
        "assessment_text": "", "transcript_text": "", "job": cap_job}
agents.run_candidate(cand, cap_job, api={"model": "m", "provider": "openai", "api_key": "k"},
                     repeats=1)
judge_sys = [s for s in CAPTURE["system"] if "评委" in s]
check("评委提示词里出现了自定义细则",
      any("业绩必须能拆到个人贡献占比" in s for s in judge_sys),
      judge_sys[0][-200:] if judge_sys else "无评委调用")
check("评委提示词里出现了补充说明",
      any("没有可验证的业绩数字一律存疑" in s for s in judge_sys))
check("评委提示词里**没有**出现权重百分比（避免双重施加）",
      not any("%" in s.split("【本岗位的评分细则")[-1] for s in judge_sys),
      [s[-120:] for s in judge_sys][:1])
adj_user = [u for u in CAPTURE["user"] if "评分细则" in u]
check("裁定裁判也拿到了细则", bool(adj_user), len(adj_user))

print("\n--- 7. 没有细则时不留空壳文本 ---")
cap_job2 = dict(cap_job); cap_job2["rubric"] = {}
check("无细则 → 提示词不含细则标题",
      agents.rubric_criteria_block(cap_job2) == "",
      agents.rubric_criteria_block(cap_job2)[:80])
check("job 里没有 rubric 键也不炸",
      agents.rubric_criteria_block({"id": "x"}) == "")

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
