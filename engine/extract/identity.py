"""
身份消歧（Identity Resolution）。

为什么不再"同名即同一人"
------------------------
早期实现用 `姓名` 精确匹配判定是不是同一个人，并在 talent 库里用
`sha1(姓名)` 当主键。真实招聘里同名是常态（一个岗位收到三份「张伟」简历），
一旦撞名会出现两种灾难：

  · **静默合并**：第二个人上传的简历被追加到第一个人的记录后面，
    两人材料拼成一份 → 评审基于混合材料出结论，而界面上看不出任何异常；
  · **静默拆分**：同一个人换个名字写法（空格/全半角/英文名）就被当成新人。

两种都是"看起来正常的错误结论"，本项目最不能容忍的那类。

现在的做法：多信号证据 + 显式裁决
--------------------------------
强标识（phone / email 精确一致）→ 直接判定同一人。
弱标识（姓名 / 学历院校 / 所在地 / 工作经历单位 / 出生年份）→ 打分：

  一致得分高 → same        （自动合并）
  冲突得分高 → different   （自动分开，各建一条）
  两者都不够 → uncertain   （**必须由 HR 确认**，界面弹窗二选一）

`uncertain` 是这套设计的重点：宁可多问一句，也不要静默合并。
没有任何其他信号可比（比如只有姓名，材料里也没有学校/公司/城市）时，
同样落到 uncertain —— 因为"只有名字相同"根本不构成同一人的证据。

纯标准库实现：正则抽取 + 规则打分，可离线跑、可单测、结论可解释。
"""
import re

# ---------------------------------------------------------------------------
# 信号权重（强标识直接决定，不计分）
# ---------------------------------------------------------------------------
STRONG_SIGNALS = ("phone", "email")

# 弱信号权重：学历与工作单位最有辨识度，地点次之，姓名只作入场券
SIGNAL_WEIGHTS = {
    "name": 1.0,
    "education": 3.0,      # 学校 + 学历，重名者同校同专业的概率很低
    "work_history": 3.0,   # 任职单位重合是最强的行为证据
    "location": 1.5,
    "birth_year": 2.0,
}

# 裁决阈值（对弱信号总权重归一化后）
SAME_THRESHOLD = 0.65
DIFFERENT_THRESHOLD = 0.25
# 至少要比过这么多个弱信号，否则不足以自动判 same
MIN_SIGNALS_FOR_AUTO = 2

# ---------------------------------------------------------------------------
# 抽取用正则
# ---------------------------------------------------------------------------
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# 院校：以「大学 / 学院 / 学校 / 研究院」结尾的实体，或英文 University / College
_SCHOOL_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{2,20}(?:大学|学院|学校|研究院|职业技术学院))"
)
# 英文简历：Peking University / Tsinghua University / MIT / College
_SCHOOL_EN_RE = re.compile(
    r"\b([A-Z][A-Za-z&.\-]*(?:\s+[A-Z][A-Za-z&.\-]*){0,4}\s+"
    r"(?:University|College|Institute of Technology|Polytechnic|School))\b"
)
_DEGREE_WORDS = {
    "博士": "博士", "phd": "博士", "doctor": "博士",
    "硕士": "硕士", "研究生": "硕士", "master": "硕士", "mba": "硕士",
    "本科": "本科", "学士": "本科", "bachelor": "本科",
    "专科": "专科", "大专": "专科", "高职": "专科",
    "高中": "高中", "中专": "中专",
}

# 单位：以「公司 / 集团 / 科技 / 有限 / Inc / Ltd / Co」等结尾
_COMPANY_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{2,24}(?:股份有限公司|有限责任公司|有限公司|公司|集团|研究院|银行|事务所))"
)

