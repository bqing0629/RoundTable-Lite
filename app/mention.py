"""@提及解析（需求 1.3，v2.1 定案规则）。

- `@`（兼容全角 `＠`）后截取候选名，候选名之后必须是边界字符（空白/中英文标点/串尾）；
- 候选名与显示名**精确匹配**，按显示名长度降序尝试——天然最长匹配：
  `@Codex2` 在成员 Codex2 存在时命中 Codex2，只有 Codex 时不会误命中 Codex；
- 显示名禁含空格（models.py 校验），故无需处理名字内空白。
"""
from __future__ import annotations

_AT = ("@", "＠")

# 出现即视为名字结束的字符（空白另行判断）
_BOUNDARY = set(
    "，。、；：！？,.!?;:"
    "()（）【】[]{}<>《》“”‘’\"'"
    "|/\\+-*=&^%$#@~`"
)


def _is_boundary(ch: str) -> bool:
    return ch.isspace() or ch in _BOUNDARY


def parse(content: str, names: list[str]) -> list[tuple[str, int, int]]:
    """返回 [(name, start_offset, length)]，length 含 @；按出现顺序，互不重叠。"""
    if not content or not names:
        return []
    ordered = sorted(set(names), key=len, reverse=True)
    out: list[tuple[str, int, int]] = []
    i, n = 0, len(content)
    while i < n:
        if content[i] in _AT:
            rest = content[i + 1:]
            for name in ordered:
                if name and rest.startswith(name):
                    end = i + 1 + len(name)
                    nxt = content[end] if end < n else ""
                    if not nxt or _is_boundary(nxt):
                        out.append((name, i, 1 + len(name)))
                        i = end
                        break
            else:
                i += 1
        else:
            i += 1
    return out


def contains_at(content: str) -> bool:
    return any(ch in content for ch in _AT)


if __name__ == "__main__":  # 自测：python -m app.mention
    names = ["Codex", "Codex2", "DSH", "GPT-4o"]
    cases = [
        ("@Codex 你看下", [("Codex", 0, 6)]),
        ("请 @Codex2 复核", [("Codex2", 2, 7)]),
        ("只有 @Codex 时", [("Codex", 3, 6)]),
        ("全角＠DSH 也算", [("DSH", 2, 4)]),
        ("带标点 @DSH，请回复", [("DSH", 4, 4)]),
        ("前缀不误伤 @Codexxyz", []),
        ("串尾@GPT-4o", [("GPT-4o", 2, 7)]),
        ("连续 @Codex 和 @DSH", [("Codex", 3, 6), ("DSH", 12, 4)]),
        ("无人可提 @nobody", []),
    ]
    for text, expect in cases:
        got = parse(text, names)
        assert got == expect, f"{text!r}: got {got}, expect {expect}"
    print(f"mention self-test OK ({len(cases)} cases)")
