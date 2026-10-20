# EvidenceGate · 浅色原型（design/light）

> **这份原型已经落地**：主应用 `viewer/` 采用同一套 token 与信息结构，
> 案件页 / 证据链页 / 案件详情直接可以在应用里用。这里的 HTML 保留为设计对照，
> 不是产品代码。真实主题的源文件是 `viewer/styles-dark.css`（深色原稿）与
> `viewer/styles.css`（生成的浅色 + 手工修正层）。

| 文件 | 内容 |
|---|---|
| `theme.css` | 完整 token + 组件样式（顶栏 / 卡片 / 表格 / 状态 / 引用 chip / 打印） |
| `run-suite.html` | 运行详情：校验表 + 证据栏 + 裁定摘要（对应参考截图的 Golden Thread 页） |
| `gate.html` | 闸门裁定：非补偿阻断 + 三级复核 + 留痕 |
| `run-suite.png` / `gate.png` | 1440px 截图，用于贴进提案与评审 |

## 怎么看

直接双击 HTML（无构建、无外部依赖、无字体下载）。截图用 Edge/Chrome 无头模式生成：

```powershell
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
& $edge --headless=new --disable-gpu --hide-scrollbars --screenshot="run-suite.png" `
        --window-size=1440,1600 "file:///<repo>/design/light/run-suite.html"
```

## 三条约束（改样式时别破坏）

1. **对比度**：正文与状态色对白底 ≥ 4.5:1。`--faint`（3.1:1）只能用于示例标记这类非必要信息。
2. **状态不靠颜色**：`✓ 通过 / ! 需人工 / × 未闭合` 永远图标 + 文字 + 颜色三件套。
3. **可打印**：`@media print` 下卡片不拆分、按钮隐藏、A4 页边距 14mm。

## 数据是假的

页面里的人名、金额、哈希、Trace ID 都是示例。唯一真实的是**信息结构**：结论旁边必须有证据、被剔除的东西必须可见、缺口必须阻断。
