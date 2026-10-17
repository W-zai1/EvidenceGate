"""
SSE 事件流：把评审过程实时推给前端，并支持断线重放。

旧实现的问题
------------
`server.py` 里用 `queue.Queue` + `_STREAMS[job_id]`，有三个硬伤：

  1. **刷新即失忆**：事件只在队列里，页面一刷新，之前的过程全没了，
     用户只看到空白等结果；
  2. **进程重启即丢失**：正在跑的评审人间蒸发，候选人永远卡在 running；
  3. **单消费者**：同一岗位开两个标签页，事件被两个队列消费者抢走一半。

现在事件先落 `run_events` 表（带自增 seq），SSE 只是"从某个 seq 开始读，
没有新事件就等"。因此：
  · `?after=N` 可以补发所有错过的历史；
  · 多标签页各自读各自的游标，互不干扰；
  · 进程重启后游标仍然有效（事件在库里）。
"""
import json
import time

import runs

HEARTBEAT_SECONDS = 15      # 长时间无事件时发注释帧，防止中间层掐连接
MAX_WAIT_SECONDS = 300      # 单连接最长存活，之后让浏览器自己重连


def _frame(event):
    """SSE 帧格式：event 名 + data JSON。"""
    etype = (event or {}).get("type") or "message"
    data = json.dumps(event or {}, ensure_ascii=False, default=str)
    return "event: %s\ndata: %s\n\n" % (etype, data)


def stream(handler, run_id, after=0):
    """把一个 SSE 连接挂到某个 run 上，从 seq>after 开始推送。

    退出条件：run 结束且事件读完 / 连接断开 / 超时。
    任何情况下都不阻塞请求线程池太久——这是 ThreadingHTTPServer，
    每个连接一个线程，长连接数量天然受客户端数量限制。
    """
    run = runs.get_run(run_id)
    if not run:
        handler.send_error_json("评审记录不存在：%s" % run_id, 404)
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("Connection", "keep-alive")
    # 关掉 Nginx 之类反向代理的缓冲（本地通常没有，但写上无害）
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

    # HTTP/1.1 下 chunked 由我们自己不声明长度来处理：
    # 不设 Content-Length + end_headers 会让 BaseHTTPRequestHandler 认为
    # 这是 HTTP/1.0 风格的"读到 EOF 为止"，配合 Connection: keep-alive
    # 在部分客户端会挂住。因此这里显式关闭长连接语义的歧义。
    wfile = handler.wfile
    cursor = max(int(after or 0), 0)
    started = time.time()
    last_write = time.time()

    def write(chunk):
        nonlocal last_write
        wfile.write(chunk.encode("utf-8"))
        wfile.flush()
        last_write = time.time()

    # 先发一个 hello，让前端立刻知道连接成功（并带上断点位置）
    write("event: hello\ndata: %s\n\n" % json.dumps(
        {"run_id": run_id, "resume_from": cursor,
         "status": run.get("status"), "total": run.get("total"),
         "rubric_version": run.get("rubric_version")}, ensure_ascii=False))

    # 持久事件走数据库轮询（保证补发完整、有序），
    # 流式增量走内存队列（不落库，也就没法从库里补）。
    # 两条通道并存：先补库，再从队列取增量——顺序上完全够用，
    # 因为增量只属于"正在说的那一段"，而那段的全文本随后会以
    # agent_message 落库，重连时照旧能拿到。
    import queue as _queue
    sub = runs.subscribe(run_id)

    try:
        while True:
            events = runs.events_since(run_id, cursor, limit=200)
            if events:
                for ev in events:
                    cursor = ev.get("seq") or cursor
                    write(_frame(ev))
                continue

            # 队列里可能有增量，也可能有刚落库的那些事件的副本（_broadcast 全发）。
            # 后者交给上面的数据库通道写，避免重复；这里只写 ephemeral 的。
            got_delta = False
            try:
                live = sub.get(timeout=0.05)
            except _queue.Empty:
                live = None
            while live is not None:
                if live.get("ephemeral"):
                    write(_frame(live))
                    got_delta = True
                try:
                    live = sub.get_nowait()
                except _queue.Empty:
                    live = None
            if got_delta:
                # 有增量说明此刻正在产出，别去睡 0.25s，赶紧接着收
                continue

            cur = runs.get_run(run_id) or {}
            status = cur.get("status") or ""
            if status in ("done", "failed", "cancelled"):
                # 再捞一次，避免"最后一帧刚好落在结束之后"
                tail = runs.events_since(run_id, cursor, limit=200)
                for ev in tail:
                    cursor = ev.get("seq") or cursor
                    write(_frame(ev))
                write("event: __end__\ndata: %s\n\n" % json.dumps(
                    {"status": status, "cursor": cursor}, ensure_ascii=False))
                break

            if time.time() - started > MAX_WAIT_SECONDS:
                write("event: timeout\ndata: %s\n\n" % json.dumps(
                    {"cursor": cursor, "message": "单次连接超时，浏览器会自动重连（带 after 参数续传）"},
                    ensure_ascii=False))
                break

            if time.time() - last_write > HEARTBEAT_SECONDS:
                # 注释帧：客户端 EventSource 会忽略，但能保住连接
                write(": heartbeat %d\n\n" % int(time.time()))

            time.sleep(0.2)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    except OSError:
        pass
    finally:
        runs.unsubscribe(run_id, sub)
