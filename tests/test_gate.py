"""证据脊柱（gate/）的单元测试。

重点不是"能跑"，而是三条不能退让的性质：
  1. **非补偿**：加分项再多，也补不回一个缺口；
  2. **三态**：缺证据 → 阻断；要人判 → 待复核；都不能被折叠成"通过"；
  3. **可复现**：同一输入 100 次，闸门 hash 必须完全一致。
外加证据包的离线可验证性（对方不信任我们时，跑脚本就能验）。
"""
import json
import os
import subprocess
import sys
import tempfile
import zipfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import engine.spine
from   engine.spine import ir
from   engine.spine import verify as vfy
from engine.spine import gate as g
from engine.spine.profiles import registry

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


def make_artifacts():
    return [
        ir.Artifact(id="inv", role="invoice", filename="发票.pdf",
                    text="增值税普通发票 发票号 88213 金额 42800.00 元 开票日期 2025-11-03 "
                         "抬头 某某科技有限公司"),
        ir.Artifact(id="appr", role="approval", filename="审批流.json",
                    text="审批链 申请人 张伟 部门经理 李娜 财务 王强 审批结果 同意 金额 42800.00"),
        ir.Artifact(id="pol", role="policy", filename="报销制度.md",
                    text="差旅费用标准 交通费 单次不超过 3000 元 需附发票与审批记录"),
    ]


def ref(art, quote, loc="p.1"):
    return ir.EvidenceRef(artifact_id=art.id, locator=loc, quote=quote,
                          sha256=art.ensure_hash())


def nice_claims(arts):
    return [
        ir.Claim(id="c1", text="发票金额为 42800.00 元",
                 citations=[ref(arts[0], "金额 42800.00 元")]),
        ir.Claim(id="c2", text="审批结果为同意，审批金额与发票一致",
                 citations=[ref(arts[1], "审批结果 同意 金额 42800.00")]),
    ]


def approvals():
    return [
        ir.Approval("claimant", "张伟", "2025-11-03T09:00:00"),
        ir.Approval("approver", "李娜", "2025-11-03T10:00:00"),
        ir.Approval("reviewer", "王强", "2025-11-04T09:00:00"),
    ]


TRAIL = ir.TrailSnapshot(ok=True, count=418, head="a" * 64, anchored=False)


def ctx_for(arts, claims, findings=(), approvals_=None, trail=TRAIL):
    return g.GateContext(claims=claims, findings=list(findings),
                         artifacts=arts,
                         approvals=approvals() if approvals_ is None else approvals_,
                         trail=trail)


print("--- 1. IR 序列化可复现 ---")
a = ir.Claim(id="c", text="x", citations=[ir.EvidenceRef("a", "p.1", "q")])
b = ir.Claim.from_dict(a.to_dict())
check("Claim 往返一致", b.to_dict() == a.to_dict())
check("同一对象两次 digest 相同", a.digest() == b.digest())
check("键序不同但内容相同 → digest 相同",
      ir.digest({"b": 1, "a": 2}) == ir.digest({"a": 2, "b": 1}))
check("内容变化 → digest 变化",
      ir.digest({"a": 2}) != ir.digest({"a": 3}))

print("\n--- 2. 引用核验（确定性） ---")
arts = make_artifacts()
by_id = {x.id: x for x in arts}
claims = nice_claims(arts)
out, stat = vfy.verify_claims(claims, arts)
check("逐字引用 → verified", out[0].status == ir.VERIFIED, out[0].note)
check("统计正确", stat == {"total": 2, "verified": 2, "unsupported": 0}, stat)

bad_quote = ir.Claim(id="bad1", text="金额为 99999 元",
                     citations=[ref(arts[0], "金额 99999 元")])
o1, _ = vfy.verify_claims([bad_quote], arts)
check("原文没有这句话 → unsupported", o1[0].status == ir.UNSUPPORTED, o1[0].note)

no_cite = ir.Claim(id="bad2", text="供应商为关联方")
o2, _ = vfy.verify_claims([no_cite], arts)
check("无引用 → unsupported", o2[0].status == ir.UNSUPPORTED, o2[0].note)

ghost = ir.Claim(id="bad3", text="引用不存在的材料",
                 citations=[ir.EvidenceRef("nope", "p.1", "x")])
o3, _ = vfy.verify_claims([ghost], arts)
check("引用不存在的材料 → unsupported", o3[0].status == ir.UNSUPPORTED)

tampered = ir.EvidenceRef(artifact_id="inv", locator="p.1",
                          quote="金额 42800.00 元", sha256="0" * 64)
o4, _ = vfy.verify_claims([ir.Claim(id="bad4", text="t", citations=[tampered])], arts)
check("材料指纹不符 → unsupported", o4[0].status == ir.UNSUPPORTED, o4[0].note)

