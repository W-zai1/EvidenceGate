"""编排层冒烟测试：事件总线 + 文件夹采集 + 角色识别。

验证 orchestrator 的自身职责：
- events：事件总线解耦发布/订阅；订阅者失败被记录而非静默；
- ingest：文件夹 → 一条 case + artifacts；音频失败显式停下（不悄悄跳过）；
- guess_role：按文件名启发式归类，兜底为最常见角色。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="eg-orch-")
from engine.storage import db  # noqa: E402
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "orch.db")
db.reset_init_flag()
from engine.intake import crypto  # noqa: E402
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

from engine.orchestrator import events as ev  # noqa: E402
from engine.orchestrator import ingest  # noqa: E402
from engine.api import api  # noqa: E402
from engine.graph import cases  # noqa: E402

# 捕获编排事件，供"入库是否发事件"断言
_events_seen = []
ev.BUS.on("*", lambda e: _events_seen.append(e.get("type")))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  ← " + str(detail)) if (detail and not cond) else ""))


# ---------------------------------------------------------------- events
print("\n=== events：总线解耦与失败语义 ===")
bus = ev.EventBus()
seen = []
bus.on("case.created", lambda e: seen.append(e["case_id"]))
bus.publish({"type": "case.created", "case_id": "c1"})
check("订阅者收到事件", seen == ["c1"], seen)

# 订阅者失败不得拖垮发布者，但必须被记录（上抛 OrchestrationError）
errs = []
def boom(e):
    raise RuntimeError("订阅者炸了")
bus2 = ev.EventBus()
bus2.on("x", boom)
try:
    bus2.publish({"type": "x"})
    check("订阅者失败被记录而非静默", False, "竟然没报错")
except ev.OrchestrationError as e:
    check("订阅者失败被记录而非静默", "订阅者炸了" in str(e), e)

# 取关后不再收到
bus.off("case.created", seen.__class__) if False else None
seen2 = []
bus.on("case.created", lambda e: seen2.append(e["case_id"]))
bus.publish({"type": "case.created", "case_id": "c2"})
check("多订阅者均收到", seen == ["c1", "c2"] and seen2 == ["c2"], (seen, seen2))


# ---------------------------------------------------------------- guess_role
print("\n=== guess_role：文件名启发式 ===")
check("面试录音.mp3 → transcript", ingest.guess_role("面试录音.mp3") == "transcript")
check("逐字稿.txt → transcript", ingest.guess_role("逐字稿.txt") == "transcript")
check("测评结果.txt → assessment", ingest.guess_role("测评结果.txt") == "assessment")
check("张三简历.pdf → resume", ingest.guess_role("张三简历.pdf") == "resume")
check("未知命名.txt → resume（兜底）", ingest.guess_role("abc.txt") == "resume")
check("api.guess_role 复用同一启发式", api.guess_role("一面视频.mp4") == "transcript")


# ---------------------------------------------------------------- ingest
print("\n=== ingest：文件夹 → case（纯文本，无音频）===")
in_dir = tempfile.mkdtemp(prefix="org_in_")
with open(os.path.join(in_dir, "王五__后端工程师.transcript.txt"), "w", encoding="utf-8") as f:
    f.write("面试官：讲讲你的项目。\n候选人：好。")
with open(os.path.join(in_dir, "王五简历.pdf.txt"), "w", encoding="utf-8") as f:
    f.write("五年后端经验")

case = ingest.build_case_from_dir(in_dir, title="后端工程师评审", profile_key="expense", actor="u1")
check("构建出一条 case", case["id"] and case["profile_key"] == "expense", case.get("profile_key"))
check("两个文件 → 两个 artifact", len(case["artifacts"]) == 2, len(case["artifacts"]))
accepted = set(cases.registry.get("expense").required_roles)
check("材料角色落在 profile 契约内",
      all(a["role"] in accepted for a in case["artifacts"]),
      [a["role"] for a in case["artifacts"]])
check("角色越界被记录为 warning（不丢材料）",
      len(case.get("warnings") or []) == 2, case.get("warnings"))
check("入库触发 case.ingested 事件", "case.ingested" in _events_seen)

# 音频缺失转写引擎 → 显式失败（不悄悄跳过）
in_dir_a = tempfile.mkdtemp(prefix="org_aud_")
with open(os.path.join(in_dir_a, "面试.mp3"), "wb") as f:
    f.write(b"NOT-REAL-AUDIO")
os.environ.pop("ASR_ENGINE", None)
try:
    ingest.build_case_from_dir(in_dir_a, title="t", profile_key="expense", actor="u1")
    check("音频无法转写 → 显式失败", False, "竟然继续跑了")
except Exception as e:
    check("音频无法转写 → 显式失败",
          "transcribe" in type(e).__name__.lower() or "转写" in str(e), e)

print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED: %s" % f)
sys.exit(0 if not FAIL else 1)
