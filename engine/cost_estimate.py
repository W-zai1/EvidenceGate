"""
评审成本预估。

为什么要估
----------
用真实模型跑一次评审，每个候选人要调模型 **9 次**
（正方立论、反方立论、2 轮×2 方驳斥、裁定裁判、3 位评委）。
不预估的话，一口气丢 30 份简历进去，额度哗哗地就没了，事后才发现。

旧版的问题（必须写明，否则会重犯）
----------------------------------
旧实现里有一句：

    credits = mult * per_candidate_units * n * 0.012

那个 `0.012` 是**手编的经验系数**，没有任何数据支撑，注释自己都承认。
它会给出一个看起来很专业的数字（"预计消耗 8.64 积分"），
但那个数字和真实消耗之间只有巧合关系。这比"不知道"更糟——
人看到精确到小数点的数字会信。

现在的口径
----------
1. **优先用真实数据**：`usage.summary()` 里已有的历史调用量，
   按"每次调用平均 token"折算 → 标注 `basis="实测均值"`。
2. **没有历史数据**：用字符数 × 经验 token 比（1.5 字符/token）估算
   → 标注 `basis="字符数估算"` 且 `confident=False`。
3. **能算钱就算钱**：模型单价登记在 llm_client.PRICING 里就算了报出来，
   没登记就只报 token，不给金额。
4. WorkBuddy 积分模式：只有拿到网关倍率才折算积分，否则只说"约 N 次调用"。

所有返回值都带 `confident` 与 `basis`，前端必须把"不确信"显示出来。
"""
from util import DIMENSIONS  # noqa: F401  （保留导入以表明维度来源统一）

# 角色 -> 输入文本的相对规模（相对一次"立论"）
ROLE_WEIGHTS = {
    "pro_open": 1.0, "con_open": 1.0,
    "pro_rebut": 1.4, "con_rebut": 1.4,   # 驳斥要看完整辩论历史
    "adjudication": 1.3,                  # 裁判要读全部辩论
    "judge": 1.3,                         # 评委读全部材料+辩论
}

# 每候选人调用次数：2 立论 + 2*DEBATE_ROUNDS 驳斥 + 1 裁判 + 3 评委。
# ⚠ 原实现写的是 9，实际是 10——它把"2 轮驳斥"算成了 2 次而不是 4 次
# （每轮正反各一次）。这个错误让每一次成本预估都系统性低估 11%。
CALLS_PER_CANDIDATE = 2 + 2 * 2 + 1 + 3   # = 10
DEFAULT_MULTIPLIER = 0.5                 # 拿不到倍率时的**保守**兜底，明确标注
CHARS_PER_TOKEN = 1.5                    # 中文粗略比（保守估计 token 偏多）
DEFAULT_INPUT_CHARS = 3000                # 每次调用输入规模基准
DEFAULT_OUTPUT_CHARS = 1200               # 每次调用输出规模基准


def _model_multiplier(model=None):
    """取 WorkBuddy 该模型的积分倍率；取不到返回 None。"""
    try:
        import workbuddy_client as wb
        for m in wb.describe_models():
            if m["id"] == model and m.get("multiplier") is not None:
                return float(m["multiplier"])
    except Exception:
        pass
    return None


def _historical_avg_tokens(job_id=None):
    """历史实测：每次调用平均 token。样本不足返回 None。"""
    try:
        import usage
        return usage.calibration_factor(job_id)
    except Exception:
        return None


