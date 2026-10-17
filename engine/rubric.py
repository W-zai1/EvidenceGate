"""
评分标准版本（rubric version）与结果可复现性。

问题
----
改一次权重、换一次维度、调一次阈值，历史分数就和新分数**不可比**了。
但库里两条记录长得一模一样，界面上都是"7.8 分 B 级"。
于是 HR 会拿三周前的分数和今天的分数直接排序——而它们其实来自两把不同的尺子。

做法
----
把"这次打分依据的全部口径"哈希成一个短版本号，写进每条结果：

  rubric_version = sha256(维度 + 权重 + 提示词指纹 + 分档阈值 + 模型 + 温度 + 重复次数)[:8]

于是：
  · 同一版本内的分数可以横向比；
  · 版本不同时，界面必须提示"口径已变，分数不可直接比较"；
  · 标定/漂移监控按版本分组，才能看出"换模型导致的分数漂移"而不是"换口径"。

注意：rubric_version 里**包含模型名**是有意为之——换模型等于换尺子，
这一点必须体现在数据里，否则漂移分析会得出错误结论。

提示词指纹为什么是自动算的
--------------------------
原来靠一个手工维护的 `PROMPT_REVISION = "2026.11-a"`，注释里写"改动任何 system
prompt 必须 +1"。这是**约定**，不是**机制**：半年后有人优化一句提示词、
忘了改版本号，历史分数就会被当作同口径数据参与对比和漂移分析——
而这正是本模块想防的事。现在改为对实际提示词文本取哈希，
改一个字版本号自动变，忘不了。
"""
import hashlib

from util import DIMENSIONS, normalize_weights

# 分档阈值（改了就换尺子）
GRADE_THRESHOLDS = {"A": 8.0, "B": 6.5}

# 流程结构（用于版本哈希）：轮次、评委数、聚合方式
DEBATE_ROUNDS = 2
JUDGE_COUNT = 3
AGGREGATION = "median"

# 手工版本号仅作为"流程意图"的标记保留（例如整体重构），
# 真正防漏的是自动的 prompt_fingerprint()。
PROMPT_REVISION = "2026.11-a"

# 影响分数的采样温度（由 agents 提供，避免循环 import 时留空）
_EXTRA_SIGNATURE = {}


def register_signature_parts(**parts):
    """把运行期的口径参数注册进来（温度、重复次数等）。

    agents.py 在 import 时调用。用注册而不是直接 import，是为了避免
    agents ↔ rubric 的循环依赖。
    """
    _EXTRA_SIGNATURE.update({k: v for k, v in parts.items() if v is not None})


def _prompt_texts():
    """取当前实际使用的提示词。取不到时返回空——**不能伪造**。"""
    try:
        import agents
        return agents.prompt_bundle()
    except Exception:
        return []


def prompt_fingerprint():
    """对实际提示词文本取哈希（12 位）。

    取不到提示词时返回空串而不是瞎编一个值：空串会让 version 计算里
    那一项变成"未知"，总比给出一个看起来正常的假哈希好。
    """
    texts = _prompt_texts()
    if not texts:
        return ""
    h = hashlib.sha256()
    for t in texts:
        h.update((t or "").encode("utf-8"))
        h.update(b"\x1e")     # 分隔符：防止拼接歧义
    return h.hexdigest()[:12]


def signature(weights=None, model=None, provider=None, rubric_config=None):
    """计算当前口径的完整签名。

    返回 {"version": "r7c3a1f2", "parts": {...}}——parts 用于界面上"为什么版本变了"。

    rubric_config：岗位级评审细则。它进签名意味着**两个岗位可以有不同版本号**，
    这正是我们要的：A 岗的权重/细则与 B 岗不同，就不该被当成同一把尺子。
    只在全局（不传岗位）时退回默认细则。
    """
    cfg = normalize_config(rubric_config, base_weights=weights)
    # 权重以 cfg 为准（它已经吸收了传入的 weights），这样"界面显示的权重"
    # 和"算分用的权重"不会各说一套。
    w = cfg["weights"]
    parts = {
        "dimensions": list(DIMENSIONS),
        "weights": {d: round(w[d], 4) for d in DIMENSIONS},
        "thresholds": dict(GRADE_THRESHOLDS),
        "prompt_revision": PROMPT_REVISION,
        "prompt_fingerprint": prompt_fingerprint(),
        "rubric_fingerprint": config_fingerprint(cfg),
        "debate_rounds": DEBATE_ROUNDS,
        "judge_count": JUDGE_COUNT,
        "aggregation": AGGREGATION,
        "model": (model or "").strip().casefold(),
        "provider": (provider or "").strip().casefold(),
    }
    for k, v in _EXTRA_SIGNATURE.items():
        parts[k] = v
    raw = "|".join([
        ",".join(parts["dimensions"]),
        ",".join("%s=%.4f" % (d, parts["weights"][d]) for d in DIMENSIONS),
        ",".join("%s>%.1f" % (k, parts["thresholds"][k]) for k in sorted(parts["thresholds"])),
        parts["prompt_revision"],
        parts["prompt_fingerprint"],
        parts["rubric_fingerprint"],
        "r%d" % DEBATE_ROUNDS,
        "j%d" % JUDGE_COUNT,
        parts["aggregation"],
        parts["model"],
        parts["provider"],
        ",".join("%s=%s" % (k, parts[k]) for k in sorted(_EXTRA_SIGNATURE)),
    ])
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return {"version": "r" + h, "parts": parts}


