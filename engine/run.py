"""
入口：运行多 Agent 评判引擎，产出人才储备库数据契约。

用法：
  OPENAI_API_KEY=sk-xxx python run.py --provider deepseek --job jobs/backend-senior.json --inputs ../inputs
  OPENAI_API_KEY=sk-xxx python run.py --provider qwen --job jobs/backend-senior.json --inputs ../inputs
  python run.py --provider ollama --job jobs/backend-senior.json --inputs ../inputs   # 本地，无需 Key

设计底线（不要退回）：
  没有真实模型可用时**直接报错退出**，不再产出 mock 占位数据。
  评审结论必须来自真实模型，否则"谁强谁弱"是脚本编的。

输出：
  - ../talent_pool.json          储备库兼容契约（viewer 默认读取）
  - ../output/<日期>_<岗位>.json  按日期+岗位归档
"""
import json
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from agents import run_batch_concurrent, DIMENSIONS, WEIGHTS
from llm_client import PROVIDER_PRESETS, LLMError, LLMConfigError


def run_engine(config):
    """核心流水线：依据 config 运行多 Agent 评判，返回 talent_pool dict（默认写盘）。
    config 字段：api_key, base_url, model, provider,
                 job(JD 路径), inputs(素材目录), workers, out, write
    前端 /api/run 与命令行均复用此函数。
    缺凭证 / 缺 JD / 缺素材 一律抛 LLMConfigError，不做任何降级。
    """
    provider = config.get("provider")
    api_key = config.get("api_key") or os.getenv("OPENAI_API_KEY")
    preset = PROVIDER_PRESETS.get(provider) if provider else None

    # ★ WorkBuddy 反代：凭证是本机桌面端登录态，不需要（也不使用）api_key
    if provider == "workbuddy":
        base_url = config.get("base_url") or preset["base_url"]
        model = (config.get("model")
                 or preset["model"]
                 or os.getenv("OPENAI_MODEL")
                 or "deepseek-v4-pro")
        api = {"base_url": base_url, "model": model, "provider": provider}
        print(f"[engine] WorkBuddy 反代模式：消耗 WorkBuddy 账号积分  模型：{model}")
        status = _preflight_workbuddy()
        if not status.get("ok"):
            raise LLMConfigError("WorkBuddy 通道不可用：" + status.get("error", "未知原因"))
        who = status.get("nickname") or "(未取到昵称)"
        print(f"[engine] WorkBuddy 已就绪：账号 {who}"
              + (f"（{status['domain']}）" if status.get("domain") else ""))
    else:
        # 本地推理服务（Ollama）不校验密钥，给个占位值以免被"必须有凭证"挡住
        if not api_key and provider == "ollama":
            api_key = "ollama"

        if not api_key:
            raise LLMConfigError(
                "未配置模型凭证：请设置 OPENAI_API_KEY 环境变量，"
                "或用 --provider 选择预设（deepseek/qwen/openai）并填入对应 Key；"
                "本地模型可用 --provider ollama，或用 --provider workbuddy 复用 WorkBuddy 积分。"
            )

        base_url = (config.get("base_url")
                    or (preset["base_url"] if preset else None)
                    or os.getenv("OPENAI_BASE_URL")
                    or "https://api.openai.com/v1")
        model = (config.get("model")
                 or (preset["model"] if preset else None)
                 or os.getenv("OPENAI_MODEL")
                 or "gpt-4o-mini")
        api = {"api_key": api_key, "base_url": base_url, "model": model, "provider": provider}
        print(f"[engine] 真实 LLM 后端：{base_url}  模型：{model}"
              + (f"  （预设：{provider}）" if provider else ""))

    job = load_job(config.get("job"))
    weights = job.get("weights") or WEIGHTS

    inputs = config.get("inputs")
    if not inputs:
        raise LLMConfigError("未指定素材目录：请用 --inputs 指定简历/测评素材所在目录。")
    print(f"[engine] 从 {inputs} 走 ingest 装配候选人 ...")
    candidates = resolve_inputs(inputs, job)
    if not candidates:
        raise LLMConfigError(
            f"在 {inputs} 未扫描到任何候选人素材（支持 .md/.txt/.json 简历与转写稿）。"
        )

    print(f"[engine] 开始评判 {len(candidates)} 位候选人（岗位：{job.get('title','?')}）...")
    results = run_batch_concurrent(candidates, api, max_workers=config.get("workers", 4))

    pool = {
        "version": "1.0",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "mode": "llm",
        "dimensions": DIMENSIONS,
        "weights": weights,
        "job": {k: job.get(k) for k in ("job_id", "title")},
        "candidates": results,
    }

    if config.get("write", True):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # talent_screen/
        root_json = os.path.join(root, "talent_pool.json")
        with open(root_json, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=2)
        out = config.get("out") or os.path.join(os.path.dirname(__file__), "..", "output")
        os.makedirs(out, exist_ok=True)
        safe_title = (job.get("title") or "job").replace("/", "-")
        stamp = datetime.date.today().isoformat()
        arch = os.path.join(out, f"{stamp}_{safe_title}.json")
        with open(arch, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=2)
        print(f"[engine] 完成。兼容契约 -> {root_json}")
        print(f"[engine] 归档 -> {arch}")

    for r in results:
        print(f"  - {r['name']}  综合 {r['total']}  等级 {r['grade']}  标签 {r['tags']}")
    return pool


