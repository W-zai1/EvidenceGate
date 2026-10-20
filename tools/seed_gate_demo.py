#!/usr/bin/env python
"""注入 EvidenceGate 演示案件（虚构），覆盖两个场景 + 阻断/通过两种结局。

为什么单独一个脚本
------------------
和 seed_demo.py 一样：**绝不自动注入**。真实使用时，库里混进虚构案件，
比没有数据更糟——因为闸门会给出一个看起来权威的结论。

会创建三个案件（标题带 `[演示]`，可整案删除）：
  1. 费用报销 · 有编造引用 → 阻断（演示"被剔除的主张可见"）
  2. 费用报销 · 证据齐备 + 三级签署 → 通过（演示证据包可导出）
  3. 人才筛选 · 简历引用 + 一条无据主张 → 阻断（演示同一套脊柱跨场景）

用法：
    python tools/seed_gate_demo.py --base http://127.0.0.1:8124 \
        --user admin --password 'Passw0rd!x'
"""
import argparse
import   json
import sys
import   urllib.error
import urllib.request

TAG = "[演示]"


def call(base, method, path, body=None, jar=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if jar:
        headers["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in jar.items())
        if "ts_csrf" in jar:
            headers["X-CSRF-Token"] = jar["ts_csrf"]
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            txt = resp.read().decode("utf-8", "replace")
            for h in resp.headers.get_all("Set-Cookie") or []:
                kv = h.split(";")[0]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    jar[k.strip()] = v.strip()
            return resp.status, json.loads(txt or "{}")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": txt}


INVOICE_BAD = ("增值税普通发票  发票号 88213  金额 42800.00 元  开票日期 2025-11-03  "
               "购买方 某某科技有限公司  销售方 某某服务商")
APPROVAL_BAD = ("费用报销审批流  申请人 张伟  部门经理 李娜 同意  财务 王强 复核通过  "
                "审批金额 42800.00  审批日期 2025-11-04")
POLICY = ("差旅与业务招待费用标准：交通费单次不超过 3000 元；"
          "云资源采购单笔 10000 元以内由部门经理审批；报销须附发票与审批记录。")

INVOICE_OK = ("增值税普通发票  发票号 90211  金额 6800.00 元  开票日期 2025-12-01  "
              "服务名称 云服务器  购买方 某某科技有限公司")
APPROVAL_OK = ("审批流  申请人 张伟  部门经理 李娜 同意  财务 王强 复核通过  "
               "金额 6800.00  审批日期 2025-12-02")

RESUME = ("清华大学 本科 计算机  字节跳动 后端工程师 5 年  "
          "主导日均 2 亿请求的网关重构，Go / Kubernetes  北京")

# 职责分离不是文案：三个角色必须真的由三个账号签。
# 演示账号口令统一，仅用于本地演示库。
SIGNERS = {
    "张伟": ("demo_zhangwei", "Demo!pass1"),
    "李娜": ("demo_lina", "Demo!pass1"),
    "王强": ("demo_wangqiang", "Demo!pass1"),
}


def ensure_signer(base, admin_jar, name):
    """确保演示签署人存在（hr 角色，够用：case.write 已授予 hr）。"""
    user, pwd = SIGNERS[name]
    st, r = call(base, "POST", "/api/users",
                 {"username": user, "display_name": name,
                  "password": pwd, "role": "hr"}, admin_jar)
    if st != 200 and "已存在" not in str(r.get("error") or ""):
        print("  创建签署人失败：%s → %s" % (name, r.get("error") or r))


def sign_as(base, cid, role, name):
    """以该账号本人登录后签署——不能用 admin 代签三个人。"""
    user, pwd = SIGNERS[name]
    jar = {}
    st, r = call(base, "POST", "/api/auth/login",
                 {"username": user, "password": pwd}, jar)
    if st != 200:
        print("  签署人登录失败：%s → %s" % (name, r.get("error") or r))
        return False
    st, r = call(base, "POST", "/api/cases/%s/sign" % cid,
                 {"role": role, "note": "演示签署"}, jar)
    if st != 200:
        print("  签署失败：%s/%s → %s" % (name, role, r.get("error") or r))
        return False
    return True


def create_case(base, jar, title, profile, artifacts, claims, signatures):
    st, r = call(base, "POST", "/api/cases",
                 {"title": TAG + title, "profile_key": profile}, jar)
    if st != 200:
        print("  建案失败：%s" % (r.get("error") or r))
        return None
    case = r["case"]
    cid = case["id"]
    ids = {}
    for role, filename, text in artifacts:
        st, r = call(base, "POST", "/api/cases/%s/artifacts" % cid,
                     {"role": role, "filename": filename, "text": text}, jar)
        if st != 200:
            print("  加材料失败：%s" % (r.get("error") or r))
            return None
        ids[role] = r["artifact"]["id"]

    payload = []
    for c in claims:
        cites = []
        for role, quote in c.get("cites", []):
            cites.append({"artifact_id": ids[role], "locator": c.get("locator", ""),
                          "quote": quote})
        payload.append({"text": c["text"], "citations": cites})
    st, r = call(base, "POST", "/api/cases/%s/claims" % cid,
                 {"claims": payload}, jar)
    if st != 200:
        print("  写主张失败：%s" % (r.get("error") or r))
        return None

    for role, name in signatures:
        if name in SIGNERS:
            ensure_signer(base, jar, name)
            sign_as(base, cid, role, name)
        else:
            # 场景要求的角色不在演示签署人表里时，退化为用 admin 签，
            # 并在输出里说明——绝不能假装"分离已满足"。
            call(base, "POST", "/api/cases/%s/sign" % cid,
                 {"role": role, "note": "演示签署（同一账号）"}, jar)

    st, r = call(base, "POST", "/api/cases/%s/gate" % cid, {}, jar)
    if st != 200:
        print("  求值失败：%s" % (r.get("error") or r))
        return None
    g = r["case"]["gate"]
    print("  ✓ %s%s → %s（%s）" % (TAG, title, g["verdict"], g["hash"][:10]))
    return cid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8124")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="Passw0rd!x")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.dry_run:
        print("将创建 3 个 %s 案件（费用报销 ×2、人才筛选 ×1）" % TAG)
        return 0

    jar = {}
    st, r = call(args.base, "POST", "/api/auth/login",
                 {"username": args.user, "password": args.password}, jar)
    if st != 200 or not r.get("ok"):
        print("登录失败（%s）：%s" % (st, r.get("error") or r))
        return 2

    print("=" * 66)
    print(" EvidenceGate 演示案件注入（全部虚构）")
    print("=" * 66)

    create_case(
        args.base, jar, "11 月差旅报销", "expense",
        [("invoice", "发票.pdf", INVOICE_BAD),
         ("approval", "审批流.txt", APPROVAL_BAD),
         ("policy", "报销制度.md", POLICY)],
        [{"text": "发票金额为 42800.00 元",
          "cites": [("invoice", "金额 42800.00 元")], "locator": "p.1"},
         {"text": "该供应商为集团关联方，存在利益输送风险",
          "cites": [("invoice", "关联方 集团")], "locator": "p.1"},
         {"text": "本次报销符合公司制度"}],
        [("claimant", "张伟"), ("approver", "李娜"), ("reviewer", "王强")])

    create_case(
        args.base, jar, "12 月云服务采购", "expense",
        [("invoice", "发票.pdf", INVOICE_OK),
         ("approval", "审批流.txt", APPROVAL_OK),
         ("policy", "报销制度.md", POLICY)],
        [{"text": "云服务器费用 6800.00 元，审批金额一致",
          "cites": [("invoice", "金额 6800.00 元"),
                    ("approval", "金额 6800.00")], "locator": "p.1"},
         {"text": "审批链完整（申请人 → 部门经理 → 财务）",
          "cites": [("approval", "申请人 张伟  部门经理 李娜 同意  财务 王强 复核通过")],
          "locator": "L1"}],
        [("claimant", "张伟"), ("approver", "李娜"), ("reviewer", "王强")])

    create_case(
        args.base, jar, "周宇靖 · 高级后端评审", "talent",
        [("resume", "简历.txt", RESUME)],
        [{"text": "候选人有 5 年后端经验，任职字节跳动",
          "cites": [("resume", "字节跳动 后端工程师 5 年")], "locator": "L1"},
         {"text": "主导过日均 2 亿请求的网关重构",
          "cites": [("resume", "主导日均 2 亿请求的网关重构")], "locator": "L1"},
         {"text": "带过 10 人团队并负责绩效",
          "cites": [("resume", "带过 10 人团队")], "locator": "L2"}],
        [("maker", "张伟"), ("checker", "李娜"), ("approver", "王强")])

    st, r = call(args.base, "GET", "/api/validation/golden-thread", None, jar)
    if st == 200:
        print("\n证据链校验：%s（%d 项检查）"
              % (r.get("verdict"), r.get("summary", {}).get("total")))
    print("\n清理：在「案件」页逐个删除，或直接删 data/talent.db（演示库）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
