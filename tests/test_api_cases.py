"""证据链接口冒烟测试：profile → 建案 → 材料 → 主张 → 闸门 → 签署 → 导出 → 校验。

这里刻意**不经过 HTTP**，直接调 `api.match()/handler()`：路由表与权限点才是
最需要被验证的部分，套一层 socket 只会让失败更难定位。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="eg-api-")
from engine.storage import db  # noqa: E402
db.DATA_DIR = TMP
db.DB_PATH = os.path.join(TMP, "api.db")
db.reset_init_flag()
from engine.intake import crypto  # noqa: E402
crypto._cached_key = None
crypto._data_dir = lambda: TMP
db.init()

from engine.api import api  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  ← " + str(detail)) if (detail and not cond) else ""))


ADMIN = {"id": "u-admin", "username": "admin", "role": "admin"}


def call(method, path, body=None, query=None, user=ADMIN):
    r, params = api.match(method, path)
    assert r, "路由未注册: %s %s" % (method, path)
    ctx = {"method": method, "path": path, "body": body or {},
           "params": params or {}, "query": query or {}, "user": user}
    return r["fn"](ctx)


# ---------------------------------------------------------------- 路由与场景
print("\n=== 场景清单（界面上「新建案件」的选项来源）===")
prof = call("GET", "/api/profiles")
keys = [p["key"] for p in prof["profiles"]]
check("两个场景都在册", set(keys) >= {"expense", "talent"}, keys)
p0 = [p for p in prof["profiles"] if p["key"] == "expense"][0]
check("场景带材料角色选项", p0["artifact_roles"] == ["invoice", "approval", "policy"], p0["artifact_roles"])
check("场景带签署角色（决定详情页几个签署格）",
      p0["judge_roles"] == ["claimant", "approver"], p0["judge_roles"])

# ---------------------------------------------------------------- 建案
print("\n=== 建案 → 材料 → 主张 → 闸门 ===")
c = call("POST", "/api/cases", {"title": "8月差旅报销", "profile_key": "expense"})["case"]
cid = c["id"]
check("建案成功", cid and c["status"] == "draft", c.get("status"))
check("详情自带视图字段（counts/gate/sign_roles）",
      "counts" in c and "sign_roles" in c, sorted(c.keys()))

# 引用不存在的材料 → 拒绝
try:
    call("POST", "/api/cases/%s/claims" % cid,
         {"claims": [{"text": "x", "citations": [{"artifact_id": "art_nope"}]}]})
    check("引用幽灵材料被拒绝", False, "竟然成功了")
except api.ApiError as e:
    check("引用幽灵材料被拒绝", "不存在" in str(e) or 400 == e.status, str(e))

# 材料角色越界 → 拒绝
try:
    call("POST", "/api/cases/%s/artifacts" % cid, {"role": "not_a_role", "text": "x"})
    check("越界材料角色被拒绝", False, "竟然成功了")
except api.ApiError as e:
    check("越界材料角色被拒绝", e.status == 400, str(e))

inv = call("POST", "/api/cases/%s/artifacts" % cid,
           {"role": "invoice", "filename": "invoice.pdf",
            "text": "金额 42800 元。供应商星河科技。"})["case"]
apr = call("POST", "/api/cases/%s/artifacts" % cid,
           {"role": "approval", "filename": "approval.txt",
            "text": "审批人李四已签字，日期 2026-08-01。"})["case"]
check("材料入库并给字符数", all(a["chars"] > 0 for a in apr["artifacts"]),
      [a["chars"] for a in apr["artifacts"]])
check("材料正文回读正确（加密后可解）",
      any("星河科技" in a["text"] for a in apr["artifacts"]))

inv_id = [a for a in apr["artifacts"] if a["role"] == "invoice"][0]["id"]
pol_id = [a for a in apr["artifacts"] if a["role"] == "approval"][0]["id"]

# 跑一次闸门：缺 policy → 应阻断
g = call("POST", "/api/cases/%s/gate" % cid)
check("缺必需材料时闸门阻断（不可出具）", g["gate"]["verdict"] == "fail", g["gate"]["verdict"])
check("案件状态随之变为 blocked", g["case"]["status"] == "blocked", g["case"]["status"])
check("阻断条款可读（有 requirement 文案）",
      all("requirement" in cl for cl in g["gate"]["clauses"]), g["gate"]["clauses"][:1])

# 补齐材料 + 主张 → 转为可出具
call("POST", "/api/cases/%s/artifacts" % cid,
     {"role": "policy", "filename": "policy.txt", "text": "差旅标准为每人每天 600 元。"})
call("POST", "/api/cases/%s/claims" % cid, {"claims": [
    {"text": "本笔报销金额为 42800 元", "citations": [
        {"artifact_id": inv_id, "locator": "char 0", "quote": "金额 42800 元"}]},
    {"text": "审批人已签字", "citations": [
        {"artifact_id": pol_id, "locator": "char 0", "quote": "审批人李四已签字"}]},
]})
g2 = call("POST", "/api/cases/%s/gate" % cid)
check("补齐后主张可回链", g2["case"]["counts"]["verified"] == 2, g2["case"]["counts"])
check("证据指纹（可复现编号）已生成", len(g2["gate"]["hash"]) == 64, g2["gate"]["hash"][:12])

# ---------------------------------------------------------------- 签署
print("\n=== 签署：人签，且需三方职责分离 ===")
s1 = call("POST", "/api/cases/%s/sign" % cid, {"role": "claimant"})
check("claimant 签署入账", len(s1["case"]["approvals"]) == 1, s1["case"]["approvals"])
try:
    call("POST", "/api/cases/%s/sign" % cid, {"role": "not_a_role"})
    check("非法签署角色被拒绝", False, "竟然成功了")
except api.ApiError as e:
    check("非法签署角色被拒绝", e.status == 400, str(e))

# ---------------------------------------------------------------- 导出
print("\n=== 导出：离线可核验证据包 ===")
call("POST", "/api/cases/%s/sign" % cid, {"role": "approver"})
# 签署会改动案件，结论随之过期——所以签署后必须重算闸门再导出。
# 这不是麻烦，这是「结论必须对应当前证据」的代价。
stale = call("POST", "/api/cases/%s/gate" % cid)["case"]
b = call("POST", "/api/cases/%s/bundle" % cid)
check("证据包已生成", b["bundle"]["data_base64"], b["bundle"].get("filename"))
check("证据包有文件名", b["bundle"]["filename"].endswith(".zip"), b["bundle"]["filename"])
check("签署后结论不过期（重算即一致）", stale["gate"]["stale"] is False, stale["gate"]["stale"])

# 闸门后再改材料 → 结论过期 → 导出被拒
call("POST", "/api/cases/%s/artifacts" % cid,
     {"role": "policy", "filename": "policy2.txt", "text": "补充：跨市交通需事前审批。"})
try:
    call("POST", "/api/cases/%s/bundle" % cid)
    check("结论过期时拒绝导出", False, "竟然导出了")
except api.ApiError as e:
    check("结论过期时拒绝导出（提示重算）",
          e.status == 409 and "重新求值" in str(e), "%s / %s" % (e.status, e))

# ---------------------------------------------------------------- 校验
print("\n=== 校验：8 项证据链自检 ===")
call("POST", "/api/cases/%s/gate" % cid)
v = call("GET", "/api/validation/golden-thread", query={"case_id": cid})
check("校验出 8 项检查", v["summary"]["total"] == 8, v["summary"])
check("每项检查都有可读文案与算出的值",
      all(c.get("title") and c.get("value") for c in v["checks"]), v["checks"][:1])
check("校验带追踪 ID 与数据指纹",
      v["trace_id"].startswith("gt-") and len(v["dataset_hash"]) == 64,
      (v["trace_id"], v["dataset_hash"][:12]))
check("留痕链状态如实暴露（未锚定不能说不可篡改）",
      v["trail"]["anchored"] is False, v["trail"])
check("校验口径键名齐全",
      all(k in v["summary"] for k in ("total", "pass", "warn", "fail",
                                      "passed", "warnings", "failed")), v["summary"])

# ---------------------------------------------------------------- 权限
print("\n=== 权限：viewer 只读 ===")
VIEWER = {"id": "u-v", "username": "v", "role": "viewer"}
from engine.api import auth  # noqa: E402
check("viewer 可读案件", auth.can(VIEWER, "case.read"))
check("viewer 不可写案件", not auth.can(VIEWER, "case.write"))
check("viewer 不可签署", not auth.can(VIEWER, "case.sign"))
check("viewer 不可导出证据包", not auth.can(VIEWER, "case.export"))
check("hr 可签署（业务主要使用者）",
      auth.can({"role": "hr"}, "case.sign") and auth.can({"role": "hr"}, "case.export"))

print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED: %s" % f)
sys.exit(0 if not FAIL else 1)