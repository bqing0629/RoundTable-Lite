"""Pydantic 请求模型（需求 5.1）。"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

TRANSPORTS = {"cli", "mcp"}
TRIGGERS = {"mention", "always"}


def norm_cli_args(v: Any) -> str:
    """cli_args 接受 JSON 数组字符串或数组，统一存为 JSON 数组字符串（需求 4.1）。"""
    if v is None:
        return "[]"
    if isinstance(v, list):
        return json.dumps([str(x) for x in v], ensure_ascii=False)
    s = str(v).strip() or "[]"
    try:
        arr = json.loads(s)
        if isinstance(arr, list):
            return json.dumps([str(x) for x in arr], ensure_ascii=False)
    except json.JSONDecodeError:
        pass
    raise ValueError('cli_args 必须是 JSON 数组，如 ["exec","-o","{outfile}"]')


class MeetingIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class ParticipantIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=40)
    transport: str = "cli"
    cli_cmd: str = ""
    cli_args: Any = "[]"
    model: str = ""
    system_prompt: str = ""
    trigger_mode: str = "mention"
    enabled: bool = True

    @field_validator("display_name")
    @classmethod
    def _no_space(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("显示名不能为空")
        if any(ch.isspace() for ch in v):
            raise ValueError("显示名不能包含空格（@提及解析规则要求，见需求 1.3）")
        if v in {"我", "系统"}:
            raise ValueError("该显示名为保留名")
        return v

    @field_validator("transport")
    @classmethod
    def _transport(cls, v: str) -> str:
        if v not in TRANSPORTS:
            raise ValueError("transport 必须是 cli 或 mcp")
        return v

    @field_validator("trigger_mode")
    @classmethod
    def _trigger(cls, v: str) -> str:
        if v not in TRIGGERS:
            raise ValueError("trigger_mode 必须是 mention 或 always")
        return v

    @field_validator("cli_args")
    @classmethod
    def _args(cls, v: Any) -> str:
        return norm_cli_args(v)


class ParticipantPatch(BaseModel):
    display_name: str | None = None
    enabled: bool | None = None
    transport: str | None = None
    cli_cmd: str | None = None
    cli_args: Any | None = None
    model: str | None = None
    system_prompt: str | None = None
    trigger_mode: str | None = None

    @field_validator("display_name")
    @classmethod
    def _no_space(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("显示名不能为空")
        if any(ch.isspace() for ch in v):
            raise ValueError("显示名不能包含空格（@提及解析规则要求，见需求 1.3）")
        return v

    @field_validator("transport")
    @classmethod
    def _transport(cls, v: str | None) -> str | None:
        if v is not None and v not in TRANSPORTS:
            raise ValueError("transport 必须是 cli 或 mcp")
        return v

    @field_validator("trigger_mode")
    @classmethod
    def _trigger(cls, v: str | None) -> str | None:
        if v is not None and v not in TRIGGERS:
            raise ValueError("trigger_mode 必须是 mention 或 always")
        return v

    @field_validator("cli_args")
    @classmethod
    def _args(cls, v: Any) -> Any:
        return norm_cli_args(v) if v is not None else v

    def to_db(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class MessageIn(BaseModel):
    author_id: str | None = None
    content: str = Field(min_length=1, max_length=20000)


class AnswerIn(BaseModel):
    answer: dict[str, Any] = Field(default_factory=dict)
    cancel: bool = False


class ControlIn(BaseModel):
    paused: bool | None = None
    meeting_id: str | None = None
