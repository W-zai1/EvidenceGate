# EvidenceGate 模块手册

> **怎么用这份文档**：先看第 0 节的"我该看哪一层"，再跳到对应小节。
> 每个模块都按同一套格式写：**它做什么 / 谁该关心 / 它不做什么**。
> "它不做什么"和"它做什么"同样重要——边界模糊是这类系统出问题的头号原因。

---

## 0. 我该看哪一层

| 你想做的事 | 去这一层 | 别碰 |
| --- | --- | --- |
| 加一个新业务场景（合同、审计…） | `spine/profiles/` | 不要改 `spine` 核心 |
| 改判定规则 | `spine/gate.py` + `profiles/` | 不要在 `api/` 里写判断 |
| 改数据库、加字段 | `storage/db.py`（加迁移） | 不要在业务层写裸 SQL |
| 加一个接口 | `api/api.py` | 不要在路由里做业务计算 |
| 改"案件"的流程 | `graph/cases.py` | 不要绕过 spine 自己判 |
| 支持新的输入格式（PDF、音频…） | `intake/` | 不要在 `extract/` 里读文件 |
| 改"谁说的"识别 | `extract/diarization.py` | 不要在 `graph/` 里做文本处理 |
| 改界面 | `viewer/` | 不要在前端算结论 |

---

## 1. `engine/spine/` — 证据脊柱

### `ir.py` — 中间表示

**做什么**：定义整个系统共用的数据结构。`Artifact`（材料）、`EvidenceRef`
（指向原文某个位置的一段引文）、`Claim`（主张）、`Finding`（发现的矛盾）、
`Approval`（签署）、`TrailSnapshot`（审计链快照）、`Case`。

**谁该关心**：任何需要读写"案件/证据/主张"的人。**这是全系统唯一的术语表。**

**不做什么**：不读数据库、不做判断、不带业务规则。

---

### `verify.py` — 引用核验器

**做什么**：检查一条主张引用的那段原文，**是否真的逐字出现在**对应材料里。
纯字符串比对，不调模型。

**谁该关心**：想让"每条结论都能回到原文"这件事成立的人。

**不做什么**：不判断这段话是否"支持"该主张。它只回答"有没有这段字"，
不回答"这段话说明了什么"。后者是人的工作，系统只负责把不能核对的挑出来。

---

### `gate.py` — 非补偿闸门

**做什么**：按 profile 配置的条款逐条判定，任一条 `block` 级失败则整体不放行。
产出带 hash 的 `GateResult`。

**谁该关心**：想改判定规则的人。**改这里会改变所有场景的结论。**

**不做什么**：不读数据库、不看时间、不调模型。输入是 IR，输出是结论，
中间没有副作用。**这是它能被复现的全部原因。**

**关键约束**：参与 hash 的字段是封闭集合（见 `CORE_KEYS` / `CLAUSE_CORE_KEYS`）。
给结论加展示字段时，必须加在集合外。

---

### `bundle.py` — 证据包

**做什么**：把案件的材料、主张、发现、闸门结论打包成 zip，并附一个
`verify.py` 脚本。拿到包的人不装这个系统、不联网也能复核。

**谁该关心**：要把结论交给外部（监管、审计、客户）的人。

**不做什么**：不决定"能不能导出"（那是 `graph/cases.py` 的事）。

---

### `profiles/` — 场景配置

**做什么**：`base.py` 定义 `Profile`；`expense.py` / `talent.py` 是两个实例；
`__init__.py` 是注册表。

**谁该关心**：要加新业务场景的人。**这是唯一应该为新场景改动的地方。**

**不做什么**：不包含任何执行逻辑，只是配置数据。

**注意**：`engine.spine.profiles` 这个属性在 `spine/__init__.py` 里被**重绑**成了
注册表实例。所以 `from engine.spine import profiles` 拿到的是注册表，不是模块。
用 `profiles.keys()` / `profiles.get(key)`。

---

## 2. `engine/graph/` — 案件层

### `cases.py` — 案件编排

**做什么**：
- 创建/查询/删除案件
- 材料入库（含加密）
- 主张入库
- 签署（人签，按 profile 的 `judge_roles` 校验）
- 跑闸门（组装 IR → 调 spine → 存结论）
- 导出证据包
- **维护结论过期判断**