def version_only(weights=None, model=None, provider=None, rubric_config=None):
    return signature(weights, model, provider, rubric_config)["version"]


def compare(a, b):
    """比较两个版本号，返回人类可读的差异说明（version 相同则 None）。"""
    if not a or not b or a == b:
        return None
    return ("两次评审的口径版本不同（%s ≠ %s）：维度/权重/阈值/提示词/模型/温度任一项变化都会换尺子，"
            "分数与等级**不可直接横向比较**。" % (a, b))


# ---------------------------------------------------------------------------
# 岗位级评审细则（权重 + 每个维度"看什么"）
#
# 为什么要有预设：权重手填看起来自由，实际上是在让人"随便设"。
# 三个维度的权重没有一个客观正确答案，但**有依据可以讲**：
#   · 硬实力门槛（学历/年限/证书）权重高，适用于有硬性准入条件的岗位
#     —— 这类条件的判断是二值的，交给面试官去"感觉"反而更不一致；
#   · 风险与软素质权重高，适用于协作/合规/带人风险大的岗位
#     （销售、财务、管理），因为这类岗位的失败成本主要不在技能上；
#   · 岗位能力匹配度权重高，适用于技术栈/经验高度特化的岗位。
# 预设表达的是"这类岗位常见的重要性分布"，不是真理。改它不需要理由，
# 但改完版本号会变，与旧结果不可比——这一点必须让人看见。
# ---------------------------------------------------------------------------
WEIGHT_MAX = 100
CRITERIA_MAX = 800
NOTES_MAX = 800

