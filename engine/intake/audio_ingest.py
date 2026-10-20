"""音频采集桥（intake 层）。

职责很窄：**把音频/视频字节变成结构化转写稿**。不含任何业务判断——
不区分"这是面试还是会议"，不替任何 profile 归类。归属、抽取、闸门都是下游的事。

设计底线（沿用 ASR 链路的历史教训）：
- 没有真实转写引擎时**必须显式失败**（TranscriptionUnavailable），
  绝不返回占位文本——假稿曾污染整场评审。
- 空转写与引擎内部异常都包装成可读错误，禁止裸 traceback 逃逸。
- 临时音频文件用后即删，不留本地副本。
"""
import os
import tempfile
import shutil

_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm",
              ".ogg", ".flac", ".aac", ".wma", ".avi"}


class TranscriptionUnavailable(RuntimeError):
    """转写走不通：未配置引擎 / 依赖缺失 / 空稿 / 引擎内部异常。"""


class ParseError(ValueError):
    """音频桥接层收到的不是可转写的输入（非音频扩展名 / 空文件 / 引擎掉了）。"""


def is_audio(name):
    return os.path.splitext(name)[1].lower() in _AUDIO_EXT


def _whisper_installed():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _resolve_device(auto):
    if auto != "auto":
        return auto
    try:
        import torch  # noqa: F401
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _resolve_compute(device, auto):
    if auto != "auto":
        return auto
    return "float16" if device == "cuda" else "int8"


def describe():
    """返回当前 ASR 配置摘要（不加载模型）。"""
    engine = os.environ.get("ASR_ENGINE", "")
    return {
        "engine": engine or ("whisper" if _whisper_installed() else "unavailable"),
        "model": os.environ.get("ASR_MODEL", ""),
        "device": os.environ.get("ASR_DEVICE", "auto"),
        "compute_type": os.environ.get("ASR_COMPUTE_TYPE", "auto"),
        "installed": _whisper_installed() if engine == "whisper" else None,
    }


_MODEL_CACHE = {}


def _get_model(model, device, compute_type):
    key = (model, device, compute_type)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    try:
        from faster_whisper import WhisperModel
    except Exception:
        raise TranscriptionUnavailable(
            "未安装 faster-whisper：pip install faster-whisper")
    try:
        m = WhisperModel(model, device=device, compute_type=compute_type)
    except Exception as e:
        # CUDA 运行库缺失是有驱动无 cublas 的典型症状：auto 模式下回退 CPU
        if device == "cuda" and ("cublas" in str(e).lower() or "cuda" in str(e).lower()):
            warnings = ["CUDA 运行时不可用，已回退 CPU"]
            m = WhisperModel(model, device="cpu", compute_type="int8")
            _MODEL_CACHE[key] = m
            return m, warnings
        raise TranscriptionUnavailable("模型加载失败：%s（检查 ASR_MODEL）" % e)
    _MODEL_CACHE[key] = m
    return m, []


def _load_asr():
    """返回转写函数。未配置引擎时抛 TranscriptionUnavailable。"""
    engine = os.environ.get("ASR_ENGINE", "")
    if not engine:
        if _whisper_installed():
            return _transcribe_whisper
        raise TranscriptionUnavailable(
            "未配置 ASR_ENGINE；安装并配置 faster-whisper 后才可转写音频")
    if engine == "whisper":
        if not _whisper_installed():
            raise TranscriptionUnavailable(
                "ASR_ENGINE=whisper 但 faster-whisper 未安装：pip install faster-whisper")
        return _transcribe_whisper
    raise TranscriptionUnavailable("未知 ASR 引擎 %r" % engine)


def _normalize_text(segments):
    parts = []
    for s in segments:
        t = (s.get("text") or "").strip()
        if t:
            parts.append(t)
    return "\n".join(parts)


def _transcribe_whisper(path, engine="whisper"):
    cfg_model = os.environ.get("ASR_MODEL", "small")
    dev = _resolve_device(os.environ.get("ASR_DEVICE", "auto"))
    comp = _resolve_compute(dev, os.environ.get("ASR_COMPUTE_TYPE", "auto"))
    model, warnings = _get_model(cfg_model, dev, comp)
    vad = os.environ.get("ASR_VAD", "1") == "1"
    lang = os.environ.get("ASR_LANGUAGE") or None
    beam = int(os.environ.get("ASR_BEAM", "5"))
    try:
        segs, info = model.transcribe(
            path, language=lang, beam_size=beam, vad_filter=vad)
    except Exception as e:
        raise TranscriptionUnavailable("转写失败：%s（检查 ASR_MODEL/ASR_DEVICE）" % e)
    out_segs = []
    for s in segs:
        text = (s.text or "").strip()
        if not text:
            continue
        out_segs.append({"start": float(s.start), "end": float(s.end),
                         "text": text})
    if not out_segs:
        raise TranscriptionUnavailable("转写结果为空：音频可能无语音或编码异常")
    return {
        "text": _normalize_text(out_segs),
        "segments": out_segs,
        "duration_sec": float(getattr(info, "duration", 0.0) or 0.0),
        "language": getattr(info, "language", "") or "",
        "engine": "whisper",
        "source": os.path.basename(path),
        "warnings": warnings,
    }


def transcribe(path, engine=None):
    """对外转写入口：路径或已落地的临时文件路径都行。"""
    return _load_asr()(path, engine=engine or os.environ.get("ASR_ENGINE", "whisper"))


def transcribe_audio(filename, data, engine=None):
    """字节入口：写入带原扩展名的临时文件 → 转写 → 删除。"""
    if not is_audio(filename):
        raise ParseError("不是音频/视频文件：%s" % filename)
    if not data:
        raise ParseError("空音频文件：%s" % filename)
    ext = os.path.splitext(filename)[1] or ".mp3"
    fd, tmp = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            skill = _load_asr()
            out = skill.transcribe(tmp, engine=engine or os.environ.get("ASR_ENGINE", "whisper"))
        except TranscriptionUnavailable as e:
            raise ParseError(str(e))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return out
