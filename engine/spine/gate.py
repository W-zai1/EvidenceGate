"""非补偿闸门（纯函数，可复现）。

把「能不能出具结论」从 prompt 里抽出来，写成「数据 + 纯函数」：

* 条款是数据（GateClause），随 profile 版本化，不写死在 if 里；
* 闸门是纯函数：输入 (claims, findings, artifacts, approvals, trail)，
  输出 GateResult(verdict, clauses, caveats, blocking, hash)，不调 LLM、不读时钟
  （时间由外部 evaluated_at 传入）；
* 三态：PASS / FAIL / NEEDS_HUMAN。UNKNOWN 不允许折叠成 PASS；
* 可复现：同一输入跑 100 次，GateResult.hash 必须完全一致。
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from engine.spine.ir import (
    digest, canonical_json, Claim, Finding, Approval, TrailSnapshot,
    UNVERIFIED, VERIFIED, UNSUPPORTED, DEFERRED,
)

PASS = "pass"
FAIL = "fail"
NEEDS_HUMAN = "needs_human"

KIND_COVERAGE = "coverage"
KIND_CLAIMS = "claims_exist"
KIND_CITATION = "citation"
KIND_CONTRADICTION = "contradiction"
KIND_MISSING = "missing"
KIND_ARITHMETIC = "arithmetic"
KIND_SEPARATION = "separation"
KIND_SIGNING = "signing"
KIND_TRAIL = "trail"
KIND_DEFER = "defer"


@dataclass
class GateClause:
    code: str
    title: str
    kind: str
    predicate: Callable = None
    severity: str = "block"
    status: str = ""
    detail: str = ""

    def check(self, ctx):
        if self.predicate is None:
            self.status = FAIL
            self.detail = "未定义判定函数"
            return FAIL, self.detail
        try:
            st, det = self.predicate(ctx)
            if st is True:
                st = PASS
            elif st is False:
                st = FAIL
            self.status = st
            self.detail = det
            return st, det
        except Exception as e:
            self.status = FAIL
            self.detail = "条款执行异常: %s" % e
            return FAIL, self.detail

    def to_dict(self):
        return {"code": self.code, "title": self.title, "kind": self.kind,
                "status": self.status, "detail": self.detail,
                "severity": self.severity}


@dataclass
class GateContext:
    claims: List[Claim]
    findings: List[Finding] = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    approvals: List[Approval] = field(default_factory=list)
    trail: Optional[TrailSnapshot] = None
    required_roles: list = field(default_factory=list)
    judge_roles: list = field(default_factory=list)

    def open_findings(self):
        return [f for f in self.findings if f.status in ("open", "deferred")]


@dataclass
class GateResult:
    verdict: str
    clauses: list
    caveats: list
    blocking: list
    issuable: bool
    hash: str = ""
    evaluated_at: str = ""

    def to_dict(self):
        return {
            "verdict": self.verdict,
            "clauses": [c.to_dict() if hasattr(c, "to_dict") else c for c in self.clauses],
            "caveats": self.caveats,
            "blocking": self.blocking,
            "issuable": self.issuable,
            "hash": self.hash,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            verdict=d.get("verdict", FAIL),
            clauses=d.get("clauses", []),
            caveats=d.get("caveats", []),
            blocking=d.get("blocking", []),
            issuable=d.get("issuable", False),
            hash=d.get("hash", ""),
            evaluated_at=d.get("evaluated_at", ""),
        )


def _all_verified(ctx):
    claims = ctx.claims or []
    if not claims:
        return False, "无任何主张"
    bad = [c.id for c in claims if c.status != VERIFIED]
    if bad:
        return False, "存在未通过引用的主张: %s" % ", ".join(bad)
    return True, ""


def _no_blocking_findings(ctx):
    blocking = [f for f in ctx.open_findings()
                if f.category in ("citation", "contradiction", "missing")]
    if blocking:
        return False, "存在阻断级发现: %s" % ", ".join(f.code for f in blocking)
    return True, ""


def _coverage_ok(ctx):
    from engine.spine.verify import coverage
    cov = coverage(ctx.artifacts, ctx.required_roles)
    if cov["missing"]:
        return False, "缺失必需材料: %s" % ", ".join(cov["missing"])
    return True, ""


def _separated(ctx):
    roles = ctx.judge_roles or ["claimant", "approver"]
    claimants = {a.actor for a in ctx.approvals if a.role == roles[0]}
    approvers = {a.actor for a in ctx.approvals if a.role == roles[1]} if len(roles) > 1 else set()
    if claimants & approvers:
        return False, "申报人与批准人存在同一人"
    return True, ""


def _signed_complete(ctx):
    need = ctx.judge_roles or ["claimant", "approver"]
    roles = {a.role for a in ctx.approvals}
    if all(r in roles for r in need):
        return True, ""
    return NEEDS_HUMAN, "签署不全（需 %s）" % " 与 ".join(need)


def _trail_ok(ctx):
    if ctx.trail is None:
        return False, "无审计链快照"
    if not ctx.trail.ok:
        return False, "审计链不完整"
    return True, ""


def _no_deferred(ctx):
    deferred = [f for f in ctx.findings if getattr(f, "status", "") == DEFERRED]
    if deferred:
        return NEEDS_HUMAN, "存在需人工研判的发现: %s" % ", ".join(f.code for f in deferred)
    return True, ""


def standard_clauses():
    return [
        GateClause("E3", "所有主张必须带可核验引用", KIND_CITATION, _all_verified),
        GateClause("D1", "阻断级发现必须清零", KIND_CONTRADICTION, _no_blocking_findings),
        GateClause("E1", "必需材料齐全", KIND_COVERAGE, _coverage_ok),
        GateClause("S1", "申报与批准职责分离", KIND_SEPARATION, _separated),
        GateClause("S2", "签署齐全", KIND_SIGNING, _signed_complete, severity="warn"),
        GateClause("J1", "挂起项需人工研判", KIND_DEFER, _no_deferred, severity="warn"),
        GateClause("T1", "审计链完整", KIND_TRAIL, _trail_ok),
    ]


def evaluate(clauses, ctx):
    results = []
    blocking = []
    caveats = []
    for cl in clauses:
        status, detail = cl.check(ctx)
        results.append(cl)
        if status == FAIL:
            if cl.severity == "block":
                blocking.append(cl.code)
            else:
                caveats.append("%s: %s" % (cl.code, detail))
        elif status == NEEDS_HUMAN:
            caveats.append("%s: 需人工复核 - %s" % (cl.code, detail))
    if ctx.trail is not None and getattr(ctx.trail, "anchored", False) is False:
        caveats.append("未外部锚定：链头未做外部时间戳/签名锚定，本结论仅可核验、不可抵赖")
    if blocking:
        verdict = FAIL
    elif any(getattr(c, "status", "") == NEEDS_HUMAN for c in results):
        verdict = NEEDS_HUMAN
    else:
        verdict = PASS
    res = GateResult(verdict=verdict, clauses=results, caveats=caveats,
                     blocking=blocking, issuable=(verdict == PASS))
    res.hash = hash_of(res.to_dict())
    return res


def hash_of(d):
    return digest(d)


def from_profile(profile, ctx, evaluated_at=""):
    clauses = getattr(profile, "clauses", None) or standard_clauses()
    if not ctx.required_roles and getattr(profile, "required_roles", None):
        ctx.required_roles = list(profile.required_roles)
    if not ctx.judge_roles and getattr(profile, "judge_roles", None):
        ctx.judge_roles = list(profile.judge_roles)
    res = evaluate(clauses, ctx)
    res.evaluated_at = evaluated_at
    # 时间不进 hash，且 hash 字段本身不进 hash：可复现核心只含
    # verdict/clauses/caveats/blocking/issuable（见 CORE_KEYS）
    res.hash = hash_of(core_of(res.to_dict()))
    return res


# 证据包导出（来自 spine.bundle，这里再导出一次，方便从 gate 入口统一调用）。
from engine.spine.bundle import build_bundle  # noqa: E402


# 参与 hash 的字段是**封闭集合**。界面层会往结论里加 stale/requirement 之类
# 只用于展示的键；若用「排除法」重算，多一个键就复算失败，把好结论误判成被篡改。
CORE_KEYS = ("verdict", "clauses", "caveats", "blocking", "issuable")
# 条款里哪些字段进 hash。展示层会往条款里加 requirement 之类的别名字段，
# 这些必须在复算前剔掉，否则「加了个给人看的字段」= 「结论被篡改」。
CLAUSE_CORE_KEYS = ("code", "title", "kind", "status", "detail", "severity")


def core_of(gate_dict):
    """取出可复现核心：只认 CORE_KEYS，多出来的展示字段一律不参与。"""
    core = {k: gate_dict[k] for k in CORE_KEYS if k in gate_dict}
    if isinstance(core.get("clauses"), list):
        core["clauses"] = [
            {k: cl[k] for k in CLAUSE_CORE_KEYS if k in cl} if isinstance(cl, dict) else cl
            for cl in core["clauses"]]
    return core


def verify_stored(gate_dict):
    """对存储的闸门结论做复算校验：用核心字段重算 hash。"""
    if not isinstance(gate_dict, dict):
        return {"ok": False, "reason": "not-a-dict"}
    recomputed = hash_of(core_of(gate_dict))
    return {"ok": recomputed == gate_dict.get("hash"),
            "stored": gate_dict.get("hash"), "recomputed": recomputed}
