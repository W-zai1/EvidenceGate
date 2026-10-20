"""编排层：把 intake 和 extract 接成一条流水线，并对外发事件。

    ingest.py     文件夹 → 案件（自动猜材料角色，再映射到 profile 契约）
    events.py     事件总线（BUS.on("*", ...) 可监听全部）
    context.py    上下文预算管理（超长材料显式截断）
    agents.py     多 Agent 对立式评审（人才筛选场景）
    runs.py       评审运行管理（互斥、取消、事件重放）
    jobs_runner.py 后台执行
    rubric.py     评分标准版本化
    interview.py  面试追问清单生成

**ingest 的约定**：猜出来的角色如果和 profile 要求不一致，以 profile 为准
做映射，并把映射结果记进 warnings。不静默丢材料。

**context 的约定**：超预算时按真实 token 校验（不是按字符估），
保留头尾、中间插入「…（中段已省略）」，并记录 truncated_sections。
只保开头会把关键事实截掉，而结论表面看起来仍然正常。
"""
