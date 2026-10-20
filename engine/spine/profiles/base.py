"""场景 profile 基类。

EvidenceGate 是领域无关的；「人才筛选」「费用报销」只是不同的 profile。新增一个场景 =
在 engine/spine/profiles/ 加一个文件并 registry.register()，脊柱一行不用改。
"""
from dataclasses import dataclass, field
from typing import List
from   engine.spine.ir import digest


@dataclass
class Profile:
    key: str
    version: str
    label: str
    judge_count: int = 1
    required_roles: List[str] = field(default_factory=list)
    clauses: list = field(default_factory=list)
    dimensions: list = field(default_factory=list)
    judge_roles: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "key": self.key, "version": self.version, "label": self.label,
            "judge_count": self.judge_count, "required_roles": self.required_roles,
            "judge_roles": self.judge_roles,
        }

    def digest(self):
        return digest(self.to_dict())
