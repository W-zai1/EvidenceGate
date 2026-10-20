"""测试用的模型打桩：**同时**替换非流式与流式两条传输路径。

为什么需要这个文件
------------------
流式输出之后，辩论发言走 `llm_client._stream_raw`，而评委/裁定走
`llm_client._raw_completion`。测试如果只替换后者，辩论那几步会真的去连
api.openai.com：表现为「并行评委测试跑了 18 秒」这种看似莫名其妙的数字，
以及事件数量对不上——报错点离真正的原因很远。

所以打桩要覆盖两条路径。流式桩把全文作为**一个增量**吐出去：
这些套件验证的是链路与业务逻辑，不是碎块粒度；
碎块粒度由 `test_streaming.py` 专门验证。
"""
import os
import sys

# 自己把 engine/ 放进 sys.path：调用方虽然都会先加，但这个助手要能被
# 单独 import（也能被工具当脚本扫到而不报 ModuleNotFoundError）。
_ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "engine")
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

from engine.api import llm_client


def install(fake_raw):
    """把 fake_raw 同时装到非流式与流式两条路径上。

    fake_raw 的签名与原来的 `_raw_completion` 一致：
        (messages, model, api_key, base_url, temperature, use_json, timeout)
    返回 (文本, usage)。
    """
    llm_client._raw_completion = fake_raw

    def _stream(messages, model, api_key, base_url, temperature, use_json,
                timeout, on_delta):
        text, usage = fake_raw(messages, model, api_key, base_url,
                               temperature, use_json, timeout)
        if on_delta and text:
            on_delta(text)
        return text, usage

    llm_client._stream_raw = _stream
    return _stream


def restore(raw, stream):
    """还原两条路径（用于 finally）。"""
    llm_client._raw_completion = raw
    llm_client._stream_raw = stream