PRESETS = {
    "engineering": {
        "label": "研发 / 工程",
        "desc": "技术栈与项目深度是主要区分度，硬门槛（学历/年限）作减分项。",
        "weights": {DIMENSIONS[0]: 0.45, DIMENSIONS[1]: 0.25, DIMENSIONS[2]: 0.30},
        "criteria": {
            DIMENSIONS[0]: "只看与 JD 主线职责直接相关的技术深度与规模：做过什么系统、多大流量/数据量、"
                           "他本人负责哪一层。框架名称堆砌不算能力；「参与」与「负责」要区分开。",
            DIMENSIONS[1]: "关注协作方式、代码/文档习惯、故障与事故的处理态度。"
                           "重点是能否被追责与复盘，不是性格好坏。",
            DIMENSIONS[2]: "学历、年限、必备技能是否存在**硬性缺口**。"
                           "满足即满分起算，不因为学校更好而额外加分。",
        },
        "why": "研发岗的产出主要由能力和实际做过的事决定；学历的作用集中在筛掉硬性不符的人，"
               "不应成为排序主轴。",
    },
    "product": {
        "label": "产品 / 设计",
        "desc": "看判断力与落地痕迹，软素质中「能否被说服、能否说服别人」权重偏高。",
        "weights": {DIMENSIONS[0]: 0.40, DIMENSIONS[1]: 0.35, DIMENSIONS[2]: 0.25},
        "criteria": {
            DIMENSIONS[0]: "找得到「他发现了一个问题、做了什么决策、结果如何」的完整链条吗？"
                           "只有功能罗列没有取舍理由，不算能力。",
            DIMENSIONS[1]: "跨团队推进的证据：和谁有冲突、怎么解决的。"
                           "文档与沟通是产品的主要交付物，不能当成加分项而应当是主项。",
            DIMENSIONS[2]: "行业经验与必备工具的硬性要求是否满足。",
        },
        "why": "产品岗的失败多数不是「不会做」而是「做错了方向、推不动」，"
               "所以判断力与协作证据的权重应高于学历。",
    },
    "sales": {
        "label": "销售 / BD",
        "desc": "业绩数字是否可信 + 抗压与合规边界，学历权重最低。",
        "weights": {DIMENSIONS[0]: 0.30, DIMENSIONS[1]: 0.45, DIMENSIONS[2]: 0.25},
        "criteria": {
            DIMENSIONS[0]: "业绩必须能拆到「卖什么、卖给谁、周期多长、他个人的贡献占比」。"
                           "只有总额没有口径的数字一律存疑，不要在打分上替他补全。",
            DIMENSIONS[1]: "抗压、被拒绝后的恢复、以及**合规边界**（是否愿意为了签单越线）。"
                           "后者是这条线上一票否决项，但应由人判断，不是分数自动淘汰。",
            DIMENSIONS[2]: "行业资源与语言/资质等硬条件。",
        },
        "why": "销售岗的能力高度依赖平台与产品，简历上的数字最容易注水；"
               "而软素质与合规风险是这类岗位真正的失败来源。",
    },
    "operations": {
        "label": "运营 / 市场",
        "desc": "数据意识与执行闭环，介于产品与销售之间。",
        "weights": {DIMENSIONS[0]: 0.40, DIMENSIONS[1]: 0.35, DIMENSIONS[2]: 0.25},
        "criteria": {
            DIMENSIONS[0]: "能否说清「拉新/留存/转化」里他负责哪一环、用什么手段、指标变化多少。"
                           "只写「负责XX活动」而没有前后对比数字的，不算证据。",
            DIMENSIONS[1]: "与产品/销售/供应商的协作，以及预算与资源的使用是否可追溯。",
            DIMENSIONS[2]: "必备工具与渠道经验的硬性要求。",
        },
        "why": "运营的产出常被团队业绩掩盖，所以更依赖本人能否讲清归因；"
               "学历与这条线的工作质量相关性最弱。",
    },
    "function": {
        "label": "职能 / 支持（HR、财务、法务、行政）",
        "desc": "硬门槛权重最高：资质与年限在这类岗位上就是准入条件。",
        "weights": {DIMENSIONS[0]: 0.30, DIMENSIONS[1]: 0.30, DIMENSIONS[2]: 0.40},
        "criteria": {
            DIMENSIONS[0]: "流程与制度建设的完整度：他做过的东西现在还在用吗、"
                           "有没有留下可复用的规范。",
            DIMENSIONS[1]: "保密与合规意识、对敏感信息的处理习惯。这一类岗位的失误代价是事故级的。",
            DIMENSIONS[2]: "执业资格、专业年限、专业背景是否为硬性必须。"
                           "这一维在这类岗位上通常是一票否决的前置条件。",
        },
        "why": "职能岗有明确的外部准入（证书、年限、专业），"
               "把硬门槛权重调高是对现实的描述，不是歧视。",
    },
    "campus": {
        "label": "校招 / 应届 / 初级",
        "desc": "没有工作履历，硬门槛与可培养性权重上调，项目深度权重下调。",
        "weights": {DIMENSIONS[0]: 0.30, DIMENSIONS[1]: 0.30, DIMENSIONS[2]: 0.40},
        "criteria": {
            DIMENSIONS[0]: "看实习/课程项目里**他本人动手的那部分**：能讲清技术细节与踩过的坑。"
                           "学术项目与竞赛奖项只作为佐证，不作为能力本身。",
            DIMENSIONS[1]: "学习速度与求助方式：遇到不会的东西怎么补。"
                           "应届生的成长曲线比当前水平更重要。",
            DIMENSIONS[2]: "学历层次、专业对口度、必备基础课。校招里这一维承担主要的准入筛选作用。",
        },
        "why": "应届生没有可比较的工作产出，硬性条件与可培养性自然成为主要可判断依据；"
               "把项目深度权重压低，是为了避免被包装过的课程项目误导。",
    },
    "senior": {
        "label": "专家 / 管理",
        "desc": "看决策影响面与带人/担责证据，学历几乎不再区分。",
        "weights": {DIMENSIONS[0]: 0.40, DIMENSIONS[1]: 0.40, DIMENSIONS[2]: 0.20},
        "criteria": {
            DIMENSIONS[0]: "他做的决策影响了多少人/多少钱/多长时间？"
                           "只写「负责XX团队」而没有具体决策与后果的，不算能力证据。",
            DIMENSIONS[1]: "担责记录：出问题时是否承担、如何复盘；"
                           "以及他带出来的人现在怎么样。",
            DIMENSIONS[2]: "年限与背景是否达到该层级的常规门槛。"
                           "这一维在这个层级区分度很低，主要用于排除明显不符。",
        },
        "why": "高层级岗位的差异体现在决策质量与担责上，学历与年限的解释力已经很低，"
               "所以硬门槛权重下调。",
    },
}

DEFAULT_PRESET = "engineering"


def _dim_index(name):
    return DIMENSIONS.index(name) if name in DIMENSIONS else -1


def _clean_text(v, limit):
    return (v or "").strip()[:limit]


def preset_payload(key):
    """预设的可序列化副本（含 why，让用户知道凭什么这么设）。"""
    p = PRESETS.get(key) or PRESETS[DEFAULT_PRESET]
    return {
        "key": key if key in PRESETS else DEFAULT_PRESET,
        "label": p["label"],
        "desc": p["desc"],
        "why": p.get("why", ""),
        "weights": {d: round(p["weights"][d], 4) for d in DIMENSIONS},
        "criteria": {d: p["criteria"].get(d, "") for d in DIMENSIONS},
    }


