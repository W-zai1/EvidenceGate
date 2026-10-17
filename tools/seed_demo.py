#!/usr/bin/env python
"""显式注入演示数据（虚构候选人），**只用于演示/联调**，不要用于真实招聘。

为什么单独做成脚本而不是内置在服务里：
  README 刻意承诺"不内置演示数据"，因为真实使用时库里混进虚构简历是灾难——
  你以为在评估真人，其实在评估一份编造的样本。所以这里守三条：
    1. 必须显式运行本脚本（没有任何接口或启动参数会自动注入）；
    2. 岗位标题带 `[演示]` 前缀，一眼能认出；
    3. 结束打印清单并提示如何清理（按 job_id 删岗位即可连带删候选人）。

用法：
    python tools/seed_demo.py                 # 注入默认演示岗位
    python tools/seed_demo.py --jobs 3        # 注入 3 个岗位
    python tools/seed_demo.py --base http://127.0.0.1:8123 --user admin --pass xxx
    python tools/seed_demo.py --dry-run       # 只打印将创建什么

依赖：只用标准库（与本项目零依赖约束一致）。
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

DEMO_TAG = "[演示]"

PRESETS = [
    {
        "title": "高级后端工程师",
        "jd": "本科以上，5 年后端经验，熟悉 Go 与分布式。负责核心网关的高并发改造。",
        "requirements": ["本科及以上", "5 年以上后端经验", "熟悉 Go"],
        "preferred": ["有高并发网关经验", "熟悉 Kubernetes"],
        "red_flags": ["无线上系统经验"],
        "candidates": [
            ("周宇靖", "清华大学 本科\n字节跳动 后端工程师 5 年\n"
                      "主导日均 2 亿请求的网关重构，Go / Kubernetes\n北京"),
            ("张伟", "某大学 本科\n外包公司 后端 6 年\n"
                    "参与支付系统维护，Java\n上海"),
            ("李思远", "985 硕士\n美团 后端 4 年\n"
                      "负责订单服务分库分表，QPS 3 万\n北京"),
            ("王琳", "双非本科\n创业公司 3 年\n"
                    "从 0 搭建后端，Go，团队 4 人\n杭州"),
            ("陈昊", "本科\n银行科技部 8 年\n"
                    "维护内部系统，Java / Oracle\n北京"),
        ],
    },
    {
        "title": "产品经理（B 端）",
        "jd": "3 年以上 B 端产品经验，能独立完成需求调研到上线。",
        "requirements": ["3 年以上 B 端产品经验", "有完整项目落地记录"],
        "preferred": ["有 SaaS 经验", "有数据产品经验"],
        "red_flags": ["只有功能罗列没有取舍理由"],
        "candidates": [
            ("孙悦", "本科\nSaaS 公司 产品经理 4 年\n"
                    "负责审批流产品，服务 2000 家企业客户，DAU 8 万"),
            ("刘畅", "本科\n教育公司 产品 2 年\n负责课程报名页功能迭代"),
            ("赵敏", "硕士\n咨询转产品 3 年\n"
                    "主导供应链协同平台，对接 30 家供应商"),
        ],
    },
    {
        "title": "销售经理（华东）",
        "jd": "有 B 端大客户销售经验，能独立跟进百万级项目。",
        "requirements": ["3 年以上 B 端销售", "有百万级项目经验"],
        "preferred": ["有华东区域资源"],
        "red_flags": ["业绩数字无法拆解"],
        "candidates": [
            ("吴强", "本科\n软件公司 销售 5 年\n年签单 1200 万，单笔最大 300 万，华东"),
            ("郑楠", "专科\n硬件销售 4 年\n负责区域渠道，年回款 600 万"),
            ("冯磊", "本科\n保险转行 2 年\n个人业绩 200 万，主要靠转介绍"),
        ],
    },
]


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
            out = json.loads(txt or "{}")
            for c in (out.get("cookies") or []) if isinstance(out, dict) else []:
                kv = str(c).split(";")[0]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    jar[k.strip()] = v.strip()
            return resp.status, out
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": txt}


def main():
    ap = argparse.ArgumentParser(description="注入演示数据（虚构候选人）")
    ap.add_argument("--base", default="http://127.0.0.1:8123",
                    help="服务地址")
    ap.add_argument("--user", default="admin01")
    ap.add_argument("--password", default="Passw0rd!x")
    ap.add_argument("--jobs", type=int, default=len(PRESETS),
                    help="注入几个岗位（最多 %d 个）" % len(PRESETS))
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    ap.add_argument("--init", action="store_true",
                    help="库还是空的时，用 --user/--password 创建首个管理员")
    args = ap.parse_args()

    print("=" * 66)
    print(" %s 演示数据注入 —— 全部为**虚构**内容，仅用于演示/联调" % DEMO_TAG)
    print(" 这些岗位的标题带 %s 前缀，可整岗删除以清理。" % DEMO_TAG)
    print("=" * 66)

    chosen = PRESETS[:max(1, min(args.jobs, len(PRESETS)))]
    if args.dry_run:
        for p in chosen:
            print(" 将创建：%s%s（%d 位候选人）"
                  % (DEMO_TAG, p["title"], len(p["candidates"])))
        print("--dry-run：未写入任何数据")
        return 0

    jar = {}
    st, r = call(args.base, "POST", "/api/auth/login",
                 {"username": args.user, "password": args.password}, jar)
    if (st != 200 or not r.get("ok")) and args.init:
        # 只在显式 --init 时尝试初始化，绝不自动建号：
        # 静默造一个管理员账号本身就是安全问题。
        st0, b0 = call(args.base, "GET", "/api/bootstrap", None, jar)
        if not (b0 or {}).get("needs_setup"):
            print("登录失败（%s）：%s" % (st, r.get("error") or r))
            print("系统已有账号，--init 不适用；请用 --user/--password 指定。")
            return 2
        st2, r2 = call(args.base, "POST", "/api/setup",
                       {"username": args.user, "password": args.password,
                        "display_name": "演示管理员"}, jar)
        if st2 != 200 or not r2.get("ok"):
            print("初始化管理员失败（%s）：%s" % (st2, r2.get("error") or r2))
            return 2
        print("  ✓ 已创建首个管理员：%s" % args.user)
        st, r = 200, {"ok": True}

    if st != 200 or not r.get("ok"):
        print("登录失败（%s）：%s" % (st, r.get("error") or r))
        print("提示：先启动服务；首次使用需在页面上创建管理员，"
              "或用 --user/--password 指定已有账号"
              "（库为空时加 --init 由本脚本创建）。")
        return 2

    created = []
    for p in chosen:
        st, r = call(args.base, "POST", "/api/jobs", {
            "title": DEMO_TAG + p["title"],
            "raw_text": p["jd"],
            "requirements": p["requirements"],
            "preferred": p["preferred"],
            "red_flags": p["red_flags"],
        }, jar)
        if st != 200 or not r.get("ok"):
            print("  岗位创建失败：%s → %s" % (p["title"], r.get("error") or r))
            continue
        jid = (r.get("job") or {}).get("id")
        n_ok = 0
        for name, text in p["candidates"]:
            # 走"上传纯文本"路径：不需要准备文件，也不用 base64
            st2, r2 = call(args.base, "POST", "/api/jobs/%s/candidates" % jid, {
                "candidates": [{"name": name,
                                "files": [{"filename": "简历.txt", "role": "resume",
                                           "data_base64": _b64(text)}]}],
            }, jar)
            if st2 == 200 and r2.get("added"):
                n_ok += 1
        created.append((jid, p["title"], n_ok))
        print("  ✓ %s%s：%d 位候选人" % (DEMO_TAG, p["title"], n_ok))

    print()
    print("完成。清理方式（逐个删除演示岗位即可连带删除其候选人）：")
    for jid, title, _ in created:
        print("  DELETE /api/jobs/%s    # %s%s" % (jid, DEMO_TAG, title))
    print()
    print("提醒：真实使用时请把这些 %s 岗位全部删掉，"
          "避免虚构简历混进评估结果。" % DEMO_TAG)
    return 0


def _b64(text):
    import base64
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


if __name__ == "__main__":
    sys.exit(main())