**谁该关心**：所有 API 路由的调用对象；想理解"一次判定怎么走完"的人。

**不做什么**：不做文本处理、不解析文档、不判定条款。

**关键机制**：`revision` / `gate_revision` 两个计数器。求值时
`gate_revision = revision`；之后任何改动推高 `revision`，结论即过期。

---

### `validation.py` — 证据链自检

**做什么**：8 项检查，回答"这条链现在有多可信"：

| ID | 查什么 |
| --- | --- |
| `CHAIN-01` | 审计链完整性 + 闸门 hash 可复算 |
| `GATE-01` | 闸门可复现 |
| `CITE-01` | 每条引用都能回链到原文 |
| `ANCHOR-01` | 链头是否外部锚定（**未锚定报 warn，不报 pass**） |
| `ROLE-01` | 签署角色合法 |
| `COVERAGE-01` | 必需材料齐全 |
| `SEPARATION-01` | 申报与复核职责分离 |
| `EVIDENCE-01` | 证据包可离线验 |

**谁该关心**：想知道"这份结论到底能信到什么程度"的人。

**不做什么**：不改变结论，只做体检。每项都有具体算出来的值（如
`"12/12 条主张逐字命中原文"`），不是一个笼统的勾。

---

## 3. `engine/intake/` — 原始输入层

| 模块 | 做什么 | 不做什么 |
| --- | --- | --- |
| `docparse.py` | 各种格式 → 纯文本 | 不判断内容真伪 |
| `audio_ingest.py` | 音频 → 文本 | 不做说话人区分（那是 `extract/`） |
| `crypto.py` | 静态加密，明文不落盘（`enc:v1:<b64>`） | 不管业务 |
| `fence.py` | 提示词注入防护 | 不修改正常内容 |

**共同约定**：失败必须**明确报错**。返回空字符串会让下游把"没读到"当成
"确实没有内容"，这是最危险的一类静默错误。

---

## 4. `engine/extract/` — 抽取层

| 模块 | 做什么 | 关键点 |
| --- | --- | --- |
| `diarization.py` | 文本 → 带说话人归属的片段 | 认 `面试官：` / `候选人：` 等前缀 |
| `interview_analysis.py` | 问答配对：问了有没有答、答没答上 | 以**问题**为中心配对，未匹配的问题记为 `not_asked`，多余的回答记为 `ad_hoc` |
| `extract_claims.py` | 文本 → 待核验主张草稿 | 只产出草稿，入库需人确认 |
| `identity.py` | 身份消歧（同名不同人） | — |
| `jd_struct.py` | 岗位要求 → 结构化字段 | — |

**共同约定**：输出必须**带归属**（哪份材料、哪个说话人、哪个位置）。
没有归属的内容无法追责。

---

## 5. `engine/orchestrator/` — 编排层

| 模块 | 做什么 |
| --- | --- |
| `ingest.py` | 文件夹 → 案件。自动猜材料角色，再映射到 profile 契约 |
| `events.py` | 事件总线（`BUS.on("*", ...)` 可监听全部） |
| `context.py` | 上下文预算管理（超长材料显式截断） |
| `agents.py` | 多 Agent 对立式评审（人才筛选场景） |
| `runs.py` / `jobs_runner.py` | 评审运行管理、后台执行 |
| `rubric.py` | 评分标准版本化 |
| `interview.py` | 面试追问清单生成 |
| `util.py` | 共享工具 |

**`ingest.py` 的关键约定**：猜出来的角色如果和 profile 要求不一致，
**以 profile 为准**做映射，并把映射记进 `warnings`。不静默丢材料。

**`context.py` 的关键约定**：超预算时按**真实 token** 校验（不是按字符估），
保留头尾、中间插入 `…（中段已省略）`，并记录 `truncated_sections`。
只保开头会把关键事实截掉，而结论表面看起来仍然正常。

---

## 6. `engine/api/` — 接入层

