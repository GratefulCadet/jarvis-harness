from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

"""Agent Delegation V1 — AgentRun 도메인 모델 + CodexRunner (Codex CLI subprocess).

역할 경계 (마일스톤 계약):
- Task   = 무엇을 해야 하는가 (기존 TaskStore, tasks.md — 여기서 다루지 않는다)
- AgentRun = 어떤 agent가 그 Task를 실제로 수행한 "한 번의 실행"

설계 원칙:
- persistence는 기존 JSON store 관례를 그대로 따른다: versioned 파일 1개
  (<memory_dir>/agent_runs.json), tmp + os.replace 원자 교체. 새 DB는 만들지 않는다.
- bridge는 요청 1개씩 순차 처리하므로 agent run은 절대 동기 대기하지 않는다:
  start는 즉시 반환하고 watcher thread가 프로세스를 돌본다. Electron은
  agent_run_status 폴링으로 진행을 본다(기존 tree_snapshot 폴링과 같은 패턴).
- 상태는 queued → running → completed|failed|cancelled 만 있다. awaiting_approval
  같은 상태는 실제 필요성이 확인될 때 추가한다. 앱 재시작 후 running이던 run은
  "interrupted"로 정직하게 표시한다 — 죽은 프로세스를 살아 있다고 가장하지 않는다.
- 보안: runner가 받는 입력은 agent_type/instruction/repo_path 같은 constrained
  값뿐이다. 모델·renderer가 command string을 넘기는 구조를 만들지 않는다. 실제
  CLI argv는 여기서(Harness가) 조립하고, instruction은 stdin(-)으로 전달해
  argv 주입 표면을 줄인다.
- isolation: run마다 <repo>/.worktrees/ar-<run_id> git worktree + 전용 브랜치.
  사용자의 (dirty일 수 있는) working tree는 건드리지 않는다 — reset/checkout 금지.
"""

AGENT_RUNS_VERSION = 1
RUN_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
INTERRUPTED_STATUS = "interrupted"  # 재시작 후의 정직한 상태 (§13)
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# 기존 state 파일 관례와 동형 — <memory_dir>/agent_runs.json.
def default_agent_runs_file(memory_dir: Path | str | None) -> Path:
    if memory_dir is None:
        raise AgentRunError("memory_dir 미설정 — JARVIS state 경로가 없습니다")
    return Path(memory_dir) / "agent_runs.json"


def default_run_log_dir(memory_dir: Path | str | None) -> Path:
    """run 로그 원본 파일 위치. trace(JSON 1파일/이벤트)와 별개로 프로세스의
    stdout/stderr 전문은 텍스트 로그로 남긴다 — UI는 tail만, 원본은 파일."""
    if memory_dir is None:
        raise AgentRunError("memory_dir 미설정 — JARVIS state 경로가 없습니다")
    return Path(memory_dir) / "agent_run_logs"


class AgentRunError(ValueError):
    """인자/상태 검증 실패 — bridge가 status=error로 되돌려준다."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return f"ar-{uuid.uuid4().hex[:8]}"


def _pid_alive(pid: Any) -> bool:
    """best-effort 프로세스 생존 확인. pid 재사용은 완전히 배제 못 하므로
    판정 결과는 '힌트'로만 쓴다(재시작 표시 문구에만 사용)."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int
            )
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid_int, 0)
        return True
    except OSError:
        return False


# ---------- AgentRunStore ----------

_UPDATABLE_FIELDS = frozenset({
    "status", "started_at", "finished_at", "exit_code", "result_summary",
    "error", "changed_files", "verification", "pid", "worktree_path",
    "branch", "log_path", "log_tail",
})


