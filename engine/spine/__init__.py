"""EvidenceGate 证据脊柱（领域无关）。

分层
----
    spine.ir          中间表示：Claim / EvidenceRef / Finding / GateResult ...
    spine.verify      引用核验（确定性，不调模型）
    spine.gate        非补偿闸门（纯函数，可复现）
    spine.bundle      证据包导出 + 离线校验脚本
    spine.profiles    场景配置：talent / expense（注册即可扩展）

刻意不做的事
------------
这里不 import llm_client、不读数据库、不读时钟。脊柱只处理「已经拿到的主张
与材料」，保证闸门可复现、可被任何入口复用。
"""
from engine.spine.ir import (
    Artifact, Approval, Case, Claim, EvidenceRef, Finding, TrailSnapshot,
    VERIFIED, UNVERIFIED, UNSUPPORTED, DEFERRED,
    canonical_json, digest,
)
from engine.spine.verify import (
    audit_claims, coverage, locate, verify_claim, verify_claims, sums_equal,
)
from engine.spine.gate import (
    GateClause, GateContext, GateResult, NEEDS_HUMAN, PASS, FAIL,
    evaluate, from_profile, hash_of, standard_clauses,
    KIND_CLAIMS, KIND_CITATION, KIND_CONTRADICTION, KIND_COVERAGE,
    KIND_DEFER, KIND_MISSING, KIND_SEPARATION, KIND_TRAIL,
)
from engine.spine.bundle import build_bundle
from   engine.spine.profiles import registry as profiles

__all__ = [
    "Artifact", "Approval", "Case", "Claim", "EvidenceRef", "Finding",
    "TrailSnapshot", "VERIFIED", "UNVERIFIED", "UNSUPPORTED", "DEFERRED",
    "canonical_json", "digest",
    "audit_claims", "coverage", "locate", "verify_claim", "verify_claims", "sums_equal",
    "GateClause", "GateContext", "GateResult", "PASS", "FAIL", "NEEDS_HUMAN",
    "evaluate", "from_profile", "build_bundle", "profiles",
    "hash_of", "standard_clauses",
    "KIND_CLAIMS", "KIND_CITATION", "KIND_CONTRADICTION", "KIND_COVERAGE",
    "KIND_DEFER", "KIND_MISSING", "KIND_SEPARATION", "KIND_TRAIL",
]
