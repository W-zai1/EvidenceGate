"""采集 / 抽取 / 归属 层冒烟测试（领域无关，离线）。

这些模块属于 EvidenceGate 的"输入端"：把杂乱的真实输入，规整成脊柱认得的形状。
本测试只验证各模块的**自身职责**（纯函数 + 字节桥接），不联网、不装 faster-whisper。

覆盖：
- audio_ingest：is_audio / transcribe_audio 的字节桥接与失败语义 / 未配置引擎显式失败；
- diarization：前缀解析（层 0）/ 相似度 / 三层归属统计 / 人工修正不被翻案；
- interview_analysis：问答四分类与覆盖统计；
- extract_claims：句子切分 + 引号回链定位。
"""
import os
import sys
import tempfile
import base64

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from engine.intake import audio_ingest
from engine.extract import diarization
from engine.extract import interview_analysis
from engine.extract import extract_claims

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  ← " + str(detail)) if (detail and not cond) else ""))


# ---------------------------------------------------------------- audio_ingest
print("\n=== audio_ingest：字节桥接与失败语义 ===")
check("is_audio 识别音频/视频", audio_ingest.is_audio("一面.mp3")
      and audio_ingest.is_audio("a.MP4") and not audio_ingest.is_audio("简历.txt"))

# 未配置引擎 → 显式失败（不是返回占位文本）
try:
    os.environ.pop("ASR_ENGINE", None)
    audio_ingest.transcribe("x.mp3")
    check("未配置引擎 → 显式失败", False, "竟然返回了")
except audio_ingest.TranscriptionUnavailable:
    check("未配置引擎 → 显式失败", True)

# 经 transcribe_audio 字节桥接时，引擎失败走上传 errors 通道（ParseError）
try:
    audio_ingest.transcribe_audio("x.mp3", b"FAKE")
    check("字节桥接遇引擎失败 → ParseError", False, "竟然返回了")
except audio_ingest.ParseError:
    check("字节桥接遇引擎失败 → ParseError", True)

# 非音频扩展名 → ParseError
try:
    audio_ingest.transcribe_audio("简历.txt", b"hello")
    check("非音频扩展名 → ParseError", False, "竟然返回了")
except audio_ingest.ParseError:
    check("非音频扩展名 → ParseError", True)

# 空文件 → ParseError
try:
    audio_ingest.transcribe_audio("空.mp3", b"")
    check("空文件 → ParseError", False, "竟然返回了")
except audio_ingest.ParseError:
    check("空文件 → ParseError", True)

# 字节桥接：临时文件用后即删、保留原扩展名（注入假 skill）
class FakeSkill:
    def __init__(self):
        self.seen = []
    def transcribe(self, path, engine=None):
        self.seen.append(path)
        return {"text": "面试官：你好。\n候选人：你好。", "engine": "whisper",
                "duration_sec": 12.0, "language": "zh",
                "segments": [{"start": 0.0, "end": 2.0, "text": "面试官：你好。"},
                             {"start": 2.1, "end": 5.0, "text": "候选人：你好。"}]}

fake = FakeSkill()
_orig = audio_ingest._load_asr
audio_ingest._load_asr = lambda: fake
try:
    out = audio_ingest.transcribe_audio("一面.mp3", b"FAKE-AUDIO-BYTES")
    check("字节转写：字段透传", out["text"].startswith("面试官：你好")
          and len(out["segments"]) == 2 and out["duration_sec"] == 12.0, out)
    p = fake.seen[-1]
    check("临时文件用后即删", not os.path.exists(p), p)
    check("临时文件保留原扩展名", p.endswith(".mp3"), p)
finally:
    audio_ingest._load_asr = _orig


# ---------------------------------------------------------------- diarization
print("\n=== diarization：层 0 前缀 + 相似度 + 三层统计 ===")
segs = diarization.segments_from_text(
    "面试官：请先做个自我介绍。\n候选人：我做过五年后端开发。\n面试官B：谈谈高并发。\n\n   \n长行无标点" * 1)
check("前缀解析出发言人（含二元）",
      [s["speaker"] for s in segs[:3]] == ["interviewer", "candidate", "interviewer"],
      [s["speaker"] for s in segs[:3]])
check("前缀置信度 1.0 且注明来源", segs[0]["confidence"] == 1.0
      and segs[0]["source"] == "prefix", segs[0])
check("前缀被剥离、正文干净", segs[0]["text"] == "请先做个自我介绍。"
      and segs[2]["text"] == "谈谈高并发。", [s["text"] for s in segs[:3]])
check("空行丢弃", len(segs) == 4, len(segs))

check("相似度：同文为 1", diarization.similarity("主从延迟故障", "主从延迟故障") == 1.0)
check("相似度：无关近 0", diarization.similarity("主从延迟故障", "薪资期望") < 0.2)

