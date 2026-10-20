"""通用案件层（cases.py）与证据链校验（validation.py）的端到端测试。

它要证明的是**同一套脊柱跑两个场景**：
  · expense：两种材料 + 一条编造引用 + 三级签署 → 先阻断、补齐后通过；
  · talent ：换 profile、换材料角色，代码一行不改，照样通过。
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import zipfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="eg-cases-")
from engine.storage import db  # noqa: E402
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "cases.db")
db.reset_init_flag()
from engine.intake import crypto  # noqa: E402
crypto._cached_key = None
crypto._data_dir = lambda: TMP

from engine.graph import cases  # noqa: E402
from engine.graph import validation  # noqa: E402
import engine.spine.gate as spine_gate  # noqa: E402
FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


db.init()
conn = db.connect()
check("schema 迁到最新版", db.SCHEMA_VERSION >= 10 and
      conn.execute("SELECT MAX(version) v FROM schema_migrations").fetchone()["v"]
      == db.SCHEMA_VERSION)

INVOICE = ("增值税普通发票  发票号 88213  金额 42800.00 元  开票日期 2025-11-03  "
           "购买方 某某科技有限公司  销售方 某某服务商")
APPROVAL = ("费用报销审批流  申请人 张伟  部门经理 李娜 同意  财务 王强 复核通过  "
            "审批金额 42800.00  审批日期 2025-11-04")
POLICY = "差旅与业务招待费用标准：交通费单次不超过 3000 元；报销须附发票与审批记录。"

print("--- 1. 建案 / 材料 / 加密落库 ---")
c = cases.create_case("11 月差旅报销", "expense", note="演示数据", actor="u1")
cid = c["id"]
check("建案成功", bool(cid) and c["profile_label"] == "expense@2026.10.1", c.get("profile_label"))
check("初始状态 draft", c["status"] == "draft", c["status"])

a_inv = cases.add_artifact(cid, "invoice", "发票.pdf", INVOICE, actor="u1")
a_appr = cases.add_artifact(cid, "approval", "审批流.txt", APPROVAL, actor="u1")
a_pol = cases.add_artifact(cid, "policy", "报销制度.md", POLICY, actor="u1")
check("三类材料入库", len(cases.get_case(cid)["artifacts"]) == 3)

raw = conn.execute("SELECT text_enc FROM case_artifacts WHERE id=?", (a_inv["id"],)).fetchone()
check("材料正文加密存储", raw["text_enc"].startswith("enc:v1:"), raw["text_enc"][:20])
check("库里没有明文发票号", "88213" not in raw["text_enc"])

try:
    cases.add_artifact(cid, "resume", "简历.txt", "x")
    check("拒绝不属于该场景的材料角色", False)
except cases.CaseError as e:
    check("拒绝不属于该场景的材料角色", "不接受材料角色" in str(e), str(e))

try:
    cases.create_case("x", "nope")
    check("未知 profile 报错", False)
except KeyError:
    check("未知 profile 报错", True)

print("\n--- 2. 主张与引用校验 ---")
good = {"text": "发票金额为 42800.00 元",
        "citations": [{"artifact_id": a_inv["id"], "locator": "p.1",
                       "quote": "金额 42800.00 元"}]}
bad = {"text": "该供应商为集团关联方",
       "citations": [{"artifact_id": a_inv["id"], "locator": "p.1",
                      "quote": "关联方 集团"}]}
no_cite = {"text": "本次报销符合公司制度"}
cases.set_claims(cid, [good, bad, no_cite], actor="u1")
detail = cases.get_case(cid)
check("三条主张入库且状态未验", [x["status"] for x in detail["claims"]] ==
      ["unverified"] * 3, [x["status"] for x in detail["claims"]])

try:
    cases.set_claims(cid, [{"text": "x", "citations": [{"artifact_id": "ghost",
                                                        "locator": "p.1", "quote": "x"}]}])
    check("引用不存在的材料被拒", False)
except cases.CaseError as e:
    check("引用不存在的材料被拒", "不存在的材料" in str(e), str(e))

print("\n--- 3. 闸门：先阻断 ---")
d = cases.run_gate(cid, actor="u1")
check("有编造引用 → 阻断", d["gate"]["verdict"] == "fail", d["gate"]["verdict"])
check("案件状态变 blocked", d["status"] == "blocked", d["status"])
check("无法回链的主张被标记",
      sum(1 for x in d["claims"] if x["status"] == "unsupported") == 2,
      [(x["text"], x["status"]) for x in d["claims"]])
check("引用失败生成 finding",
      any(f["code"].startswith("CITE-") for f in d["findings"]),
      [f["code"] for f in d["findings"]])
check("阻断条款含 E3", "E3" in d["gate"]["blocking"], d["gate"]["blocking"])
check("闸门 hash 可复算（存储一致）", spine_gate.verify_stored(
    d["gate"])["ok"])

print("\n--- 4. 三级签署 ---")
cases.sign(cid, "claimant", "张伟")
cases.sign(cid, "approver", "李娜")
d = cases.sign(cid, "reviewer", "王强")
check("三级签署入库", len(d["approvals"]) == 3, d["approvals"])
try:
    cases.sign(cid, "ceo", "某人")
    check("拒绝不适用的签署角色", False)
except cases.CaseError as e:
    check("拒绝不适用的签署角色", "不适用于场景" in str(e), str(e))

print("\n--- 5. 补齐后通过（同一套脊柱） ---")
cases.set_claims(cid, [good], actor="u1")
d = cases.run_gate(cid, actor="u1")
check("引用全部可回链 → pass", d["gate"]["verdict"] == "pass",
      [(x["code"], x["status"]) for x in d["gate"]["clauses"]])
check("案件状态 verified", d["status"] == "verified", d["status"])
check("有未锚定保留项（不粉饰）",
      any("未外部锚定" in x for x in d["gate"]["caveats"]), d["gate"]["caveats"])

print("\n--- 6. 证据包 ---")
b = cases.bundle_bytes(cid, actor="u1")
raw_zip = base64.b64decode(b["bundle"]["data_base64"] if "bundle" in b else b["data_base64"])
tmp = tempfile.mkdtemp(prefix="eg-case-bundle-")
with zipfile.ZipFile(__import__("io").BytesIO(raw_zip)) as z:
    z.extractall(tmp)
    names = z.namelist()
check("证据包含 verify.py 等成员",
      {"manifest.json", "claims.jsonl", "findings.jsonl", "gate.json", "verify.py"} <= set(names),
      names)
env = dict(os.environ, PYTHONIOENCODING="utf-8")
r = subprocess.run([sys.executable, "verify.py"], cwd=tmp, env=env,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("离线校验通过", r.returncode == 0, (r.stdout or "") + (r.stderr or ""))

# 结论之后改案件 → 必须拒绝导出（防止包内结论对不上材料）
# 用修订号而不是时间戳模拟改动：时间只到秒，同秒内的先后比不出来。
conn.execute("UPDATE cases SET revision=revision+1 WHERE id=?", (cid,))
conn.commit()
try:
    cases.bundle_bytes(cid)
    check("结论过期时拒绝导出", False)
except cases.CaseError as e:
    check("结论过期时拒绝导出", "重新求值闸门" in str(e), str(e))

print("\n--- 7. 证据链校验页 ---")
gt = validation.run_all()
check("八项检查齐全", gt["summary"]["total"] == 8, gt["summary"])
by_id = {x["id"]: x for x in gt["checks"]}
check("审计链通过", by_id["CHAIN-01"]["status"] == "pass", by_id["CHAIN-01"])
check("闸门可复现通过", by_id["GATE-01"]["status"] == "pass", by_id["GATE-01"])
check("引用检查通过", by_id["CITE-01"]["status"] == "pass", by_id["CITE-01"])
check("未锚定是 warn 而不是 pass",
      by_id["ANCHOR-01"]["status"] == "warn", by_id["ANCHOR-01"])
check("总状态为有条件通过（存在保留项）",
      gt["verdict"] == "attention", gt["verdict"])
check("trace_id 稳定且可复算", gt["trace_id"].startswith("gt-") and
      len(gt["dataset_hash"]) == 64)

print("\n--- 8. 第二个 profile：同一代码跑 talent ---")
t = cases.create_case("后端工程师评审", "talent", actor="u2")
tid = t["id"]
resume = ("清华大学 本科 计算机  字节跳动 后端工程师 5 年  "
          "主导日均 2 亿请求的网关重构  Go / Kubernetes  北京")
a_res = cases.add_artifact(tid, "resume", "简历.txt", resume, actor="u2")
cases.set_claims(tid, [{
    "text": "候选人有 5 年后端经验",
    "citations": [{"artifact_id": a_res["id"], "locator": "L1",
                   "quote": "字节跳动 后端工程师 5 年"}],
}], actor="u2")
for role, who in (("maker", "招聘A"), ("checker", "复核B"), ("approver", "批准C")):
    cases.sign(tid, role, who)
td = cases.run_gate(tid, actor="u2")
check("talent profile 用同一套闸门通过", td["gate"]["verdict"] == "pass",
      [(x["code"], x["status"]) for x in td["gate"]["clauses"]])
check("talent 保留三评委配置", cases.registry.get("talent").judge_count == 3)

print("\n--- 9. 删除级联 ---")
cases.delete_case(cid, actor="u1")
n_claims = conn.execute("SELECT COUNT(*) n FROM case_claims WHERE case_id=?",
                        (cid,)).fetchone()["n"]
n_gates = conn.execute("SELECT COUNT(*) n FROM gate_results WHERE case_id=?",
                       (cid,)).fetchone()["n"]
check("删案件连带清主张与闸门", n_claims == 0 and n_gates == 0, (n_claims, n_gates))
check("另一个案件不受影响", len(cases.list_cases()) == 1, cases.list_cases())

print()
print("失败 %d 项" % len(FAIL))
sys.exit(0 if not FAIL else 1)
