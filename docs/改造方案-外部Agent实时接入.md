# 改造方案：外部 Agent 实时接入（供评估 / 实施）

> 版本：v1 草案 · 目标版本：RoundTable Lite v2.1 → v2.2
> 对象：没有 CLI、只能被"唤起"的外部 Agent（典型：WorkBuddy）
> 目标：被 @点名 时能及时收到通知，且消息不丢。

---

## 一、先对齐口径：改造能做到什么

| 场景 | 现状 | 改造后 |
|---|---|---|
| 外部 Agent 会话开着，主动 listen | 轮询，延迟 ≤500ms | **事件驱动，亚秒级** |
| 外部 Agent 会话没开（无人值守） | 完全收不到 | **仍收不到，但消息进 inbox 不丢** |
| 外部系统（n8n / 机器人 / 自建服务） | 无通道 | **Webhook 主动推送** |

**必须讲清的一点**：真正的"无人值守实时"不在本项目能力范围内。因为外部 Agent 侧没有可被 Hub 调用的入口（WorkBuddy 无 CLI；定时调度最小粒度为小时级，不是分钟级）。本方案的目标是——**醒着时秒回，睡着时不漏，想接外部系统时有标准通道**。

---

## 二、现状分析（代码级）

### 2.1 触发链路

主席发言 → `orch.post_message()`（`app/orchestrator.py:67`）→ 解析 mention → `db.insert_message()` → `bus.broadcast()`（WS 推前端）→ `_schedule_from_message()` → 每会议一个 FIFO worker → `_run_trigger()`（`:195`）。

### 2.2 卡点 1：MCP 型成员永不主动调起

`app/orchestrator.py:199-200`：

```python
if p["transport"] != "cli":
    return  # MCP 型永不主动调起（需求 4.1）
```

这是需求 4.1 的既定设计。后果：**配置了 MCP 也不会被 Hub 通知**，MCP 客户端只能自己 `listen` 拉。

### 2.3 卡点 2：`listen` 是轮询，不是推送

`app/mcp_server.py:217-246`，核心是 0.5s 一次查库：

```python
while True:
    msgs = _poll()
    if msgs: ...
    if waited >= timeout: ...
    await asyncio.sleep(0.5)
    waited += 0.5
```

`timeout` 上限 60s（`mcp_server.py:212`），无消息时空转。

### 2.4 卡点 3：CLI 通道是唯一事件驱动，但要求可执行程序

`app/runner.py:115` `run_cli()` 用 `shutil.which(cmd)` 探测命令并 fork 子进程。WorkBuddy 无 CLI（`command -v workbuddy` 无结果），此路不通；`codex` 在 PATH 中，可用。

### 2.5 附带事实（影响设计）

- 落库入口只有 `db.insert_message()` / `db.update_message()`（`app/db.py:180` / `:209`），`update_message` 用于 streaming 占位转最终稿。
- WS 总线 `app/ws.py` 的 `Bus` 只面向 WebSocket 对象，无法被普通协程复用（但可作为接线参考）。
- `app/models.py:9` 有 **transport 白名单**：`TRANSPORTS = {"cli", "mcp"}`，新增类型必须同步改，否则 REST 建成员会被 Pydantic 拒。
- `schema.sql` 全用 `CREATE TABLE IF NOT EXISTS`，加表幂等，重启即生效，无需迁移脚本。
- 防失控机制（token bucket、四条熔断）在 `orchestrator.py` 与 `brakes.py`，新增通道**不应绕过刹车**。

---

## 三、改造项 A：listen 改事件驱动（P0，建议先做）

### 落点

新建 `app/events.py`；改 `app/mcp_server.py:217` 的 `listen`；在 `orchestrator.py` 落库处发信号。

### 设计

新增一个进程内信号总线（按 meeting_id 分组的 `asyncio.Event` 集合）。消息落库 / 更新后 `notify(meeting_id)`，`listen` 的等待从 `sleep(0.5)` 换成 `wait_for(ev.wait(), ...)`。

> 注意：这是**进程内**信号。Hub 是单进程，够用；跨进程（多实例）不适用——本项目单机单实例，无需考虑。

### 代码草案：`app/events.py`