DUAL = [
    "请讲讲你做过的高并发支付网关，峰值是多少？",
    "我在上一家公司负责支付网关，QPS 峰值两万，做了三年。",
    "主从延迟那次故障，你具体改了什么？",
    "我把读请求按延迟动态路由，主库只写。",
    "好，这边差不多了，你还有什么想问我们的？",
    "想问一下这个岗位的团队规模和技术栈。",
    "你带的团队有几个人，怎么分工？",
    "带过四个人，两个后端一个前端。",
    "为什么想离开上一家公司？",
    "好，那今天就到这里，后续 HR 会联系你。",
    "你老家是哪里的，过来面试方便吗？",
    "我住望京，过来一个小时。",
]
QUESTIONS = [{"q": "请讲讲你做过的高并发支付网关，峰值是多少？", "intent": "深挖"},
             {"q": "主从延迟那次故障，你具体改了什么？", "intent": "核实"},
             {"q": "你带的团队有几个人，怎么分工？", "intent": "边界"},
             {"q": "为什么想离开上一家公司？", "intent": "风险"},
             {"q": "你对我们公司有什么了解？", "intent": "核实"}]

def stub_judge(txt):
    if any(h in txt for h in ("面试官", "？", "?", "今天就到这里", "想问我们")):
        return "interviewer", 0.9, "提问或推进流程", ""
    return "candidate", 0.85, "自述经历", ""

raw = diarization.segments_from_text("\n".join(DUAL))
out = diarization.attribute(raw, questions=QUESTIONS, judge=stub_judge)
sp = {s["idx"]: s["speaker"] for s in out["segments"]}
check("双人对话角色分离全对",
      [sp[i] for i in range(12)] ==
      ["interviewer", "candidate", "interviewer", "candidate", "interviewer",
       "candidate", "interviewer", "candidate", "interviewer", "interviewer",
       "interviewer", "candidate"], sp)
st = out["stats"]
check("统计口径：interviewer=7 / candidate=5", (st["interviewer"], st["candidate"]) == (7, 5), st)

# 人工修正不被翻案
manual = [dict(s) for s in out["segments"]]
manual[5].update(speaker="interviewer", confidence=1.0, reason="人工修正", source="manual")
rerun = diarization.attribute(manual, questions=QUESTIONS, judge=stub_judge)["segments"]
check("manual 段重跑保留（不被模型覆盖）",
      rerun[5]["speaker"] == "interviewer" and rerun[5]["source"] == "manual", rerun[5])


# ---------------------------------------------------------------- interview_analysis
print("\n=== interview_analysis：问答四分类与覆盖 ===")
paired = interview_analysis.pair_qa(out["segments"], {"questions": QUESTIONS})
by = {}
for p in paired["pairs"]:
    by.setdefault(p["status"], []).append(p)
# 问题清单驱动：问了+没问 的配对数，恰等于问题清单长度（每条清单问题必有归属）
qdriven = len(by.get("asked_answered", [])) + len(by.get("asked_dodged", [])) + len(by.get("not_asked", []))
check("问题清单驱动配对数 = 问题数", qdriven == len(QUESTIONS), qdriven)
check("照单提问被答 = 3（清单前 3 问均有答辩方回答）",
      len(by.get("asked_answered", [])) == 3, len(by.get("asked_answered", [])))
check("未问的问题被如实暴露", len(by.get("not_asked", [])) == 1
      and "面试官" in by["not_asked"][0]["attribution"], by.get("not_asked"))
# 回答内容可追溯到段号
ans = by["asked_answered"][0]
check("回答段号可追溯", isinstance(ans["answer_idx"], list) and ans["answer_idx"], ans["answer_idx"])
check("回答内容被挂到问题上", "QPS 峰值两万" in ans["answer"], ans["answer"])
check("无问题清单时全部 ad_hoc", all(p["status"] == "ad_hoc"
      for p in interview_analysis.pair_qa(out["segments"], None)["pairs"]))
ps = paired["stats"]
check("配对统计自洽", (ps["questions"] == len(QUESTIONS)
      and ps["segments_confirmed"] == 12
      and ps["has_question_list"] is True), ps)


# ---------------------------------------------------------------- extract_claims
print("\n=== extract_claims：切句 + 引号回链 ===")
text = "本笔报销金额 42800 元。供应商为星河科技。审批人李四已签字。"
claims = extract_claims.extract_claims(text)
check("按句切为 3 条主张", len(claims) == 3, len(claims))
check("每条标 unverified", all(c["status"] == "unverified" for c in claims))
ok = claims[0]
check("首条命中原文 quote 且有定位", ok["citations"]
      and ok["citations"][0]["quote"] == "本笔报销金额 42800 元。"
      and ok["citations"][0]["locator"].startswith("char"), ok["citations"])
spine_in = extract_claims.claims_to_spine(claims)
check("claims_to_spine 只留 text+citations",
      all(set(c.keys()) == {"text", "citations"} for c in spine_in))


print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED: %s" % f)
sys.exit(0 if not FAIL else 1)
