"""
评审执行器：在后台线程里跑完一个 run，事件落库并广播。

为什么单独成文件
----------------
原来这段逻辑是 `server._run_job`，跑在请求处理线程的 `threading.Thread` 里，
状态全在进程内的 `_STREAMS` 字典：

  · 进程重启 → 正在跑的评审人间蒸发，库里候选人永远卡在 running；
  · 刷新页面 → 事件流断了，过程看不到了；
  · 重复点击 → 两个线程同时跑同一批候选人（双倍花钱，结果互相覆盖）。

现在：
  · 运行状态在 `runs` 表，事件在 `run_events` 表（可重放）；
  · 数据库唯一索引保证同一岗位只能有一个 running；
  · 取消是合作式的：每个候选人边界检查 `cancel_requested`。
"""
import threading
import traceback

from engine.orchestrator import agents
from   engine.storage import db
from   engine.orchestrator import run as run_mod
from engine.orchestrator import runs
from   engine.storage import store
from   engine.orchestrator.util import DIMENSIONS

# 进程内线程句柄（仅用于避免同一 run 被重复 spawn，不作为互斥依据）
_THREADS = {}
_THREADS_LOCK = threading.Lock()


def spawn(run_id, api=None):
    """为一个已登记的 run 起后台线程。同一 run 只起一次。"""
    with _THREADS_LOCK:
        t = _THREADS.get(run_id)
        if t and t.is_alive():
            return t
        t = threading.Thread(target=execute, args=(run_id, api or {}),
                             name="run-%s" % run_id, daemon=True)
        _THREADS[run_id] = t
    t.start()
    return t


