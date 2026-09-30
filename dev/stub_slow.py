"""慢速测试桩：模拟耗时推理（自测"停止杀进程树"用）。

会再派生一个 python 孙进程（sleep），用于验证 taskkill /T 整树终止。
用法（cli_args）：[str(ROOT/dev/stub_slow.py), "{outfile}"]，prompt 追加为最后参数。
"""
import subprocess
import sys
import time

if __name__ == "__main__":
    outfile, prompt = sys.argv[1], sys.argv[2]
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(20)  # 足够长，等待主席停止
        with open(outfile, "w", encoding="utf-8") as f:
            f.write("（slow stub）终于想完了。")
    finally:
        if child.poll() is None:
            child.kill()
