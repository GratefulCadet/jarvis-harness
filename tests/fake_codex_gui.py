import sys, os

# GUI acceptance test용 fake codex — test_agent_runs의 fake와 동일 계약.
# stdin instruction을 읽고 -C 작업 루트에 결과 파일을 만든 뒤 성공 종료한다.
args = sys.argv[1:]
root = args[args.index("-C") + 1] if "-C" in args else "."
sys.stdin.read()
with open(os.path.join(root, "SESSION_SMOKE.txt"), "w", encoding="utf-8") as f:
    f.write("session run from GUI")
with open(os.path.join(root, ".agent-last-message.txt"), "w", encoding="utf-8") as f:
    f.write("done")
sys.exit(0)
