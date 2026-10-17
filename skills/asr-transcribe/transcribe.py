"""asr-transcribe · 音视频转写（未接真实引擎时**必须报错**）。

为什么不再返回占位文本
----------------------
这里曾经返回一段硬编码的假转写稿（"候选人：我曾负责一个高并发系统的稳定性治理…"）。
后果是：ingest 扫到 .mp3 → 拿到假文本 → 塞进 interview_transcript →
ContextStore.has_material() 检查通过（字符串非空）→ 评照跑 →
三位评委拿着这段**从未存在过的**"高并发治理经验"给"硬实力门槛"打分。

整个项目其他部分都在拼命防"看起来像样的假结论"，这个函数是唯一能合法产出它的入口。
所以现在：没有真实 ASR 引擎就抛 NotImplementedError，让评审在入口处失败，
而不是在一份编造的转写稿上给出 8.2 分。

接真实引擎
----------
1. 本地 whisper：`pip install faster-whisper`（唯一允许引入的第三方依赖）
   然后实现 transcribe(path, engine="whisper")。
2. 外部 ASR：在 engine="api" 分支里调用你自己的转写服务。

两种都要保持返回结构不变：
    {"text": str, "engine": str, "duration_sec": float, "source": str}
且 text 为空时必须抛错（空转写稿与假转写稿一样有害）。
"""
import argparse
import json
import os
import sys


class TranscriptionUnavailable(RuntimeError):
    """没有可用的 ASR 引擎——这是"未配置"，不是"可以糊过去"。"""


def transcribe(path, engine=None):
    """把音视频转成文字稿。

    当前仅保留显式失败路径：本项目不内置任何 mock 转写。
    """
    engine = engine or os.getenv("ASR_ENGINE") or "unavailable"

    if engine in ("unavailable", "mock", ""):
        raise TranscriptionUnavailable(
            "未配置 ASR 引擎，无法转写音频「%s」。"
            "请改用 .txt/.md 转写稿，或安装 faster-whisper 后设置 ASR_ENGINE=whisper。"
            "（本项目不提供 mock 转写——编造的面试稿会直接污染评审结论。）"
            % (os.path.basename(path or "") or "未提供文件")
        )

    if engine == "whisper":
        try:
            from faster_whisper import WhisperModel  # 可选依赖
        except ImportError:
            raise TranscriptionUnavailable(
                "ASR_ENGINE=whisper 但未安装 faster-whisper。请执行：pip install faster-whisper"
            )
        model = WhisperModel(os.getenv("ASR_MODEL", "base"))
        segments, info = model.transcribe(path)
        text = "".join(s.text for s in segments).strip()
        if not text:
            raise TranscriptionUnavailable("whisper 转写结果为空：%s" % path)
        return {
            "text": text,
            "engine": "whisper",
            "duration_sec": round(getattr(info, "duration", 0.0) or 0.0, 2),
            "source": os.path.abspath(path),
        }

    raise TranscriptionUnavailable("未知 ASR 引擎：%s（支持 whisper；其它需自行实现）" % engine)


def main():
    ap = argparse.ArgumentParser(description="音视频转写")
    ap.add_argument("path", nargs="?", default="", help="音视频文件路径")
    ap.add_argument("--engine", default=os.getenv("ASR_ENGINE", "unavailable"))
    args = ap.parse_args()
    try:
        print(json.dumps(transcribe(args.path, args.engine), ensure_ascii=False, indent=2))
    except TranscriptionUnavailable as e:
        print("[error] %s" % e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
