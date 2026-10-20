"""费用报销 profile（示例场景）。"""
from engine.spine.profiles.base import Profile
from   engine.spine.gate import standard_clauses


def build():
    return Profile(
        key="expense",
        version="2026.10.1",
        label="expense@2026.10.1",
        judge_count=1,
        required_roles=["invoice", "approval", "policy"],
        clauses=standard_clauses(),
        judge_roles=["claimant", "approver"],
        dimensions=["真实性", "金额一致", "审批合规", "费用标准"],
    )