# 知名企业白名单：简历里绝大多数时候只写「字节跳动」「腾讯」，
# 不会加「有限公司」。不认这些词，work_history 恒为空——
# 而它恰恰是身份比对里权重最高的信号之一（3.0），
# 白名单缺失等于把这个信号整体关掉。
_KNOWN_COMPANIES = (
    # 互联网 / 大厂
    "字节跳动", "阿里巴巴", "蚂蚁集团", "腾讯", "百度", "京东", "美团", "滴滴",
    "网易", "新浪", "搜狐", "小米", "华为", "快手", "拼多多", "携程", "去哪儿",
    "贝壳", "bilibili", "哔哩哔哩", "知乎", "小红书", "得物", "shopee", "虾皮",
    "微软", "谷歌", "亚马逊", "苹果", "英特尔", "英伟达", "高通", "三星", "索尼",
    "IBM", "Oracle", "SAP", "Adobe", "Salesforce", "Uber", "Airbnb",
    # 金融
    "工商银行", "建设银行", "农业银行", "中国银行", "交通银行", "招商银行",
    "浦发银行", "中信银行", "民生银行", "兴业银行", "平安银行", "光大银行",
    "华夏银行", "广发银行", "邮储银行", "国家开发银行",
    "中金", "中信证券", "华泰证券", "国泰君安", "招商证券", "广发证券",
    "平安", "泰康", "中国人寿", "太平洋保险", "友邦",
    # 制造 / 通信 / 能源 / 地产
    "比亚迪", "蔚来", "小鹏", "理想汽车", "特斯拉", "宁德时代", "大疆",
    "中国移动", "中国电信", "中国联通", "国家电网", "中石油", "中石化",
    "万科", "碧桂园", "恒大", "保利", "龙湖",
    # 咨询 / 会计 / 外企
    "麦肯锡", "波士顿咨询", "贝恩", "德勤", "普华永道", "安永", "毕马威",
    "埃森哲", "宝洁", "联合利华", "欧莱雅", "玛氏",
)
# 长名优先匹配，避免「中国银行」被「中信银行」之类截断
_KNOWN_COMPANIES_SORTED = tuple(sorted(_KNOWN_COMPANIES, key=len, reverse=True))

# 城市：常见城市名 + "市"，用于抽所在地
_CITY_RE = re.compile(
    r"(北京|上海|广州|深圳|杭州|成都|武汉|南京|西安|苏州|天津|重庆|长沙|郑州|青岛|"
    r"合肥|福州|厦门|济南|大连|宁波|无锡|东莞|佛山|珠海|沈阳|哈尔滨|昆明|南昌|贵阳|"
    r"太原|石家庄|温州|常州|徐州|泉州|嘉兴|南通|绍兴|烟台|潍坊|保定|兰州|海口|乌鲁木齐|"
    r"呼和浩特|南宁|银川|西宁|拉萨|长春|三亚)(?:市)?"
)
_LOCATION_HINT_RE = re.compile(
    r"(现居|居住地|所在地|地址|城市|籍贯|期望工作地|工作地点)[：:\s]*([^\n，,。；;]{2,20})"
)

# 常见院校简称 → 规范名。简历里「浙大 / 浙江大学」混用极普遍，
# 不归一就会把同一个人判成"院校无重合"，于是本该自动合并的落进人工确认。
# 只收高频项；未收录的简称仍走原样比较（保守，不猜测）。
UNIV_ALIAS = {
    "浙大": "浙江大学", "北大": "北京大学", "清华": "清华大学",
    "复旦": "复旦大学", "上交": "上海交通大学", "上海交大": "上海交通大学",
    "中科大": "中国科学技术大学", "中国科大": "中国科学技术大学",
    "南大": "南京大学", "武大": "武汉大学", "华科": "华中科技大学",
    "华中科大": "华中科技大学", "中山大学": "中山大学", "哈工大": "哈尔滨工业大学",
    "西交": "西安交通大学", "西安交大": "西安交通大学", "同济": "同济大学",
    "人大": "中国人民大学", "北航": "北京航空航天大学", "北邮": "北京邮电大学",
    "东南": "东南大学", "川大": "四川大学", "重大": "重庆大学",
    "厦大": "厦门大学", "山大": "山东大学", "吉大": "吉林大学",
    "湖大": "湖南大学", "中南": "中南大学", "天大": "天津大学",
    "南开": "南开大学", "北理工": "北京理工大学", "华工": "华南理工大学",
    "华师": "华东师范大学", "上财": "上海财经大学", "央财": "中央财经大学",
    "电子科大": "电子科技大学", "成电": "电子科技大学", "北交": "北京交通大学",
}

