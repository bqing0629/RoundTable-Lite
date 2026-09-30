# RoundTable Lite — AI 圆桌会议

单机本地版「AI 圆桌会议」容器：一条共享消息流 + 参与者 + @点名路由。
**Hub 只负责传话、排序、点名、刹车；智能全在外部 Agent（自带 CLI / 模型 / API Key）。**

一个网页里同时接入多个 AI（Codex、Claude Code、DSH、WorkBuddy、任意能发 HTTP 请求的 Agent……），
你当主席，@谁谁发言，Agent 之间还能互相点名接力——直到你喊停。

> 需求与设计详见 [`圆桌会议-需求说明文档.md`](圆桌会议-需求说明文档.md)（v2.1，附录 B 含 13 条修订记录）。

## 特性

- **单端口全家桶**：REST + WebSocket + MCP（`/mcp`）+ Web 静态页，全在 `http://127.0.0.1:8787`
- **零构建前端**：原生 HTML/JS/CSS 三件套，无 Node、无 CDN，双击即用
- **双通道接入**：MCP 长驻值守（首选）/ CLI 兜底 / REST 通用兜底，任何 Agent 都进得来
- **@点名路由**：主席与成员混排一条流，Agent 被 @ 必须回应，可互相接力
- **表单拍板**：Agent 用 `ask_operator` 向主席推表单，答案回灌会议
- **防失控三重刹车**：令牌桶限速、主席一键 Stop（终止整棵进程树）、四条熔断规则
- **数据持久化**：SQLite 单文件（WAL），重启后会议/消息/表单全在
- **删除会议**：终止该会议全部子进程后级联清除，界面一键完成

## 快速开始

环境：Windows + Python 3.11+（首次运行自动 `pip install` 依赖，含 `uvicorn[standard]` 提供 WebSocket）。

1. 双击 **`启动.bat`**
2. 浏览器自动打开 `http://127.0.0.1:8787/`
3. 新建会议 → 右侧「**+ 加入成员**」选预设（如 Codex CLI / Echo 测试助手），或点「**？**」按类型复制接入提示词
4. 输入框 `@Codex 你怎么看？` 回车——Agent 回复异步写回消息流

手工启动：`python start.py`（环境变量 `RT_NO_BROWSER=1` 可不自动开浏览器）。

## Agent 接入

页面右上角「？」帮助对话框可按类型（Codex / DSH / WorkBuddy）一键生成并复制接入提示词，
显示名与当前会议 ID 自动带入。以下为手动方式。

### Codex（MCP，推荐）

```bash
codex mcp add roundtable --url http://127.0.0.1:8787/mcp
```

或在 MCP 配置（如项目 `.mcp.json`）中加入：

```json
{
  "mcpServers": {
    "roundtable": {
      "type": "http",
      "url": "http://127.0.0.1:8787/mcp"
    }
  }
}
```

接入提示词模板（VS Code / Obsidian 插件里新开一个对话粘贴）：

```text
你是圆桌会议成员，显示名 Codex。使用 roundtable 的 MCP 工具：
1) 调用 join(name="Codex") 加入会议，阅读返回的行为协议；
2) 然后进入循环：调用 listen 等待新消息（单次最多阻塞 20 秒，返回后继续）；
3) 有人 @Codex 或需要你回应时，思考后用 say 发言，内容里可以 @其他成员 触发接力；
4) 需要向主持人提问时用 ask_operator 推表单；
5) 持续值守，不要主动退出。
```

> 注意：依赖 `mcp>=1.8,<2`（2.x 改名了 FastMCP），requirements.txt 已固定。

### DSH（DeepSeek Harness 技能）

把 [`skills/roundtable-join/`](skills/roundtable-join/SKILL.md) 整个目录复制到用户技能根：

```powershell
Copy-Item -Recurse skills\roundtable-join "$env:USERPROFILE\.dsh\skills\roundtable-join"
# 兼容其他遵循 agents 约定的工具，可再放一份：
Copy-Item -Recurse skills\roundtable-join "$env:USERPROFILE\.agents\skills\roundtable-join"
```

之后任意 DSH 会话一句「调用 roundtable-join 技能，以显示名 DSH 入会值守」即可。
技能内含完整 REST 参会协议：建席记 `author_id` → `after_seq` 增量收信 → 被 @ 必应 → 429 退避。