# NFKC / 空白归一：全角数字也应能匹配
art_fw = ir.Artifact(id="fw", role="invoice", text="金额　４２８００．００ 元")
o5, _ = vfy.verify_claims(
    [ir.Claim(id="fwc", text="t",
              citations=[ir.EvidenceRef("fw", "p.1", "金额 42800.00 元")])],
    [art_fw])
check("全角/空白差异可归一匹配", o5[0].status == ir.VERIFIED, o5[0].note)

seg_bad = ir.EvidenceRef(artifact_id="inv", locator="p.1",
                         quote="金额 42800.00 元", char_start=0, char_end=4)
o6, _ = vfy.verify_claims([ir.Claim(id="bad5", text="t", citations=[seg_bad])], arts)
check("字符区间与引文不符 → unsupported", o6[0].status == ir.UNSUPPORTED, o6[0].note)

check("算术复算一致", vfy.sums_equal([100.0, 200.5, 42.3], 342.8))
check("算术复算不一致", not vfy.sums_equal([100.0, 200.0], 301.0))
cov = vfy.coverage(arts, ["invoice", "approval", "policy", "receipt"])
check("覆盖率把缺失项列出", cov["missing"] == ["receipt"], cov)

print("\n--- 3. 闸门：三态 ---")
exp = registry.get("expense")
r_pass = g.from_profile(exp, ctx_for(arts, out))
check("全部闭合 → pass", r_pass.verdict == g.PASS, [c.to_dict() for c in r_pass.clauses])
check("pass 时 issuable=True", r_pass.issuable)
check("未锚定会产生 caveat（不假装不可篡改）",
      any("未外部锚定" in c for c in r_pass.caveats), r_pass.caveats)
check("闸门 hash 64 位", len(r_pass.hash) == 64, len(r_pass.hash))

bad_claims = out + [ir.Claim(id="bad9", text="编造的关联方关系",
                             status=ir.UNSUPPORTED)]
r_block = g.from_profile(exp, ctx_for(arts, bad_claims))
check("有一条无法回链 → block", r_block.verdict == g.FAIL, r_block.verdict)
check("阻断条款被列出", "E3" in r_block.blocking, r_block.blocking)

# 非补偿：再堆十条可回链的漂亮主张，也不能把缺口补回来
more = nice_claims(arts) + [
    ir.Claim(id="extra%d" % i, text="附加主张 %d" % i,
             citations=[ref(arts[0], "金额 42800.00 元")])
    for i in range(10)
]
r_more = g.from_profile(exp, ctx_for(arts, out + more + [bad_claims[-1]]))
check("加分项不能补偿缺口（非补偿）", r_more.verdict == g.FAIL, r_more.verdict)

deferred = [ir.Finding(code="J1", title="业务真实性需人判",
                       category=ir.CAT_JUDGMENT, status=ir.DEFERRED)]
r_human = g.from_profile(exp, ctx_for(arts, out, findings=deferred))
check("挂起 → needs_human", r_human.verdict == g.NEEDS_HUMAN, r_human.verdict)
check("needs_human 不可出具", not r_human.issuable)

r_human_block = g.from_profile(
    exp, ctx_for(arts, bad_claims, findings=deferred))
check("阻断优先于待复核", r_human_block.verdict == g.FAIL, r_human_block.verdict)

missing = [ir.Finding(code="M1", title="缺关联方声明",
                      category=ir.CAT_MISSING, status=ir.OPEN)]
r_missing = g.from_profile(exp, ctx_for(arts, out, findings=missing))
check("应有未提供的证据 → block", r_missing.verdict == g.FAIL, r_missing.verdict)

print("\n--- 4. 闸门：可复现与纯函数 ---")
hashes = {g.from_profile(exp, ctx_for(arts, out)).hash for _ in range(100)}
check("100 次求值 hash 唯一", len(hashes) == 1, hashes)
r1 = g.from_profile(exp, ctx_for(arts, out), evaluated_at="2025-01-01T00:00:00")
r2 = g.from_profile(exp, ctx_for(arts, out), evaluated_at="2030-01-01T00:00:00")
check("时间不进 hash（可复现）", r1.hash == r2.hash)
check("时间保留在结果里", r2.evaluated_at == "2030-01-01T00:00:00")
check("未知条款类型 → fail（不静默跳过）",
      g.evaluate([g.GateClause("X", "未知", "nonsense")],
                 ctx_for(arts, out)).verdict == g.FAIL)

print("\n--- 5. 职责分离 / 覆盖 ---")
same = [ir.Approval("claimant", "张伟", "t"),
        ir.Approval("approver", "张伟", "t"),
        ir.Approval("reviewer", "王强", "t")]
r_same = g.from_profile(exp, ctx_for(arts, out, approvals_=same))
check("同一人签多个角色 → block", r_same.verdict == g.FAIL, r_same.verdict)