_BIRTH_RE = re.compile(r"(19[5-9]\d|20[0-2]\d)\s*[年\-/\.]\s*\d{1,2}\s*[月\-/\.]")
_YEAR_ONLY_RE = re.compile(r"出生[：:\s]*(19[5-9]\d|20[0-2]\d)")
_AGE_RE = re.compile(r"(?<![\d.])(\d{2})\s*岁")


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def norm_name(name):
    """姓名归一：去空白/全角空格，统一大小写。用于判"看起来是不是同一个名字"。"""
    s = (name or "").strip()
    s = s.replace("\u3000", "").replace(" ", "").replace("\t", "")
    return s.casefold()


def _norm_token(s):
    return re.sub(r"[\s·・\-_]+", "", (s or "")).casefold()


# ---------------------------------------------------------------------------
# 特征抽取
# ---------------------------------------------------------------------------
def _find_degrees(text):
    low = text.casefold()
    out = []
    for word, label in _DEGREE_WORDS.items():
        if word in low and label not in out:
            out.append(label)
    return out


_SCHOOL_SUFFIX_RE = re.compile(r"(大学|学院|学校|研究院|职业技术学院)")


def _find_schools(text, name=""):
    """抽取院校名。

    踩过的坑：这里原本是"把简称全量替换成规范名"再匹配，
    于是「清华大学」里的「清华」被替换成「清华大学」，得到「清华大学大学」。
    后果不只是名字难看——比对时「清华大学大学」和「清华大学」被判成两所学校，
    同校背景反而变成"不一致"信号，把同一人推成"不同人"。
    现在只在简称**后面没有紧跟院校后缀**时才替换。
    """
    seen, out = set(), []
    low = text or ""
    # 简称 → 规范名。用负向先行断言避开"清华(大学)"这类已经带后缀的写法。
    for alias, canon in UNIV_ALIAS.items():
        low = re.sub(re.escape(alias) + r"(?!" + _SCHOOL_SUFFIX_RE.pattern + r")", canon, low)
    for m in _SCHOOL_RE.finditer(low):
        s = _norm_token(m.group(1))
        # 去掉"毕业于""就读于"这类前缀残留
        s = re.sub(r"^(毕业于|就读于|本科|硕士|博士|研究生)", "", s)
        # 兜底：万一还是叠出了"XX大学大学"，压回一层
        s = re.sub(r"(大学|学院|学校|研究院)\1+$", r"\1", s)
        if len(s) >= 3 and s not in seen:
            seen.add(s)
            out.append(s)

    # 英文院校（Peking University / Tsinghua University）。
    # ⚠ 英文简历里姓名常紧挨着校名（"Li Wei\nPeking University"），
    # 正则会把姓名一起吞成"Li Wei Peking University"。
    # 两个人若姓名不同，同一所学校会因此被判成"院校不一致"，
    # 反倒把真正同校的两个人推开。这里显式把已知姓名从结果开头剥掉。
    nm = (name or "").strip()
    for m in _SCHOOL_EN_RE.finditer(text or ""):
        s = re.sub(r"\s+", " ", m.group(1)).strip()
        if nm:
            # 姓名可能是"Li Wei"这种带空格的，整段比；也可能是"Li"
            for cand in (nm, nm.split()[0] if nm.split() else ""):
                if cand and s.lower().startswith(cand.lower() + " "):
                    s = s[len(cand) + 1:].strip()
        if len(s) >= 6 and s.casefold() not in {x.casefold() for x in out}:
            out.append(s)
    return out


