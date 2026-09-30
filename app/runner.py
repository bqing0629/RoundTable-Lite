"""CLI 子进程调度器（需求 4.1 / 5.1.1）。

- fork 子进程执行命令模板，{outfile} 占位符替换为临时文件，utf-8 读回；
- 含 "-" 占位符时 prompt 走 stdin；两者皆无时 prompt 追加为最后一个参数；
- 超时（默认 300s）与"停止"均按**进程树终止**（Windows: taskkill /T /F）；
- 维护 meeting_id -> 运行中进程 的注册表，供急停 / 踢出 / 删除会议使用。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

_procs: dict[str, set[asyncio.subprocess.Process]] = {}

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PROJECT_ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)


# ---------- 进程注册表 ----------

def _register(meeting_id: str, proc: asyncio.subprocess.Process) -> None:
    _procs.setdefault(meeting_id, set()).add(proc)


def _unregister(meeting_id: str, proc: asyncio.subprocess.Process) -> None:
    s = _procs.get(meeting_id)
    if s is not None:
        s.discard(proc)
        if not s:
            _procs.pop(meeting_id, None)


def running_count(meeting_id: str | None = None) -> int:
    if meeting_id is None:
        return sum(len(s) for s in _procs.values())
    return len(_procs.get(meeting_id, ()))


# ---------- 进程树终止（需求 5.1.1） ----------

def kill_tree_sync(pid: int) -> None:
    """同步终止整棵进程树；taskkill 失败（已退出）时静默。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, creationflags=_CREATE_NO_WINDOW,
        )
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    kill_tree_sync(proc.pid)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass


async def kill_meeting(meeting_id: str) -> int:
    """终止该会议全部子进程，返回数量（停止 / 急停 / 踢出 / 删除会议用）。"""
    procs = list(_procs.get(meeting_id, ()))
    for p in procs:
        await kill_tree(p)
    return len(procs)


async def kill_all() -> int:
    n = 0
    for mid in list(_procs):
        n += await kill_meeting(mid)
    return n


# ---------- 参数组装（需求 4.1） ----------

def build_args(cli_args_json: str, outfile: str | None) -> tuple[list[str], bool]:
    """返回 (args, use_outfile)。{outfile}/{project_root} 已替换为真实路径。"""
    try:
        args = json.loads(cli_args_json) if cli_args_json else []
        if not isinstance(args, list):
            args = [args]
    except json.JSONDecodeError:
        args = [cli_args_json] if cli_args_json else []
    args = [str(a) for a in args]
    args = [PROJECT_ROOT if a == "{project_root}" else a for a in args]
    use_outfile = outfile is not None and "{outfile}" in args
    if use_outfile and outfile is not None:
        args = [outfile if a == "{outfile}" else a for a in args]
    return args, use_outfile


def _read_outfile(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ---------- 执行 ----------

async def run_cli(participant: dict, prompt: str, meeting_id: str,
                  timeout: float = 300.0) -> tuple[bool, str, str]:
    """执行一次 CLI Agent。返回 (ok, text, error)。（需求 F8 / 5.1 第 7 步 b-c）"""
    cmd = (participant.get("cli_cmd") or "").strip()
    if not cmd:
        return False, "", "未配置命令（cli_cmd 为空）"
    resolved = shutil.which(cmd)
    if not resolved:
        return False, "", f"未检测到命令：{cmd}"

    wants_outfile = "{outfile}" in (participant.get("cli_args") or "")
    outfile: str | None = None
    if wants_outfile:
        fd, outfile = tempfile.mkstemp(prefix="rt_", suffix=".md")
        os.close(fd)

    proc: asyncio.subprocess.Process | None = None
    try:
        args, use_outfile = build_args(participant.get("cli_args") or "", outfile)
        stdin_mode = "-" in args
        argv = [resolved] + args
        if not stdin_mode:
            # prompt 始终作为最后一个参数追加（与 4.2 推荐形式一致：
            # codex exec ... -o <outfile> "<prompt>"），stdin 模式除外
            argv.append(prompt)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_mode else None,
        )
        _register(meeting_id, proc)
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(
                    prompt.encode("utf-8") if stdin_mode else None
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await kill_tree(proc)
            return False, "", "执行超时"
        finally:
            _unregister(meeting_id, proc)

        err_text = (err_b or b"").decode("utf-8", "replace").strip()
        text = _read_outfile(outfile) if (use_outfile and outfile) else ""
        if not text.strip():
            text = (out_b or b"").decode("utf-8", "replace").strip()

        if proc.returncode != 0:
            if text.strip():  # 有的 CLI 失败退出但已写出结果，仍算成功
                return True, text.strip(), ""
            return False, "", (err_text or f"退出码 {proc.returncode}")[:500]
        if not text.strip():
            return False, "", "命令执行完成但没有任何输出"
        return True, text.strip(), ""
    except Exception as e:  # 命令不存在 / 权限等，绝不向上抛崩 worker
        return False, "", f"启动失败：{e}"
    finally:
        if proc is not None:
            _unregister(meeting_id, proc)
        if outfile and os.path.exists(outfile):
            try:
                os.remove(outfile)
            except OSError:
                pass


def probe(cmd: str) -> dict[str, Any]:
    """预设可用性探测（/api/presets 用）。"""
    path = shutil.which(cmd or "")
    return {"available": bool(path), "path": path}