```python
"""进程内消息信号总线：让 listen / SSE 从轮询改为事件驱动。"""
from __future__ import annotations

import asyncio

_waiters: dict[str, set[asyncio.Event]] = {}


def new_waiter(meeting_id: str) -> asyncio.Event:
    ev = asyncio.Event()
    _waiters.setdefault(meeting_id, set()).add(ev)
    return ev


def drop(meeting_id: str, ev: asyncio.Event) -> None:
    s = _waiters.get(meeting_id)
    if s is not None:
        s.discard(ev)
        if not s:
            _waiters.pop(meeting_id, None)


def notify(meeting_id: str) -> None:
    for ev in list(_waiters.get(meeting_id, ())):
        ev.set()
```

### 接线点

1. `orchestrator.py:111` 后（人类/Agent 发言落库并广播后）：`events.notify(meeting_id)`
2. `orchestrator.py:214`（占位 `··· 正在思考` 广播后）：**不要** notify，避免 listen 收到半成品（`_poll()` 已过滤 `status='streaming'`）
3. `orchestrator.py:259`（最终稿 `message.updated` 后）：`events.notify(meeting_id)`
4. `orchestrator.py:332` `system_message()` 广播后：`events.notify(meeting_id)`

### `listen` 改写要点

```python
import time
loop_t0 = time.monotonic()
ev = events.new_waiter(mid)
try:
    while True:
        m = db.get_meeting(mid)
        st = m["status"] if m else "stopped"
        if st == "stopped":
            _cursors[rec["display_name"]] = db.max_seq(mid)
            return {...}
        msgs = _poll()
        if msgs:
            _cursors[rec["display_name"]] = msgs[-1]["seq"]
            return {"ok": True, "meeting_status": st, "messages": msgs, "timeout": False}
        elapsed = time.monotonic() - loop_t0
        if elapsed >= timeout:
            return {"ok": True, "meeting_status": st, "messages": [], "timeout": True}
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout - elapsed)
        except asyncio.TimeoutError:
            pass
        ev.clear()          # 复用同一个 Event，醒来后必须清
        # 会议暂停/停止也要能醒来：见下方"必须处理"
finally:
    events.drop(mid, ev)
```

**必须处理**：`control.state` 变化（暂停/停止）也要 `notify`，否则 `listen` 在暂停期间会一直挂到 timeout。建议在 `orchestrator.py:381` `bus.broadcast(control.state)` 之前加 `events.notify(meeting_id)`。

### 验收

```
# 终端 1：MCP 客户端 listen（或写一个调用 listen 的小脚本）
# 终端 2：curl 发一条消息
```
- 消息落库后 listen 应立即返回（<1s），而不是等到下一个 0.5s 刻度（实测对比延迟即可）
- 空闲时 CPU 占用应明显下降
- 会议 stop 后 listen 立即返回 `control: stopped`（不能挂满 timeout）

---

## 四、改造项 B：SSE 事件流端点（P1）

### 落点

`app/main.py`，新增 `GET /api/meetings/{mid}/stream?after_seq=0`。

### 设计

给非 MCP 客户端（桥接脚本、其他语言 Agent、调试用）一个长连接。复用改造项 A 的 `events` 总线：连接后先按 `after_seq` 补齐历史，再挂 `ev.wait()`，有信号就拉增量推送。

### 代码草案

```python
from fastapi.responses import StreamingResponse

@app.get("/api/meetings/{mid}/stream")
async def stream_messages(mid: str, after_seq: int = 0):
    if db.get_meeting(mid) is None:
        raise HTTPException(404, "no_meeting")
    cursor = after_seq

    async def gen():
        nonlocal cursor
        ev = events.new_waiter(mid)
        try:
            while True:
                rows = db.messages_after(mid, cursor)     # db.py:216 已存在
                for r in rows:
                    cursor = max(cursor, r["seq"])
                    yield f"event: message.new\ndata: {json.dumps(r, ensure_ascii=False)}\n\n"
                try:
                    await asyncio.wait_for(ev.wait(), timeout=25)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"   # 注释行，防代理掐连接
                ev.clear()
        finally:
            events.drop(mid, ev)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
```

### 注意

- `db.messages_after(meeting_id, after_seq, limit)` 已存在（`db.py:216`），直接复用，别重写。
- 客户端断开会抛 `asyncio.CancelledError`，`finally` 清理即可。
- 前端现在走 `/ws`，**不要**把这个端点接进前端，避免双通道重复渲染。

### 验收

```bash
curl -N "http://127.0.0.1:8787/api/meetings/{mid}/stream?after_seq=0"
# 另开终端发消息，应立即看到 event: message.new
```