def default_config():
    return normalize_config({"preset": DEFAULT_PRESET})


def normalize_config(cfg, base_weights=None):
    """校验并规范化岗位评审细则。

    容错策略：**能修的修，修不了的退回默认**，不抛错。
    理由是这个对象会被旧数据、手写 JSON、前端脏值喂进来；
    一次格式错误不应该让整个岗位页打不开。真正需要拒绝的情况
    （比如评审进行中改口径）由 API 层判断。

    base_weights：调用方已知的"真实权重"（jobs 表里存的那份）。
    当 cfg 里没有权重时用它，避免"界面显示的权重"和"算分用的权重"不一致。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    preset = cfg.get("preset")
    if preset not in PRESETS:
        preset = DEFAULT_PRESET

    base = base_weights if isinstance(base_weights, dict) else None
    fallback = {}
    for d in DIMENSIONS:
        f = None
        if base:
            try:
                f = float(base.get(d, PRESETS[preset]["weights"][d]))
            except (TypeError, ValueError):
                f = None
        if f is None:
            f = float(PRESETS[preset]["weights"][d])
        fallback[d] = f

    src_w = cfg.get("weights")
    src_w = src_w if isinstance(src_w, dict) else {}

    # 权重有两种常见写法：小数（0.45）和百分比（45）。**必须先统一量纲**。
    # 踩过的坑：早期直接 `max(1.0, v)` 把小数当百分比，于是 0.45/0.25/0.30
    # 全被抬到 1.0，归一化后变成 33/33/33——用户设的权重被静默抹平，
    # 界面上显示的还是他填的值，算分用的却已经是三等分。
    def _is_fraction(vals):
        return bool(vals) and max(vals) <= 1.0 + 1e-9

    src_vals = []
    for d in DIMENSIONS:
        try:
            src_vals.append(float(src_w[d]))
        except (KeyError, TypeError, ValueError):
            pass
    src_scale = 100.0 if (src_vals and not _is_fraction(src_vals)) else 1.0
    # fallback 一定来自 PRESETS 或 normalize_weights，都是小数
    raw = {}
    for d in DIMENSIONS:
        if d in src_w:
            try:
                v = float(src_w[d]) / src_scale
            except (TypeError, ValueError):
                v = fallback[d]
        else:
            v = fallback[d]
        # 最小 0.001（约 0.1%）：允许极端倾斜，但不允许某一维被彻底归零，
        # 否则那一维的分数无论多低都不影响结果，等于没评
        raw[d] = max(0.001, min(1.0, v))
    weights = normalize_weights(raw)

    src_c = cfg.get("criteria")
    src_c = src_c if isinstance(src_c, dict) else {}
    criteria = {}
    for d in DIMENSIONS:
        v = src_c.get(d)
        criteria[d] = _clean_text(v, CRITERIA_MAX) if isinstance(v, str) else \
            PRESETS[preset]["criteria"].get(d, "")

    return {
        "preset": preset,
        "weights": weights,
        "criteria": criteria,
        "notes": _clean_text(cfg.get("notes"), NOTES_MAX),
    }


def config_fingerprint(cfg):
    """评审细则的指纹（12 位）。用于进 rubric_version。

    为什么不并进 prompt_fingerprint：提示词指纹算的是**代码里的模板**，
    它是全局的；评审细则是**每个岗位各自**的文本。混在一起会导致
    "A 岗位改了细则，B 岗位的版本号也跟着变"，那就没法判断
    "这次和上次是同口径吗"。
    """
    cfg = normalize_config(cfg)
    raw = "|".join(
        [cfg["preset"]]
        + ["%s=%.4f" % (d, cfg["weights"][d]) for d in DIMENSIONS]
        + ["%s:%s" % (d, cfg["criteria"][d]) for d in DIMENSIONS]
        + [cfg["notes"]]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def describe(weights=None, model=None, provider=None, rubric_config=None):
    """给界面展示的"当前口径"说明。"""
    sig = signature(weights, model, provider, rubric_config)
    p = sig["parts"]
    return {
        "version": sig["version"],
        "dimensions": p["dimensions"],
        "weights": p["weights"],
        "thresholds": p["thresholds"],
        "prompt_revision": p["prompt_revision"],
        "prompt_fingerprint": p["prompt_fingerprint"],
        "rubric_fingerprint": p.get("rubric_fingerprint", ""),
        "debate_rounds": p["debate_rounds"],
        "judge_count": p["judge_count"],
        "aggregation": p["aggregation"],
        "model": p["model"],
        "provider": p["provider"],
    }
