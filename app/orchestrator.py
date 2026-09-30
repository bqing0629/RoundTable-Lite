"""触发 → 接力 → 防失控总控（需求 5.1 / 9）。

v2.1 关键点：
- **触发异步化**：post_message() 在请求内只做"校验 → 写库 → 解析 → 入队"，
  立即返回；Agent 触发与接力全部在每会议一个 FIFO 队列的常驻 worker 里串行执行；
- "我"的发言与主席操作永远不排队；排队的只是 Agent 触发；
- 四条熔断：接力深度 ≤6、单条消息触发 ≤3、自点名禁止、CLI 超时 300s；
- Hub 代写 CLI 回复时以该 agent 身份过 token bucket（需求 9 刹车 1）。
"""
from __future__ import annotations

import asyncio
import json

from . import db, mention, runner
from .brakes import brakes
from .ws import bus

MAX_DEPTH = 6            # 熔断 1：单条接力链最大轮数
MAX_PER_MESSAGE = 3      # 熔断 2：单条消息最多触发 Agent 数
SELF_MENTION_LIMIT = 1   # 熔断 3：同一 Agent 的回复不得再次触发自己
CLI_TIMEOUT = 300        # 熔断 4：单次 CLI 超时（秒）

CTX_MAX_MSGS = 30        # 需求 4.6：上下文最多 30 条
CTX_MAX_CHARS = 12000    # 且 ≤12000 字符
CTX_KEEP_HEAD = 5        # 超长时保留最早 5 条
CTX_KEEP_TAIL = 20       # + 最近 20 条


