"""文件夹采集 → 案件装配（orchestrator 层）。

把"一堆落盘文件"变成"一条 cases 记录 + 一组 artifacts"。这是系统对外的入口之一：
负责把杂乱的真实输入（命名五花八门的录音、简历、发票）规整成脊柱认得的形状。

- 角色识别（guess_role）：按文件名猜材料归属（transcript/resume/assessment/...）；
  纯文件名启发式，不读内容，可离线、可解释。
- 音频文件：调 intake.audio_ingest 转写（失败即显式停下，不悄悄跳过——
  漏转写比报错更危险，因为没人会发现缺了一段证据）。
- 其它文本：直接入库。

不在此层做真伪判断、不做闸门——那是 graph/cases 与 spine 的职责。
"""
import os
import re

from engine.graph import cases
from engine.intake import audio_ingest
from engine.orchestrator import events

_AUDIO_HINT = re.compile(r"(录音|音频|audio|recording|面试)", re.IGNORECASE)


def guess_role(filename):
    """按文件名推断材料角色（兜底为最常见角色）。"""
    base = os.path.basename(filename)
    low = base.lower()
    if low.endswith((".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm",
                     ".ogg", ".flac", ".aac")) or _AUDIO_HINT.search(base):
        return "transcript"
    if "逐字稿" in base or low.endswith(".transcript.txt"):
        return "transcript"
    if "测评" in base or "assessment" in low:
        return "assessment"
    if "简历" in base or "resume" in low or "cv" in low:
        return "resume"
    if "jd" in low or "岗位" in base or "招聘" in base:
        return "jd"
    return "resume"


def _strip_audio_tag(name):
    """剥掉文件名里的 .audio 标记，避免岗位名变成"后端工程师.audio"。"""
    return re.sub(r"\.audio\b", "", name)


def build_case_from_dir(in_dir, title=None, profile_key="expense", actor="",
                        role_map=None):
    """扫描 in_dir，构建一条 case + artifacts。返回 case 字典（含 warnings）。

    材料角色以 **profile 的契约为准**：profile.required_roles 之外的猜测角色会被
    映射到最接近的契约角色（或第一个契约角色兜底），并在 warnings 里留痕——
    既不丢材料，也不越权往案件里塞 profile 不认的角色。

    音频转写失败会显式抛错（ParseError/TranscriptionUnavailable）：漏掉一段证据
    比报错危险得多，因为没人会发现少了它。
    """
    if not os.path.isdir(in_dir):
        raise FileNotFoundError("目录不存在: %s" % in_dir)
    prof = cases.registry.get(profile_key)
    accepted = list(prof.required_roles)
    if not accepted:
        raise ValueError("profile %s 未声明必需材料角色" % profile_key)
    case = cases.create_case(title or os.path.basename(in_dir.rstrip("/\\")),
                             profile_key, actor=actor)
    cid = case["id"]
    warnings = []
    for fn in sorted(os.listdir(in_dir)):
        path = os.path.join(in_dir, fn)
        if not os.path.isfile(path):
            continue
        guessed = guess_role(fn)
        if audio_ingest.is_audio(fn):
            with open(path, "rb") as f:
                data = f.read()
            if not data:
                raise audio_ingest.ParseError("空音频文件: %s" % fn)
            out = audio_ingest.transcribe_audio(fn, data)
            text = out.get("text", "")
            guessed = "transcript"
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        role = _map_role(guessed, accepted, role_map)
        if role != guessed:
            warnings.append("文件 %r 猜测角色 %r 不在 %s 的材料契约内，已归入 %r"
                            % (fn, guessed, profile_key, role))
        cases.add_artifact(cid, role, fn, text, actor=actor)
        events.BUS.publish({"type": "artifact.added", "case_id": cid,
                            "role": role, "filename": fn})
    events.BUS.publish({"type": "case.ingested", "case_id": cid, "actor": actor})
    out = cases.get_case(cid)
    out["warnings"] = warnings
    return out


def _map_role(guessed, accepted, role_map=None):
    """把猜测角色映射到 profile 契约内的角色。"""
    if role_map:
        if guessed in role_map:
            return role_map[guessed]
    if guessed in accepted:
        return guessed
    # 契约内已有同名子串 → 取最接近的；否则兜底第一个
    for a in accepted:
        if a in guessed or guessed in a:
            return a
    return accepted[0]
