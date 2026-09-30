"""测试用 CLI Agent（stub）：不联网，读 prompt、写回复到 {outfile}。

预设"Echo 测试助手"的用法：
  cli_args = ["{project_root}/dev/stub_agent.py", "{outfile}"]
runner 会把 prompt 追加为最后一个参数，故：
  argv[1] = outfile 路径，argv[2] = prompt

行为：
- 普通回复：确认收到；
- prompt 中含 "@Echo2" 且会议里存在 Echo2 时回复中带 "@Echo2"，用于接力测试；
- 环境变量 STUB_DELAY 可模拟耗时（秒），用于超时/停止测试。
"""
import os
import sys
import time


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: stub_agent.py <outfile> <prompt>", file=sys.stderr)
        return 2
    outfile, prompt = sys.argv[1], sys.argv[2]
    delay = float(os.environ.get("STUB_DELAY", "0") or 0)
    if delay > 0:
        time.sleep(delay)
    reply = f"（stub）已读 prompt（{len(prompt)} 字符）。"
    if "@Echo2" in prompt:
        reply += " 这个问题需要 @Echo2 补充实现视角。"
    if "接力" in prompt or "下一位" in prompt:
        reply += " 我把话筒交给下一位：@Echo2 请继续。"
    if "报错" in prompt or "error" in prompt.lower():
        print("stub simulated failure", file=sys.stderr)
        return 3
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