---

## 五、改造项 C：出站 Webhook（P1）

### 落点

`schema.sql` 加表；新建 `app/webhooks.py`；`orchestrator.py` 落库后异步派发；`main.py` 加管理端点。

### 表结构

```sql
CREATE TABLE IF NOT EXISTS webhook (
  id          TEXT PRIMARY KEY,
  meeting_id  TEXT,                       -- NULL 表示全局（所有会议）
  url         TEXT NOT NULL,
  events      TEXT DEFAULT 'message.new', -- 逗号分隔：message.new,message.updated,form.answered
  secret      TEXT DEFAULT '',            -- HMAC 签名密钥，可空
  enabled     INTEGER DEFAULT 1,
  created_at  TEXT NOT NULL
);
```

### 派发

在 `orchestrator.post_message()` 落库成功后（`orchestrator.py:111` 之后）：

```python
asyncio.create_task(webhooks.dispatch("message.new", meeting_id, msg))
```

`dispatch()` 要点：

- 用 `httpx.AsyncClient`（需在 `requirements.txt` 加 `httpx`），超时 5s
- 失败退避重试 2 次，失败写日志，**绝不抛回主流程**
- 带 `X-RT-Signature: sha256=<HMAC-SHA256(secret, body)>` 与 `X-RT-Event`
- payload 建议：

```json
{
  "event": "message.new",
  "meeting_id": "...",
  "ts": "2026-09-30T15:00:00",
  "message": { "seq": 15, "author_kind": "human", "author_name": "我", "content": "@WorkBuddy ..." },
  "mentions": [{ "participant_id": "...", "display_name": "WorkBuddy" }]
}
```

### 管理端点

`GET/POST/DELETE /api/webhooks`、`POST /api/webhooks/{id}/test`（发一条 ping）。可选做 UI；不做 UI 也能用 curl 配置。

### 注意

- Webhook 是**旁路通知**，不参与 token bucket，也不影响熔断计数——它不产生会议消息。
- 别在 `post_message` 里同步 await 网络请求，会拖慢人类发言的响应（当前设计是毫秒级返回）。

---

## 六、改造项 D：bridge 通道（P0，与 A 配套）

### 落点

`app/models.py:9`（白名单）、`app/orchestrator.py:195` `_run_trigger`、新建 `app/bridge.py`。

### 设计

新增 `transport = "bridge"`：Hub 被 @ 命中时**不 fork 子进程**（因为没有可执行的 CLI），而是把任务写成 inbox 文件，等外部 Agent 自己来取。

### 步骤

1. **放开白名单**（不做这步，新增类型会被 Pydantic 拒）：

```python
TRANSPORTS = {"cli", "mcp", "bridge"}
```

`ParticipantIn` / `ParticipantPatch` 的 `_transport` 校验共用该常量，改一处即可。

2. **写 inbox**：新建 `app/bridge.py`

```python
def inbox_dir(meeting_id: str, display_name: str) -> Path:
    p = DATA_ROOT / "inbox" / meeting_id / display_name
    p.mkdir(parents=True, exist_ok=True)
    return p

async def enqueue(meeting_id, participant, prompt, context) -> Path:
    """写一个待办任务文件，返回路径。"""
```

文件命名：`{seq:06d}-{message_id[:8]}.json`，内容：

```json
{
  "meeting_id": "...", "seq": 15, "message_id": "...",
  "author_name": "我", "content": "@WorkBuddy 你能否看到消息",
  "prompt": "（build_prompt 组装好的完整上下文）",
  "created_at": "2026-09-30T15:00:00",
  "state": "pending"
}
```

目录建议 `data/inbox/`（`data/` 已在用，且 SQLite 库在同目录，需确认 `.gitignore` 是否要加 `data/inbox/`）。

3. **`_run_trigger` 分支**（`orchestrator.py:199` 处）：

```python
if p["transport"] == "cli":
    ...  # 现有逻辑不变
elif p["transport"] == "bridge":
    await self._run_bridge(meeting_id, p, task)
    return
else:
    return
```

`_run_bridge` 要点：

- 复用 `self.build_prompt(meeting_id, p)` 组装上下文（`orchestrator.py:300`）
- `await bridge.enqueue(...)` 落文件
- 写一条**系统消息**或在占位消息里写明状态，例如 `（已排队，等待 WorkBuddy 取走）`
- **participant.status 必须回到 `idle`**（不能长期 `busy`，否则下一轮被"正忙，已跳过"挡掉，见 `orchestrator.py:201`）
- 不调用 `runner`，不涉及子进程与 300s 超时

