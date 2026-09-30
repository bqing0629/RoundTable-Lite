"""默认参与者预设（需求 F7 / 4.2 / 4.3）：含本机可用性探测。"""
from __future__ import annotations

import sys

from .runner import probe


def presets() -> list[dict]:
    codex = probe("codex")
    dsh = probe("dsh")
    py_exe = sys.executable or "python"

    return [
        {
            "key": "codex-cli",
            "name": "Codex（CLI 通道）",
            "available": codex["available"],
            "note": "" if codex["available"] else "未检测到 codex 命令",
            "display_name": "Codex",
            "transport": "cli",
            "cli_cmd": "codex",
            "cli_args": ["exec", "-s", "read-only", "--skip-git-repo-check",
                         "-o", "{outfile}"],
            "model": "",
            "system_prompt": (
                "你是圆桌会议的参与者（架构师视角）。发言给出明确结论和理由，"
                "控制在 300 字内。如需点名其他成员，使用 @显示名 格式；"
                "遇到只有人类能拍板的决策，说明你的建议并等待。"
            ),
            "trigger_mode": "mention",
        },
        {
            "key": "codex-mcp",
            "name": "Codex（MCP 通道）",
            "available": codex["available"],
            "note": (
                "MCP 型参与者由 Codex 主动连接 http://127.0.0.1:8787/mcp 后"
                "自动加入（join 时创建），无需在此手动创建。此预设仅作提示。"
            ),
            "display_name": "Codex",
            "transport": "mcp",
            "cli_cmd": "",
            "cli_args": "[]",
            "model": "",
            "system_prompt": "",
            "trigger_mode": "mention",
        },
        {
            "key": "dsh",
            "name": "DSH（DeepSeek Harness）",
            "available": dsh["available"],
            "note": "" if dsh["available"] else "未检测到命令：装好后填实际路径即可启用",
            "display_name": "DSH",
            "transport": "cli",
            "cli_cmd": "dsh",
            "cli_args": ["-p", "{outfile}"],  # 占位模板：装好后按实际 CLI 修改
            "model": "",
            "system_prompt": "你是圆桌会议的参与者（工程实现视角）。发言精炼，可 @其他成员 点名。",
            "trigger_mode": "mention",
        },
        {
            "key": "echo",
            "name": "Echo 测试助手（stub，用于自测）",
            "available": bool(py_exe),
            "note": "本地回声脚本，不联网；用于验收接力/刹车/停止等机制",
            "display_name": "Echo",
            "transport": "cli",
            "cli_cmd": py_exe,
            "cli_args": ["{project_root}/dev/stub_agent.py", "{outfile}"],
            "model": "",
            "system_prompt": "",
            "trigger_mode": "mention",
        },
    ]