def execute(run_id, api=None):
    """执行一个 run。所有状态变更都落库，事件全部持久化。"""
    api = api or {}
    run = runs.get_run(run_id)
    if not run:
        return
    job_id = run["job_id"]
    job = store.get_job(job_id)
    if not job:
        runs.emit(run_id, {"type": "error", "error": "岗位已被删除"})
        runs.finish(run_id, "failed")
        return

    engine_job = _engine_job(job)
    cand_ids = run["candidate_ids"]
    cands = [c for c in store.list_candidates(job_id) if c["id"] in set(cand_ids)]
    if not cands:
        runs.emit(run_id, {"type": "error", "error": "没有可评审的候选人"})
        runs.finish(run_id, "failed")
        return

    runs.emit(run_id, {"type": "run_start", "job": job.get("title"),
                       "total": len(cands), "dimensions": DIMENSIONS,
                       "rubric_version": run.get("rubric_version"),
                       "run_id": run_id,
                       # 口径声明：告诉前端"这批分数能跟什么比"
                       "note": "本批评分口径 %s；与其他口径的分数不可直接比较。"
                               % (run.get("rubric_version") or "未标注")})
    store.update_job(job_id, {"status": "screening"})

    # ------------------------------------------------------------------
    # 候选人级并发
    #
    # 原来是纯串行 for 循环：200 人 × 10 次模型调用，
    # 按每人 25–40 秒算要 1.5–2 小时，而且中途取消只能等当前人跑完。
    # 单个人内部（正反立论→互驳→裁判→三评委）存在真实依赖，不能拆；
    # 但**人与人之间完全独立**，所以并行粒度放在候选人这一层。
    #
    # 为什么不无脑开大：每个候选人在飞时会占 1 个模型连接，
    # 且三位评委本身已经并行。3–5 路是"总在飞调用 ≈ 3×并发数"的量级，
    # 对常见 provider 的限流比较安全。可用 REFINE_CONCURRENCY 覆盖。
    # ------------------------------------------------------------------
    workers = _concurrency(len(cands))
    runs.emit(run_id, {"type": "run_plan", "total": len(cands),
                       "concurrency": workers,
                       "note": "候选人级并行 %d 路；同一候选人内部仍严格串行。" % workers})

    ok_n, fail_n, cancelled = 0, 0, False
    done_n = 0
    state_lock = threading.Lock()
    # 取消后不再启动新任务；已在飞的任务允许跑完（不浪费已花的调用）
    stop = threading.Event()
    # 记下取消发生时"还没开始的"个体，用于回退状态
    skipped = []

    def one(idx, cand):
        """跑一个候选人。返回 (ok, cancelled_flag)。"""
        if stop.is_set():
            return None
        name = cand.get("name") or "候选人"
        runs.emit(run_id, {"type": "candidate_queue", "index": idx, "total": len(cands),
                           "candidate": name, "candidate_id": cand["id"]})
        store.update_candidate(cand["id"], {"status": "running", "error": ""})

        c = {
            "id": cand["id"], "job_id": job_id, "name": name,
            "position": job.get("title") or "",
            "resume_text": cand.get("resume_text") or "",
            "assessment_text": cand.get("assessment_text") or "",
            "transcript_text": cand.get("transcript_text") or "",
            "job": engine_job,
        }
        try:
            # repeats 由环境变量 REFINE_REPEATS 控制（默认 1）。
            # 设为 3 时整场重复 3 次取中位数，用来估计评审的随机波动——
            # 这是"分数有多稳"的唯一直接证据，代价是 3 倍调用。
            res = agents.run_candidate(
                c, engine_job, api=api,
                on_event=lambda ev: runs.emit(run_id, ev))
            store.update_candidate(cand["id"], {
                "status": "done", "result": res, "error": "",
                "rubric_version": res.get("rubric_version") or "",
                # _run_id 不在可写白名单里，只用于把这次评审挂到对应的 run 上；
                # 它是元数据不是候选人字段，所以不落 candidates 表。
                "_run_id": run_id,
            })
            return True
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            print("[runner] %s 评审失败：%s\n%s" % (name, e, tb))
            store.update_candidate(cand["id"], {"status": "failed", "error": str(e)})
            runs.emit(run_id, {"type": "candidate_failed", "candidate": name,
                               "candidate_id": cand["id"], "error": str(e)})
            return False

    def guard(idx, cand):
        nonlocal ok_n, fail_n, done_n
        if stop.is_set():
            with state_lock:
                skipped.append(cand)
            return
        r = one(idx, cand)
        if r is None:
            with state_lock:
                skipped.append(cand)
            return
        with state_lock:
            if r:
                ok_n += 1
            else:
                fail_n += 1
            done_n += 1
            # 每完成一个就看一眼是否被要求取消，及时收手
            if runs.cancel_requested(run_id):
                stop.set()
        runs.emit(run_id, {"type": "run_progress", "done": done_n,
                           "total": len(cands), "succeeded": ok_n, "failed": fail_n})

    if workers <= 1:
        for idx, cand in enumerate(cands, 1):
            guard(idx, cand)
            if stop.is_set():
                cancelled = True
                break
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="cand") as pool:
            futs = [pool.submit(guard, idx, cand) for idx, cand in enumerate(cands, 1)]
            for f in futs:
                f.result()
        cancelled = stop.is_set()

    if cancelled:
        runs.emit(run_id, {"type": "run_cancelled",
                           "after": done_n,
                           "not_started": len(skipped),
                           "message": "已按请求取消；已在跑的候选人跑完后停止，未开始的未计费。"})

    status = "cancelled" if cancelled else ("done" if ok_n or not fail_n else "failed")
    if not cancelled:
        store.update_job(job_id, {"status": "done"})
    runs.emit(run_id, {"type": "run_done", "succeeded": ok_n, "failed": fail_n,
                       "cancelled": cancelled, "status": status,
                       "concurrency": workers})
    runs.finish(run_id, status, ok_n, fail_n)
    _cleanup(run_id)


def _concurrency(n):
    """并发路数：默认 4，环境变量可覆盖，且不超过待跑人数。"""
    import os
    try:
        w = int(os.environ.get("REFINE_CONCURRENCY", "4"))
    except (TypeError, ValueError):
        w = 4
    return max(1, min(w, n or 1))


def _cleanup(run_id):
    with _THREADS_LOCK:
        _THREADS.pop(run_id, None)


def _engine_job(job):
    """把仓储的岗位 dict 转成引擎认的形状（含 title/requirements/weights/rubric）。"""
    base = {
        "id": job.get("id"),
        "title": job.get("title") or "",
        "requirements": job.get("requirements") or [],
        "preferred": job.get("preferred") or [],
        "red_flags": job.get("red_flags") or [],
        "weights": job.get("weights") or {},
        # 评审细则必须传下去：评委提示词靠它，缺了就等于用户改的细则没生效
        "rubric": job.get("rubric") or {},
    }
    try:
        out = run_mod.job_to_engine(job)
        if isinstance(out, dict):
            out.setdefault("rubric", base["rubric"])
            return out
        return base
    except Exception:
        return base


def active_threads():
    with _THREADS_LOCK:
        return {k: v.is_alive() for k, v in _THREADS.items()}