partial = [ir.Approval("claimant", "张伟", "t")]
r_partial = g.from_profile(exp, ctx_for(arts, out, approvals_=partial))
check("签署不全 → needs_human", r_partial.verdict == g.NEEDS_HUMAN, r_partial.verdict)

few = arts[:1]
r_cov = g.from_profile("x" and exp, ctx_for(few, out))
check("材料缺失 → block 且列出缺项",
      r_cov.verdict == g.FAIL and "E1" in r_cov.blocking, r_cov.blocking)

print("\n--- 6. profile 注册表（领域无关性） ---")
labels = registry.labels()
check("talent 与 expense 同时注册", labels == ["expense@2026.10.1", "talent@2026.10.1"], labels)
t = registry.get("talent")
check("talent 保留三评委", t.judge_count == 3, t.judge_count)
check("expense 不需要多数票", exp.judge_count == 1, exp.judge_count)
check("两个 profile 的条款不同", [c.code for c in t.clauses] != [c.code for c in exp.clauses])
try:
    registry.get("nope")
    check("未知 profile 抛错", False)
except KeyError:
    check("未知 profile 抛错", True)
check("profile digest 稳定", t.digest() == registry.get("talent").digest())
check("profile 改了版本 → label 变（等于换尺子）",
      "%s@%s" % (t.key, "2099") != t.label)

print("\n--- 7. 证据包：离线可验证 ---")
case = ir.Case(id="case1", title="11 月差旅报销", profile_key="expense",
               profile_version=exp.version, created_at="2025-12-12T22:41:00")
b1 = g.build_bundle(case, arts, out, [], r_pass,
                       approvals=approvals(), generated_at="2025-12-12T22:42:00")
b2 = g.build_bundle(case, arts, out, [], r_pass,
                       approvals=approvals(), generated_at="2025-12-12T22:42:00")
check("同输入 → 同样字节（可做基线）", b1 == b2)
names = zipfile.ZipFile(__import__("io").BytesIO(b1)).namelist()
check("四个核心成员", set(names) == {"manifest.json", "claims.jsonl",
                                     "findings.jsonl", "gate.json", "verify.py"}, names)

tmp = tempfile.mkdtemp(prefix="eg-bundle-")
with zipfile.ZipFile(__import__("io").BytesIO(b1)) as z:
    z.extractall(tmp)
man = json.load(open(os.path.join(tmp, "manifest.json"), encoding="utf-8"))
check("manifest 带闸门结论", man["gate"]["verdict"] == "pass", man["gate"]["verdict"])
check("manifest 带引用核验计数", man["counts"]["verified"] == 2, man["counts"])
check("manifest 明确写了未签名边界",
      "未签名" in man["disclaimer"], man["disclaimer"])

env = dict(os.environ, PYTHONIOENCODING="utf-8")
r0 = subprocess.run([sys.executable, "verify.py"], cwd=tmp, env=env,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
check("离线校验通过", r0.returncode == 0, (r0.stdout or "") + (r0.stderr or ""))
check("输出明确提示未签名", "未签名" in (r0.stdout or ""), r0.stdout)

# 篡改一个字节：verify.py 必须发现
with open(os.path.join(tmp, "claims.jsonl"), "a", encoding="utf-8") as f:
    f.write("\n")
r1 = subprocess.run([sys.executable, "verify.py"], cwd=tmp, env=env,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
check("篡改后离线校验失败", r1.returncode == 1, (r1.stdout or "") + (r1.stderr or ""))

# 带原文的包：材料指纹也要能验
tmp2 = tempfile.mkdtemp(prefix="eg-bundle2-")
b3 = g.build_bundle(case, arts, out, [], r_pass, include_text=True,
                       generated_at="2025-12-12T22:42:00")
with zipfile.ZipFile(__import__("io").BytesIO(b3)) as z:
    z.extractall(tmp2)
r2 = subprocess.run([sys.executable, "verify.py"], cwd=tmp2, env=env,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
check("含原文的包可验证", r2.returncode == 0, (r2.stdout or "") + (r2.stderr or ""))
with open(os.path.join(tmp2, "artifacts", "inv.txt"), "a", encoding="utf-8") as f:
    f.write("篡改")
r3 = subprocess.run([sys.executable, "verify.py"], cwd=tmp2, env=env,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
check("篡改原文后失败", r3.returncode == 1, (r3.stdout or "") + (r3.stderr or ""))

print("\n--- 8. 核验失败会进 finding（不静默消失） ---")
fs = vfy.audit_claims(out + [ir.Claim(id="u1", text="无据", status=ir.UNSUPPORTED)])
check("每条 unsupported 生成一条 finding",
      [f.code for f in fs] == ["CITE-u1"], [f.code for f in fs])
check("finding 分类正确", fs[0].category == ir.CAT_CITATION, fs[0].category)

print()
print("失败 %d 项" % len(FAIL))
sys.exit(0 if not FAIL else 1)
