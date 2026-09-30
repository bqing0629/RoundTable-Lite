"""FastAPI 入口（需求 5 / C7）：REST + WebSocket + MCP(/mcp) + 静态前端，同端口。

lifespan 接线（需求 4.4 坑 2）：被 mount 的 MCP 子应用 lifespan 不会被执行，
必须在主应用 lifespan 中显式进入 mcp.session_manager.run() 上下文。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from . import db, runner
from .mcp_server import mcp
from .models import (AnswerIn, ControlIn, MeetingIn, MessageIn, ParticipantIn,
                     ParticipantPatch)
from .orchestrator import orch
from .presets import presets
from .ws import bus

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(application: FastAPI):
    db.connect()
    try:
        # 注意：session_manager 是属性（StreamableHTTPSessionManager 实例），
        # 进入其 run() 上下文；写成 session_manager() 会报 "object is not callable"。
        async with mcp.session_manager.run():
            yield
    finally:
        await orch.shutdown()  # 取消 worker + 终止全部子进程树


app = FastAPI(title="RoundTable Lite", version="2.1", lifespan=lifespan)


# ---------- 健康检查 / 预设 ----------

@app.get("/api/health")
async def health():
    return {"ok": True, "app": "roundtable-lite", "version": "2.1"}


@app.get("/api/presets")
async def api_presets():
    return {"ok": True, "presets": presets()}


# ---------- 会议 ----------

@app.get("/api/meetings")
async def meetings_list():
    return {"ok": True, "meetings": db.list_meetings()}


@app.post("/api/meetings")
async def meetings_create(body: MeetingIn):
    m = await db.create_meeting(body.name.strip(), body.description.strip())
    return {"ok": True, "meeting": m}


@app.get("/api/meetings/{mid}")
async def meeting_detail(mid: str):
    m = db.get_meeting(mid)
    if m is None:
        raise HTTPException(404, "会议不存在")
    return {"ok": True, "meeting": m, "participants": db.list_participants(mid)}


@app.delete("/api/meetings/{mid}")
async def meeting_delete(mid: str):
    if db.get_meeting(mid) is None:
        raise HTTPException(404, "会议不存在")
    orch.forget_meeting(mid)
    await runner.kill_meeting(mid)          # 需求 5.1.1：先杀干净进程树
    await db.delete_meeting_cascade(mid)
    await bus.broadcast(mid, "meeting.deleted", {"meeting_id": mid})
    return {"ok": True}


# ---------- 消息 ----------

@app.get("/api/meetings/{mid}/messages")
async def messages_list(mid: str, after_seq: int = 0, limit: int = 1000):
    if db.get_meeting(mid) is None:
        raise HTTPException(404, "会议不存在")
    limit = max(1, min(limit, 1000))
    msgs = db.messages_after(mid, after_seq, limit)
    mentions = db.mentions_of([m["id"] for m in msgs])
    for m in msgs:
        m["mentions"] = mentions.get(m["id"], [])
    return {"ok": True, "messages": msgs, "max_seq": db.max_seq(mid)}


@app.post("/api/meetings/{mid}/messages")
async def message_post(mid: str, body: MessageIn):
    if db.get_meeting(mid) is None:
        raise HTTPException(404, "会议不存在")
    author_id = body.author_id
    if author_id:
        p = db.get_participant(author_id)
        if p is None or p["meeting_id"] != mid:
            raise HTTPException(400, "author_id 不是该会议成员")
        author_kind, author_name = p["kind"], p["display_name"]
    else:  # 需求 5.1 第 1 步：author_id 为空 → 视为"我"
        human = db.human_of(mid)
        if human is None:
            raise HTTPException(500, "会议缺少人类参与者")
        author_id, author_kind, author_name = human["id"], "human", human["display_name"]

    msg, err = await orch.post_message(
        mid, author_kind=author_kind, author_id=author_id,
        author_name=author_name, content=body.content,
    )
    if err is not None:
        status = {"stopped": 409, "rate_limited": 429}.get(err["reason"], 400)
        raise HTTPException(status, detail=err)
    assert msg is not None
    return {"ok": True, "message": msg}


# ---------- 参与者 ----------

@app.get("/api/meetings/{mid}/participants")
async def participants_list(mid: str):
    if db.get_meeting(mid) is None:
        raise HTTPException(404, "会议不存在")
    return {"ok": True, "participants": db.list_participants(mid)}


@app.post("/api/meetings/{mid}/participants")
async def participants_add(mid: str, body: ParticipantIn):
    if db.get_meeting(mid) is None:
        raise HTTPException(404, "会议不存在")
    if body.transport == "cli" and not body.cli_cmd.strip():
        raise HTTPException(400, "CLI 型参与者必须填 cli_cmd")
    if db.get_participant_by_name(mid, body.display_name) is not None:
        raise HTTPException(409, f"显示名 {body.display_name} 已存在")
    p = await db.create_participant(mid, body.model_dump())
    await orch.system_message(mid, f"{p['display_name']} 已加入会议")
    await bus.broadcast(mid, "participant.new", p)
    return {"ok": True, "participant": p}


@app.patch("/api/meetings/{mid}/participants/{pid}")
async def participants_patch(mid: str, pid: str, body: ParticipantPatch):
    p = db.get_participant(pid)
    if p is None or p["meeting_id"] != mid:
        raise HTTPException(404, "参与者不存在")
    data = body.to_db()
    if "display_name" in data and data["display_name"] != p["display_name"]:
        if db.get_participant_by_name(mid, data["display_name"]) is not None:
            raise HTTPException(409, f"显示名 {data['display_name']} 已存在")
    p2 = await db.update_participant(pid, **data)
    assert p2 is not None
    status = p2["status"] if p2["enabled"] else "offline"
    await bus.broadcast(mid, "participant.status", {"id": pid, "status": status})
    return {"ok": True, "participant": p2}


@app.delete("/api/meetings/{mid}/participants/{pid}")
async def participants_kick(mid: str, pid: str):
    p = db.get_participant(pid)
    if p is None or p["meeting_id"] != mid:
        raise HTTPException(404, "参与者不存在")
    if p["kind"] == "human":
        raise HTTPException(400, "不能移除人类主席")
    await runner.kill_meeting(mid)  # 该会运行的子进程一并终止
    await db.delete_participant(pid)
    await orch.system_message(mid, f"{p['display_name']} 已被移出会议")
    await bus.broadcast(mid, "participant.deleted", {"id": pid})
    return {"ok": True}


# ---------- 表单（ask_operator 的回答端） ----------

@app.get("/api/meetings/{mid}/forms")
async def forms_list(mid: str):
    if db.get_meeting(mid) is None:
        raise HTTPException(404, "会议不存在")
    return {"ok": True, "forms": db.list_forms(mid)}


@app.post("/api/meetings/{mid}/forms/{fid}/answer")
async def form_answer(mid: str, fid: str, body: AnswerIn):
    form = db.get_form(fid)
    if form is None or form["meeting_id"] != mid:
        raise HTTPException(404, "表单不存在")
    form2, err = await orch.answer_form(fid, body.answer, body.cancel)
    if err is not None:
        code = 429 if (isinstance(err, dict) and err.get("reason") == "rate_limited") else 409
        raise HTTPException(code, detail=err)
    assert form2 is not None
    return {"ok": True, "form": form2}


# ---------- 主席控制（需求 F14：优先于一切刹车） ----------

@app.post("/api/control/pause")
async def control_pause(body: ControlIn):
    if body.paused is None:
        raise HTTPException(400, "缺少 paused")
    await orch.pause(body.paused, body.meeting_id)
    return {"ok": True}


@app.post("/api/control/stop")
async def control_stop(body: ControlIn):
    await orch.stop(body.meeting_id)
    return {"ok": True}


@app.post("/api/control/reset")
async def control_reset(body: ControlIn):
    await orch.reset(body.meeting_id)
    return {"ok": True}


# ---------- WebSocket（需求 5.2 / F13） ----------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    meeting_id = ws.query_params.get("meeting_id") or ""
    await ws.accept()
    if db.get_meeting(meeting_id) is None:
        await ws.send_json({"event": "error", "data": {"reason": "no_meeting"}})
        await ws.close()
        return
    await bus.join(meeting_id, ws)
    try:
        await ws.send_json({"event": "control.state",
                            "data": await orch.control_state(meeting_id)})
        while True:
            # 客户端仅保活/ack；一切数据靠服务端推送 + REST 补齐
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await bus.leave(meeting_id, ws)


# ---------- 前端静态（显式路由；不能用 StaticFiles 挂 "/"，原因见底部） ----------

@app.get("/", include_in_schema=False)
async def web_index():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


@app.get("/app.js", include_in_schema=False)
async def web_app_js():
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@app.get("/style.css", include_in_schema=False)
async def web_style():
    return FileResponse(WEB_DIR / "style.css", media_type="text/css")


# ---------- MCP 挂载（官方推荐形态：子应用内部路由 /mcp，挂到 "/" 兜底） ----------
# 实测两个事实：① Starlette 的 Mount("/mcp") 不匹配裸路径 POST /mcp（405）；
# ② Mount 一旦匹配不再回落，StaticFiles 挂 "/" 会与子应用互相吃掉流量。
# 故：前端走上面的显式路由，MCP 子应用最后挂 "/" 收尾。
app.mount("/", mcp.streamable_http_app())
