"""人才筛选 profile（示例场景）。"""
from engine.spine.profiles.base import Profile
from   engine.spine.gate import standard_clauses, GateClause, KIND_CLAIMS, PASS


def _hard_barrier_ok(ctx):
    # 硬门槛：简历必须存在，否则无法判定硬性条件。
    roles = {a.role for a in ctx.artifacts}
    if "resume" in roles:
        return True, ""
    return False, "缺简历，硬门槛无法核验"


def build():
    clauses = standard_clauses() + [
        GateClause("H1", "硬门槛材料齐全", KIND_CLAIMS, _hard_barrier_ok),
    ]
    return Profile(
        key="talent",
        version="2026.10.1",
        label="talent@2026.10.1",
        judge_count=3,
        required_roles=["resume"],
        clauses=clauses,
        judge_roles=["maker", "checker", "approver"],
        dimensions=["匹配度", "软素质", "硬门槛"],
    )
