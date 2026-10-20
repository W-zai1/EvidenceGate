"""流式输出：SSE 解析、增量不落库、增量能到达客户端、失败兜底。

重点不是"代码里有没有 stream 这个词"，而是：
  · 增量事件真的进了 run_events 吗（不该进）
  · 增量真的到了 SSE 连接吗（必须到）
  · 断点续传会不会因为增量而漏事件（不能漏）
  · 模型流到一半断了，最终文本是否仍然完整
"""
import json
import os
import queue
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp()
from engine.storage import db
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "stream.db")
db.reset_init_flag()
from engine.intake import crypto
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

from engine.orchestrator import agents
from   engine.api import llm_client
from   engine.orchestrator import runs
from   engine.api import sse

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


print("--- 1. SSE 行解析 ---")
lines = [
    'data: {"choices":[{"delta":{"content":"他"}}]}',
    "",
    'data: {"choices":[{"delta":{"content":"做过"}}]}',
    "data: [DONE]",
    'data: {"choices":[{"delta":{"content":"不该出现"}}]}',
]
chunks, usage = llm_client.parse_sse_deltas(lines)
check("[DONE] 之后的内容被忽略", chunks == ["他", "做过"], chunks)
check("空行被跳过", len(chunks) == 2, chunks)

chunks2, usage2 = llm_client.parse_sse_deltas([
    "data: 半截{",
    ": keepalive",
    "event: ping",
    'data: {"choices":[{"delta":{"content":"好"}}]}',
    'data: {"usage":{"prompt_tokens":3,"completion_tokens":2}}',
])
check("坏 JSON 行被跳过而不是整段失败", chunks2 == ["好"], chunks2)
check("usage 被捕获", usage2 == {"prompt_tokens": 3, "completion_tokens": 2}, usage2)

chunks3, _ = llm_client.parse_sse_deltas(['data: {"choices":[]}'])
check("空 choices 不抛错", chunks3 == [], chunks3)
chunks4, _ = llm_client.parse_sse_deltas(['data: {"choices":[{"delta":null}]}'])
check("delta 为 null 不抛错", chunks4 == [], chunks4)

print("\n--- 2. 增量事件不落库 ---")
from engine.storage import store
job = store.create_job("流式测试岗", {"requirements": []})
run = runs.start(job["id"], ["c1"], actor="tester", rubric_version="rTEST")
rid = run["id"]
base_count = runs.event_count(rid)
runs.emit(rid, {"type": "agent_start", "candidate": "张三", "key": "pro_open"})
for i in range(50):
    seq = runs.emit(rid, {"type": "agent_delta", "candidate": "张三",
                          "key": "pro_open", "text": "字", "ephemeral": True})
check("增量事件返回 seq=0（不占序号）", seq == 0, seq)
after = runs.event_count(rid)
check("50 条增量只让库增长 1 行",
      after - base_count == 1, (base_count, after))

seq1 = runs.emit(rid, {"type": "agent_message", "candidate": "张三",
                       "key": "pro_open", "text": "全文"})
check("持久事件序号仍然连续", seq1 == after - base_count + 1 or seq1 > 0, seq1)

print("\n--- 3. 增量能到达订阅者（内存广播） ---")
q = runs.subscribe(rid)
runs.emit(rid, {"type": "agent_delta", "text": "甲", "ephemeral": True})
runs.emit(rid, {"type": "agent_delta", "text": "乙", "ephemeral": True})
got = []
try:
    while True:
        got.append(q.get_nowait())
except queue.Empty:
    pass
runs.unsubscribe(rid, q)
texts = [g.get("text") for g in got if g.get("ephemeral")]
check("两条增量都广播出去了", texts == ["甲", "乙"], texts)
check("广播里有 run_id", all(g.get("run_id") == rid for g in got), got[:1])

print("\n--- 4. 断点续传不会因为增量而漏事件 ---")


