from __future__ import annotations

"""자동 gate (Slice 3): Freebuff 작업 결과 → 자동 test → 자동 git diff 검사 → PASS/FAIL 판단.

검사 항목:
  compile     — python -m compileall (harness/ scripts/ tests/)
  tests       — python -m unittest discover -s tests
  diff_scope  — git diff(HEAD~1..HEAD + 미커밋 변경)에서 금지 경로/과대 diff 감지
  secrets     — diff 추가 라인에서 시크릿 패턴(sk-*, API key, private key 등) 감지

사용:
  python -m scripts.gate            # 1회 검증. exit 0=PASS, 1=FAIL
  python -m scripts.gate --cwd ...  # 다른 저장소 검증 (임시 repo 테스트용)
  python -m scripts.gate_loop       # 재시도 1회 포함 루프 (§14 Slice 3)

판단은 전부 결정적 코드(deterministic judge)로 수행하며, 사람/모델 판단에 의존하지 않는다.
FAIL 시 생성되는 피드백(data/gate_feedback.md)은 Freebuff(에이전트)가 읽고 한 번만 수정한다.
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "base_ref": "HEAD~1",  # 직전 커밋과의 diff가 검증 대상 (없으면 root commit 대비)
    "max_attempts": 1,  # FAIL 시 재시도 횟수 — "한 번 다시"만 허용
    "banned_paths": ["data/", ".freebuff/", ".agents/"],
    "secrets_patterns": [
        r"sk-[A-Za-z0-9]{16,}",
        r"(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"][^'\"]{6,}",
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN (RSA|EC|OPENSSH|PRIVATE) KEY-----",
    ],
    "max_added_lines": 2000,  # 초과 시 warn (실패는 아님)
}

VERDICT_FILE = "data/gate_verdict.json"


@dataclass
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "warn"
    summary: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "evidence": self.evidence[:20],
        }


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """config YAML 로드. 파일이 없으면 DEFAULTS 사용 (zero-config 실행 가능)."""
    cfg: dict[str, Any] = dict(DEFAULTS)
    config_path = Path(path) if path else Path("configs/gate.yaml")
    if config_path.exists():
        try:
            import yaml

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception as exc:  # YAML 파싱 실패 → DEFAULTS로 계속 (warn)
            cfg["_config_error"] = str(exc)
    return cfg


# ---------- 서브프로세스 헬퍼 ----------

def run_cmd(argv: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return run_cmd(["git", *args], cwd=cwd)


def resolve_base(cwd: Path | None = None) -> str:
    """HEAD~1이 있으면 그대로, 없으면(단일 commit) root commit sha를 사용."""
    check = git(["rev-parse", "--verify", "--quiet", "HEAD~1"], cwd=cwd)
    if check.returncode == 0 and check.stdout.strip():
        return "HEAD~1"
    root = git(["rev-list", "--max-parents=0", "HEAD"], cwd=cwd)
    if root.returncode == 0 and root.stdout.strip():
        return root.stdout.strip().splitlines()[-1]
    return "HEAD"  # commit 0개 — diff 없음, scope/secrets는 통과 처리


def head_sha(cwd: Path | None = None) -> str:
    result = git(["rev-parse", "--short", "HEAD"], cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else ""


def diff_text(base: str, cwd: Path | None = None) -> str:
    """검증 대상 diff: base..HEAD (커밋) + HEAD 기준 미커밋 변경 (staged+unstaged)."""
    committed = git(["diff", base, "HEAD"], cwd=cwd).stdout
    uncommitted = git(["diff", "HEAD"], cwd=cwd).stdout
    return committed + "\n" + uncommitted


def diff_stat(base: str, cwd: Path | None = None) -> str:
    committed = git(["diff", "--stat", base, "HEAD"], cwd=cwd).stdout
    uncommitted = git(["diff", "--stat", "HEAD"], cwd=cwd).stdout
    return committed + "\n" + uncommitted


def untracked_files(cwd: Path | None = None) -> list[str]:
    status = git(["status", "--porcelain"], cwd=cwd)
    return [line[3:].strip() for line in status.stdout.splitlines() if line.startswith("??")]


def added_lines(diff: str) -> list[str]:
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


# ---------- 개별 검사 ----------

def run_compile(cwd: Path | None = None) -> CheckResult:
    targets = [p for p in ("harness", "scripts", "tests") if (cwd / p).is_dir()] if cwd else ["harness", "scripts", "tests"]
    if not targets:
        return CheckResult("compile", "pass", "검사할 Python 소스 없음 (skip)")
    result = run_cmd([sys.executable, "-m", "compileall", "-q", *targets], cwd=cwd)
    if result.returncode == 0:
        return CheckResult("compile", "pass", f"compileall OK ({', '.join(targets)})")
    return CheckResult("compile", "fail", "compileall 실패", result.stderr.strip().splitlines()[:20])


def parse_test_output(output: str) -> list[tuple[str, str]]:
    """unittest 출력에서 (테스트명, traceback 앞부분) 목록 추출."""
    failures: list[tuple[str, str]] = []
    current: str | None = None
    body: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if line.startswith("FAIL: ") or line.startswith("ERROR: "):
            if current:
                failures.append((current, "\n".join(body[:4])))
            current = stripped[6:].strip()
            body = []
        elif current is not None and stripped:
            body.append(line)
        elif current is not None and not stripped:
            failures.append((current, "\n".join(body[:4])))
            current = None
            body = []
    if current:
        failures.append((current, "\n".join(body[:4])))
    return failures


def run_tests(cwd: Path | None = None) -> CheckResult:
    result = run_cmd([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=cwd, timeout=600)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return CheckResult("tests", "pass", "unittest 전체 통과")
    failures = parse_test_output(output)
    names = [name for name, _ in failures]
    summary = f"unittest 실패 ({len(failures)}개: {', '.join(names) or '요약 파싱 불가'})"
    evidence = [f"{name}\n{body}" if body else name for name, body in failures]
    return CheckResult("tests", "fail", summary, evidence)


def _mask_secrets(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        try:
            text = re.sub(pattern, "****[REDACTED]****", text)
        except re.error:
            continue
    return text


def check_secrets(diff: str, patterns: list[str], cwd: Path | None = None) -> CheckResult:
    if not diff.strip():
        return CheckResult("secrets", "pass", "diff 없음")
    hits: list[str] = []
    for line in added_lines(diff):
        masked = _mask_secrets(line, patterns)
        if masked != line:
            hits.append(masked.strip()[:160])
    if not hits:
        return CheckResult("secrets", "pass", "시크릿 패턴 없음")
    return CheckResult("secrets", "fail", f"시크릿 패턴 감지 ({len(hits)}개 라인)", hits[:10])


def parse_diff_stat(stat: str) -> list[tuple[str, int]]:
    """' path | N +++' 형태에서 (경로, 변경 라인 수) 추출."""
    entries: list[tuple[str, int]] = []
    for line in stat.splitlines():
        match = re.match(r"^\s*(\S.*?)\s*\|\s*(\d+)", line)
        if match:
            entries.append((match.group(1).strip(), int(match.group(2))))
    return entries


def _is_banned(path: str, banned: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    for entry in banned:
        entry = entry.replace("\\", "/").rstrip("/")
        if normalized == entry or normalized.startswith(entry + "/") or normalized.startswith(entry):
            return True
    return False


def check_diff_scope(stat: str, banned: list[str], max_added: int) -> CheckResult:
    entries = parse_diff_stat(stat)
    if not entries:
        return CheckResult("diff_scope", "pass", "diff 없음")
    offending = [path for path, _ in entries if _is_banned(path, banned)]
    total_added = sum(count for _, count in entries)
    if offending:
        return CheckResult("diff_scope", "fail", f"금지 경로 변경 ({', '.join(offending)})", offending[:10])
    if total_added > max_added:
        return CheckResult(
            "diff_scope",
            "warn",
            f"변경 라인 {total_added} > {max_added} (과대 diff — 의도 확인 필요)",
            [f"{total_added} lines added across {len(entries)} files"],
        )
    return CheckResult("diff_scope", "pass", f"{len(entries)}개 파일, {total_added} 라인 변경")


def check_untracked(files: list[str]) -> CheckResult:
    if not files:
        return CheckResult("untracked", "pass", "추적되지 않은 파일 없음")
    return CheckResult(
        "untracked",
        "warn",
        f"추적되지 않은 파일 {len(files)}개 (커밋 또는 gitignore 필요)",
        files[:10],
    )


# ---------- 집계 ----------

def check_all(cfg: dict[str, Any], cwd: Path | None = None) -> list[CheckResult]:
    checks: list[CheckResult] = []
    checks.append(run_compile(cwd))
    checks.append(run_tests(cwd))
    base = resolve_base(cwd)
    checks.append(check_diff_scope(diff_stat(base, cwd), cfg.get("banned_paths", []), int(cfg.get("max_added_lines", 2000))))
    checks.append(check_secrets(diff_text(base, cwd), cfg.get("secrets_patterns", [])))
    checks.append(check_untracked(untracked_files(cwd)))
    return checks


def verdict(results: list[CheckResult]) -> str:
    return "pass" if not any(r.status == "fail" for r in results) else "fail"


def build_feedback(results: list[CheckResult], attempt: int, max_attempts: int) -> str:
    failed = [r for r in results if r.status == "fail"]
    passed = [r for r in results if r.status in ("pass", "warn")]
    lines: list[str] = [
        f"# Gate FAIL 피드백 (재시도 {attempt}/{max_attempts})",
        "",
        "지시: 아래 실패 내용만 반영해 수정한 뒤 `python -m scripts.gate_loop`를 다시 실행하라.",
    ]
    if max_attempts == 1:
        lines.append("이번이 마지막 재시도 — 다시 실패하면 블로커로 기록하고 중단한다.")
    lines += ["", "## 실패한 검사"]
    for result in failed:
        lines.append(f"- **{result.name}** — {result.summary}")
        for item in result.evidence:
            lines.append(f"  ```")
            lines.extend(f"  {e}" for e in item.splitlines()[:8])
            lines.append(f"  ```")
    lines += ["", "## 통과/경고 검사"]
    for result in passed:
        lines.append(f"- {result.name}: {result.summary}")
    return "\n".join(lines)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _verdict_path(cwd: Path | None) -> Path:
    return (cwd / VERDICT_FILE) if cwd else Path(VERDICT_FILE)


def run_checks(cfg: dict[str, Any], cwd: Path | None = None) -> tuple[str, list[CheckResult]]:
    results = check_all(cfg, cwd)
    v = verdict(results)
    payload = {
        "verdict": v,
        "head": head_sha(cwd),
        "base": resolve_base(cwd),
        "checks": [r.to_dict() for r in results],
    }
    write_json(_verdict_path(cwd), payload)
    return v, results


def print_results(results: list[CheckResult]) -> None:
    for result in results:
        mark = {"pass": "✅", "fail": "❌", "warn": "⚠️"}[result.status]
        print(f"{mark} {result.name}: {result.summary}")
    for result in results:
        if result.status == "fail":
            print(f"\n--- {result.name} 상세 ---")
            for item in result.evidence[:20]:
                print(item[:400])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harness Slice 3 gate — test + git diff 검사 + PASS/FAIL 판단.",
    )
    parser.add_argument("--config", type=str, default=None, help="gate config YAML 경로 (기본: configs/gate.yaml)")
    parser.add_argument("--cwd", type=str, default=None, help="검증 대상 저장소 경로 (기본: 현재 디렉터리)")
    parser.add_argument("--base", type=str, default=None, help="diff 기준 ref 덮어쓰기 (기본: HEAD~1)")
    parser.add_argument("--json", action="store_true", help="PASS/FAIL 결과를 JSON으로 stdout 출력")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    if args.base:
        cfg["base_ref"] = args.base
    cwd = Path(args.cwd).resolve() if args.cwd else None

    v, results = run_checks(cfg, cwd)
    print_results(results)
    print(f"\n>>> GATE {'PASS' if v == 'pass' else 'FAIL'}")
    if args.json:
        payload = {
            "verdict": v,
            "checks": [r.to_dict() for r in results],
        }
        print(json.dumps(payload, ensure_ascii=False))
    return 0 if v == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())