def _find_companies(text):
    """抽取任职单位。

    两路合并：
      1. 词尾特征（「XX有限公司」「XX银行」「XX集团」）——覆盖规范写法
      2. 知名企业白名单——覆盖「字节跳动」「腾讯」这类不带后缀的简写
    白名单命中位置要打码，避免同一段文字在两路里各抽一次导致重复。
    """
    seen, out = set(), []
    low = text or ""
    masked = low

    for name in _KNOWN_COMPANIES_SORTED:
        if name in masked:
            if name not in seen:
                seen.add(name)
                out.append(name)
            masked = masked.replace(name, "　" * len(name))

    for m in _COMPANY_RE.finditer(masked):
        s = _norm_token(m.group(1))
        if len(s) >= 4 and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _find_location(text, name=""):
    """抽取所在地。

    踩过的坑：兜底分支直接取"正文里第一个出现的城市名"，
    于是「北京大学 金融」「上海交通大学」里的城市前缀被当成了居住地。
    后果很严重且方向反直觉——
      · A 简历写「清华大学 / 北京市朝阳区」→ 北京
      · B 简历写「北京大学 / 上海市浦东新区」→ 也被判成北京
    两份材料的 location 于是"一致"，凭空多出一个 1.5 权重的
    支持同一人的信号，把两个不同的人往"合并"方向推。
    正确做法：先把院校名和单位名从文本里抹掉（这些地方出现城市名
    是机构名的一部分，不代表居住地），再在剩下的文本里找城市。
    """
    # 有显式标签时优先按标签走（"现居北京""工作地点：上海"）
    m = _LOCATION_HINT_RE.search(text)
    if m:
        city = _CITY_RE.search(m.group(2))
        if city:
            return city.group(1)

    # 抹掉机构名：院校（含"北京大学""上海交通大学"）与已知单位
    masked = text or ""
    for s in _find_schools(masked, name):
        masked = masked.replace(s, "　" * len(s))
    for c in _find_companies(masked):
        masked = masked.replace(c, "　" * len(c))
    # 抹掉别名归一后可能残留的院校写法
    for alias, canon in UNIV_ALIAS.items():
        masked = masked.replace(canon, "　" * len(canon))

    m = _CITY_RE.search(masked)
    return m.group(1) if m else ""


def _find_birth_year(text):
    m = _YEAR_ONLY_RE.search(text)
    if m:
        return int(m.group(1))
    m = _BIRTH_RE.search(text)
    if m:
        return int(m.group(1))
    m = _AGE_RE.search(text)
    if m:
        try:
            return 2026 - int(m.group(1))     # 粗算，仅用于身份比对
        except ValueError:
            return None
    return None