4. **消费端约定**：外部 Agent 取走后删除 json 文件（或把 `state` 改为 `done`）。提供查询端点：

```
GET  /api/meetings/{mid}/participants/{pid}/inbox   # 列出待办
POST /api/meetings/{mid}/participants/{pid}/inbox/ack  # 标记已处理
```

### 注意

- bridge 型**不产生自动回复**，所以不会触发 `_relay` 接力——这是预期行为（等外部 Agent 用 `say` 发言时才接力）。
- 若担心 inbox 堆积，可加一个清理策略（超过 N 条或超过 24h 标记 stale），建议作为可选增强。

---

## 七、已知坑（实施前必读）

1. **`models.py:9` 白名单** —— 新增 transport 不改这里，REST 建成员直接 422。
2. **`Event.clear()` 时机** —— 复用同一个 `Event` 时必须清，否则 `listen` 会立即返回、退化成忙等。
3. **暂停 / 停止要能唤醒** —— `control.state` 变化也要 `notify`，否则挂起的 listen 要等到 timeout 才醒。
4. **streaming 占位不要 notify** —— listen 的 `_poll()` 过滤了 `status='streaming'`，但 notify 会让循环空转；只在最终稿后通知。
5. **别阻塞主流程** —— webhook / inbox 写入都必须异步或极轻；`post_message` 当前是毫秒级返回，不能劣化。
6. **不要绕过刹车** —— 新增通道不产生消息就不参与 token bucket；一旦产生消息必须过 `brakes.allow()`。
7. **schema 幂等** —— 加表用 `CREATE TABLE IF NOT EXISTS`，重启生效，老库不受影响。
8. **端口可能不是 8787** —— 被占用时顺延 8787→8797，所有示例地址以控制台实际打印为准。

---

## 八、建议实施顺序

| 顺序 | 项 | 理由 |
|---|---|---|
| 1 | **A**（listen 事件驱动） | 改动最小、收益最直接，且不引入新依赖 |
| 2 | **D**（bridge 通道） | 与 A 配套，解决"不漏消息"；依赖 A 的信号能力可选 |
| 3 | **B**（SSE） | 独立增量，方便调试与外部集成 |
| 4 | **C**（webhook） | 需要新依赖（httpx）与设计重试/签名，成本最高 |

A + D 做完即可满足"醒着秒回、睡着不漏"。B / C 按需。

**回滚**：全部改动均为新增文件或局部替换，备份 `app/` 目录与 `data/roundtable.db` 后即可整体还原；`schema.sql` 加表不会破坏旧版本读取（旧版忽略新表）。

---

## 九、验收清单

- [ ] A：消息落库后 listen 立即返回（<1s），不再等 0.5s 刻度
- [ ] A：会议 stop 后 listen 立即返回 `control: stopped`
- [ ] A：空闲 60s 内 CPU 无空转尖峰
- [ ] B：`curl -N` 能持续收到 `event: message.new`，断连后服务端清理 waiter
- [ ] C：配置的 URL 收到带正确 HMAC 签名的 POST；目标不可达不影响会议
- [ ] D：`transport=bridge` 能通过 REST 创建（白名单已放开）
- [ ] D：被 @ 后 `data/inbox/` 生成 json，participant 回到 `idle`
- [ ] 回归：Codex（CLI 与 MCP）两条既有通道行为不变；四条熔断与 token bucket 未被绕过
- [ ] 回归：主席 Stop 仍能终止 CLI 进程树

---

## 十、附：WorkBuddy 侧配套（本方案之外，由我方完成）

1. MCP 配置已写入 `C:/Users/XueQing/.workbuddy/mcp.json`（`RoundTable` → `http://127.0.0.1:8787/mcp`），待在连接器页面 Trust 后生效。
2. `docs/workbuddy-skill.md` 需补一步：先查 inbox（`GET .../inbox`），再决定是否发言。
3. A 上线后，WorkBuddy 在会话内可循环 `listen`，实现亚秒级响应。
4. 已确认：MCP `join("WorkBuddy")` 会复用 REST 创建的同一 `participant_id`（`mcp_server.py:112-119` 的 transport 复用分支），不会撞名。