class AgentRunStore:
    """agent_runs.json 단일 writer — resource_links.json과 같은 파생 메타데이터 규약.

    filesystem(worktree 안의 실제 파일)은 여전히 canonical이고, 이 레지스트리는
    run의 identity/상태/결과 참조만 저장한다. 여러 watcher thread가 동시에
    update하므로 쓰기는 lock으로 직렬화한다.
    """

    def __init__(self, registry_file: Path | str) -> None:
        self.registry_file = Path(registry_file)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}  # 손상된 레지스트리 → 빈 상태 (crash 금지, filesystem canonical)
        runs = raw.get("runs")
        return dict(runs) if isinstance(runs, dict) else {}

    def save(self, runs: dict[str, dict[str, Any]]) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": AGENT_RUNS_VERSION, "runs": runs}
        fd, tmp_name = tempfile.mkstemp(dir=str(self.registry_file.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_name, self.registry_file)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def create_run(
        self,
        *,
        agent_type: str,
        instruction: str,
        repo_path: str,
        task_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        run_id = _new_run_id()
        with self._lock:
            runs = self._load()
            while run_id in runs:
                run_id = _new_run_id()
            run: dict[str, Any] = {
                "run_id": run_id,
                "task_id": task_id or None,
                "project_id": project_id or None,
                "session_id": session_id or None,
                "agent_type": agent_type,
                "status": "queued",
                "instruction": instruction,
                "repo_path": repo_path,
                "worktree_path": None,
                "branch": None,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "result_summary": None,
                "error": None,
                "changed_files": [],
                "verification": None,
                "pid": None,
                "log_path": None,
                "timeout_s": int(timeout_s) if timeout_s else default_run_timeout_s(),
                "created_at": _now_iso(),
            }
            runs[run_id] = run
            self.save(runs)
        return dict(run)

    def get(self, run_id: str) -> dict[str, Any] | None:
        if not isinstance(run_id, str) or not run_id.strip():
            return None
        run = self._load().get(run_id.strip())
        return dict(run) if run else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        runs = self._load()
        ordered = sorted(
            runs.values(), key=lambda r: str(r.get("created_at") or ""), reverse=True
        )
        return [dict(r) for r in ordered[: max(1, min(int(limit), 100))]]

    def update(self, run_id: str, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise AgentRunError(f"알 수 없는 run 필드: {sorted(unknown)}")
        with self._lock:
            runs = self._load()
            run = runs.get(run_id)
            if run is None:
                raise AgentRunError(f"알 수 없는 run: {run_id}")
            run.update(fields)
            self.save(runs)
            return dict(run)

    def mark_interrupted_on_startup(self) -> list[dict[str, Any]]:
        """앱 재시작 직후 호출 — running/queued였던 run은 거짓말을 하면 안 된다.

        프로세스는 이전 프로세스 트리에 속했고(사실상 죽었거나 고아), 결과를
        알 수 없다. status=interrupted로 표시하고, pid가 아직 살아 있으면 그
        사실을 error에 덧붙인다(사용자가 직접 정리할 수 있게).
        """
        marked: list[dict[str, Any]] = []
        with self._lock:
            runs = self._load()
            changed = False
            for run in runs.values():
                if run.get("status") not in ("queued", "running"):
                    continue
                run["status"] = INTERRUPTED_STATUS
                run["finished_at"] = _now_iso()
                note = "JARVIS가 다시 시작되어 실행이 중단되었습니다. 결과를 알 수 없습니다."
                if _pid_alive(run.get("pid")):
                    note += f" (주의: pid {run.get('pid')} 프로세스가 아직 살아 있는 것으로 보입니다)"
                run["error"] = note
                changed = True
                marked.append(dict(run))
            if changed:
                self.save(runs)
        return marked


def default_run_timeout_s() -> int:
    raw = os.environ.get("JARVIS_AGENT_RUN_TIMEOUT_S", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return 1800


# ---------- git worktree isolation ----------

class WorktreeError(AgentRunError):
    pass


def _git(repo: Path, *args: str, timeout: float = 60.0) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args[:2])} 실패 (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    return result.stdout


def ensure_worktrees_excluded(repo: Path) -> None:
    """.worktrees/를 .git/info/exclude에 추가 — 사용자 파일(.gitignore)을
    건드리지 않고 이 저장소에서만 untracked 노이즈를 막는다(로컬 전용)."""
    exclude = repo / ".git" / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".worktrees/" in existing:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(".worktrees/\n")
    except OSError:
        pass  # 방어적 — exclude 실패가 run을 막지는 않는다


def prepare_run_worktree(repo_path: str | Path, run_id: str) -> dict[str, str]:
    """run 전용 git worktree 생성. 사용자 working tree는 절대 건드리지 않는다.

    - repo는 커밋이 1개 이상 있어야 한다(HEAD 필요). 없으면 억지로 만들지
      않고 WorktreeError로 실제 blocker를 보고한다(계약 §5).
    - worktree 위치: <repo>/.worktrees/ar-<run_id>, 브랜치: ar-<run_id>.
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        raise WorktreeError(f"repo 경로가 존재하지 않습니다: {repo}")
    if not (repo / ".git").exists():
        raise WorktreeError(f"git 저장소가 아닙니다: {repo}")
    _git(repo, "rev-parse", "--is-inside-work-tree")
    try:
        _git(repo, "rev-parse", "HEAD")
    except WorktreeError as exc:
        raise WorktreeError(
            "커밋이 없는 저장소라 run 전용 worktree를 만들 수 없습니다 "
            f"(초기 커밋이 필요합니다): {exc}"
        ) from exc
    ensure_worktrees_excluded(repo)
    worktree_path = repo / ".worktrees" / run_id
    branch = run_id
    if worktree_path.exists():
        raise WorktreeError(f"worktree 위치가 이미 사용 중입니다: {worktree_path}")
    _git(repo, "worktree", "add", "-b", branch, str(worktree_path), "HEAD")
    return {"worktree_path": str(worktree_path), "branch": branch}


def remove_run_worktree(repo_path: str | Path, worktree_path: str | Path) -> None:
    """수동 정리용 — run 결과물 폐기 시에만 호출한다. 완료 run의 worktree는
    결과 자산이므로 자동 삭제하지 않는다(사용자가 검사할 수 있어야 한다)."""
    repo = Path(repo_path)
    try:
        _git(repo, "worktree", "remove", "--force", str(worktree_path))
    except WorktreeError:
        _git(repo, "worktree", "prune")


# ---------- subprocess helpers ----------

def _resolve_executable(name: str) -> str:
    """binary 해석. Windows에서 npm/codex shim은 .cmd이므로 PATHEXT까지 본다.

    shim(.cmd/.bat)은 *전체 경로*로 리스트 argv의 첫 요소로 넘기면 Python
    CreateProcess가 직접 실행하므로 cmd.exe wrapper가 필요 없다. wrapper를 쓰면
    'C:\\Program Files\\...' 공백 경로 인용이 깨진다는 것을 검증했다.
    """
    expanded = Path(os.path.expanduser(name))
    if expanded.is_file():
        return str(expanded)
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if os.name == "nt":
        for suffix in (".cmd", ".bat", ".exe"):
            resolved = shutil.which(name + suffix)
            if resolved:
                return resolved
    raise AgentRunError(f"실행 파일을 찾을 수 없습니다: {name}")


def _needs_cmd_wrapper(binary: str) -> bool:
    return os.name == "nt" and binary.lower().endswith((".cmd", ".bat"))


def _spawn_process(argv: list[str], cwd: Path, log_path: Path) -> subprocess.Popen:
    """프로세스 트리 통째로 정리 가능한 형태로 spawn. stdout/stderr는 로그 파일로.

    Windows 주의: argv에 공백이 섞인 경로(npm.CMD 등)가 있으면 cmd.exe /c로
    재조립하는 과정에서 인용이 깨진다. .cmd/.bat shim은 인자 리스트 그대로
    CreateProcess에 넘기면 Python이 직접 실행해 주므로 wrapper가 필요 없다 —
    wrapper는 코드에 공백이 섞인 fake binary(JARVIS_CODEX_BIN="python fake.py")
    문자열을 tokenize할 때만 쓴다.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    finally:
        log_handle.close()  # 자식이 fd를 물려받았다 — 부모 쪽은 닫는다


def _split_command_line(value: str) -> list[str]:
    """JARVIS_CODEX_BIN 같은 환경변수 명령줄을 argv로 (shlex 규약)."""
    import shlex

    try:
        parts = shlex.split(value, posix=False)
    except ValueError:
        parts = value.split()
    cleaned = [part.strip('"') for part in parts if part.strip('"')]
    return cleaned or [value]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Windows는 taskkill /T로 cmd shim 아래 node까지, POSIX는 프로세스 그룹
    전체에 SIGTERM. UI만 cancelled로 바꾸고 프로세스를 남겨두면 안 된다."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _tail_text(path: Path, max_chars: int) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")[-max_chars:]


def _git_changed_files(worktree: Path) -> list[str]:
    """worktree 기준 변경 파일 (HEAD 대비 + untracked). 상태 코드는 버리고
    경로만 — 'a -> b' rename 표기는 b(최종 이름)를 쓴다."""
    try:
        out = _git(worktree, "status", "--porcelain")
    except WorktreeError:
        return []
    files: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip().strip('"')
        if entry and entry not in files:
            files.append(entry)
    return files


# ---------- AgentRunner interface + CodexRunner ----------

class AgentRunner:
    """runner 추상화 — start/status/cancel/collect_result (계약 §3).

    V1 구현체는 CodexRunner 하나. 미래 확장(FreebuffRunner, 로컬 Qwen)을 위한
    인터페이스만 남긴다 — 미래 기능을 미리 구현하지 않는다.
    """

    name = "agent"

    def __init__(self, store: AgentRunStore, memory_dir: Path | str) -> None:
        self.store = store
        self.memory_dir = Path(memory_dir)

    def start(self, run_id: str, spec: dict[str, Any]) -> None:
        raise NotImplementedError

    def status(self, run_id: str) -> dict[str, Any]:
        run = self.store.get(run_id)
        if run is None:
            raise AgentRunError(f"알 수 없는 run: {run_id}")
        return run

    def cancel(self, run_id: str) -> None:
        raise NotImplementedError

    def collect_result(self, run_id: str) -> dict[str, Any]:
        run = self.store.get(run_id)
        if run is None:
            raise AgentRunError(f"알 수 없는 run: {run_id}")
        return run

    def cancel_all(self) -> None:
        """bridge shutdown 경로 — 살아 있는 자식 프로세스를 고아로 남기지 않는다."""


def codex_binary() -> list[str]:
    """Codex CLI 실행 argv prefix. JARVIS_CODEX_BIN이 테스트/fake 주입 지점.

    "python path/to/fake.py"처럼 여러 토큰이면 tokenize한다. 반환은 argv prefix —
    executable 단일 문자열이 아니다 (코드에 공백이 섞인 경로 지원).

    해석 순위: JARVIS_CODEX_BIN > JARVIS_CODEX_HOME의 shim > PATH의 codex.
    JARVIS_CODEX_HOME은 기계마다 최신 codex가 설치된 npm global 디렉터리를
    가리킨다 — PATH에 옛 버전(예: 0.147 shim)이 남아 있어도 이견을 낼 수 있다.
    npm shim은 Windows에서 codex.cmd / codex(shell script) 순으로 고른다 —
    POSIX shell script를 CreateProcess로 직접 띄우면 WinError 193이 난다.
    """
    override = os.environ.get("JARVIS_CODEX_BIN", "").strip()
    if override:
        return _split_command_line(override)
    home = os.environ.get("JARVIS_CODEX_HOME", "").strip()
    if home:
        base = Path(os.path.expanduser(home))
        for name in ("codex.cmd", "codex"):
            candidate = base / name
            if candidate.is_file():
                if name == "codex.cmd" or not os.name == "nt":
                    return [str(candidate)]
                # Windows에서 확장자 없는 POSIX shim은 제외하고 .cmd를 쓴다.
                continue
        if os.name != "nt" and (base / "codex").is_file():
            return [str(base / "codex")]
    return [_resolve_executable("codex")]


class CodexRunner(AgentRunner):
    """Codex CLI(`codex exec`)를 subprocess로 실행하는 V1 runner.

    CLI 계약은 설치된 codex-cli 0.147.0의 실제 `codex exec --help`에서 확정:
    - `codex exec -` : instruction을 stdin에서 읽음 (argv 주입 표면 제거)
    - `-C <dir>`     : agent의 작업 루트 = run 전용 worktree
    - `-s workspace-write` : sandbox 쓰기를 작업 루트로 제한 (CLI 차원 isolation)
    - `--json`       : stdout JSONL 이벤트 → 로그 파일
    - `-o <file>`    : 최종 agent 메시지 → result_summary 원천
    """

    name = "codex"

    def __init__(self, store: AgentRunStore, memory_dir: Path | str) -> None:
        super().__init__(store, memory_dir)
        self._processes: dict[str, subprocess.Popen] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ---- lifecycle ----

    def start(self, run_id: str, spec: dict[str, Any]) -> None:
        repo_path = spec["repo_path"]
        worktree = prepare_run_worktree(repo_path, run_id)
        run = self.store.get(run_id)
        if run is None:
            raise AgentRunError(f"알 수 없는 run: {run_id}")

        worktree_path = Path(worktree["worktree_path"])
        log_path = default_run_log_dir(self.memory_dir) / f"{run_id}.log"
        last_message_path = worktree_path / ".agent-last-message.txt"
        argv = [
            *codex_binary(), "exec", "--json",
            "-s", "workspace-write",
            "-C", str(worktree_path),
            "-o", str(last_message_path),
            "-",
        ]

        proc = _spawn_process(argv, worktree_path, log_path)
        try:
            if proc.stdin is not None:
                proc.stdin.write((spec["instruction"] + "\n").encode("utf-8"))
                proc.stdin.close()
        except (OSError, ValueError):
            pass  # 조기 종료 — watcher가 exit code로 정리한다

        self.store.update(
            run_id,
            status="running",
            started_at=_now_iso(),
            pid=proc.pid,
            worktree_path=worktree["worktree_path"],
            branch=worktree["branch"],
            log_path=str(log_path),
        )
        cancel_event = threading.Event()
        with self._lock:
            self._processes[run_id] = proc
            self._cancel_events[run_id] = cancel_event
        thread = threading.Thread(
            target=self._watch,
            args=(run_id, proc, worktree_path, last_message_path, cancel_event),
            daemon=True,
            name=f"agent-run-{run_id}",
        )
        with self._lock:
            self._threads[run_id] = thread
        thread.start()

    def _watch(
        self,
        run_id: str,
        proc: subprocess.Popen,
        worktree_path: Path,
        last_message_path: Path,
        cancel_event: threading.Event,
    ) -> None:
        run = self.store.get(run_id) or {}
        timeout_s = int(run.get("timeout_s") or default_run_timeout_s())
        deadline = time.monotonic() + timeout_s
        exit_code: int | None = None
        timed_out = False
        while True:
            try:
                exit_code = proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancel_event.is_set():
                    _kill_process_tree(proc)
                    exit_code = proc.wait()
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    _kill_process_tree(proc)
                    exit_code = proc.wait()
                    break
        log_path = Path(run.get("log_path") or "")
        log_tail = _tail_text(log_path, 8000) if log_path and log_path.exists() else ""
        changed = _git_changed_files(worktree_path)

        fields: dict[str, Any] = {
            "exit_code": exit_code,
            "finished_at": _now_iso(),
            "changed_files": changed,
            "log_tail": log_tail,
        }
        if cancel_event.is_set() and not timed_out:
            fields["status"] = "cancelled"
            fields["error"] = "사용자가 실행을 중단했습니다."
        elif timed_out:
            fields["status"] = "failed"
            fields["error"] = f"timeout after {timeout_s}s — 실행이 한도를 초과해 종료되었습니다."
        elif exit_code == 0:
            fields["status"] = "completed"
            fields["result_summary"] = self._read_last_message(last_message_path)
        else:
            fields["status"] = "failed"
            fields["error"] = f"Codex가 실패로 종료되었습니다 (exit code {exit_code})."
        self.store.update(run_id, **fields)

        with self._lock:
            self._processes.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            self._threads.pop(run_id, None)

    @staticmethod
    def _read_last_message(last_message_path: Path) -> str | None:
        try:
            text = last_message_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text[:2000] if text else None

    # ---- control ----

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.store.get(run_id)
        if run is None:
            raise AgentRunError(f"알 수 없는 run: {run_id}")
        if run.get("status") in TERMINAL_STATUSES:
            return run
        if run.get("status") == "queued":
            return self.store.update(run_id, status="cancelled", finished_at=_now_iso())
        event = self._cancel_events.get(run_id)
        proc = self._processes.get(run_id)
        if event is not None:
            event.set()
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc)  # 즉시 겨냥 사격 — watcher가 상태를 확정한다
            run = self.store.get(run_id) or run
        return run

    def cancel_all(self) -> None:
        for run in self.store.list_runs(limit=100):
            if run.get("status") in ("queued", "running"):
                try:
                    self.cancel(run["run_id"])
                except AgentRunError:
                    pass

    def is_process_alive(self, run_id: str) -> bool:
        proc = self._processes.get(run_id)
        return proc is not None and proc.poll() is None


# ---------- verification (계약 §10) ----------

# 고정 화이트리스트 — 모델/renderer가 command string을 넘기는 구조가 아니다.
VERIFICATION_COMMANDS: dict[str, list[str]] = {
    "verify:fast": ["npm", "run", "verify:fast"],
}

VERIFICATION_TIMEOUT_S = 900


def resolve_verification_argv(command: str) -> list[str]:
    argv = VERIFICATION_COMMANDS.get(command)
    if argv is None:
        raise AgentRunError(
            f"허용되지 않은 verification command: {command!r}. "
            f"허용 목록: {sorted(VERIFICATION_COMMANDS)}"
        )
    resolved = [_resolve_executable(argv[0]), *argv[1:]]
    return resolved


def start_verification(store: AgentRunStore, run_id: str, command: str) -> dict[str, Any]:
    """completed run에 verify:fast 실행 — 별도 post-run action(자동 실행 아님).

    완료된 run의 worktree 안에서 실행하고 결과를 run.verification에 저장한다.
    환경 실패(예: fresh worktree에 node_modules 부재)도 정직하게 failed로 기록.
    """
    run = store.get(run_id)
    if run is None:
        raise AgentRunError(f"알 수 없는 run: {run_id}")
    if run.get("status") != "completed":
        raise AgentRunError(
            f"verification은 completed run에만 실행할 수 있습니다 (현재: {run.get('status')})"
        )
    verification = run.get("verification") or {}
    if verification.get("status") == "running":
        raise AgentRunError("verification이 이미 실행 중입니다")
    worktree = run.get("worktree_path")
    if not worktree or not Path(worktree).is_dir():
        raise AgentRunError("run의 worktree가 더 이상 존재하지 않습니다")
    argv = resolve_verification_argv(command)
    log_path = default_run_log_dir(store.registry_file.parent) / f"{run_id}.verify.log"
    record = {
        "command": command,
        "status": "running",
        "started_at": _now_iso(),
        "exit_code": None,
        "duration_ms": None,
        "log_path": str(log_path),
        "log_tail": "",
    }
    store.update(run_id, verification=record)

    def worker() -> None:
        started = time.monotonic()
        try:
            proc = _spawn_process(argv, Path(worktree), log_path)
            try:
                exit_code: int | None = proc.wait(timeout=VERIFICATION_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                exit_code = proc.wait()
                record["status"] = "failed"
                record["log_tail"] = (
                    _tail_text(log_path, 4000)
                    + f"\n[timeout after {VERIFICATION_TIMEOUT_S}s]"
                )
            else:
                record["status"] = "completed" if exit_code == 0 else "failed"
                record["log_tail"] = _tail_text(log_path, 4000)
            record["exit_code"] = exit_code
        except AgentRunError as exc:  # binary 해석 실패 등 — 환경 문제도 정직히 기록
            record["status"] = "failed"
            record["exit_code"] = None
            record["log_tail"] = str(exc)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        record["finished_at"] = _now_iso()
        try:
            store.update(run_id, verification=dict(record))
        except AgentRunError:
            pass  # run이 삭제된 극단 케이스 — 기록은 부수효과

    thread = threading.Thread(target=worker, daemon=True, name=f"agent-verify-{run_id}")
    thread.start()
    return store.get(run_id) or {}


# ---------- runner registry ----------

_RUNNER_FACTORIES: dict[str, Any] = {"codex": CodexRunner}


def get_runner(agent_type: str, store: AgentRunStore, memory_dir: Path | str) -> AgentRunner:
    factory = _RUNNER_FACTORIES.get(agent_type)
    if factory is None:
        raise AgentRunError(
            f"알 수 없는 agent_type: {agent_type!r}. 지원: {sorted(_RUNNER_FACTORIES)}"
        )
    return factory(store, memory_dir)