def extract_profile(name="", resume_text="", extra_text=""):
    """从姓名 + 简历正文抽取身份信号。

    返回：{
      name, name_key, phone, email,
      education: {"schools": [...], "degrees": [...]},
      work_history: [...],  location, birth_year,
      signals: {signal_name: value}   # 只含有值的信号，便于统一打分
    }
    纯正则抽取、确定性、可解释——不调用模型，避免为"判重"引入幻觉。
    """
    text = "\n".join([resume_text or "", extra_text or ""])

    phone = ""
    m = _PHONE_RE.search(text)
    if m:
        phone = m.group(1)

    email = ""
    m = _EMAIL_RE.search(text)
    if m:
        email = m.group(0).casefold()

    education = {
        "schools": _find_schools(text, name),
        "degrees": _find_degrees(text),
    }
    work_history = _find_companies(text)
    location = _find_location(text, name)
    birth_year = _find_birth_year(text)

    signals = {}
    if phone:
        signals["phone"] = phone
    if email:
        signals["email"] = email
    if education["schools"] or education["degrees"]:
        signals["education"] = education
    if work_history:
        signals["work_history"] = work_history
    if location:
        signals["location"] = location
    if birth_year:
        signals["birth_year"] = birth_year

    return {
        "name": (name or "").strip(),
        "name_key": norm_name(name),
        "phone": phone,
        "email": email,
        "education": education,
        "work_history": work_history,
        "location": location,
        "birth_year": birth_year,
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------
def _jaccard(a, b):
    sa, sb = set(a or []), set(b or [])
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _cmp_education(a, b):
    """院校重合看 Jaccard；学历只要都抽到就按是否一致加分。"""
    ea, eb = a or {}, b or {}
    school = _jaccard(ea.get("schools"), eb.get("schools"))
    da, db = set(ea.get("degrees") or []), set(eb.get("degrees") or [])
    if not da or not db:
        degree = None
    else:
        degree = 1.0 if (da & db) else 0.0
    if degree is None:
        return school, "仅院校可比"
    return round(0.7 * school + 0.3 * degree, 3), "院校+学历"


def compare_profiles(a, b):
    """比较两份身份档案，给出可解释的裁决。

    返回：{
      verdict: "same" | "different" | "uncertain",
      score: 0~1 相似度（弱信号加权）,
      matched: [信号说明], conflicts: [冲突说明],
      reasons: [人类可读的依据],
      signals_compared: N
    }
    """
    reasons, matched, conflicts = [], [], []
    a = a or {}
    b = b or {}

    # ---- 强标识：一票定生死 ----
    for key, label in (("phone", "手机号"), ("email", "邮箱")):
        va, vb = (a.get(key) or ""), (b.get(key) or "")
        if va and vb:
            if _norm_token(va) == _norm_token(vb):
                return {
                    "verdict": "same", "score": 1.0,
                    "matched": ["%s一致（%s）" % (label, va)],
                    "conflicts": [], "signals_compared": 1,
                    "reasons": ["%s精确一致，判定为同一人（强标识，无需其它信号）" % label],
                }
            conflicts.append("%s不一致（%s ≠ %s）" % (label, _mask(va), _mask(vb)))
            reasons.append("%s不一致，且为强标识——判定为不同的人" % label)
            return {
                "verdict": "different", "score": 0.0,
                "matched": [], "conflicts": conflicts,
                "signals_compared": 1, "reasons": reasons,
            }

    # ---- 姓名：入场券 ----
    na, nb = a.get("name_key") or "", b.get("name_key") or ""
    if na and nb and na != nb:
        return {
            "verdict": "different", "score": 0.0, "matched": [],
            "conflicts": ["姓名不同（%s ≠ %s）" % (a.get("name") or "?", b.get("name") or "?")],
            "signals_compared": 0,
            "reasons": ["姓名不一致，视为不同的人"],
        }

    # ---- 弱信号加权打分 ----
    total_w = 0.0
    earned = 0.0
    compared = 0

    # ⚠ 姓名只算"入场券"，**不计入 compared**。
    # 踩过的坑：早期这里 counted += 1，而 MIN_SIGNALS_FOR_AUTO=2，
    # 于是"同名 + 同学校"就凑满 2 个信号、相似度 100%，被自动合并成同一人。
    # 同名同校完全可能是两个人（一届同校同名并不罕见），
    # 而且这正是本功能要防的核心误判。姓名不构成任何身份证据。
    if na and nb:
        total_w += SIGNAL_WEIGHTS["name"]
        earned += SIGNAL_WEIGHTS["name"]
        matched.append("姓名一致（仅作入场条件，不计为身份证据）")

    ea, eb = a.get("education") or {}, b.get("education") or {}
    if (ea.get("schools") or ea.get("degrees")) and (eb.get("schools") or eb.get("degrees")):
        w = SIGNAL_WEIGHTS["education"]
        total_w += w
        compared += 1
        s, how = _cmp_education(ea, eb)
        earned += w * s
        if s >= 0.99:
            matched.append("学历/院校完全一致（%s）" % how)
        elif s > 0:
            matched.append("学历/院校部分重合（%s，%.0f%%）" % (how, s * 100))
        else:
            conflicts.append("学历/院校无任何重合（%s）" % how)

    wa, wb = a.get("work_history") or [], b.get("work_history") or []
    if wa and wb:
        w = SIGNAL_WEIGHTS["work_history"]
        total_w += w
        compared += 1
        s = _jaccard(wa, wb)
        earned += w * s
        if s > 0:
            matched.append("任职单位重合 %.0f%%" % (s * 100))
        else:
            conflicts.append("任职单位无任何重合")

    la, lb = (a.get("location") or ""), (b.get("location") or "")
    if la and lb:
        w = SIGNAL_WEIGHTS["location"]
        total_w += w
        compared += 1
        if la == lb:
            earned += w
            matched.append("所在地一致（%s）" % la)
        else:
            conflicts.append("所在地不同（%s ≠ %s）" % (la, lb))

    ba, bb = a.get("birth_year"), b.get("birth_year")
    if ba and bb:
        w = SIGNAL_WEIGHTS["birth_year"]
        total_w += w
        compared += 1
        if abs(int(ba) - int(bb)) <= 1:
            earned += w
            matched.append("年龄段一致（%s）" % ba)
        else:
            conflicts.append("出生年份差异较大（%s ≠ %s）" % (ba, bb))

    score = round(earned / total_w, 3) if total_w > 0 else 0.0

    # ---- 裁决 ----
    # 没有任何独立身份信号可比时，绝不自动合并：
    # 只有同名，不构成同一人的证据（姓名已被排除在 compared 之外）。
    if compared == 0:
        verdict = "uncertain"
        reasons.append(
            "除姓名外没有院校/单位/城市等可比信号——无法自动判定，需人工确认"
        )
    elif score >= SAME_THRESHOLD and compared >= MIN_SIGNALS_FOR_AUTO:
        verdict = "same"
        reasons.append("多信号高度一致（相似度 %.0f%%，%d 个独立信号），判定为同一人"
                       % (score * 100, compared))
    elif score <= DIFFERENT_THRESHOLD:
        verdict = "different"
        reasons.append("关键信号冲突（相似度仅 %.0f%%），判定为不同的人" % (score * 100))
    elif compared < MIN_SIGNALS_FOR_AUTO:
        # 相似度够高但独立信号不足：仍然交给人。
        verdict = "uncertain"
        reasons.append(
            "只有 %d 个独立信号可比（相似度 %.0f%%），证据不足以下结论，需人工确认"
            % (compared, score * 100)
        )
    else:
        verdict = "uncertain"
        reasons.append(
            "证据不充分（相似度 %.0f%%，%d 个独立信号）——为避免误合并，需人工确认"
            % (score * 100, compared)
        )

    if conflicts:
        reasons.extend("冲突：" + c for c in conflicts)
    return {
        "verdict": verdict, "score": score,
        "matched": matched, "conflicts": conflicts,
        "signals_compared": compared, "reasons": reasons,
    }


def _mask(s):
    s = str(s or "")
    if "@" in s:
        name, _, domain = s.partition("@")
        return (name[:2] + "***@" + domain) if len(name) > 2 else ("***@" + domain)
    return s[:3] + "****" if len(s) > 5 else "****"


def signature(profile):
    """可比较的签名（去掉正文，只留身份特征），存进候选人记录。

    ⚠ 必须包含 `name_key`：比对逻辑靠它做"姓名是否一致"的入场券判断。
    早期版本漏了这个字段，于是入库再比对时姓名检查被整段跳过——
    不同姓名、但同校同公司的两个人会被误判为同一人（比不比对更糟）。
    """
    p = profile or {}
    name = (p.get("name") or "").strip()
    return {
        "name": name,
        "name_key": p.get("name_key") or norm_name(name),
        "phone": p.get("phone") or "",
        "email": p.get("email") or "",
        "education": p.get("education") or {"schools": [], "degrees": []},
        "work_history": p.get("work_history") or [],
        "location": p.get("location") or "",
        "birth_year": p.get("birth_year"),
        "signals": sorted((p.get("signals") or {}).keys()),
    }