def estimate(candidate_count, provider="workbuddy", model=None, multiplier=None,
             job_id=None, avg_material_chars=None):
    """估算跑一批评审的消耗。

    返回 dict：{
      provider, model, candidates, calls_per_candidate, total_calls,
      estimated_credits: float|None,   # 仅 WorkBuddy 且拿到倍率时
      estimated_tokens: int|None,
      estimated_cost_usd: float|None,  # 仅当模型单价已登记
      basis: str, confident: bool,
    }
    """
    n = max(int(candidate_count or 0), 0)
    total_calls = n * CALLS_PER_CANDIDATE
    out = {
        "provider": provider,
        "model": model,
        "candidates": n,
        "calls_per_candidate": CALLS_PER_CANDIDATE,
        "total_calls": total_calls,
        "estimated_credits": None,
        "estimated_tokens": None,
        "estimated_cost_usd": None,
        "basis": "",
        "confident": False,
        "known": False,
    }
    if n <= 0:
        out["basis"] = "没有待评审候选人。"
        return out

    # ① 每次调用的 token 量：优先实测，其次字符数折算
    hist = _historical_avg_tokens(job_id)
    if hist and hist.get("avg_tokens_per_call"):
        tokens_per_call = int(hist["avg_tokens_per_call"])
        basis_tok = "按历史实测均值 %d token/次（%d 次采样）" % (
            tokens_per_call, hist.get("samples") or 0)
        confident = (hist.get("samples") or 0) >= 50
    else:
        weight_sum = sum(ROLE_WEIGHTS.values())
        chars = (avg_material_chars or DEFAULT_INPUT_CHARS) * weight_sum \
            + DEFAULT_OUTPUT_CHARS * CALLS_PER_CANDIDATE
        tokens_per_call = int(chars / CHARS_PER_TOKEN / CALLS_PER_CANDIDATE)
        basis_tok = ("按每次调用约 %d 字符粗估（%.1f 字符/token），样本不足，实际可能偏差较大"
                     % (DEFAULT_INPUT_CHARS + DEFAULT_OUTPUT_CHARS, CHARS_PER_TOKEN))
        confident = False

    total_tokens = tokens_per_call * total_calls
    out["estimated_tokens"] = total_tokens

    # ② 折算成钱（仅当单价已登记）
    try:
        import llm_client
        price = llm_client.PRICING.get((model or "").strip().casefold())
        if price:
            # 粗略按 1:3 拆分输入/输出（输出通常短但更贵，这里不追求精确）
            pt = total_tokens * 0.75
            ct = total_tokens * 0.25
            out["estimated_cost_usd"] = round(
                pt / 1e6 * price[0] + ct / 1e6 * price[1], 4)
    except Exception:
        pass

    # ③ WorkBuddy 积分模式
    if provider == "workbuddy":
        mult = multiplier if multiplier is not None else _model_multiplier(model)
        guessed = False
        if mult is None:
            mult = DEFAULT_MULTIPLIER
            guessed = True
        # 积分与 token 的关系由网关定义，我们只知道相对倍率，
        # 因此只在拿到倍率时给出"相对量"，不假装知道绝对积分。
        out["multiplier"] = mult
        out["guessed_multiplier"] = guessed
        out["estimated_credits"] = round(mult * total_tokens / 10000.0, 2) \
            if not guessed else None
        out["known"] = not guessed
    else:
        out["known"] = bool(out["estimated_cost_usd"])

    out["confident"] = confident and out["known"]
    parts = [basis_tok,
             "每候选人 %d 次调用（2 立论 + 2×2 驳斥 + 1 裁判 + 3 评委）" % CALLS_PER_CANDIDATE]
    if out["estimated_cost_usd"] is not None:
        parts.append("按已登记单价估算约 $%.4f" % out["estimated_cost_usd"])
    else:
        parts.append("该模型未登记单价，不折算金额")
    if provider == "workbuddy":
        if out["estimated_credits"] is None:
            parts.append("未从网关读到该模型积分倍率，故**不给出积分数字**（旧版在此处编了一个系数）")
        else:
            parts.append("按网关倍率 x%s 折算" % out["multiplier"])
    if not confident:
        parts.append("⚠ 历史样本不足，本预估仅供量级参考")
    out["basis"] = "；".join(parts)
    return out


def actual_vs_estimate(job_id=None):
    """预估 vs 实际：让"估得准不准"自己暴露出来。

    这是旧版最缺的一环——从来没人核对过预估，所以那个 0.012 一直没被发现是编的。
    """
    try:
        import usage
        s = usage.summary(job_id=job_id)
    except Exception:
        return {"available": False}
    if not s.get("calls"):
        return {"available": False, "message": "还没有真实调用记录，无从对比。"}
    return {
        "available": True,
        "actual_calls": s["calls"],
        "actual_tokens": s["total_tokens"],
        "actual_avg_tokens_per_call": s["avg_tokens_per_call"],
        "estimated_rows": s["estimated_rows"],
        "estimated_share": round((s["estimated_rows"] or 0) / s["calls"], 3),
        "by_model": s["by_model"],
        "message": ("真实记录里有 %d/%d 条没有拿到网关 usage（占比 %.0f%%），"
                    "这部分是按 0 计的，成本报表会偏低。"
                    % (s["estimated_rows"], s["calls"],
                       100.0 * (s["estimated_rows"] or 0) / s["calls"]))
        if s["estimated_rows"] else
        "所有调用都拿到了网关返回的真实 usage。",
    }


def remaining_credits():
    """取 WorkBuddy 当前剩余积分；取不到返回 None（前端可静默跳过）。"""
    try:
        import workbuddy_client as wb
        c = wb.fetch_credits()
        if not c.get("error"):
            return {
                "total_remain": c.get("total_remain"),
                "total_size": c.get("total_size"),
            }
    except Exception:
        pass
    return None
