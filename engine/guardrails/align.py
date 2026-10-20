"""
回测对齐：回答"它排的前 N 名里，有几个是我真会推的"。

为什么这个模块是整个项目里最该先有的东西
------------------------------------------
在它之前，系统能告诉你"我给谁打了高分"，但**无法回答它自己的判断准不准**。
所有"分数未标定""不可跨版本比较"的免责声明，本质都是因为这个数字没有被
任何外部标准检验过。没有对齐，"能不能投用"就是个无法回答的问题，
只能靠感觉说"好像还行"。

对齐之后能回答的三件事
----------------------
1. **precision@k**：按分数取前 k 名，其中几个是 HR 真推的。
   这是最贴近实际用法的问题——"我要推 15 个，它能帮我把人找出来吗"。
   也是唯一不要求分数有绝对含义的指标：它只用排序。
2. **recall@k**：HR 真推的人里，有几个落进了前 k。
   单独看 precision 会被"HR 只推了 3 个人"这种情况误导，必须成对看。
3. **lift**：相对"闭眼随机取 k 个"提升了多少倍。用来判断这个排序
   是不是**真的**有信息量，而不是恰好命中几个。
   lift≈1 意味着这个排序跟随机抽没区别——那就不该用它排序。

刻意不做的事
------------
· 不把分数当刻度算"平均分差异"：分数未标定，做这个统计是自欺欺人。
· 不跨 rubric_version 混算：口径不同不可比。批次建好后锁定版本，
  若中途口径变了，只保留同版本的那部分并显式说明。
· 不给"通过/不通过"的阈值建议：那会把排序工具变成自动拒绝器，
  正是这个项目反复警告的误用。

样本纪律
--------
decided 人数 < MIN_SAMPLES 时**不给任何比率**，只报"样本不足"和还差多少人。
宁可让用户觉得这功能没用，也不能给他一个基于 3 个样本的命中率。

MIN_SAMPLES=10 是什么含义
-------------------------
它是一个**下限**，不是"够了"。诚实地说：n=10 时 precision 的 95% 区间
可以宽到 ±30 个百分点——`precision@5=0.8` 在 n=10 下说不了太多。
之所以仍然定在 10 而不是 30：低于 10 连方向都看不出来，
而要求 30 会让绝大多数真实试用（一次招聘往往就十几个决定）永远拿不到任何反馈。
所以这里的设计是——

  **n≥10 给数字，但一定同时给 bootstrap 区间；区间宽就是它在说实话。**

不设"样本够了"的放行阈值，是因为那会诱导用户去凑样本。区间摆在那里，
由使用者自己判断这份证据撑不撑得起他要做的决定。
"""
from engine.storage import db
from   engine.storage import store

# 低于这个已决样本数就不给任何比率——分母太小的百分比是噪音，不是信息。
# 注意 10 是**下限**不是"够了"：n=10 时区间仍有 ±30pp 量级，
# 所以 report() 一定会同时给出 bootstrap 区间，而不是只给一个点估计。
MIN_SAMPLES = 10

DECISIONS = (
    ("advance", "推进面试"),
    ("reject", "淘汰"),
    ("hold", "待定"),
)
DECISION_LABEL = dict(DECISIONS)
POSITIVE = "advance"


# ---------------------------------------------------------------------------
# 建立 / 查询对齐批次
# ---------------------------------------------------------------------------
def create_batch(job_id, label="", source="manual", note="", actor=""):
    """为一个岗位建一个对齐批次。批次用于把"同一次真实决策"圈在一起。"""
    conn = db.connect()
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise ValueError("岗位不存在：%s" % job_id)
    # 锁定当前口径：之后若重跑导致版本变化，比对时只认同版本
    rv = ""
    row = conn.execute(
        "SELECT rubric_version FROM candidates WHERE job_id=? AND "
        "IFNULL(rubric_version,'')<>'' ORDER BY updated_at DESC LIMIT 1", (job_id,)
    ).fetchone()
    if row:
        rv = row["rubric_version"]

    aid = store._new_id("al")
    with db.tx(conn) as c:
        c.execute(
            "INSERT INTO align_runs(id, job_id, label, rubric_version, source, note, "
            "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, job_id, label or "", rv, source or "", note or "", actor or "",
             db.now_iso()))
    return {"id": aid, "job_id": job_id, "rubric_version": rv,
            "label": label, "source": source}


