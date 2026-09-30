"""RoundTable Lite 启动入口（需求 8，v2.1）。

职责：
1. 依赖检查（fastapi / uvicorn / mcp 逐个 import），缺失给可读提示；
2. 端口探测（8787 起，占用 +1）；占用者若是对端 /api/health 匹配的旧实例，
   直接开浏览器复用并退出，绝不起第二个进程写同一个 SQLite；
3. 启动 uvicorn（127.0.0.1），/api/health 200 后才开浏览器（杜绝竞态错误页）。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

HOST = "127.0.0.1"
BASE_PORT = 8787
PORT_TRIES = 20
APP_MARKER = "roundtable-lite"


def pause() -> None:
    try:
        input("\n按回车键退出...")
    except EOFError:
        pass


def check_deps() -> list[str]:
    missing = []
    for mod in ("fastapi", "uvicorn", "mcp"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return missing


def health_ok(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{port}/api/health", timeout=timeout
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
            return bool(data) and data.get("app") == APP_MARKER
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def open_browser_when_ready(port: int) -> None:
    if os.environ.get("RT_NO_BROWSER"):
        return  # 无头环境 / 自测用
    def run() -> None:
        url = f"http://{HOST}:{port}/"
        for _ in range(150):  # 最多等 30 秒
            if health_ok(port):
                print(f"服务就绪，打开浏览器：{url}")
                webbrowser.open(url)
                return
            time.sleep(0.2)
        print(f"健康检查超时，请手动访问：{url}")

    threading.Thread(target=run, name="opener", daemon=True).start()


def main() -> int:
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    missing = check_deps()
    if missing:
        print(f"缺少依赖：{', '.join(missing)}")
        print("请先执行：python -m pip install -r requirements.txt")
        pause()
        return 1

    # 端口探测 + 旧实例复用
    chosen: int | None = None
    for p in range(BASE_PORT, BASE_PORT + PORT_TRIES):
        if port_free(p):
            chosen = p
            break
        if health_ok(p):
            url = f"http://{HOST}:{p}/"
            print(f"检测到 RoundTable 已在运行：{url}")
            print("直接复用旧实例（避免两个进程写同一个数据库）。")
            if not os.environ.get("RT_NO_BROWSER"):
                webbrowser.open(url)
            return 0
    if chosen is None:
        print(f"{BASE_PORT}~{BASE_PORT + PORT_TRIES - 1} 端口均被占用且不是本程序，请清理后重试。")
        pause()
        return 1

    port = chosen
    print(f"Hub:      http://{HOST}:{port}")
    print(f"控制台:    http://{HOST}:{port}/")
    print(f"MCP 接入:  http://{HOST}:{port}/mcp")
    print("Ctrl+C 停止服务。")
    open_browser_when_ready(port)

    try:
        import uvicorn
        from app.main import app
        uvicorn.run(app, host=HOST, port=port, log_level="warning")
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0
    except Exception as e:
        print(f"\n服务启动失败：{e}")
        pause()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
