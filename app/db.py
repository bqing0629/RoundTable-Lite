"""SQLite 访问层。

需求 2 节并发规约（v2.1）：
- WAL + busy_timeout=5000 + check_same_thread=False
- 单写者：所有写操作经进程内同一把 asyncio.Lock 串行执行
- seq 与消息插入在同一条 INSERT ... SELECT 内原子分配，撞唯一索引重试 <=3 次
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "roundtable.db"

_conn: sqlite3.Connection | None = None
_wlock: asyncio.Lock | None = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def writer_lock() -> asyncio.Lock:
    global _wlock
    if _wlock is None:
        _wlock = asyncio.Lock()
    return _wlock


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        )
        conn.commit()
        _conn = conn
    return _conn


# ---------- 基础读写（读不走锁：WAL 下读写不阻塞） ----------

def q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def q1(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


async def execute(sql: str, params: tuple = ()) -> None:
    async with writer_lock():
        conn = connect()
        conn.execute(sql, params)
        conn.commit()


def rowd(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else {k: row[k] for k in row.keys()}


def rowsd(rows: list[sqlite3.Row]) -> list[dict]:
    return [rowd(r) for r in rows]


# ---------- 会议 ----------

def get_meeting(mid: str) -> dict | None:
    return rowd(q1("SELECT * FROM meeting WHERE id=?", (mid,)))


def list_meetings() -> list[dict]:
    return rowsd(q(
        """SELECT m.*,
                  (SELECT COUNT(*) FROM participant p WHERE p.meeting_id = m.id) AS participant_count,
                  (SELECT COUNT(*) FROM message g    WHERE g.meeting_id    = m.id) AS message_count
           FROM meeting m ORDER BY m.created_at DESC, m.id DESC"""
    ))


async def create_meeting(name: str, description: str = "") -> dict:
    mid = new_id()
    await execute(
        "INSERT INTO meeting (id, name, description, status, created_at) VALUES (?,?,?,'running',?)",
        (mid, name, description, now_iso()),
    )
    # F1：自动创建唯一用户"我"
    await execute(
        "INSERT INTO participant (id, meeting_id, kind, transport, display_name, joined_at)"
        " VALUES (?,?,'human','human','我',?)",
        (new_id(), mid, now_iso()),
    )
    return get_meeting(mid)


async def delete_meeting_cascade(meeting_id: str) -> None:
    """级联删除：消息 -> mention、参与者、表单、会议（F 删除需先终止子进程，见 main.py）。"""
    async with writer_lock():
        conn = connect()
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM message WHERE meeting_id=?", (meeting_id,)).fetchall()]
        if ids:
            ph = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM mention WHERE message_id IN ({ph})", ids)
        for table in ("message", "participant", "form"):
            conn.execute(f"DELETE FROM {table} WHERE meeting_id=?", (meeting_id,))
        conn.execute("DELETE FROM meeting WHERE id=?", (meeting_id,))
        conn.commit()


# ---------- 参与者 ----------

def get_participant(pid: str) -> dict | None:
    return rowd(q1("SELECT * FROM participant WHERE id=?", (pid,)))


def get_participant_by_name(meeting_id: str, display_name: str) -> dict | None:
    return rowd(q1("SELECT * FROM participant WHERE meeting_id=? AND display_name=?",
                   (meeting_id, display_name)))


def list_participants(meeting_id: str) -> list[dict]:
    return rowsd(q("SELECT * FROM participant WHERE meeting_id=? ORDER BY joined_at, id",
                   (meeting_id,)))


def human_of(meeting_id: str) -> dict | None:
    return rowd(q1("SELECT * FROM participant WHERE meeting_id=? AND kind='human' LIMIT 1",
                   (meeting_id,)))


async def create_participant(meeting_id: str, d: dict) -> dict:
    pid = new_id()
    await execute(
        """INSERT INTO participant
           (id, meeting_id, kind, transport, display_name, cli_cmd, cli_args,
            model, system_prompt, trigger_mode, enabled, joined_at)
           VALUES (?,?,'agent',?,?,?,?,?,?,?,? ,?)""",
        (pid, meeting_id, d["transport"], d["display_name"], d.get("cli_cmd", ""),
         d.get("cli_args", "[]"), d.get("model", ""), d.get("system_prompt", ""),
         d.get("trigger_mode", "mention"), 1 if d.get("enabled", True) else 0, now_iso()),
    )
    return get_participant(pid)


async def update_participant(pid: str, **fields: Any) -> dict | None:
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        await execute(f"UPDATE participant SET {sets} WHERE id=?", (*fields.values(), pid))
    return get_participant(pid)


async def delete_participant(pid: str) -> None:
    await execute("DELETE FROM participant WHERE id=?", (pid,))


def name_to_id_map(meeting_id: str) -> dict[str, str]:
    return {r["display_name"]: r["id"]
            for r in q("SELECT id, display_name FROM participant WHERE meeting_id=?",
                       (meeting_id,))}


# ---------- 消息 ----------

async def insert_message(
    meeting_id: str, author_kind: str, author_id: str | None, author_name: str,
    content: str, status: str = "done", delivery: str = "delivered", error: str = "",
) -> dict:
    """seq 在同一条 INSERT 内原子分配；撞唯一索引则重试（需求 2 节规约 3）。"""
    last_err: Exception | None = None
    for attempt in range(3):
        mid = new_id()
        try:
            async with writer_lock():
                conn = connect()
                conn.execute(
                    """INSERT INTO message
                       (id, meeting_id, seq, author_kind, author_id, author_name,
                        content, status, error, delivery, created_at)
                       SELECT ?, ?, COALESCE(MAX(seq),0)+1, ?, ?, ?, ?, ?, ?, ?, ?
                       FROM message WHERE meeting_id = ?""",
                    (mid, meeting_id, author_kind, author_id, author_name,
                     content, status, error, delivery, now_iso(), meeting_id),
                )
                conn.commit()
            row = rowd(q1("SELECT * FROM message WHERE id=?", (mid,)))
            assert row is not None
            return row
        except sqlite3.IntegrityError as e:  # 并发撞 seq：重读重试
            last_err = e
    raise last_err  # type: ignore[misc]


async def update_message(message_id: str, **fields: Any) -> dict | None:
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        await execute(f"UPDATE message SET {sets} WHERE id=?", (*fields.values(), message_id))
    return rowd(q1("SELECT * FROM message WHERE id=?", (message_id,)))


def messages_after(meeting_id: str, after_seq: int = 0, limit: int = 1000) -> list[dict]:
    return rowsd(q(
        "SELECT * FROM message WHERE meeting_id=? AND seq>? ORDER BY seq LIMIT ?",
        (meeting_id, after_seq, limit),
    ))


def max_seq(meeting_id: str) -> int:
    r = q1("SELECT COALESCE(MAX(seq),0) AS s FROM message WHERE meeting_id=?", (meeting_id,))
    return int(r["s"])


# ---------- mention ----------

async def add_mentions(message_id: str, pairs: list[tuple[str, int, int]]) -> None:
    """pairs: [(participant_id, start_offset, length)]"""
    if not pairs:
        return
    async with writer_lock():
        conn = connect()
        conn.executemany(
            "INSERT INTO mention (message_id, participant_id, start_offset, length) VALUES (?,?,?,?)",
            [(message_id, p, s, l) for (p, s, l) in pairs],
        )
        conn.commit()


async def replace_mentions(message_id: str, pairs: list[tuple[str, int, int]]) -> None:
    """整组刷新某消息的 mention 行。

    Agent 消息先以占位内容（无提及）落库，最终回复替换内容后必须调用本函数
    重新解析，否则 mention 表与正文不一致，接力链条会断。
    """
    async with writer_lock():
        conn = connect()
        conn.execute("DELETE FROM mention WHERE message_id=?", (message_id,))
        conn.executemany(
            "INSERT INTO mention (message_id, participant_id, start_offset, length) VALUES (?,?,?,?)",
            [(message_id, p, s, l) for (p, s, l) in pairs],
        )
        conn.commit()


def mentions_of(message_ids: list[str]) -> dict[str, list[dict]]:
    if not message_ids:
        return {}
    ph = ",".join("?" * len(message_ids))
    out: dict[str, list[dict]] = {}
    for r in q(f"SELECT * FROM mention WHERE message_id IN ({ph}) ORDER BY id",
               tuple(message_ids)):
        out.setdefault(r["message_id"], []).append({
            "participant_id": r["participant_id"],
            "start_offset": r["start_offset"],
            "length": r["length"],
        })
    return out


def mentioned_enabled_agents(message_id: str) -> list[dict]:
    """该消息命中且启用中的 agent，按提及顺序（触发用，需求 5.1 第 6/7 步）。"""
    return rowsd(q(
        """SELECT p.* FROM mention m JOIN participant p ON p.id = m.participant_id
           WHERE m.message_id=? AND p.kind='agent' AND p.enabled=1
           ORDER BY m.id""",
        (message_id,),
    ))


# ---------- 表单（ask_operator） ----------

async def create_form(meeting_id: str, author_id: str | None, author_name: str,
                      title: str, fields_json: str) -> dict:
    fid = new_id()
    await execute(
        "INSERT INTO form (id, meeting_id, author_id, author_name, title, fields, status, created_at)"
        " VALUES (?,?,?,?,?,?,'pending',?)",
        (fid, meeting_id, author_id, author_name, title, fields_json, now_iso()),
    )
    return get_form(fid)


def get_form(fid: str) -> dict | None:
    return rowd(q1("SELECT * FROM form WHERE id=?", (fid,)))


def list_forms(meeting_id: str, status: str | None = None) -> list[dict]:
    if status:
        return rowsd(q("SELECT * FROM form WHERE meeting_id=? AND status=? ORDER BY created_at, id",
                       (meeting_id, status)))
    return rowsd(q("SELECT * FROM form WHERE meeting_id=? ORDER BY created_at, id", (meeting_id,)))


async def update_form(fid: str, **fields: Any) -> dict | None:
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        await execute(f"UPDATE form SET {sets} WHERE id=?", (*fields.values(), fid))
    return get_form(fid)


# ---------- app_state ----------

async def set_app_state(k: str, v: str) -> None:
    await execute(
        "INSERT INTO app_state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, v),
    )


def get_app_state(k: str, default: str | None = None) -> str | None:
    r = q1("SELECT v FROM app_state WHERE k=?", (k,))
    return r["v"] if r else default
