"""证据中间表示（IR，领域无关）。

EvidenceGate 脊柱的底层数据契约。关键设计：

* EvidenceRef 必须带定位：artifact_id + locator(页码/段落) + quote(逐字引文) +
  可选 char_start/char_end。只记「引用自发票 P.12」不算回链——无法定位到句，
  就无法机器核验，也无法在原文里高亮。
* 序列化必须可复现：同一对象在任何机器上 ``digest()`` 相同。统一用
  ``canonical_json()``（键排序、无空格、UTF-8 原样），不用 repr / pickle。

本文件不 import llm_client、不读数据库、不读时钟——脊柱只处理「已经拿到的主张
与材料」，保证闸门可复现、可被任何入口（HTTP / CLI / 测试）复用。
"""
import hashlib
import json
from dataclasses import dataclass, field, asdict

# Claim 的四种核验状态。UNVERIFIED 是"还没验"，不是"通过"。
UNVERIFIED = "unverified"
VERIFIED = "verified"
UNSUPPORTED = "unsupported"
DEFERRED = "deferred"

# Finding 的分类：闸门条款按 category 选，而不是按文案猜。
CAT_CITATION = "citation"
CAT_CONTRADICTION = "contradiction"
CAT_MISSING = "missing"
CAT_ARITHMETIC = "arithmetic"
CAT_JUDGMENT = "judgment"
CAT_PROCESS = "process"

# Finding 的状态。
OPEN = "open"
CLOSED = "closed"
RESOLVED = "resolved"


def canonical_json(obj):
    """键序稳定的 JSON。同一内容在任何机器上得到同一串字节。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def digest(obj):
    """对象级指纹：对规范化 JSON 取 SHA-256。"""
    return sha256_text(canonical_json(obj))


def short(h, n=8):
    return (h or "")[:n]


@dataclass
class Artifact:
    """一份原始材料：发票 / 审批流 / 制度 / 合同 ……"""
    id: str
    role: str
    filename: str = ""
    text: str = ""
    sha256: str = ""

    def ensure_hash(self):
        if not self.sha256:
            self.sha256 = sha256_text(self.text)
        return self.sha256

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k, "") for k in ("id", "role", "filename", "text", "sha256")})


@dataclass
class EvidenceRef:
    """一条引用：回到某份材料的某句话。

    char_start/char_end 是可选字符级区间；给了就做精确高亮，没给就只查引文是否逐字命中。
    """
    artifact_id: str
    locator: str = ""
    quote: str = ""
    sha256: str = ""
    char_start: int = None
    char_end: int = None

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d):
        return cls(
            artifact_id=d.get("artifact_id", ""),
            locator=d.get("locator", ""),
            quote=d.get("quote", ""),
            sha256=d.get("sha256", ""),
            char_start=d.get("char_start"),
            char_end=d.get("char_end"),
        )


@dataclass
class Claim:
    """一条可被证伪的论断，必须带原文引用。"""
    id: str
    text: str = ""
    citations: list = field(default_factory=list)
    status: str = UNVERIFIED
    note: str = ""

    def to_dict(self):
        return {
            "id": self.id,
            "text": self.text,
            "citations": [c.to_dict() if isinstance(c, EvidenceRef) else c for c in self.citations],
            "status": self.status,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d.get("id", ""),
            text=d.get("text", ""),
            citations=[EvidenceRef.from_dict(c) if isinstance(c, dict) else c for c in d.get("citations", [])],
            status=d.get("status", UNVERIFIED),
            note=d.get("note", ""),
        )

    def digest(self):
        return digest(self.to_dict())


@dataclass
class Finding:
    """一次核验/裁定产物。"""
    code: str
    title: str = ""
    category: str = CAT_PROCESS
    status: str = OPEN
    detail: str = ""
    severity: str = "info"

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k, "") for k in ("code", "title", "category", "status", "detail", "severity")})


@dataclass
class Approval:
    """一次签署动作（职责分离用）。role 为 claimant/approver/reviewer 等。"""
    role: str
    actor: str
    timestamp: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class TrailSnapshot:
    """审计链快照：用于判定「链是否完整、是否已外部锚定」。"""
    ok: bool
    count: int = 0
    head: str = ""
    anchored: bool = False

    def to_dict(self):
        return asdict(self)


@dataclass
class Case:
    """一次审查活动的边界：材料集合、标准版本、责任人。"""
    id: str
    title: str = ""
    profile_key: str = ""
    profile_version: str = ""
    created_at: str = ""

    def to_dict(self):
        return asdict(self)