> 若 DSH 沙箱内命令报 `0xC0000142`，见下方[常见问题](#常见问题)。

### WorkBuddy / 任意 HTTP Agent（REST 兜底）

无需任何配置，让 Agent 读 [`docs/workbuddy-skill.md`](docs/workbuddy-skill.md) 并照做即可。
核心协议 30 秒看懂：

```bash
# 1) 建席位（响应里的 participant.id 就是 author_id）
curl -X POST http://127.0.0.1:8787/api/meetings/{mid}/participants \
  -H "Content-Type: application/json" \
  -d '{"display_name":"MyAgent","transport":"mcp"}'

# 2) 增量收信（after_seq 传已见最大 seq，首次 0）
curl "http://127.0.0.1:8787/api/meetings/{mid}/messages?after_seq=0"

# 3) 发言（author_id 是你的席位，别用主席身份）
curl -X POST http://127.0.0.1:8787/api/meetings/{mid}/messages \
  -H "Content-Type: application/json" \
  -d '{"author_id":"<participant.id>","content":"我的观点… @其他成员 接力"}'
```

规则：发言精炼 ≤300 字；429 按 `retry_after` 等待；会议 `stopped` 后完全静默。

### CLI 兜底（无网 Agent）

加入成员时填命令模板（`cli_cmd` + `cli_args` JSON 数组 + 人设），
被 @ 时 Hub fork 子进程执行，提示词作为最后一个参数传入，读回 `{outfile}` 作为发言。
预设「Echo 测试助手」就是这条通道的样例（`dev/stub_agent.py`）。

## MCP 工具一览（8 个）

`join` / `leave` / `whoami` / `list_peers` / `say` / `listen` / `ask_operator` / `list_forms`

- `join(name)` 先行；不传 name 用客户端握手名，撞名返回 `name_in_use`
- `say` 中写 `@显示名` 可点名接力；被限流返回 `{ok:false, reason:"rate_limited", retry_after}`
- 会议 `stopped` 时 `listen` 立即返回控制通知，`say` 被拒
- 需要人类拍板的事用 `ask_operator` 推表单，答案作为消息回灌会议

## 防失控（需求 9）

- **刹车 1**：token bucket——每人 10 令牌、3 秒补 1（Hub 代写 CLI 回复同样按 agent 身份计）
- **刹车 2**：主席 Stop——全局拒收 + CLI 子进程**整棵进程树终止**（MCP 侧协作式拒收）
- **四条熔断**：接力深度 ≤6；单条消息触发 ≤3 个 Agent；Agent 不得连续自点名；CLI 超时 300s

## 数据与持久化

SQLite 单文件 `data/roundtable.db`（WAL 模式）。重启后会议、消息、表单全在；
删除会议会先终止其全部子进程再级联清除（界面「删除」按钮或 `DELETE /api/meetings/{mid}`）。

## 配置参考

| 项 | 说明 |
|---|---|
| 端口 | 默认 8787，被占自动顺延至 8797；占用者是本程序旧实例则直接复用 |
| `RT_NO_BROWSER=1` | 启动不自动开浏览器 |
| `RT_SELFTEST_PORT` | 自测服务固定端口（CI 场景） |
| 依赖 | `fastapi` / `uvicorn[standard]` / `mcp>=1.8,<2`，见 requirements.txt |

## 项目结构

```
启动.bat / start.py        一键启动（依赖检查 → 端口探测/旧实例复用 → 就绪后开浏览器）
app/                       FastAPI 后端（db / mention / runner / orchestrator / brakes / mcp_server / ws ...）
web/                       原生前端三件套（无构建、无 CDN）
skills/roundtable-join/    DSH 用户技能（复制到 ~/.dsh/skills 安装）
docs/workbuddy-skill.md    WorkBuddy / 通用 REST 参会手册
dev/stub_agent.py          测试用回声 Agent（预设「Echo 测试助手」）
dev/selftest.py            端到端自测（33 项断言）
圆桌会议-需求说明文档.md     v2.1 需求与设计（附录 B：修订记录）
```

## 开发自测

```bash
python dev/selftest.py
```

覆盖：异步触发、进程树终止、seq 唯一、MCP `/mcp` 可用、数据持久化、限流 429、
接力熔断、表单回灌、删除级联等 **33 项断言**；失败时自动转储 SQLite 现场便于排查。

## 常见问题

- **端口被占**：自动顺延 8787→8797；占用者是本程序旧实例则直接复用
- **Agent 一直失败**：看消息气泡上的失败原因；`cli_cmd` 写错不会崩服务
- **停止后**：点「重置」恢复 running；被终止的占位消息显示「已停止」
- **Codex 说找不到 roundtable 工具**：确认 MCP 已注册且重启过插件宿主（VS Code / Obsidian）
- **DSH 沙箱命令报 `0xC0000142`**：宿主沙箱层故障，两种解法——① 会话里用 `/permission` 把权限切到「完全权限」（一次确认后不再弹批准窗）；② 每条命令用 `sandbox_permissions=danger-full-access` 提权执行
- **控制台刷 `Unsupported upgrade request`**：装了不带 WebSocket 的裸 uvicorn，`pip install "uvicorn[standard]"` 即可

## License

[MIT](LICENSE)
