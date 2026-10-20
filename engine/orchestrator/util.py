"""
共享工具层。

之前 `_extract_json` 在 agents.py / interview.py / jd_struct.py 各抄了一份，
`DIMENSIONS` 在 4 个文件里各定义一次，`WEIGHTS` 有两份——一旦改维度口径，
必然有一处忘改，于是"同一个候选人，评委按三维打分，聚合按另一套权重算"。

这里收成单一来源。新增模块请从这里导入，不要再复制粘贴。
"""
import json
import os
import re
from   datetime import datetime

# ---------------------------------------------------------------------------
# 评审维度与默认权重（唯一来源）
# ---------------------------------------------------------------------------
DIMENSIONS = ["岗位能力匹配度", "风险与软素质", "硬实力门槛"]

DEFAULT_WEIGHTS = {
    "岗位能力匹配度": 0.4,
    "风险与软素质": 0.3,
    "硬实力门槛": 0.3,
}


def normalize_weights(weights):
    """把任意形态的权重归一化到和为 1。

    HR 可能填 3 个 10，也可能只填一部分。归一化而不是报错：
    界面上"权重会自动归一化"这句话必须是真的。
    """
    src = weights if isinstance(weights, dict) else {}
    clean = {}
    for d in DIMENSIONS:
        try:
            v = float(src.get(d, DEFAULT_WEIGHTS[d]))
        except (TypeError, ValueError):
            v = DEFAULT_WEIGHTS[d]
        if v < 0:
            v = DEFAULT_WEIGHTS[d]
        clean[d] = v
    total = sum(clean.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: round(v / total, 4) for k, v in clean.items()}


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def fmt_minute(iso):
    return (iso or "").replace("T", " ")[:16]


# ---------------------------------------------------------------------------
# JSON 解析（容错，但不编造）
# ---------------------------------------------------------------------------
def extract_json(text):
    """从模型回复里取出 JSON 对象。

    模型常把 JSON 包在 ```json 围栏里，或在前后加一句解释。三层容错：
    去围栏 → 直接解析 → 截取首个 '{' 到末个 '}' 再解析。
    全都失败就抛错——绝不用本地数据顶替，否则分数成了脚本编的。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模型回复为空")
    s = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        return json.loads(s[i:j + 1])
    raise ValueError("回复里找不到 JSON 对象")


_FLATTEN_KEYS = ("claim", "point", "content", "text", "argument", "reason",
                 "description", "support", "evidence", "quote", "summary")


def as_text_list(value):
    """把模型返回的"列表"统一压平成字符串列表。

    模型并不总是照 schema 走：`pro_evidence` 可能是字符串，也可能是
    {"claim":..., "support":...} 这类对象，甚至嵌套数组。上游一个 dict
    就能把整个流水线炸掉（TypeError: expected str instance, dict found）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, dict):
        parts = []
        for k in _FLATTEN_KEYS:
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        if not parts:
            for k, v in sorted(value.items()):
                if isinstance(v, (str, int, float)) and str(v).strip():
                    parts.append("%s：%s" % (k, v))
                elif isinstance(v, list):
                    parts.extend(as_text_list(v))
        if not parts:
            return []
        joined = "；".join(parts) if len(parts) > 1 else parts[0]
        return [p.strip() for p in joined.split("\n") if p.strip()]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(as_text_list(item))
        return out
    if isinstance(value, bool):
        return []
    s = str(value).strip()
    return [s] if s else []


def as_str_list(value):
    """只接受字符串/字符串列表的严格版本（用于 JD 字段）。"""
    if isinstance(value, str):
        return [x.strip() for x in value.splitlines() if x.strip()]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            for k in ("text", "item", "content", "requirement", "description", "title"):
                if isinstance(item.get(k), str) and item[k].strip():
                    out.append(item[k].strip())
                    break
    return out


# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------
def mask_secret(key):
    """日志里只留首尾，别把密钥打进控制台。"""
    if not key:
        return "<none>"
    return key[:4] + "…" + key[-2:] if len(key) > 8 else "****"


_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name):
    """上传文件名落盘前的清理（防目录穿越 + 去非法字符）。"""
    base = os.path.basename(name or "file")
    base = _UNSAFE_NAME.sub("_", base).strip(" .")
    return base[:120] or "file"


def clamp(x, lo=0.0, hi=10.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:          # NaN
        return None
    return max(lo, min(hi, v))