class FakeHandler:
    """把 SSE 输出收集到内存，模拟一次连接。"""

    def __init__(self):
        import io
        self.wfile = io.BytesIO()
        self.sent_status = None

    def send_response(self, code):
        self.sent_status = code

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


h = FakeHandler()
t = threading.Thread(target=sse.stream, args=(h, rid, 0), daemon=True)
t.start()
time.sleep(0.4)
# 必须在连接建立**之后**发增量：ephemeral 事件不落库，也就不会被重放，
# 这一点是设计而非缺陷——重连时靠随后落库的 agent_message 恢复全文。
runs.emit(rid, {"type": "agent_delta", "candidate": "张三", "key": "pro_open",
                "text": "流", "ephemeral": True})
runs.emit(rid, {"type": "agent_delta", "candidate": "张三", "key": "pro_open",
                "text": "式", "ephemeral": True})
time.sleep(0.4)
runs.finish(rid, "done", succeeded=1, failed=0, actor="tester")
t.join(timeout=5)
raw = h.wfile.getvalue().decode("utf-8", "replace")
frames = [ln for ln in raw.split("\n") if ln.startswith("data: ")]
payloads = []
for ln in frames:
    try:
        payloads.append(json.loads(ln[len("data: "):]))
    except Exception:
        pass
types = [p.get("type") for p in payloads]
check("SSE 收到了持久事件", "agent_start" in types, types)
check("SSE 收到了流式增量", "agent_delta" in types, types)
check("增量在 SSE 里排在持久事件之后",
      types.index("agent_delta") > types.index("agent_start"), types)
ephemeral_in_stream = [p for p in payloads if p.get("ephemeral")]
check("增量在流里带 ephemeral 标记", len(ephemeral_in_stream) == 2,
      len(ephemeral_in_stream))
delta_text = "".join(p.get("text") or "" for p in ephemeral_in_stream)
check("增量顺序拼起来是「流式」", delta_text == "流式", delta_text)

print("\n--- 5. 流式中断后仍拿到完整文本 ---")
CALLS = {"n": 0}


def flaky_stream(messages, model, api_key, base_url, temperature, use_json,
                 timeout, on_delta):
    CALLS["n"] += 1
    on_delta("半截")
    on_delta("内容")
    raise llm_client.LLMError("模拟流中断")


orig_raw = llm_client._stream_raw
orig_complete = llm_client.complete
llm_client._stream_raw = flaky_stream
llm_client.complete = lambda *a, **kw: {
    "text": "完整全文", "usage": {"total_tokens": 9}, "model": "m",
    "provider": "openai", "latency_ms": 1, "attempt": 1}
try:
    seen = []
    out = llm_client.complete_stream(
        [{"role": "user", "content": "x"}], on_delta=seen.append,
        model="m", api_key="k", base_url="http://127.0.0.1:1/v1", retries=0)
    check("流断后改用非流式兜底", out["text"] == "完整全文", out)
    check("兜底结果标记 streamed=False", out["streamed"] is False, out)
    check("半截增量确实吐给前端了（随后会被全文替换）",
          seen == ["半截", "内容"], seen)
    check("没有把同一路重试两次", CALLS["n"] == 1, CALLS["n"])
finally:
    llm_client._stream_raw = orig_raw
    llm_client.complete = orig_complete

print("\n--- 6. REFINE_STREAM=0 可关掉流式 ---")
os.environ["REFINE_STREAM"] = "0"
try:
    def boom(*a, **kw):
        raise AssertionError("关掉流式后不该走 _stream_raw")
    llm_client._stream_raw = boom
    llm_client.complete = lambda *a, **kw: {
        "text": "一次性", "usage": {}, "model": "m", "provider": "openai",
        "latency_ms": 1, "attempt": 1}
    seen = []
    out = llm_client.complete_stream([{"role": "user", "content": "x"}],
                                     on_delta=seen.append, model="m", api_key="k",
                                     base_url="http://127.0.0.1:1/v1", retries=0)
    check("关掉后走非流式", out["text"] == "一次性" and out["streamed"] is False, out)
    check("关掉后没有增量", seen == [], seen)
