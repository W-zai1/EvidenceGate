"""前端一致性检查：app.js 引用的 DOM id 必须都在 index.html 里存在。

这类错误在浏览器里表现为 `Cannot read properties of null`，
但 `node --check` 语法检查发现不了——必须做引用交叉核对。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER = os.path.join(os.path.dirname(HERE), "viewer")

html = open(os.path.join(VIEWER, "index.html"), encoding="utf-8").read()
js = open(os.path.join(VIEWER, "app.js"), encoding="utf-8").read()
api = open(os.path.join(VIEWER, "api.js"), encoding="utf-8").read()
css = open(os.path.join(VIEWER, "styles.css"), encoding="utf-8").read()

ids = set(re.findall(r'id="([^"]+)"', html))
# 有些 id 是 app.js 在渲染时动态写进 innerHTML 的（例如 #privacyMsg 由 loadPrivacy 注入），
# 它们不在 index.html 里，但运行时确实存在。所以静态定义集合要把这些也算进去，
# 否则会误报成"引用了不存在的元素"。
ids |= set(re.findall(r'id="([^"$]+)"', js))
ids |= set(re.findall(r"id='([^'$]+)'", js))

used = set(re.findall(r'getElementById\(["\']([^"\']+)["\']\)', js))
# app.js 里 $(id) 是 document.getElementById 的包装（传的是裸 id，没有 # 前缀）
used |= set(re.findall(r'(?<![\w$.])\$\(["\']([^"\'#][^"\']*)["\']\)', js))
used |= set(re.findall(r'querySelector\(["\']#([^"\']+)["\']\)', js))

missing = sorted(used - ids)
print("index.html 定义 id：%d 个" % len(ids))
print("app.js 引用 id：  %d 个" % len(used))
print("引用但不存在：    %s" % (missing if missing else "无 ✓"))

# api.js 里的路径必须都能在 api.py 找到对应路由
routes = open(os.path.join(os.path.dirname(HERE), "engine", "api.py"), encoding="utf-8").read()
declared = []
for m in re.finditer(r'@route\(\s*"(GET|POST|PUT|PATCH|DELETE)"\s*,\s*"([^"]+)"', routes):
    declared.append(re.sub(r":\w+", "*", m.group(2)))

paths = set(re.findall(r"""["'](/api/[^"'`]*)["']""", api))
bad = []
for p in paths:
    p = p.split("?")[0]
    # 前端拼路径时常写成 '/api/jobs/' + enc(id) + '/run'，
    # 这里把参数位归一成 *，再和路由模板比。
    norm = re.sub(r"\$\{[^}]+\}", "*", p).rstrip("/")
    norm = re.sub(r"/\*$", "/*", norm)
    ok = False
    for d in declared:
        dp = d.rstrip("/")
        dd = re.sub(r"/\*$", "/*", dp)
        if norm == dd or dd.startswith(norm + "/*") or norm.startswith(dd.split("/*")[0] + "/*"):
            ok = True
            break
        if norm.rstrip("*").rstrip("/") == dd.split("/*")[0].rstrip("/"):
            ok = True
            break
    if not ok:
        bad.append(p)
print("api.js 引用 /api/ 路径：%d 个" % len(paths))
print("找不到对应路由：    %s" % (bad if bad else "无 ✓"))

# esc() 使用检查：innerHTML 里插入外部文本必须转义。
# 只关注"看起来是外部数据"的表达式（对象属性、数组元素），
# 纯数字/循环变量/常量不算。逐个 ${...} 判断是否被 esc() 包住。
risky = []
SAFE = {"k", "i", "l", "v", "n", "idx", "len", "outs"}   # 循环变量
for m in re.finditer(r"innerHTML\s*=\s*([^;]+);", js, re.S):
    for var in re.findall(r"\$\{((?:[^{}]|\{[^{}]*\})*)\}", m.group(1)):
        var = var.strip()
        if "esc(" in var or "escape(" in var or "encodeURI" in var:
            continue
        if var in SAFE:
            continue
        if re.fullmatch(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*|\[[^\]]+\])*", var):
            # 裸标识符/属性链：可能是外部数据，报出来让人确认
            if "." in var or "[" in var:
                risky.append(var)
print("innerHTML 中未经 esc() 的数据字段：%s"
      % (sorted(set(risky))[:10] if risky else "无 ✓"))

# CSS 类使用检查（抽样）：只比对静态写死的类名，模板拼接出来的跳过。
# 注意：导出的 HTML 报告自带一份内联 <style>，其中的类不在 styles.css 里，
# 所以要把那份内联样式也并进"已定义"集合，否则会误报。
report_css = "".join(re.findall(r"<style>(.*?)</style>", js, re.S))
classes = set()
for m in re.finditer(r'class="([^"]+)"', js):
    v = m.group(1)
    if "$" in v or "{" in v:
        continue
    classes |= set(v.split())
defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
defined |= set(re.findall(r"\.([a-zA-Z][\w-]*)", report_css))
undef = sorted(c for c in classes if c and c not in defined)
print("app.js 使用但样式未定义的类：%s" % (undef[:10] if undef else "无 ✓"))

# ---- 死接口检查 ----------------------------------------------------------
# 这一条是被真实坑出来的：api.js 里声明了 backupCreate/backupRestore，
# 后端路由也齐全、e2e 测试也跑通了，但**界面上没有任何地方调用它们**。
# 于是"备份功能"在演示里存在、在用户手里不存在，而所有检查都是绿的。
# 现在：api.js 里每个封装（除了明确说明"仅内部用"的）都必须被 app.js 调用。
api_fns = set(re.findall(r"^\s{2,}(\w+):\s*(?:\(|async)", api, re.M))
# 这些是底层/内部封装，不直接出现在界面逻辑里
API_INTERNAL = {"jsonFetch", "post", "get", "put", "del", "enc", "qs", "setModelConfig"}
called = set(re.findall(r"\bAPI\.(\w+)\s*\(", js))
orphan = sorted(f for f in api_fns
                if f not in API_INTERNAL and f not in called)
print("api.js 声明但 app.js 从未调用：%s" % (orphan if orphan else "无 ✓"))
# 上面这些是**已知**的"后端有、界面无入口"。不是本轮引入的，但确实是缺口：
# 接口能调、测试能过、用户点不到。列在这里而不是放宽检查，是为了让
# 新增的死接口立刻失败，同时不掩盖既有的这些。
KNOWN_ORPHANS = {
    "addCalibItem", "alignDelete", "compareIdentity", "deleteOutcome",
    "deleteTalent", "erasePerson", "exportPerson", "health",
    "parseDocs", "resetPassword", "updateCandidate",
}
new_orphan = sorted(set(orphan) - KNOWN_ORPHANS)
print("其中新增（必须修）：%s" % (new_orphan if new_orphan else "无 ✓"))

# 调用名大小写：全局对象是 window.API，写成小写 api.xxx 会在运行时
# 抛 `ReferenceError: api is not defined`，而 node --check 完全不报。
lower = sorted(set(re.findall(r'(?<![\w.])api\.(\w+)\s*\(', js)))
print("app.js 里误用小写 api. 调用：%s" % (lower if lower else "无 ✓"))

# ---- 重复定义检查 --------------------------------------------------------
# 被真实坑过：我在评审细则里写了个 readWeights()，而 JD 编辑器里早就有一个
# 同名函数。函数声明会提升，后定义的那个静默覆盖前者，于是我的代码调用到
# 读 #jdWeights 的版本，返回空对象——权重框全变成 NaN，两个函数的代码
# 各自看都没问题，node --check 也不报。
top_fns = re.findall(r"^function ([A-Za-z_$][\w$]*)\s*\(", js, re.M)
dup_fns = sorted({n for n in top_fns if top_fns.count(n) > 1})
print("app.js 顶层重复函数声明：%s" % (dup_fns if dup_fns else "无 ✓"))

# 顶层 const/let 重名同样会 SyntaxError（这次没踩，但一并防）
top_vars = re.findall(r"^(?:const|let|var) ([A-Za-z_$][\w$]*)\s*=", js, re.M)
dup_vars = sorted({n for n in top_vars if top_vars.count(n) > 1})
print("app.js 顶层重复变量声明：%s" % (dup_vars if dup_vars else "无 ✓"))

# ---- 报告打印/PDF --------------------------------------------------------
# 归档是这个工具"可追溯"卖点的延伸，所以打印样式不能只是"能凑合看"：
# 必须①不打印工具栏 ②每位候选人另起一页 ③表头翻页重复 ④背景色照打
# ⑤数字不许把浮点尾数印出来。
print_i = js.find("@media print{")
print("报告里有 @media print 块：%s" % ("是 ✓" if print_i >= 0 else "没有 ✗"))
print_block = js[print_i:print_i + 1600] if print_i >= 0 else ""
checks_print = {
    "工具栏用 no-print 隐藏": "no-print{display:none" in js.replace(" ", "")
                              or "no-print" in js,
    "候选人另起一页": "break-before:page" in print_block,
    "卡片不跨页切断": "break-inside:avoid" in print_block,
    "表头翻页重复": "table-header-group" in print_block,
    "背景色照打": "print-color-adjust" in print_block,
}
bad_print = [k for k, ok in checks_print.items() if not ok]
print("打印规则：%s" % ("齐全 ✓" if not bad_print else bad_print))

# 概览表里的数字必须过 _repNum，不能直出浮点
raw_total = re.findall(r'class="c big">\$\{(?!_repNum)', js)
print("概览表里直出原始数字的地方：%s"
      % (len(raw_total) if raw_total else "无 ✓"))

# 打印入口必须真的挂到界面上
print_entry = ("printReport" in js and "printReport()" in html)
print("打印入口已接到界面：%s" % ("是 ✓" if print_entry else "否 ✗"))

sys.exit(0 if not missing and not bad and not new_orphan and not lower
         and not dup_fns and not dup_vars and not bad_print
         and not raw_total and print_i >= 0 and print_entry else 1)
