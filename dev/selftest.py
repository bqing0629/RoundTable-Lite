"""RoundTable Lite 端到端自测脚本（dev 用，不属于运行时依赖）。

覆盖需求第 11 节验收的大部分条目：
  1. 健康检查 / 前端可访问
  2. 创建会议、发言接口即返（不等 Agent）
  3. stub Agent 被 @ 触发并写回回复
  4. Agent 回复 @ 另一 Agent 触发接力（深度 2）
  5. 并发发言 seq 不重号、严格递增
  6. @ 不存在成员 → delivery=no_recipients
  7. MCP /mcp：initialize → tools/list(8 个) → join → say（触发接力）→
     listen → ask_operator → 控制台答题回灌
  8. 停止：占位消息置 failed/已停止（进程树被终止的可见结果）
  9. token bucket：连发被拦并返回 retry_after
 10. 重启后数据仍在（持久化）

用法：python dev/selftest.py
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("RT_SELFTEST_PORT", "8917"))
PY = sys.executable

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name + (f"  [{detail}]" if detail and not cond else ""))
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  <- {detail}"))


def http(method: str, path: str, body: dict | None = None,
         timeout: float = 10, headers: dict | None = None):
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:  # HTML 等非 JSON 响应：原样带回
                parsed = {"raw": raw}
            return r.status, parsed, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except Exception:
            return e.code, {"raw": raw}, dict(e.headers)
    except Exception:  # 连接被拒等网络异常：返回可判 False 的哨兵
        return 0, {}, {}


def free_port(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def spawn_server() -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(ROOT / "dev" / "selftest-server.log", "ab")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [PY, "-X", "utf8", "-m", "uvicorn", "app.main:app", "--host", HOST,
         "--port", str(PORT), "--log-level", "info"],
        cwd=str(ROOT), env=env, creationflags=creationflags,
        stdout=log, stderr=log,
    )


def wait_health(proc: subprocess.Popen, seconds: float = 30) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            st, body, _ = http("GET", "/api/health", timeout=2)
            if st == 200 and body.get("app") == "roundtable-lite":
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def wait_message(mid: str, pred, seconds: float = 40):
    deadline = time.time() + seconds
    seen: dict[str, dict] = {}
    while time.time() < deadline:
        st, body, _ = http("GET", f"/api/meetings/{mid}/messages?after_seq=0")
        for m in body.get("messages", []):
            seen[m["id"]] = m
        for m in seen.values():
            if pred(m):
                return m
        time.sleep(0.5)
    return None


# ---------- MCP raw 客户端（Streamable HTTP） ----------

class Mcp:
    def __init__(self) -> None:
        self.session: str | None = None
        self.i = 0

    def _post(self, payload: dict) -> tuple[int, object]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["mcp-session-id"] = self.session
        req = urllib.request.Request(
            f"http://{HOST}:{PORT}/mcp",
            data=json.dumps(payload).encode("utf-8"),
            method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                self.session = r.headers.get("mcp-session-id") or self.session
                raw = r.read().decode("utf-8")
                ctype = r.headers.get("Content-Type", "")
                if "text/event-stream" in ctype:
                    for line in raw.splitlines():
                        if line.startswith("data:"):
                            return r.status, json.loads(line[5:].strip())
                    return r.status, {}
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, {"raw": e.read().decode("utf-8", "replace")[:300]}

    def initialize(self):
        return self._post({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "selftest", "version": "0"}}})

    def notify_initialized(self):
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, args: dict):
        self.i += 1
        st, resp = self._post({
            "jsonrpc": "2.0", "id": self.i, "method": "tools/call",
            "params": {"name": tool, "arguments": args}})
        if st != 200 or not isinstance(resp, dict):
            return st, {"_error": resp}
        result = resp.get("result", {})
        content = result.get("content") or []
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        try:
            return st, json.loads(text)
        except Exception:
            return st, {"_raw": text}

    def list_tools(self):
        self.i += 1
        st, resp = self._post({"jsonrpc": "2.0", "id": self.i, "method": "tools/list"})
        names = [t.get("name") for t in resp.get("result", {}).get("tools", [])]
        return st, names


def stub_participant(name: str, script: str = "stub_agent.py") -> dict:
    return {"display_name": name, "transport": "cli",
            "cli_cmd": PY,
            "cli_args": [str(ROOT / "dev" / script), "{outfile}"],
            "system_prompt": "", "trigger_mode": "mention"}


# ---------- 用例 ----------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not free_port(PORT):
        print(f"端口 {PORT} 被占用，换 RT_SELFTEST_PORT 再试")
        return 2
    print(f"== RoundTable Lite 自测（port={PORT}）==")
    proc = spawn_server()
    try:
        run_tests(proc)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    print(f"\n通过 {len(PASS)} 项；失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        return 1
    return 0


def run_tests(proc: subprocess.Popen) -> None:
    check("服务健康检查", wait_health(proc))

    st, body, _ = http("GET", "/")
    check("前端首页可访问", st == 200 and "RoundTable" in body if isinstance(body, str) else st == 200)

    # 会议 CRUD
    st, body, _ = http("POST", "/api/meetings", {"name": "自测会议", "description": "selftest"})
    check("创建会议", st == 200 and body.get("meeting", {}).get("id"), str(body)[:120])
    mid = body["meeting"]["id"]
    st, body, _ = http("GET", f"/api/meetings/{mid}")
    check("自动创建'我'", any(p["display_name"] == "我" and p["kind"] == "human"
                          for p in body.get("participants", [])))

    # 发言接口即返
    t0 = time.time()
    st, body, _ = http("POST", f"/api/meetings/{mid}/messages", {"content": "大家好"})
    dt = time.time() - t0
    check("发言接口即返(<3s)", st == 200 and dt < 3, f"{dt:.2f}s")
    check("发言返回 seq=1", body.get("message", {}).get("seq") == 1)

    # stub Agent 触发
    st, body, _ = http("POST", f"/api/meetings/{mid}/participants",
                       stub_participant("Echo"))
    check("加入 Echo(stub)", st == 200, str(body)[:120])
    http("POST", f"/api/meetings/{mid}/participants", stub_participant("Echo2"))

    st, body, _ = http("POST", f"/api/meetings/{mid}/messages",
                       {"content": "@Echo 请确认你在线"})
    check("@Echo 发言即返", st == 200)
    m = wait_message(mid, lambda m: m["author_name"] == "Echo" and m["status"] == "done")
    check("Echo 回复到达", bool(m))
    check("Echo 回复来自 stub", bool(m) and "stub" in (m or {}).get("content", ""))

    # 接力：Echo 的 stub 回复里会点名 @Echo2（真实链条：人→Echo→Echo2）
    http("POST", f"/api/meetings/{mid}/messages",
         {"content": "@Echo 请启动接力：把话题交给下一位成员继续"})
    m2 = wait_message(mid, lambda m: m["author_name"] == "Echo2" and m["status"] == "done",
                      seconds=60)
    check("接力触发 Echo2（深度2）", bool(m2))

    # 并发 seq 不重号
    import threading
    results: list = []

    def sender(i: int) -> None:
        s, b, _ = http("POST", f"/api/meetings/{mid}/messages",
                       {"content": f"并发消息 {i}"})
        results.append((s, b))

    threads = [threading.Thread(target=sender, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st, body, _ = http("GET", f"/api/meetings/{mid}/messages?after_seq=0")
    seqs = [m["seq"] for m in body.get("messages", [])]
    check("并发后 seq 严格递增且唯一", seqs == sorted(set(seqs)), str(seqs))

    # no_recipients
    st, body, _ = http("POST", f"/api/meetings/{mid}/messages",
                       {"content": "@不存在的人 你在吗"})
    check("@ 不存在成员标记未送达",
          st == 200 and body.get("message", {}).get("delivery") == "no_recipients")

    # MCP 通道
    mcp = Mcp()
    st, resp = mcp.initialize()
    check("MCP initialize", st == 200 and isinstance(resp, dict) and "result" in resp, str(resp)[:200])
    mcp.notify_initialized()
    st, names = mcp.list_tools()
    expect = {"join", "leave", "whoami", "list_peers", "say", "listen",
              "ask_operator", "list_forms"}
    check("MCP tools/list 共8工具", expect.issubset(set(names or [])), str(names))

    st, r = mcp.call("join", {"name": "MCPBot"})
    check("MCP join", r.get("ok") is True and r.get("protocol"), str(r)[:200])

    st, r = mcp.call("say", {"content": "MCPBot 向大家问好，并 @Echo 请回应"})
    check("MCP say", r.get("ok") is True, str(r)[:200])
    m3 = wait_message(mid, lambda m: m["author_name"] == "Echo"
                      and "问好" not in (m.get("content") or "")
                      and m["status"] == "done", seconds=40)
    check("MCP say 触发 CLI 接力", bool(m3))

    st, r = mcp.call("listen", {"timeout": 2})
    check("MCP listen 返回结构", r.get("ok") is True and "messages" in r, str(r)[:200])

    st, r = mcp.call("ask_operator", {
        "title": "自测表单：选一个",
        "fields": [{"key": "choice", "type": "radio", "label": "选哪个",
                    "required": True, "options": ["甲", "乙"]}]})
    fid = r.get("form_id")
    check("MCP ask_operator 建表单", r.get("ok") is True and bool(fid), str(r)[:200])

    st, body, _ = http("GET", f"/api/meetings/{mid}/forms")
    check("控制台可见 pending 表单",
          any(f["id"] == fid and f["status"] == "pending" for f in body.get("forms", [])))
    st, body, _ = http("POST", f"/api/meetings/{mid}/forms/{fid}/answer",
                       {"answer": {"choice": "甲"}})
    check("回答表单", st == 200 and body.get("form", {}).get("status") == "answered")
    m4 = wait_message(mid, lambda m: "【表单回答】" in (m.get("content") or ""))
    check("答案回灌会议消息流", bool(m4))

    # 停止：慢速 stub 占位应转为 failed/已停止
    http("POST", f"/api/meetings/{mid}/participants", stub_participant("Slow", "stub_slow.py"))
    # 主席发帖同样受限流约束（需求 9）：429 时等桶补充再试
    st, body, _ = http("POST", f"/api/meetings/{mid}/messages", {"content": "@Slow 慢慢想"})
    tries = 0
    while st == 429 and tries < 8:
        time.sleep(3)
        st, body, _ = http("POST", f"/api/meetings/{mid}/messages", {"content": "@Slow 慢慢想"})
        tries += 1
    check("@Slow 触发（限流重试后）", st == 200, f"st={st} body={str(body)[:80]}")
    time.sleep(1.5)  # 等 streaming 占位出现
    st, body, _ = http("POST", "/api/control/stop", {"meeting_id": mid})
    check("主席停止", st == 200)
    time.sleep(2.5)
    m5 = wait_message(mid, lambda m: m["author_name"] == "Slow" and m["status"] == "failed",
                      seconds=20)
    check("停止后占位消息置 failed/已停止",
          bool(m5) and "已停止" in ((m5 or {}).get("error") or ""))

    # 重置 + token bucket（bucket 测试放最后，避免污染前面用例）
    st, _, _ = http("POST", "/api/control/reset", {"meeting_id": mid})
    check("重置恢复 running", st == 200)
    got429 = False
    for i in range(15):
        st, body, _ = http("POST", f"/api/meetings/{mid}/messages",
                           {"content": f"刷屏测试 {i}"})
        if st == 429:
            got429 = True
            check("token bucket 返回 retry_after",
                  (body.get("detail") or {}).get("retry_after", 0) >= 1)
            break
    check("连发触发限流", got429)

    # 重启持久化
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    proc2 = spawn_server()
    try:
        check("重启后健康", wait_health(proc2))
        st, body, _ = http("GET", "/api/meetings")
        check("重启后会议仍在", any(m2["id"] == mid for m2 in body.get("meetings", [])))
        st, body, _ = http("GET", f"/api/meetings/{mid}/messages?after_seq=0")
        check("重启后消息仍在", len(body.get("messages", [])) >= 10,
              f"count={len(body.get('messages', []))}")
        st, body, _ = http("GET", f"/api/meetings/{mid}/forms")
        check("重启后表单仍在", any(f["id"] == fid for f in body.get("forms", [])))
    finally:
        if proc2.poll() is None:
            proc2.send_signal(signal.SIGTERM)
            try:
                proc2.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc2.kill()

    # 失败时直接读库 dump 本会议全部消息（此时服务已停，不能走 HTTP）
    if FAIL:
        print("== 失败诊断：本会议全部消息 ==")
        import sqlite3
        c = sqlite3.connect(ROOT / "data" / "roundtable.db")
        for row in c.execute(
            "SELECT seq, author_name, status, substr(error,1,60), substr(content,1,70) "
            "FROM message WHERE meeting_id=? ORDER BY seq", (mid,)):
            print("  seq=%s %s[%s] err=%s | %s" % row)

    # 清理：删除会议
    http("DELETE", f"/api/meetings/{mid}")


if __name__ == "__main__":
    sys.exit(main())