def list_batches(job_id=None):
    conn = db.connect()
    if job_id:
        rows = conn.execute(
            "SELECT * FROM align_runs WHERE job_id=? ORDER BY created_at DESC",
            (job_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM align_runs ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["truth_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM align_truth WHERE align_id=?", (r["id"],)
        ).fetchone()["n"]
        out.append(d)
    return out


def get_batch(align_id):
    conn = db.connect()
    r = conn.execute("SELECT * FROM align_runs WHERE id=?", (align_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["truth"] = [dict(x) for x in conn.execute(
        "SELECT * FROM align_truth WHERE align_id=? ORDER BY created_at", (align_id,))]
    return d


# ---------------------------------------------------------------------------
# 导入真实决定（CSV / 粘贴）
# ---------------------------------------------------------------------------
# 表头识别：容忍常见的各种叫法，不强迫用户改表。
_HEAD_NAME = ("姓名", "名字", "候选人", "name", "candidate")
_HEAD_DEC = ("决定", "结果", "决策", "状态", "是否面试", "面试", "建议",
             "decision", "result", "status", "outcome")
_HEAD_NOTE = ("备注", "说明", "理由", "note", "comment", "reason")

# 把五花八门的写法映射到三个决定
_ADVANCE_WORDS = ("推进", "通过", "面试", "是", "y", "yes", "true", "1",
                  "advance", "pass", "shortlist", "进面", "约面", "推荐")
_REJECT_WORDS = ("淘汰", "拒绝", "不通过", "否", "n", "no", "false", "0",
                 "reject", "fail", "不合适", "pass掉")
_HOLD_WORDS = ("待定", "存疑", "再看", "不确定", "hold", "pending", "maybe", "")


def _pick(headers, cands):
    for h in headers:
        key = (h or "").strip().casefold()
        for c in cands:
            if c in key:
                return h
    return None


def _parse_decision(raw):
    """把单元格内容归一到 advance / reject / hold。识别不了返回 None。"""
    s = (raw or "").strip().casefold()
    if not s:
        return "hold"
    for w in _HOLD_WORDS:
        if w and w in s:
            return "hold"
    for w in _ADVANCE_WORDS:
        if w in s:
            return "advance"
    for w in _REJECT_WORDS:
        if w in s:
            return "reject"
    return None


def parse_csv(text, sep=None):
    """解析 CSV/TSV/粘贴文本，返回 (行列表, 错误列表)。

    零依赖：不引 csv 之外的任何东西（csv 是标准库）。
    自动探测分隔符（逗号/制表符/分号），因为从 Excel 复制出来常是制表符。
    """
    import csv
    import io

    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        return [], [{"row": 0, "error": "内容为空"}]

    if sep is None:
        first = text.split("\n", 1)[0]
        if "\t" in first:
            sep = "\t"
        elif first.count(";") > first.count(","):
            sep = ";"
        else:
            sep = ","

    rows = list(csv.reader(io.StringIO(text), delimiter=sep))
    if not rows:
        return [], [{"row": 0, "error": "内容为空"}]

    headers = [h.strip() for h in rows[0]]
    h_name = _pick(headers, _HEAD_NAME)
    h_dec = _pick(headers, _HEAD_DEC)
    h_note = _pick(headers, _HEAD_NOTE)
    errors = []
    if not h_name:
        return [], [{"row": 1, "error": "缺少姓名列（可用表头：姓名 / name / 候选人）"}]
    if not h_dec:
        return [], [{"row": 1, "error": "缺少决定列（可用表头：决定 / 结果 / decision）"}]

    idx_name = headers.index(h_name)
    idx_dec = headers.index(h_dec)
    idx_note = headers.index(h_note) if h_note else -1

    out = []
    for ln, row in enumerate(rows[1:], 2):
        if not any((c or "").strip() for c in row):
            continue                      # 跳过空行
        name = (row[idx_name] if idx_name < len(row) else "").strip()
        raw_dec = (row[idx_dec] if idx_dec < len(row) else "").strip()
        note = (row[idx_note] if 0 <= idx_note < len(row) else "").strip()
        if not name:
            errors.append({"row": ln, "error": "姓名为空，已跳过"})
            continue
        dec = _parse_decision(raw_dec)
        if dec is None:
            errors.append({"row": ln, "name": name,
                           "error": "无法识别的决定：%r（请用 推进/淘汰/待定）" % raw_dec})
            continue
        out.append({"name": name, "decision": dec, "raw": raw_dec, "note": note,
                    "row": ln})
    return out, errors


def import_truth(align_id, rows, actor=""):
    """把解析好的行写进 align_truth，并尽力关联到候选人。

    关联策略：按 norm_name 匹配该岗位下的候选人。
      · 命中 1 个 → 直接绑 candidate_id
      · 命中多个（同名）→ 不猜，绑 person_id 留空并标记 ambiguous，
        因为"同名两个人推了谁"必须由人来回答，机器猜错会污染整个回测
      · 未命中 → 仍然入库（这人可能根本没进过系统），candidate_id 留空
    """
    from engine.extract import identity

    conn = db.connect()
    batch = conn.execute("SELECT * FROM align_runs WHERE id=?", (align_id,)).fetchone()
    if not batch:
        raise ValueError("对齐批次不存在")
    job_id = batch["job_id"]

    cands = conn.execute(
        "SELECT id, name FROM candidates WHERE job_id=?", (job_id,)).fetchall()
    by_key = {}
    for c in cands:
        by_key.setdefault(identity.norm_name(c["name"]), []).append(c["id"])

    added, updated, ambiguous, unmatched = 0, 0, [], []
    with db.tx(conn) as c:
        for r in rows:
            nk = identity.norm_name(r["name"])
            ids = by_key.get(nk) or []
            cid = ids[0] if len(ids) == 1 else ""
            if len(ids) > 1:
                ambiguous.append({"name": r["name"], "candidates": ids})
            if not ids:
                unmatched.append(r["name"])
            exist = c.execute(
                "SELECT id FROM align_truth WHERE align_id=? AND name_key=?",
                (align_id, nk)).fetchone()
            if exist:
                c.execute(
                    "UPDATE align_truth SET decision=?, note=?, name_raw=?, "
                    "candidate_id=? WHERE id=?",
                    (r["decision"], r.get("note") or "", r["name"], cid, exist["id"]))
                updated += 1
            else:
                c.execute(
                    "INSERT INTO align_truth(id, align_id, candidate_id, person_id, "
                    "name_key, name_raw, decision, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (store._new_id("at"), align_id, cid, "", nk, r["name"],
                     r["decision"], r.get("note") or "", db.now_iso()))
                added += 1

    return {"added": added, "updated": updated,
            "ambiguous": ambiguous, "unmatched": unmatched,
            "total_truth": added + updated}


def pending_bindings(align_id):
    """列出还没绑到具体候选人的真实决定。

    两类：
      · ambiguous —— 同名多人，机器不敢猜（这是**正确**行为，不是失败）；
      · unmatched —— 系统里根本没这个人（没投过、或名字对不上）。
    两类都必须能被人接走，否则这些行永远无法参与回测，
    而它们往往恰恰是"被拒的那批"，也就是最需要被检验的部分。
    """
    from engine.extract import identity

    conn = db.connect()
    batch = conn.execute("SELECT * FROM align_runs WHERE id=?", (align_id,)).fetchone()
    if not batch:
        raise ValueError("对齐批次不存在")
    job_id = batch["job_id"]
    cands = conn.execute(
        "SELECT id, name, status, rubric_version FROM candidates WHERE job_id=?",
        (job_id,)).fetchall()
    by_key = {}
    for c in cands:
        by_key.setdefault(identity.norm_name(c["name"]), []).append(c)

    out = {"ambiguous": [], "unmatched": [], "bound": 0}
    for t in conn.execute(
            "SELECT * FROM align_truth WHERE align_id=? ORDER BY name_key",
            (align_id,)).fetchall():
        if t["candidate_id"]:
            out["bound"] += 1
            continue
        ids = by_key.get(t["name_key"]) or []
        item = {"truth_id": t["id"], "name": t["name_raw"] or t["name_key"],
                "decision": t["decision"], "name_key": t["name_key"]}
        if len(ids) > 1:
            item["options"] = [{"id": c["id"], "name": c["name"],
                                "status": c["status"],
                                "rubric_version": c["rubric_version"]}
                               for c in ids]
            out["ambiguous"].append(item)
        else:
            out["unmatched"].append(item)
    return out


def bind_truth(truth_id, candidate_id, actor=""):
    """把一个真实决定手工绑到指定候选人。

    这是 `ambiguous` 的**唯一**出口：同名两人时机器不猜，由人指定。
    绑定动作进审计链——因为绑定本身会改变回测结论（把 A 的决定算到 B 头上，
    命中率就变了），所以必须能追溯"是谁绑的、绑给了谁"。

    校验：候选人必须与该批次同岗位。否则可以把别的岗位的人塞进来，
    让 precision 看起来变好——这是个真实的作弊面。
    """
    from engine.guardrails import audit

    conn = db.connect()
    t = conn.execute("SELECT * FROM align_truth WHERE id=?", (truth_id,)).fetchone()
    if not t:
        raise ValueError("真实决定记录不存在：%s" % truth_id)
    batch = conn.execute("SELECT * FROM align_runs WHERE id=?",
                         (t["align_id"],)).fetchone()
    if not batch:
        raise ValueError("对齐批次不存在")
    cand = conn.execute("SELECT * FROM candidates WHERE id=?",
                        (candidate_id,)).fetchone()
    if not cand:
        raise ValueError("候选人不存在：%s" % candidate_id)
    if cand["job_id"] != batch["job_id"]:
        raise ValueError("候选人不在该对齐批次对应的岗位下，拒绝绑定"
                         "（跨岗位绑定会污染命中率）。")

    old = t["candidate_id"] or ""
    with db.tx(conn) as c:
        c.execute("UPDATE align_truth SET candidate_id=? WHERE id=?",
                  (candidate_id, truth_id))
    audit.record(conn, "align.bind", actor=actor or "system",
                 target_type="align_truth", target_id=truth_id,
                 detail={"align_id": t["align_id"], "name": t["name_raw"],
                         "candidate_id": candidate_id,
                         "candidate_name": cand["name"], "previous": old})
    return {"truth_id": truth_id, "candidate_id": candidate_id,
            "candidate_name": cand["name"], "previous": old}


# ---------------------------------------------------------------------------
# 核心：precision@k / recall@k / lift
# ---------------------------------------------------------------------------
def _bootstrap_curves(decisions, ks, positives, n_resamples=1000, seed=20260101,
                      lo=5, hi=95):
    """一次重抽样，同时给出多个 k 的置信区间。

    为什么要批量：`report()` 会对 5/10/20… 每个 k 各调一次。原来每个 k 都要
    重新抽 1000 次再全量排序，复杂度是 O(len(ks) · n_resamples · n log n)。
    200 人时无所谓，千人规模就变成几十万次排序。这里抽一批样本、排一次序，
    对每个 k 只做切片和计数——同样的区间，成本降到 1/len(ks)。

    抽样的随机性只由 seed 决定，因此同一份数据永远给出同一组区间
    （"结论不能取决于跑了几次"）。
    """
    import random

    n = len(decisions)
    out = {}
    if n < MIN_SAMPLES:
        return out
    ks = [int(k) for k in ks if int(k) > 0]
    if not ks:
        return out
    rng = random.Random(seed)
    # per_k[k] = 该 k 下每次重抽样的 precision 序列
    per_k = {k: [] for k in ks}
    for _ in range(int(n_resamples)):
        sample = [decisions[rng.randrange(n)] for _ in range(n)]
        sample.sort(key=lambda x: (-x["total"], x["cid"]))
        for k in ks:
            top = sample[:min(k, n)]
            per_k[k].append(
                sum(1 for p in top if p["decision"] == POSITIVE) / len(top))
    for k in ks:
        vals = sorted(per_k[k])
        m = len(vals)
        lo_v = vals[max(0, int(m * lo / 100.0))]
        hi_v = vals[min(m - 1, int(m * hi / 100.0))]
        out[k] = {"lo": round(lo_v, 4), "hi": round(hi_v, 4),
                  "n_resamples": int(n_resamples),
                  "level": "%d%%–%d%%" % (lo, hi),
                  "note": "对已决人群有放回重抽样估计；"
                          "区间宽说明样本不足以支撑精细结论。"}
    return out


def _bootstrap_ci(decisions, kk, positives, n_resamples=1000, seed=20260101,
                  lo=5, hi=95):
    """单个 k 的 bootstrap 置信区间。

    为什么必须有它
    --------------
    `precision@5 = 0.8` 这句话在 n=20 时看起来像"八成准"，
    但它的 95% 区间大约是 [0.4, 0.96]——也就是说"其实只有四成准"
    和"几乎全对"都在这份数据能支持的范围内。只报点估计，
    使用者就会把 0.8 当成一个确定的性质，而它只是个不稳定的估计。

    没有区间，`lift` 的比较也会失效：lift=2.67 听起来很强，
    但如果随机基线的波动本身就有这个量级，那这个数字什么都没证明。

    做法
    ----
    对**已决人群**做有放回重抽样（bootstrap），每次重算 precision@kk。
    · 抽样单位是"人"，不是"命中"——人的决定是观测单位，
      这样才保留了"正例占比"本身的不确定性。
    · 用固定 seed：同一份数据必须给出同一个区间，否则又变成
      "结论取决于跑了几次"。这不是统计上的必需，而是工程上的必需。
    · 只依赖标准库 random。
    · 实现委托给 `_bootstrap_curves`（一次抽样算多个 k），
      单 k 与批量的抽样序列完全一致，所以数值不变。

    返回 {"lo":..,"hi":..,"n_resamples":..,"note":..}；
    样本太小（<MIN_SAMPLES）时返回 None，不假装能估出区间。
    """
    return _bootstrap_curves(decisions, [kk], positives,
                             n_resamples=n_resamples, seed=seed,
                             lo=lo, hi=hi).get(int(kk))


def report(align_id, ks=None):
    """算对齐报告。只使用有分数的候选人与已决的真实结果。"""
    from engine.extract import identity

    conn = db.connect()
    batch = conn.execute("SELECT * FROM align_runs WHERE id=?", (align_id,)).fetchone()
    if not batch:
        raise ValueError("对齐批次不存在")
    job_id = batch["job_id"]
    locked_rv = (batch["rubric_version"] or "").strip()

    # 候选人及其分数
    cands = conn.execute(
        "SELECT id, name, status, result_json, rubric_version FROM candidates "
        "WHERE job_id=?", (job_id,)).fetchall()

    scored = []          # (total, candidate_id, name, rubric_version)
    for c in cands:
        res = db.jload(c["result_json"] or "{}", {}) or {}
        total = res.get("total")
        if isinstance(total, (int, float)):
            scored.append({"cid": c["id"], "name": c["name"],
                           "total": float(total),
                           "rubric_version": (c["rubric_version"] or "").strip()})
    # 同分必须有确定性的次序。踩过的坑：只按 -total 排序时，Python 的
    # 稳定排序会保持输入顺序（即 created_at ASC），看似正常；
    # 但一旦上游改了查询顺序，同一份数据算出来的 precision@k 会跟着变，
    # 回测结论就成了"取决于代码怎么写的"而不是"取决于数据"。
    # 这里用 (分数降序, 候选人 id 升序) 作为全序，保证可复现。
    scored.sort(key=lambda x: (-x["total"], x["cid"]))

    # 真实结果
    truth = conn.execute(
        "SELECT name_key, name_raw, decision, candidate_id FROM align_truth "
        "WHERE align_id=?", (align_id,)).fetchall()

    # 名字命中**多个**候选人时，按名字回退会污染回测。
    #
    # 踩过的坑：原来只建 truth_by_key[name_key] → decision，然后
    # `dec = truth_by_cid.get(cid) or truth_by_key.get(nk)`。当两个同名
    # 候选人（张伟 A / 张伟 B）存在、而真实决定只写了一次「张伟，推进面试」时，
    # 这条**未绑定**的记录会同时命中两个人——两个人都被算成正例，
    # 命中率被凭空抬高。这正是"同名不能当同一人"要防的事，
    # 却在报告层漏了过去（导入层已经标了 ambiguous，但报告层没用上）。
    #
    # 现在：同名多人时只认 candidate_id 显式绑定，名字回退一律不生效。
    name_count = {}
    for s in scored:
        nk = identity.norm_name(s["name"])
        name_count[nk] = name_count.get(nk, 0) + 1

    truth_by_key = {}
    truth_by_cid = {}
    skipped_ambiguous_keys = set()
    for t in truth:
        nk = t["name_key"]
        if t["candidate_id"]:
            truth_by_cid[t["candidate_id"]] = t["decision"]
        elif name_count.get(nk, 0) > 1:
            # 同名多人且未绑定 → 不能用它给任何人下结论
            skipped_ambiguous_keys.add(nk)
        else:
            truth_by_key[nk] = t["decision"]

    # 给每个已评分候选人标上真实决定
    poolled = []
    rv_mismatch = 0
    for s in scored:
        nk = identity.norm_name(s["name"])
        dec = truth_by_cid.get(s["cid"])
        if dec is None and nk not in skipped_ambiguous_keys:
            dec = truth_by_key.get(nk)
        if dec is None:
            continue                       # 没有真实结果的人不参与命中率计算
        if locked_rv and s["rubric_version"] and s["rubric_version"] != locked_rv:
            rv_mismatch += 1
            continue                       # 跨口径不可比，剔除并报告
        poolled.append({"cid": s["cid"], "name": s["name"], "total": s["total"],
                        "decision": dec})
    poolled.sort(key=lambda x: (-x["total"], x["cid"]))

    decided = [p for p in poolled if p["decision"] in ("advance", "reject")]
    positives = [p for p in decided if p["decision"] == POSITIVE]

    base = {
        "align_id": align_id,
        "job_id": job_id,
        "label": batch["label"],
        "locked_rubric_version": locked_rv,
        "scored_total": len(scored),
        "matched": len(poolled),
        "decided": len(decided),
        "positives": len(positives),
        "excluded_rubric_mismatch": rv_mismatch,
        "min_samples": MIN_SAMPLES,
    }

    # 有多少条真实决定因为"同名/对不上人"而没能参与计算。
    # 不报这个数字的话，用户会以为 precision 是基于他导入的全部决定算的，
    # 实际上可能有一半被静默跳过了——那结论就是在一个不可见的子集上得出的。
    pending = pending_bindings(align_id)
    base["pending_bindings"] = {
        "ambiguous": len(pending["ambiguous"]),
        "unmatched": len(pending["unmatched"]),
        "bound": pending["bound"],
        "note": ("有 %d 条决定因为同名无法确定是谁、%d 条在系统里找不到对应候选人，"
                 "未参与计算。可在「待绑定」里手工指认。"
                 % (len(pending["ambiguous"]), len(pending["unmatched"]))
                 if (pending["ambiguous"] or pending["unmatched"]) else ""),
    }

    if len(decided) < MIN_SAMPLES:
        base.update({
            "sufficient": False,
            "message": "已决样本 %d 人，少于 %d 人，不给结论。"
                       "（分母太小的百分比是噪音）" % (len(decided), MIN_SAMPLES),
            "need_more": MIN_SAMPLES - len(decided),
        })
        return base

    if not positives:
        base.update({"sufficient": False,
                     "message": "这批里没有任何人被标为「推进面试」，"
                                "命中率无定义。请确认决定列填对了。"})
        return base

    total_n = len(poolled)
    if ks is None:
        # 默认看几个有实际意义的规模：HR 真推的人数、以及常见的 5/10/20
        ks = sorted({len(positives), 5, 10, 20})
    ks = [k for k in ks if k > 0]

    curves = []
    # 区间一次算完：所有 k 共用同一批重抽样（见 _bootstrap_curves 的说明）。
    # 顺带让不同 k 的区间彼此可比——它们来自同一组样本，而不是各自独立抽的。
    ks_clamped = sorted({min(k, total_n) for k in ks})
    ci_map = _bootstrap_curves(decided, ks_clamped, len(positives))
    for k in ks:
        kk = min(k, total_n)
        if kk <= 0:
            continue
        top = poolled[:kk]
        hit = sum(1 for p in top if p["decision"] == POSITIVE)
        precision = hit / kk
        recall = hit / len(positives)
        # 随机基线：从 total_n 里抽 kk 个，期望命中比例 = 正例占比
        base_rate = len(positives) / total_n
        lift = (precision / base_rate) if base_rate > 0 else None
        curves.append({
            "k": kk,
            "hits": hit,                       # 前 kk 名里有几个真推的
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "lift": (round(lift, 3) if lift is not None else None),
            "random_expected": round(base_rate * kk, 2),
            # 点估计没有区间就是半个结论。见 _bootstrap_ci 的说明。
            "precision_ci": ci_map.get(kk),
        })

    # 完整排序曲线（用于画"取前 k 个能捞到多少"的图）
    cum, curve = 0, []
    for i, p in enumerate(poolled, 1):
        if p["decision"] == POSITIVE:
            cum += 1
        curve.append({"rank": i, "hits": cum,
                      "recall": round(cum / len(positives), 4)})

    base.update({
        "sufficient": True,
        "at": curves,
        "curve": curve,
        "base_rate": round(len(positives) / total_n, 4),
        # 直接给一句话结论，避免用户盯着表格自己猜
        "verdict": _verdict(curves, len(positives), total_n),
    })
    return base


def _verdict(curves, n_pos, n_total):
    """把数字翻译成一句人话。不给出"能不能投用"的裁决——
    那需要结合岗位、后果承受度、以及被误判者的代价，不是统计能决定的。"""
    if not curves:
        return ""
    # 取最接近"HR 实际推的名额"的那个 k
    target = min(curves, key=lambda c: abs(c["k"] - n_pos))
    lift = target.get("lift")
    if lift is None:
        return ""
    if lift >= 2.0:
        tone = "排序有明显信息量"
    elif lift >= 1.3:
        tone = "排序有正的信息量，但不算强"
    elif lift >= 0.9:
        tone = "排序跟随机抽差别不大"
    else:
        tone = "排序比随机抽还差——不要用它排优先级"

    # 区间必须和点估计一起出现。只报 "precision=80%" 会让人以为
    # 八成这个数字是稳的；实际上 n=20 时它的区间可能宽到 [40%, 96%]。
    ci = target.get("precision_ci")
    ci_txt = ""
    if ci:
        ci_txt = "（%s 区间 %.0f%%–%.0f%%）" % (
            ci.get("level", "95%"), 100.0 * ci["lo"], 100.0 * ci["hi"])
        # 区间跨过随机基线时，lift 的说法就不成立，必须直说
        base = target.get("random_expected", 0) / max(target["k"], 1)
        if ci["lo"] <= base * 1.15:
            tone += "；但区间下沿已接近随机基线，样本还不足以确认这个排序有效"
    return ("前 %d 名命中 %d 人（HR 实际推了 %d 人，占 %.0f%%），"
            "precision=%.0f%%%s、recall=%.0f%%，相对随机 %s×。%s。"
            % (target["k"], target["hits"], n_pos, 100.0 * n_pos / max(n_total, 1),
               100.0 * target["precision"], ci_txt,
               100.0 * target["recall"],
               ("%.2f" % lift), tone))


# ---------------------------------------------------------------------------
# 候选人级决策（人做的决定，必须带理由）
# ---------------------------------------------------------------------------
DECIDE_MIN_REASON = 4       # 理由至少 4 个字符，挡住"ok""1""."这类敷衍


def decide(candidate_id, decision, reason, actor=""):
    """记录一个人对候选人的决定。理由必填——这是可申诉性的最低要求。

    合规动机：PIPL 第 24 条要求自动化决策可解释、可拒绝。
    只要"淘汰"这个动作是人做的、且理由被记录，系统就只是辅助工具；
    如果决策能不留理由地被批量打掉，它事实上就成了自动化拒绝器。
    """
    from engine.guardrails import audit

    if decision not in ("advance", "reject", "hold"):
        raise ValueError("决定只能是 advance / reject / hold")
    reason = (reason or "").strip()
    if len(reason) < DECIDE_MIN_REASON:
        raise ValueError("必须填写理由（至少 %d 个字）：淘汰/推进都要能说明依据。"
                         % DECIDE_MIN_REASON)

    conn = db.connect()
    row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row:
        raise ValueError("候选人不存在")
    with db.tx(conn) as c:
        c.execute(
            "UPDATE candidates SET decision=?, decision_reason=?, decided_by=?, "
            "decided_at=?, updated_at=? WHERE id=?",
            (decision, reason, actor or "", db.now_iso(), db.now_iso(), candidate_id))
        audit.record(c, "candidate.decide", actor=actor or "system",
                     target_type="candidate", target_id=candidate_id,
                     detail={"decision": decision, "reason": reason,
                             "name": row["name"]})
    return {"id": candidate_id, "decision": decision, "reason": reason,
            "decided_by": actor or "", "decided_at": db.now_iso()}


def decisions_of(job_id):
    """某岗位下所有人做过的决定，用于列表展示与复核。"""
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, name, status, decision, decision_reason, decided_by, decided_at, "
        "rubric_version, result_json FROM candidates WHERE job_id=? "
        "AND IFNULL(decision,'')<>'' ORDER BY decided_at DESC", (job_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        res = db.jload(r["result_json"] or "{}", {}) or {}
        d["total"] = res.get("total")
        d["grade"] = res.get("grade")
        d.pop("result_json", None)
        out.append(d)
    return out
