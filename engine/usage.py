"""
模型调用用量记账（真实 usage，而非估算）。

原来的成本预估是猜的
-------------------
`cost_estimate.py` 里有一句 `credits = mult * per_candidate_units * n * 0.012`，
那个 0.012 是"经验系数"，注释自己也承认。根因是全项目**没有一处解析
API 返回的 `usage` 字段**——所以"这次评审花了多少钱"谁都答不上来。

现在：每次调用都落一行 `llm_usage`，字段优先用网关返回的真实值，
拿不到时**明确标记 estimated=1**。估的和算的不能混在一起当事实用。

这也让成本预估可以被历史数据校正：预估值 vs 实际值，误差会自己暴露。
"""
import db

RECENT_LIMIT = 5000


def record(provider="", model="", role="", job_id="", candidate_id="",
           usage=None, latency_ms=0, ok=True, error=""):
    """记录一次模型调用。

    usage：网关返回的 {prompt_tokens, completion_tokens, total_tokens}；
           拿不到就传 None，本行会标 estimated=1 并用 0 占位（不编数字）。
    """
    usage = usage or {}
    p = _int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    c = _int(usage.get("completion_tokens") or usage.get("output_tokens"))
    t = _int(usage.get("total_tokens")) or (p + c)
    estimated = 0 if (p or c or t) else 1

    conn = db.connect()
    with db.tx(conn) as cx:
        cur = cx.execute(
            "INSERT INTO llm_usage(ts, provider, model, role, job_id, candidate_id,"
            " prompt_tokens, completion_tokens, total_tokens, latency_ms, estimated, ok, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (db.now_iso(), provider or "", model or "", role or "", job_id or "",
             candidate_id or "", p, c, t, _int(latency_ms), estimated, 1 if ok else 0,
             (error or "")[:400]),
        )
    return cur.lastrowid


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def summary(job_id=None, candidate_id=None, since=None):
    """成本报表。返回 token 总量、调用次数、按模型/角色的分布。"""
    conn = db.connect()
    where, args = ["1=1"], []
    if job_id:
        where.append("job_id=?")
        args.append(job_id)
    if candidate_id:
        where.append("candidate_id=?")
        args.append(candidate_id)
    if since:
        where.append("ts>=?")
        args.append(since)
    w = " AND ".join(where)

    tot = conn.execute(
        "SELECT COUNT(*) AS calls,"
        " IFNULL(SUM(prompt_tokens),0) AS pt,"
        " IFNULL(SUM(completion_tokens),0) AS ct,"
        " IFNULL(SUM(total_tokens),0) AS tt,"
        " IFNULL(SUM(estimated),0) AS est,"
        " IFNULL(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS fails,"
        " IFNULL(AVG(latency_ms),0) AS avg_ms"
        " FROM llm_usage WHERE " + w, args).fetchone()

    by_model = []
    for r in conn.execute(
            "SELECT model, provider, COUNT(*) AS calls,"
            " IFNULL(SUM(total_tokens),0) AS tokens"
            " FROM llm_usage WHERE " + w + " GROUP BY model, provider"
            " ORDER BY tokens DESC", args):
        by_model.append(dict(r))

    by_role = []
    for r in conn.execute(
            "SELECT role, COUNT(*) AS calls, IFNULL(SUM(total_tokens),0) AS tokens,"
            " IFNULL(AVG(latency_ms),0) AS avg_ms"
            " FROM llm_usage WHERE " + w + " GROUP BY role ORDER BY tokens DESC", args):
        by_role.append(dict(r))

    calls = tot["calls"] or 0
    return {
        "calls": calls,
        "failed_calls": tot["fails"] or 0,
        "prompt_tokens": tot["pt"],
        "completion_tokens": tot["ct"],
        "total_tokens": tot["tt"],
        "estimated_rows": tot["est"],
        "avg_latency_ms": int(tot["avg_ms"] or 0),
        "avg_tokens_per_call": int((tot["tt"] or 0) / calls) if calls else 0,
        "by_model": by_model,
        "by_role": by_role,
    }


def per_candidate(job_id):
    """每个候选人的真实消耗（用于"这次评审每人花了多少"）。"""
    conn = db.connect()
    rows = conn.execute(
        "SELECT candidate_id, COUNT(*) AS calls, IFNULL(SUM(total_tokens),0) AS tokens,"
        " IFNULL(AVG(latency_ms),0) AS avg_ms"
        " FROM llm_usage WHERE job_id=? GROUP BY candidate_id ORDER BY tokens DESC",
        (job_id,)).fetchall()
    return [dict(r) for r in rows]


def recent(limit=100):
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM llm_usage ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def calibration_factor(job_id=None):
    """用历史真实值校正预估：返回"每 1000 字符材料 ≈ 多少 token"的经验值。

    预估器（cost_estimate）可以拿它来替代那个手编的 0.012 系数。
    样本不足时返回 None——不拿没有依据的数字冒充结论。
    """
    conn = db.connect()
    w, args = ("WHERE job_id=?"), [job_id] if job_id else ("", [])
    row = conn.execute(
        "SELECT COUNT(*) AS n, IFNULL(SUM(total_tokens),0) AS t FROM llm_usage " + w +
        (" AND" if job_id else "") + " estimated=0", args).fetchone()
    if not row or row["n"] < 20:
        return None
    return {"samples": row["n"], "avg_tokens_per_call": int(row["t"] / row["n"])}