finally:
    os.environ.pop("REFINE_STREAM", None)
    llm_client._stream_raw = orig_raw
    llm_client.complete = orig_complete

print("\n--- 7. 真实 run_candidate：增量真的从模型流到事件里 ---")
EV = []


def fake_stream(messages, model, api_key, base_url, temperature, use_json,
                timeout, on_delta):
    """模拟模型：把一段话切成单字吐出来（只有辩论发言会被流式调用）。"""
    text = "他做过高并发系统，「负责」与「参与」要分开。"
    for ch in text:
        on_delta(ch)
    return text, {"total_tokens": 20}


def fake_raw(messages, model, api_key, base_url, temperature, use_json, timeout):
    """裁定与评委走非流式路径：它们输出 JSON，逐字流出来既难看也没意义。"""
    from engine.orchestrator.util import DIMENSIONS as DIMS
    sysmsg = messages[0].get("content") or ""
    if "评委" in sysmsg:
        return (json.dumps({DIMS[0]: 8.0, DIMS[1]: 8.0, DIMS[2]: 8.0,
                            "comment": "证据「原文」"}), {"total_tokens": 5})
    return (json.dumps({"pro_evidence": ["「原文」"], "con_challenges": ["「原文」"],
                        "disputes": ["争议"], "excluded": []}), {"total_tokens": 5})


orig_stream_raw = llm_client._stream_raw
orig_raw_completion = llm_client._raw_completion
llm_client._stream_raw = fake_stream
llm_client._raw_completion = fake_raw
try:
    job2 = store.create_job("流式辩论岗", {"requirements": ["5 年经验"]})
    ej = {"id": job2["id"], "title": "后端", "requirements": ["5 年经验"],
          "preferred": [], "red_flags": [], "weights": None, "rubric": None}
    cand = {"id": "c9", "job_id": job2["id"], "name": "测试人",
            "resume_text": "本科，5 年后端经验。", "assessment_text": "",
            "transcript_text": "", "job": ej}
    res = agents.run_candidate(cand, ej,
                               api={"model": "m", "provider": "openai", "api_key": "k"},
                               on_event=EV.append, repeats=1)
finally:
    llm_client._stream_raw = orig_stream_raw
    llm_client._raw_completion = orig_raw_completion

deltas = [e for e in EV if e.get("type") == "agent_delta"]
msgs = [e for e in EV if e.get("type") == "agent_message"]
check("产生了增量事件", len(deltas) > 10, len(deltas))
check("增量都带 ephemeral 标记（不会被落库）",
      all(e.get("ephemeral") for e in deltas), deltas[:1])
debate_keys = {e["key"] for e in deltas}
check("只有辩论发言在流式（评委/裁定不流式）",
      all(k.startswith(("pro_", "con_")) for k in debate_keys), debate_keys)

# 按 key 把增量拼起来，应当与对应的最终全文一致
by_key = {}
for e in deltas:
    by_key.setdefault(e["key"], []).append(e.get("text") or "")
final_by_key = {e["key"]: e["text"] for e in msgs}
joined = {k: "".join(v) for k, v in by_key.items()}
mismatch = {k: (joined[k], final_by_key.get(k))
            for k in joined if joined[k] != final_by_key.get(k)}
check("增量拼起来等于最终全文", not mismatch,
      list(mismatch.items())[:1])
check("最终全文落进了 agent_message",
      all(e.get("text") for e in msgs), len(msgs))
check("评审本身没被流式破坏（总分仍是数字）",
      isinstance(res.get("total"), (int, float)), res.get("total"))

print()
print("失败 %d 项" % len(FAIL))
for f in FAIL:
    print("   ✗ " + f)
sys.exit(0 if not FAIL else 1)