class Orchestrator:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._cap_announced: set[tuple[str, str]] = set()  # (meeting_id, message_id)

    # ---------- 队列与 worker ----------

    def _queue(self, meeting_id: str) -> asyncio.Queue:
        if meeting_id not in self._queues:
            self._queues[meeting_id] = asyncio.Queue()
        return self._queues[meeting_id]

    def _ensure_worker(self, meeting_id: str) -> None:
        t = self._workers.get(meeting_id)
        if t is None or t.done():
            self._workers[meeting_id] = asyncio.create_task(
                self._worker(meeting_id), name=f"worker-{meeting_id[:8]}"
            )

    async def shutdown(self) -> None:
        for t in list(self._workers.values()):
            t.cancel()
        self._workers.clear()
        self._queues.clear()
        await runner.kill_all()

    def forget_meeting(self, meeting_id: str) -> None:
        t = self._workers.pop(meeting_id, None)
        if t is not None:
            t.cancel()
        self._queues.pop(meeting_id, None)
        for key in [k for k in self._cap_announced if k[0] == meeting_id]:
            self._cap_announced.discard(key)

    # ---------- 发言入口（REST 人类 / MCP say 共用） ----------

    async def post_message(
        self, meeting_id: str, *, author_kind: str, author_id: str | None,
        author_name: str, content: str, source_depth: int = 0,
    ) -> tuple[dict | None, dict | None]:
        """写入一条消息并安排触发。返回 (message, error)。
        error 形如 {"ok": False, "reason": "stopped"} / {"ok": False,
        "reason": "rate_limited", "retry_after": N}。请求内毫秒级返回。"""
        m = db.get_meeting(meeting_id)
        if m is None:
            return None, {"ok": False, "reason": "no_meeting"}
        if m["status"] == "stopped":
            return None, {"ok": False, "reason": "stopped"}
        if m["status"] == "paused" and author_kind != "human":
            # 暂停 = 冻结 Agent 发言；"我"的发言仍记录（与协议文本一致）
            return None, {"ok": False, "reason": "paused"}

        allowed, retry = brakes.allow(author_id or f"anon:{author_name}")
        if not allowed:
            return None, {"ok": False, "reason": "rate_limited", "retry_after": retry}

        # @ 解析（需求 1.3 / 5.1 第 5-6 步）：对全部成员解析出 mention 行，
        # 触发与送达判定只看"启用中的 agent 或人类"。
        names = {r["display_name"]: r["id"]
                 for r in db.list_participants(meeting_id)}
        parsed = mention.parse(content, list(names))
        participants = {p["id"]: p for p in db.list_participants(meeting_id)}
        effective = []
        for (nm, off, ln) in parsed:
            p = participants.get(names.get(nm, ""))
            if p and (p["kind"] == "human" or (p["kind"] == "agent" and p["enabled"])):
                effective.append(p)

        delivery = "delivered"
        if mention.contains_at(content) and not effective:
            delivery = "no_recipients"  # F15：@ 了不存在或未启用的成员

        msg = await db.insert_message(
            meeting_id, author_kind, author_id, author_name, content,
            delivery=delivery,
        )
        await db.add_mentions(
            msg["id"],
            [(names[nm], off, ln) for (nm, off, ln) in parsed if nm in names],
        )
        await bus.broadcast(meeting_id, "message.new", msg)

        if m["status"] == "running":
            await self._schedule_from_message(meeting_id, msg, effective)
        return msg, None

    # ---------- 触发编排（后台） ----------

    async def _schedule_from_message(
        self, meeting_id: str, message: dict, effective: list[dict],
    ) -> None:
        """由一条消息安排下一棒。effective 为可触达成员（人 + 启用的 agent）。"""
        # 熔断 2：单条消息最多触发 3 个
        agents, seen = [], set()
        for p in effective:
            if p["kind"] == "agent" and p["enabled"] and p["id"] not in seen:
                seen.add(p["id"])
                agents.append(p)
        agents = agents[:MAX_PER_MESSAGE]

        # 熔断 3：Agent 的回复不得再次触发自己（连续自点名上限 1）
        if message["author_kind"] == "agent":
            agents = [p for p in agents if p["id"] != message["author_id"]]

        # trigger_mode=always：人类的每条发言都唤醒（不占用 mention 名额之外的量，
        # 同样受总名额约束）
        if message["author_kind"] == "human":
            for p in db.list_participants(meeting_id):
                if (p["kind"] == "agent" and p["enabled"]
                        and p["trigger_mode"] == "always" and p["id"] not in seen):
                    seen.add(p["id"])
                    agents.append(p)
            agents = agents[:MAX_PER_MESSAGE]

        if not agents:
            return

        next_depth = self._depth_of(message) + 1
        if next_depth > MAX_DEPTH:  # 熔断 1
            key = (meeting_id, message["id"])
            if key not in self._cap_announced:
                self._cap_announced.add(key)
                if len(self._cap_announced) > 1000:
                    self._cap_announced.clear()
                    self._cap_announced.add(key)
                await self.system_message(meeting_id, "已达最大接力轮数，停止自动接力。")
            return

        q = self._queue(meeting_id)
        for p in agents:
            q.put_nowait({"participant_id": p["id"], "source_message_id": message["id"],
                          "depth": next_depth})
        self._ensure_worker(meeting_id)

    @staticmethod
    def _depth_of(message: dict) -> int:
        """消息的接力深度：人类消息为 0；Agent 消息记录在内存（生成它的任务深度）。"""
        return int(message.get("_depth") or 0)

    async def _wait_if_paused(self, meeting_id: str) -> None:
        while True:
            m = db.get_meeting(meeting_id)
            if m is None or m["status"] != "paused":
                return
            await asyncio.sleep(0.5)

    async def _worker(self, meeting_id: str) -> None:
        q = self._queue(meeting_id)
        while True:
            task = await q.get()
            try:
                try:
                    await self._wait_if_paused(meeting_id)
                    m = db.get_meeting(meeting_id)
                    if m is None or m["status"] == "stopped":
                        continue  # 已停止：丢弃任务
                    await self._run_trigger(meeting_id, task)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # 任何异常都不能杀死 worker
                    await self.system_message(meeting_id, f"调度异常：{e}")
            finally:
                q.task_done()

    async def _run_trigger(self, meeting_id: str, task: dict) -> None:
        p = db.get_participant(task["participant_id"])
        if not p or p["kind"] != "agent" or not p["enabled"]:
            return
        if p["transport"] != "cli":
            return  # MCP 型永不主动调起（需求 4.1）
        if p["status"] == "busy":  # 需求 B5：忙则跳过
            await self.system_message(meeting_id, f"{p['display_name']} 正忙，已跳过本轮。")
            return

        await db.update_participant(p["id"], status="busy")
        await bus.broadcast(meeting_id, "participant.status",
                            {"id": p["id"], "status": "busy"})

        placeholder = await db.insert_message(
            meeting_id, "agent", p["id"], p["display_name"],
            "··· 正在思考", status="streaming",
        )
        placeholder["_depth"] = task["depth"]
        await bus.broadcast(meeting_id, "message.new", placeholder)

        prompt = self.build_prompt(meeting_id, p)
        try:
            ok, text, err = await runner.run_cli(p, prompt, meeting_id, CLI_TIMEOUT)
        except asyncio.CancelledError:
            await db.update_message(placeholder["id"], status="failed",
                                    error="已停止", content="（已停止）")
            await db.update_participant(p["id"], status="idle")
            raise
        except Exception as e:
            ok, text, err = False, "", f"执行异常：{e}"

        # 停止导致的子进程被杀，对外统一显示"已停止"（需求 5.1.1）
        if not ok and await self.is_stopped(meeting_id):
            ok, text, err = False, "", "已停止"

        # 刹车 1：Hub 代写 CLI 回复，以该 agent 身份过桶（需求 9）
        if ok:
            allowed, retry = brakes.allow(p["id"])
            if not allowed:
                ok, text = False, ""
                err = f"触发过频，已被刹车拦截（retry_after {retry}s）"

        if ok:
            final = await db.update_message(placeholder["id"], status="done",
                                            content=text, error="")
            final["_depth"] = task["depth"]
            # 关键：占位内容不含 @提及；最终回复落库后必须整组刷新 mention 行，
            # 否则 _relay 查不到被点名者，接力链条永远断裂（实测踩坑）。
            names = {r["display_name"]: r["id"]
                     for r in db.list_participants(meeting_id)}
            await db.replace_mentions(
                final["id"],
                [(names[nm], off, ln)
                 for (nm, off, ln) in mention.parse(text, list(names))
                 if nm in names],
            )
            await db.update_participant(p["id"], status="idle", last_seen=db.now_iso())
        else:
            final = await db.update_message(placeholder["id"], status="failed",
                                            content=f"（执行失败：{err}）", error=err)
            final["_depth"] = task["depth"]
            await db.update_participant(p["id"], status="failed", last_seen=db.now_iso())

        await bus.broadcast(meeting_id, "message.updated", final)
        status = db.get_participant(p["id"]) or {}
        await bus.broadcast(meeting_id, "participant.status",
                            {"id": p["id"], "status": status.get("status", "idle")})

        if ok:
            await self._relay(meeting_id, final, task["depth"])

    async def _relay(self, meeting_id: str, reply: dict, depth: int) -> None:
        """解析 Agent 回复中的 @提及，作为新任务入队（需求 5.1 第 7 步 d）。"""
        effective = db.mentioned_enabled_agents(reply["id"])
        await self._schedule_from_message_depth(meeting_id, reply, effective, depth)

    async def _schedule_from_message_depth(
        self, meeting_id: str, message: dict, effective: list[dict], depth: int,
    ) -> None:
        # 与 _schedule_from_message 相同的容量/自点名约束，但深度由调用方给定
        agents, seen = [], set()
        for p in effective:
            if p["id"] not in seen:
                seen.add(p["id"])
                agents.append(p)
        agents = [p for p in agents if p["id"] != message["author_id"]]
        agents = agents[:MAX_PER_MESSAGE]
        if not agents:
            return
        next_depth = depth + 1
        if next_depth > MAX_DEPTH:
            key = (meeting_id, message["id"])
            if key not in self._cap_announced:
                self._cap_announced.add(key)
                await self.system_message(meeting_id, "已达最大接力轮数，停止自动接力。")
            return
        q = self._queue(meeting_id)
        for p in agents:
            q.put_nowait({"participant_id": p["id"], "source_message_id": message["id"],
                          "depth": next_depth})
        self._ensure_worker(meeting_id)

    # ---------- prompt 组装（需求 4.6） ----------

    def build_prompt(self, meeting_id: str, participant: dict) -> str:
        rows = db.q(
            """SELECT author_name, content FROM message
               WHERE meeting_id=? AND status='done' ORDER BY seq DESC LIMIT ?""",
            (meeting_id, CTX_MAX_MSGS),
        )
        rows = list(reversed(rows))
        lines = [f"[{r['author_name']}]：{r['content']}" for r in rows]
        text = "\n".join(lines)
        if len(text) > CTX_MAX_CHARS and len(rows) > CTX_KEEP_HEAD + CTX_KEEP_TAIL:
            head, tail = rows[:CTX_KEEP_HEAD], rows[-CTX_KEEP_TAIL:]
            lines = ([f"[{r['author_name']}]：{r['content']}" for r in head]
                     + ["……（中间内容已省略）……"]
                     + [f"[{r['author_name']}]：{r['content']}" for r in tail])
            text = "\n".join(lines)

        parts = []
        sp = (participant.get("system_prompt") or "").strip()
        if sp:
            parts.append(sp)
        parts.append(
            "以下是圆桌会议记录（按时间顺序）：\n---\n" + text + "\n---\n\n"
            "现在轮到你发言。请直接给出观点，不要复述上文。\n"
            "如需点名其他成员，使用 @显示名 格式。\n"
            "若遇到只有人类能拍板的决策，不要猜，直接说明你的建议并等待。"
        )
        return "\n\n".join(parts)

    # ---------- 系统消息 / 表单回灌 ----------

    async def system_message(self, meeting_id: str, text: str) -> dict:
        msg = await db.insert_message(meeting_id, "system", None, "系统", text)
        await bus.broadcast(meeting_id, "message.new", msg)
        return msg

    async def answer_form(self, form_id: str, answer: dict | None,
                          cancel: bool) -> tuple[dict | None, dict | None]:
        """表单作答：答案作为普通消息回灌会议（需求 F10）。返回 (form, error)。"""
        form = db.get_form(form_id)
        if form is None:
            return None, {"ok": False, "reason": "no_form"}
        if form["status"] != "pending":
            return None, {"ok": False, "reason": "already_closed"}
        mid = form["meeting_id"]
        human = db.human_of(mid)
        human_name = human["display_name"] if human else "我"

        if cancel:
            form = await db.update_form(form_id, status="cancelled",
                                        answered_at=db.now_iso())
            text = f"【表单已取消】{form['title']}"
            msg, _err = await self.post_message(mid, author_kind="human",
                                                author_id=human["id"] if human else None,
                                                author_name=human_name, content=text)
            await bus.broadcast(mid, "form.answered", form)
            return form, None

        # 先发回灌消息（占用作者令牌桶）；失败则表单保持 pending，可稍后重答
        fields = json.loads(form["fields"] or "[]")
        label_by_key = {f.get("key"): f.get("label") or f.get("key") for f in fields}
        lines = [f"【表单回答】{form['title']}"]
        for k, v in (answer or {}).items():
            vv = "、".join(v) if isinstance(v, list) else str(v)
            lines.append(f"- {label_by_key.get(k, k)}：{vv}")
        msg, err = await self.post_message(mid, author_kind="human",
                                           author_id=human["id"] if human else None,
                                           author_name=human_name, content="\n".join(lines))
        if err is not None:
            return None, err
        form = await db.update_form(form_id, status="answered",
                                    answer=json.dumps(answer or {}, ensure_ascii=False),
                                    answered_at=db.now_iso())
        await bus.broadcast(mid, "form.answered", form)
        return form, None

    # ---------- 主席控制（需求 F14 / 9，优先于一切刹车） ----------

    async def _set_status(self, meeting_id: str, status: str) -> None:
        await db.execute("UPDATE meeting SET status=? WHERE id=?", (status, meeting_id))
        if status == "stopped":
            await runner.kill_meeting(meeting_id)  # 需求 5.1.1：终止整棵进程树
        await bus.broadcast(meeting_id, "control.state", await self.control_state(meeting_id))

    async def control_state(self, meeting_id: str) -> dict:
        m = db.get_meeting(meeting_id)
        st = m["status"] if m else "stopped"
        return {"meeting_id": meeting_id, "paused": st == "paused",
                "stopped": st == "stopped"}

    async def pause(self, paused: bool, meeting_id: str | None = None) -> None:
        targets = [meeting_id] if meeting_id else [m["id"] for m in db.list_meetings()]
        for mid in targets:
            m = db.get_meeting(mid)
            if m is None:
                continue
            # 只做合法迁移：running<->paused；停止中的会议不因"继续"复活（用重置恢复）
            if paused and m["status"] == "running":
                await self._set_status(mid, "paused")
            elif not paused and m["status"] == "paused":
                await self._set_status(mid, "running")

    async def stop(self, meeting_id: str | None = None) -> None:
        targets = [meeting_id] if meeting_id else [m["id"] for m in db.list_meetings()]
        for mid in targets:
            await self._set_status(mid, "stopped")
        await db.set_app_state("stopped", "1")  # 需求 2 节全局镜像

    async def reset(self, meeting_id: str | None = None) -> None:
        targets = [meeting_id] if meeting_id else [m["id"] for m in db.list_meetings()]
        for mid in targets:
            await self._set_status(mid, "running")
        stopped = [m for m in db.list_meetings() if m["status"] == "stopped"]
        if not stopped:
            await db.set_app_state("stopped", "0")

    async def is_stopped(self, meeting_id: str) -> bool:
        m = db.get_meeting(meeting_id)
        return bool(m and m["status"] == "stopped")


orch = Orchestrator()
