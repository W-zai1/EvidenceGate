"""验证口径签名的"自动失效"特性：改提示词/温度必须自动换版本号。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))

import agents
import rubric

FAIL = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  ← %s" % (detail,)))
    if not ok:
        FAIL.append(name)


W = {"岗位能力匹配度": 0.4, "风险与软素质": 0.3, "硬实力门槛": 0.3}


def ver():
    return rubric.signature(W, model="gpt-4o-mini", provider="openai")["version"]


print("--- 1. 提示词指纹自动进入版本号 ---")
v0 = ver()
fp0 = rubric.prompt_fingerprint()
check("指纹非空（能取到提示词）", bool(fp0), fp0)
check("版本号含指纹", ver() == v0)

# 模拟"有人改了一句提示词但忘了改手工版本号"
orig = agents._PROMPT_JUDGE_TMPL
try:
    agents._PROMPT_JUDGE_TMPL = orig.replace("独立打分", "独立打分并说明依据")
    v1 = ver()
    fp1 = rubric.prompt_fingerprint()
    check("改提示词后指纹变化", fp1 != fp0, (fp0, fp1))
    check("改提示词后版本号自动变化", v1 != v0, (v0, v1))
finally:
    agents._PROMPT_JUDGE_TMPL = orig
check("还原后版本号恢复", ver() == v0, (v0, ver()))

print("\n--- 2. 温度变化进版本号 ---")
rubric.register_signature_parts(judge_temperature=0.0)
va = ver()
rubric.register_signature_parts(judge_temperature=0.7)
vb = ver()
check("judge_temperature 0.0 vs 0.7 版本不同", va != vb, (va, vb))
rubric.register_signature_parts(judge_temperature=0.0)
check("改回后版本恢复", ver() == va, va)

print("\n--- 3. 重复次数进版本号 ---")
rubric.register_signature_parts(repeats=1)
vc = ver()
rubric.register_signature_parts(repeats=3)
vd = ver()
check("repeats 1 vs 3 版本不同", vc != vd, (vc, vd))
rubric.register_signature_parts(repeats=None)
rubric._EXTRA_SIGNATURE.pop("repeats", None)

print("\n--- 4. 评委会话术也在指纹里 ---")
fp_before = rubric.prompt_fingerprint()
orig_common = agents._JUDGE_COMMON
try:
    agents._JUDGE_COMMON = orig_common + "\n5. 额外一条规则。\n"
    check("改 _JUDGE_COMMON 也换指纹", rubric.prompt_fingerprint() != fp_before)
finally:
    agents._JUDGE_COMMON = orig_common
check("还原后指纹恢复", rubric.prompt_fingerprint() == fp_before)

print("\n--- 5. 三个评委 persona 都在指纹里 ---")
orig_p = list(agents.JUDGE_PERSONAS)
try:
    agents.JUDGE_PERSONAS[0] = ("严格卡JD", orig_p[0][1] + "补充：额外严格。")
    check("改评委 persona 也换指纹", rubric.prompt_fingerprint() != fp_before)
finally:
    agents.JUDGE_PERSONAS[:] = orig_p
check("还原后指纹恢复", rubric.prompt_fingerprint() == fp_before)

print("\n--- 6. 取不到提示词时不伪造 ---")
saved = agents.prompt_bundle
try:
    agents.prompt_bundle = lambda: []
    check("取不到提示词时指纹为空串（不瞎编）",
          rubric.prompt_fingerprint() == "", rubric.prompt_fingerprint())
finally:
    agents.prompt_bundle = saved

print("\n--- 7. 每个 _agent_turn 调用都必须显式传 temperature ---")
# 温度已经算进 rubric_version（它是"尺子"的一部分）。如果某个调用点漏传、
# 靠 llm_client 的默认值恰好相等而"看起来正确"，一旦默认值被改，
# 那一步的实际口径就与签名静默漂移——本系统最在意的那类错误。
# 运行期已经用"必填关键字参数"兜住，这里再做一次**静态**检查：
# 新增调用点时不传，CI 立刻失败，而不是等某条路径被跑到才炸。
import ast as _ast

_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "engine", "agents.py"), encoding="utf-8").read()
_tree = _ast.parse(_src)
_calls = []


def _walk(node):
    for child in _ast.iter_child_nodes(node):
        if isinstance(child, _ast.Call) and isinstance(child.func, _ast.Name) \
                and child.func.id == "_agent_turn":
            _calls.append(child)
        _walk(child)


_walk(_tree)
check("确实抓到了调用点（否则这条检查是空的）", len(_calls) >= 6, len(_calls))
_missing = [c.lineno for c in _calls
            if not any(k.arg == "temperature" for k in c.keywords)]
check("所有 _agent_turn 调用都显式传了 temperature", not _missing, _missing)

# 顺带守住"温度值只有两种"：辩论用 DEBATE_TEMPERATURE，打分/裁定用 0
_bad_vals = []
for c in _calls:
    kw = {k.arg: k.value for k in c.keywords}
    v = kw.get("temperature")
    if v is None:
        continue
    ok = ((isinstance(v, _ast.Name) and v.id in
           ("DEBATE_TEMPERATURE", "JUDGE_TEMPERATURE"))
          or (isinstance(v, _ast.Constant) and v.value == 0))
    if not ok:
        _bad_vals.append((c.lineno, getattr(v, "id", getattr(v, "value", "?"))))
check("温度只来自命名常量或 0（不写字面量 0.7 之类）", not _bad_vals, _bad_vals)

print("\n--- 8. 提示词常量没有被「内联复制」到调用处 ---")
# 曾经反方互驳把 _PROMPT_CON_REBUT 的正文抄了一份放在调用处：
# 改常量时实际提示词不变，而指纹（只消费常量）却以为变了。
# 这里用 AST 取出 _run_once 函数体的源码范围再比对，避免把常量定义本身也算进去。
_run_once = next(
    (n for n in _ast.walk(_tree)
     if isinstance(n, _ast.FunctionDef) and n.name == "_run_once"), None)
check("找到了 _run_once", _run_once is not None)
if _run_once is not None:
    _body_src = "\n".join(_src.splitlines()[_run_once.lineno - 1:
                                           _run_once.end_lineno])
    _inline = agents._PROMPT_CON_REBUT.split("\n", 1)[-1].strip()
    check("_PROMPT_CON_REBUT 的正文没有出现在 _run_once 里",
          _inline not in _body_src, _inline[:40])
    check("反方互驳确实引用了常量",
          "_PROMPT_CON_REBUT" in _body_src)

print()
print("失败 %d 项" % len(FAIL))
sys.exit(0 if not FAIL else 1)