| 模块 | 做什么 |
| --- | --- |
| `server.py` | HTTP 服务器：静态资源 + 路由分发（零依赖） |
| `api.py` | 路由表 + 各接口实现 |
| `auth.py` | 账号、口令、会话、角色、CSRF |
| `http_base.py` | 请求解析、响应输出、鉴权闸门 |
| `sse.py` | 服务端推送（评审进度实时更新） |
| `llm_client.py` / `provider_probe.py` | 模型调用 |
| `usage.py` / `cost_estimate.py` | 用量记账、成本预估 |
| `workbuddy_client.py` | 复用 WorkBuddy 账号调用模型 |

**共同约定**：路由函数只做"取参数 → 调 graph → 返回 JSON"。
出现业务判断说明放错层了。

**权限矩阵要点**：
- `admin`：全部
- `hr`：案件读写 + 签署 + 导出 + profile/校验读取（**业务主要使用者**）
- `viewer`：只读（`case.read`、`profile.read`、`validation.read`）

---

## 7. `engine/storage/` — 数据层

| 模块 | 做什么 |
| --- | --- |
| `db.py` | 连接池、schema、迁移（`SCHEMA_VERSION = 10`）、`now_iso()`、`tx()` |
| `store.py` | 岗位与候选人仓储（人才筛选场景） |
| `talent.py` | 人才库聚合（按 `people` 表，不按姓名） |

**加字段的唯一正确方式**：新增一个 `@_migration(N)` 函数，里面用
`PRAGMA table_info` 判断列是否存在再 `ALTER TABLE`，并推高 `SCHEMA_VERSION`。

**已知坑**：`now_iso()` 只到秒。任何"先后顺序"判断都不要用时间戳比较，
用单调计数器（见 `graph/cases.py` 的 `revision`）。

---

## 8. `engine/guardrails/` — 护栏

| 模块 | 做什么 |
| --- | --- |
| `audit.py` | 审计日志：只追加、哈希链 |
| `backup.py` | 数据库备份与恢复 |
| `privacy.py` | 保留期、导出、删除、密钥轮换 |
| `align.py` | 回测对齐：排的前 N 名里，有几个是真会推的 |
| `outcome.py` | 结果回填与校准 |

---

## 9. 其他目录

| 目录 | 内容 |
| --- | --- |
| `viewer/` | 前端（`index.html` / `app.js` / `api.js` / 样式）。**契约由它定义** |
| `tests/` | 28 个测试文件，全部零依赖 |
| `tools/` | 评测与检查脚本 |
| `data/` / `jobs/` / `inputs/` | 运行时数据 |
| `docs/` | 文档（含本文件） |

---

## 10. 常见改动的正确落点

| 需求 | 改哪里 | 不要改哪里 |
| --- | --- | --- |
| 新增一个闸门条款 | `spine/gate.py` 加函数 + profile 里配置 | 不要在 `api/` 里判断 |
| 新增一个业务场景 | 新建 `spine/profiles/xxx.py` + 注册 | 不要改 `spine` 核心 |
| 新增案件字段 | `storage/db.py` 加迁移 | 不要直接 `ALTER` 线上库 |
| 新增 API 接口 | `api/api.py` + `auth.py` 权限 | 不要在路由里算结论 |
| 改"过期"判定 | `graph/cases.py` | 不要用时间戳比较 |
| 改截断策略 | `orchestrator/context.py` | 不要静默截断 |
| 改说话人识别规则 | `extract/diarization.py` | 不要在 `graph/` 里处理文本 |

---

## 11. 读到这些代码时请注意

- **`engine.spine.profiles` 是注册表实例，不是模块。** 用 `keys()` / `get(key)`，
  不要用 `labels()` 的返回值去查表（返回的是 `key@version` 显示串）。
- **`GateResult` 的条款字段叫 `title`**，界面层映射出的别名才是 `requirement`。
- **用 `ORDER BY rowid DESC` 取"最近一次闸门"**，不要用 `created_at DESC`——
  同一秒内的多条记录按时间排不出先后。
- **`engine/` 根目录的 `do_restructure.py` / `fix_imports.py`** 是一次性重构脚本，
  不属于运行时代码，不要导入。
- 部分文件带 UTF-8 BOM，Python 能正常读，但某些 `ast.parse` 用法会报错。
