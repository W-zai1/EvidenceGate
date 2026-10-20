"""EvidenceGate 引擎。

分层（依赖方向永远向下，反向引用即架构违规）：

    intake/        原始输入 → 文本（音频、文档、解密）
    extract/       文本 → 带归属的片段 / 主张
    orchestrator/  把 intake + extract 接成流水线，对外发事件
    graph/         案件层：持久化 + 编排（唯一知道数据库和时钟的地方）
    spine/         纯核心：IR、引用核验、非补偿闸门、证据包（无 DB / 无时钟 / 无模型）
    storage/       SQLite 连接、schema、迁移
    api/           HTTP 路由、鉴权、静态资源（不含业务判断）
    guardrails/    横切：审计、备份、隐私、回测对齐

场景扩展不要改 spine，在 spine/profiles/ 下新建 profile 并注册即可。

详见 docs/ARCHITECTURE.md（为什么这样分层）与 docs/MODULES.md（每个模块干什么）。
"""