def _preflight_workbuddy():
    """WorkBuddy 反代前置检查：确认本机已登录，避免跑到一半才发现用不了。"""
    try:
        from workbuddy_client import is_available
        return is_available()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def load_job(path):
    """加载岗位 JD。找不到就报错——不再回退内置样例 JD。

    同时尝试「当前目录」与「项目根（engine 上级）相对路径」，兼容从不同 cwd 启动。
    """
    if not path:
        raise LLMConfigError(
            "未指定岗位 JD：请用 --job 指定。可以从 jobs/jd-template.json 复制一份再填。"
        )
    for p in (path, os.path.join(os.path.dirname(__file__), "..", path)):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    raise LLMConfigError(f"岗位 JD 文件未找到：{path}")


def resolve_inputs(inputs_dir, job):
    """走 ingest 胶水层：简历/转写 → 标准 candidate dict。"""
    from ingest import build_candidates_from_dir
    return build_candidates_from_dir(inputs_dir, job)


def _default_inputs():
    """默认素材目录：项目根的 inputs/（engine 的上一级）。"""
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "inputs"))


def _default_job():
    """CLI 没有默认 JD。

    ⚠ 这里刻意**不**指向某个样例文件。
    早期默认指向 `jobs/backend-senior.json`，那是个自带职位的样例——
    于是 `python run.py` 不带参数就会拿一份"别人的岗位要求"去评你的候选人，
    得出来的分数看起来完全正常，但评的根本不是你要招的岗位。
    这种错误不会报错，只会悄悄给出无意义的结论，所以宁可强制你显式指定。
    """
    return None


def main():
    ap = argparse.ArgumentParser(description="多 Agent 对立式人才评判引擎")
    ap.add_argument("--job", default=_default_job(),
                    help="岗位 JD json（必填；可从 jobs/jd-template.json 复制）")
    ap.add_argument("--inputs", default=_default_inputs(),
                    help="原始素材目录（简历/转写），走 ingest（默认 ../inputs）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "output"),
                    help="归档输出目录")
    ap.add_argument("--workers", type=int, default=4, help="并发候选人数")
    ap.add_argument("--provider", default=None, choices=sorted(PROVIDER_PRESETS),
                    help="LLM 供应商预设（deepseek/qwen/ollama/openai）。")
    ap.add_argument("--model", default=None,
                    help="覆盖默认模型名（例如 deepseek-chat / qwen-plus / gpt-4o-mini）。")
    # ---- WorkBuddy 反代辅助命令（都不跑人才库筛选）----
    ap.add_argument("--list-workbuddy-models", action="store_true",
                    help="列出 WorkBuddy 账号可用模型（含积分倍率）后退出。")
    ap.add_argument("--workbuddy-credits", action="store_true",
                    help="查询 WorkBuddy 账号剩余积分后退出。")
    ap.add_argument("--test-workbuddy", action="store_true",
                    help="只测试 --model 指定的模型能否连通后退出。")
    args = ap.parse_args()

    if args.list_workbuddy_models:
        from workbuddy_client import describe_models
        for m in describe_models():
            rate = "免费" if m["free"] else ("倍率未知" if m["multiplier"] is None
                                          else "x%s 积分" % m["multiplier"])
            print(" - %-20s %-12s ctx=%-8s out=%-7s reasoning=%s"
                  % (m["id"], rate, m["context"], m["max_output"], m["reasoning"]))
        return

    if args.workbuddy_credits:
        from workbuddy_client import fetch_credits
        c = fetch_credits()
        if c.get("error"):
            print("[error] 积分查询失败：%s" % c["error"])
            sys.exit(1)
        print("剩余积分：%s / %s（已用 %s）" % (c["total_remain"], c["total_size"], c["total_used"]))
        for p in c["packages"]:
            print(" - %-30s %s/%s%s" % (p["name"], p["remain"], p["size"],
                                        "（月度）" if p["monthly"] else ""))
        if c["expiring_soon"]:
            print("⚠ %d 个积分包将在 3 天内到期" % len(c["expiring_soon"]))
        return

    if args.test_workbuddy:
        # 只测连通性，不跑人才库筛选
        from workbuddy_client import test_connection
        res = test_connection(args.model)
        if res["ok"]:
            echo = ("该模型可用：%s（%dms）" % (res["reply"], res["latency_ms"])
                    if res["reply"] else "已连通（%dms）" % res["latency_ms"])
            print("✓ 可用 · %s · %s" % (res["model"], echo))
        else:
            print("✗ 不可用 · %s · %s" % (res["model"], res["error"]))
            sys.exit(1)
        return

    config = {
        "api_key": os.getenv("OPENAI_API_KEY"),
        "base_url": os.getenv("OPENAI_BASE_URL"),
        "model": args.model or os.getenv("OPENAI_MODEL"),
        "provider": args.provider,
        "job": args.job,
        "inputs": args.inputs,
        "workers": args.workers,
        "out": args.out,
        "write": True,
    }
    try:
        run_engine(config)
    except LLMConfigError as e:
        print(f"[error] {e}")
        sys.exit(2)
    except LLMError as e:
        print(f"[error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
