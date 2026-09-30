"""MCP Server（需求 4.4，P0 共 8 个工具）。

挂载要点（v2.1 实测定案，详见需求文档 4.4 三个坑）：
- FastMCP 保持默认内部路由 /mcp；子应用挂到 "/"（官方推荐形态）。
  不能 Mount("/mcp")：Starlette 的 Mount 正则要求斜杠后必须有内容，
  裸 POST /mcp 根本进不了子应用（405）。
- lifespan 由 main.py 显式 `async with mcp.session_manager.run():` 接线
  （session_manager 是属性不是方法），否则 /mcp 报 "Task group not initialized"。
- 依赖固定 mcp>=1.8,<2（2.x 已把 FastMCP 改名 MCPServer）。

语义与 REST 共用同一套 db / orchestrator / brakes（需求 5.3）。
所有拒绝一律结构化返回（{ok:false, reason,...}），不抛异常、不用 HTTP 423。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import db
from .orchestrator import orch
from .protocol import PROTOCOL_TEXT
from .ws import bus

mcp = FastMCP("roundtable")  # 内部路由默认 /mcp；由 main.py 挂载到 "/"

# 会话注册表：name -> {meeting_id, participant_id, display_name, session_id}
_joined: dict[str, dict] = {}
# 收听游标：name -> 已投递到的 seq
_cursors: dict[str, int] = {}

FIELD_TYPES = {"radio", "checkbox", "text", "textarea"}


# ---------- 内部工具 ----------

def _session_id(ctx: Context | None) -> str | None:
    try:
        return str(ctx.request_context.session.session_id)  # type: ignore[union-attr]
    except Exception:
        return None


def _client_name(ctx: Context | None) -> str | None:
    try:
        info = ctx.request_context.session.client_params  # type: ignore[union-attr]
        name = getattr(getattr(info, "clientInfo", None), "name", None)
        return str(name) if name else None
    except Exception:
        return None


def _active_meeting(meeting_id: str | None = None) -> dict | None:
    if meeting_id:
        m = db.get_meeting(meeting_id)
        if m:
            return m
    for m in db.list_meetings():  # 最近创建且未停止
        if m["status"] != "stopped":
            return m
    return db.list_meetings()[0] if db.list_meetings() else None


def _resolve(ctx: Context | None, name: str | None) -> dict | None:
    """按 name -> 当前会话 -> 唯一会话 的顺序解析已 join 的身份。"""
    if name and name in _joined:
        return _joined[name]
    sid = _session_id(ctx)
    if sid:
        for rec in _joined.values():
            if rec.get("session_id") == sid:
                return rec
    if name is None and len(_joined) == 1:
        return next(iter(_joined.values()))
    return None


def _err(reason: str, **extra: Any) -> dict:
    d = {"ok": False, "reason": reason}
    d.update(extra)
    return d


# ---------- 工具定义（docstring 一律 ≤ 260 字符，需求 4.4） ----------

# 注：ctx 参数用裸 `Context` 注解（而非 `Context | None`）——
# FastMCP 靠注解类型识别上下文并从工具 schema 中排除，Union 注解在部分版本不生效。
@mcp.tool()
async def join(name: str | None = None, meeting_id: str | None = None,
               ctx: Context = None) -> dict:
    """加入圆桌会议并取回行为协议；say/listen/ask_operator 之前必须调用。
    name 不传则用 MCP 客户端握手名（取不到用 mcp-agent）；撞名返回 name_in_use，
    需显式传 name。默认进入最近创建且未停止的会议，可用 meeting_id 指定。"""
    meeting = _active_meeting(meeting_id)
    if meeting is None:
        return _err("no_meeting", hint="先在控制台创建会议")
    mid = meeting["id"]

    display = (name or _client_name(ctx) or "mcp-agent").strip()
    if not display or any(ch.isspace() for ch in display):
        return _err("bad_name", hint="name 不能为空或含空格")

    existing = db.get_participant_by_name(mid, display)
    if existing is not None and existing["transport"] != "mcp":
        return _err("name_in_use", hint=f"{display} 已被 {existing['transport']} 型参与者占用，请显式传 name")

    rec = _joined.get(display)
    if rec is not None and rec["meeting_id"] == mid:
        sid = _session_id(ctx)
        if sid and rec.get("session_id") != sid:
            return _err("name_in_use", hint="该名字已被另一会话占用，请显式传 name")

    if existing is not None:  # Hub 重启后的重连：复用参与者行
        pid = existing["id"]
        await db.update_participant(pid, enabled=1, status="idle")
    else:
        p = await db.create_participant(mid, {
            "transport": "mcp", "display_name": display,
            "trigger_mode": "mention",
        })
        pid = p["id"]

    _joined[display] = {"meeting_id": mid, "participant_id": pid,
                        "display_name": display, "session_id": _session_id(ctx)}
    _cursors[display] = db.max_seq(mid)

    msg = await orch.system_message(mid, f"{display} 已加入会议")
    await bus.broadcast(mid, "participant.status", {"id": pid, "status": "idle"})

    m = db.get_meeting(mid) or {}
    return {"ok": True, "participant_id": pid, "display_name": display,
            "meeting": {"id": mid, "name": m.get("name"), "status": m.get("status")},
            "protocol": PROTOCOL_TEXT, "_system_seq": msg["seq"]}


@mcp.tool()
async def leave(name: str | None = None, ctx: Context = None) -> dict:
    """离开房间，停止收发。身份解析同 say。"""
    rec = _resolve(ctx, name)
    if rec is None:
        return _err("not_joined")
    await db.update_participant(rec["participant_id"], status="offline")
    await bus.broadcast(rec["meeting_id"], "participant.status",
                        {"id": rec["participant_id"], "status": "offline"})
    await orch.system_message(rec["meeting_id"], f"{rec['display_name']} 已离开会议")
    _joined.pop(rec["display_name"], None)
    _cursors.pop(rec["display_name"], None)
    return {"ok": True}


@mcp.tool()
async def whoami(name: str | None = None, ctx: Context = None) -> dict:
    """报告身份与加入状态。join 前也可用。"""
    rec = _resolve(ctx, name)
    if rec is None:
        return {"ok": True, "joined": False,
                "hint": "先调用 join(name) 加入会议"}
    m = db.get_meeting(rec["meeting_id"]) or {}
    return {"ok": True, "joined": True, "display_name": rec["display_name"],
            "participant_id": rec["participant_id"],
            "meeting": {"id": m.get("id"), "name": m.get("name"), "status": m.get("status")}}


@mcp.tool()
async def list_peers(name: str | None = None, ctx: Context = None) -> dict:
    """列出当前会议的参与者（名称/类型/状态/启用）。join 前也可用。"""
    rec = _resolve(ctx, name)
    mid = rec["meeting_id"] if rec else (_active_meeting() or {}).get("id")
    if not mid:
        return _err("no_meeting")
    peers = [{"display_name": p["display_name"], "kind": p["kind"],
              "transport": p["transport"], "status": p["status"],
              "enabled": bool(p["enabled"])}
             for p in db.list_participants(mid)]
    return {"ok": True, "meeting_id": mid, "peers": peers}


@mcp.tool()
async def say(content: str, name: str | None = None,
              ctx: Context = None) -> dict:
    """发言（广播全员）。content 中可写 @显示名 点名其他成员（会立即唤醒对方）。
    被拒时返回 {ok:false, reason, retry_after?}，不抛异常。"""
    rec = _resolve(ctx, name)
    if rec is None:
        return _err("not_joined", hint="先调用 join(name)")
    content = (content or "").strip()
    if not content:
        return _err("empty_content")
    msg, err = await orch.post_message(
        rec["meeting_id"], author_kind="agent",
        author_id=rec["participant_id"], author_name=rec["display_name"],
        content=content,
    )
    if err is not None:
        return err
    assert msg is not None
    return {"ok": True, "seq": msg["seq"], "message_id": msg["id"],
            "delivery": msg["delivery"]}


@mcp.tool()
async def listen(timeout: int = 20, name: str | None = None,
                 ctx: Context = None) -> dict:
    """长轮询收取新消息（不含自己发的，streaming 占位不投递、只投最终稿）。
    无消息时等待至多 timeout 秒；会议停止时立即返回控制通知。
    返回 {messages, meeting_status, timeout}；循环调用。"""
    rec = _resolve(ctx, name)
    if rec is None:
        return _err("not_joined", hint="先调用 join(name)")
    mid = rec["meeting_id"]
    my_pid = rec["participant_id"]
    timeout = max(1, min(int(timeout), 60))
    loop_cap = 30

    def _poll() -> list[dict]:
        rows = db.q(
            """SELECT * FROM message WHERE meeting_id=? AND seq>?
               AND (author_id IS NULL OR author_id<>?)
               AND status<>'streaming'
               ORDER BY seq LIMIT ?""",
            (mid, _cursors.get(rec["display_name"], 0), my_pid, loop_cap),
        )
        return [{"seq": r["seq"], "author_kind": r["author_kind"],
                 "author_id": r["author_id"], "author_name": r["author_name"],
                 "content": r["content"], "status": r["status"],
                 "delivery": r["delivery"], "created_at": r["created_at"]}
                for r in rows]

    waited = 0.0
    while True:
        m = db.get_meeting(mid)
        st = m["status"] if m else "stopped"
        if st == "stopped":
            _cursors[rec["display_name"]] = db.max_seq(mid)
            return {"ok": True, "meeting_status": st, "messages": [],
                    "control": "stopped",
                    "hint": "会议已停止：停止发言并结束循环，不要试图恢复"}
        msgs = _poll()
        if msgs:
            _cursors[rec["display_name"]] = msgs[-1]["seq"]
            return {"ok": True, "meeting_status": st, "messages": msgs,
                    "timeout": False}
        if waited >= timeout:
            return {"ok": True, "meeting_status": st, "messages": [],
                    "timeout": True}
        await asyncio.sleep(0.5)
        waited += 0.5


@mcp.tool()
async def ask_operator(title: str, fields: list[dict[str, Any]],
                       name: str | None = None,
                       ctx: Context = None) -> dict:
    """向人类主席"我"推一个表单并等待回答（答案稍后经 listen 回来）。
    fields: [{key,type,label,required,options}]，type ∈ radio/checkbox/text/textarea。
    返回 form_id。只有人类能拍板的事才用，不要用于闲聊。"""
    rec = _resolve(ctx, name)
    if rec is None:
        return _err("not_joined", hint="先调用 join(name)")
    title = (title or "").strip()
    if not title:
        return _err("bad_title")
    norm: list[dict] = []
    seen_keys: set[str] = set()
    for f in fields or []:
        if not isinstance(f, dict):
            return _err("bad_fields", hint="fields 必须是对象数组")
        key = str(f.get("key") or "").strip()
        ftype = str(f.get("type") or "text").strip()
        if not key or key in seen_keys:
            return _err("bad_fields", hint="每个 field 需要唯一的 key")
        if ftype not in FIELD_TYPES:
            return _err("bad_fields", hint=f"type 只支持 {sorted(FIELD_TYPES)}")
        seen_keys.add(key)
        item = {"key": key, "type": ftype,
                "label": str(f.get("label") or key),
                "required": bool(f.get("required", False))}
        if ftype in ("radio", "checkbox"):
            opts = [str(o) for o in (f.get("options") or [])]
            if not opts:
                return _err("bad_fields", hint=f"{ftype} 需要 options")
            item["options"] = opts
        norm.append(item)

    form = await db.create_form(rec["meeting_id"], rec["participant_id"],
                                rec["display_name"], title,
                                json.dumps(norm, ensure_ascii=False))
    await bus.broadcast(rec["meeting_id"], "form.pending", form)
    return {"ok": True, "form_id": form["id"],
            "hint": "回答会以普通消息出现在会议流里，用 listen() 收取"}


@mcp.tool()
async def list_forms(name: str | None = None, ctx: Context = None) -> dict:
    """列出本会议中待回答（pending）的表单。join 前也可用（按当前活跃会议）。"""
    rec = _resolve(ctx, name)
    mid = rec["meeting_id"] if rec else (_active_meeting() or {}).get("id")
    if not mid:
        return _err("no_meeting")
    forms = [{"id": f["id"], "title": f["title"], "author_name": f["author_name"],
              "created_at": f["created_at"]}
             for f in db.list_forms(mid, status="pending")]
    return {"ok": True, "forms": forms}
