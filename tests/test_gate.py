from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.gate import (
    CheckResult,
    build_feedback,
    check_diff_scope,
    check_secrets,
    load_config,
    parse_diff_stat,
    parse_test_output,
    resolve_base,
    verdict,
)
from scripts.gate_loop import (
    BLOCKED_FILE,
    FEEDBACK_FILE,
    STATE_FILE,
    load_state,
    run_loop,
)

DEFAULTS = load_config(None)


def _fail(name: str, summary: str, evidence: list[str] | None = None) -> CheckResult:
    return CheckResult(name, "fail", summary, evidence or [])


def _pass(name: str, summary: str = "OK") -> CheckResult:
    return CheckResult(name, "pass", summary)


class SecretsCheckTest(unittest.TestCase):
    def test_detects_sk_secret(self):
        diff = '+api_key = "sk-abc1234567890XYZ"\n'
        result = check_secrets(diff, DEFAULTS["secrets_patterns"])
        self.assertEqual(result.status, "fail")
        self.assertNotIn("sk-abc", " ".join(result.evidence))  # 값은 마스킹

    def test_detects_private_key(self):
        diff = "+-----BEGIN PRIVATE KEY-----\n+MIIE..."
        result = check_secrets(diff, DEFAULTS["secrets_patterns"])
        self.assertEqual(result.status, "fail")

    def test_clean_diff_passes(self):
        result = check_secrets('+print("hello world")\n', DEFAULTS["secrets_patterns"])
        self.assertEqual(result.status, "pass")

    def test_empty_diff_passes(self):
        self.assertEqual(check_secrets("", DEFAULTS["secrets_patterns"]).status, "pass")


class DiffScopeCheckTest(unittest.TestCase):
    def test_banned_path_fails(self):
        stat = " data/traces/20260904_1.json | 10 ++\n harness/client.py | 2 +-"
        result = check_diff_scope(stat, ["data/", ".freebuff/"], 2000)
        self.assertEqual(result.status, "fail")
        self.assertIn("data/traces/20260904_1.json", result.evidence[0])

    def test_clean_scope_passes(self):
        stat = " harness/client.py | 2 +-\n docs/harness-design.md | 50 +++++"
        result = check_diff_scope(stat, ["data/", ".freebuff/"], 2000)
        self.assertEqual(result.status, "pass")

    def test_large_diff_warns(self):
        stat = " harness/big.py | 2500 +++"
        result = check_diff_scope(stat, ["data/"], 2000)
        self.assertEqual(result.status, "warn")

    def test_parse_diff_stat(self):
        entries = parse_diff_stat(" harness/a.py | 3 +-\n scripts/b.py | 5 ++++")
        self.assertEqual(entries, [("harness/a.py", 3), ("scripts/b.py", 5)])


class TestOutputParseTest(unittest.TestCase):
    def test_parses_failures(self):
        output = (
            "test_ok (tests.test_a) ... ok\n"
            "FAIL: test_boom (tests.test_demo)\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_demo.py\", line 3, in test_boom\n"
            "    assert False\n"
            "AssertionError\n"
            "\n"
            "ERROR: test_crash (tests.test_b)\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_b.py\", line 2, in test_crash\n"
            "    raise RuntimeError('x')\n"
        )
        failures = parse_test_output(output)
        names = [name for name, _ in failures]
        self.assertIn("test_boom (tests.test_demo)", names)
        self.assertIn("test_crash (tests.test_b)", names)
        self.assertEqual(len(failures), 2)


class VerdictTest(unittest.TestCase):
    def test_any_fail_means_fail(self):
        self.assertEqual(verdict([_pass("a"), _fail("b", "x"), _pass("c")]), "fail")

    def test_pass_with_warns(self):
        result = CheckResult("diff_scope", "warn", "big")
        self.assertEqual(verdict([_pass("a"), result]), "pass")


class FeedbackTest(unittest.TestCase):
    def test_contains_instructions_and_attempts(self):
        feedback = build_feedback([_fail("tests", "2개 실패")], 1, 1)
        self.assertIn("재시도 1/1", feedback)
        self.assertIn("python -m scripts.gate_loop", feedback)
        self.assertIn("tests", feedback)


class LoopStateMachineTest(unittest.TestCase):
    """run_loop의 상태 전이: FAIL→재시도 1회→(성공 리셋 | 소진 BLOCKED). 실제 git 없이 검증."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self.cfg = dict(DEFAULTS)
        self.cfg["max_attempts"] = 1

    def tearDown(self):
        self._tmp.cleanup()

    def test_fail_then_pass_resets(self):
        provider = lambda cfg, cwd: [_fail("tests", "실패 1")]  # noqa: E731
        code = run_loop(self.cfg, cwd=self.cwd, results_provider=provider)
        self.assertEqual(code, 1)
        state = load_state(self.cwd)
        self.assertEqual(state["attempt"], 1)
        self.assertEqual(state["status"], "retry")
        self.assertTrue((self.cwd / FEEDBACK_FILE).exists())
        self.assertIn("재시도 1/1", (self.cwd / FEEDBACK_FILE).read_text(encoding="utf-8"))

        # 수정 후 재실행 → PASS → attempt 리셋
        code = run_loop(self.cfg, cwd=self.cwd, results_provider=lambda cfg, cwd: [_pass("tests")])
        self.assertEqual(code, 0)
        self.assertEqual(load_state(self.cwd)["attempt"], 0)
        self.assertEqual(load_state(self.cwd)["status"], "passed")

    def test_fail_twice_blocks(self):
        provider = lambda cfg, cwd: [_fail("tests", "계속 실패")]  # noqa: E731
        self.assertEqual(run_loop(self.cfg, cwd=self.cwd, results_provider=provider), 1)
        self.assertEqual(run_loop(self.cfg, cwd=self.cwd, results_provider=provider), 2)
        self.assertEqual(load_state(self.cwd)["status"], "blocked")
        self.assertTrue((self.cwd / BLOCKED_FILE).exists())
        self.assertIn("BLOCKED", (self.cwd / BLOCKED_FILE).read_text(encoding="utf-8"))

    def test_pass_first_try(self):
        code = run_loop(self.cfg, cwd=self.cwd, results_provider=lambda cfg, cwd: [_pass("tests")])
        self.assertEqual(code, 0)
        self.assertEqual(load_state(self.cwd)["attempt"], 0)
        self.assertFalse((self.cwd / FEEDBACK_FILE).exists())


@unittest.skipUnless(shutil.which("git"), "git 필요")
class RealRepoLoopTest(unittest.TestCase):
    """실 git 저장소에서 gate_loop 전 구간 검증: PASS → FAIL(피드백) → PASS. (§14 Slice 3)"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self._git(["init", "-q"])
        self._git(["config", "user.name", "gate-test"])
        self._git(["config", "user.email", "gate-test@local"])
        self.cfg = dict(DEFAULTS)
        self.cfg["max_attempts"] = 1
        # gate의 data/ 산출물(gate_state 등)이 diff에 섞이지 않도록 무시
        self._write(".gitignore", "data/\n")
        self._commit_all("init")

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=str(self.cwd), encoding="utf-8"
        )

    def _write(self, rel: str, content: str) -> None:
        path = self.cwd / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _commit_all(self, message: str) -> None:
        self._git(["add", "-A"])
        self._git(["commit", "-q", "-m", message])

    def test_fail_feedback_then_fix_then_pass(self):
        good = "import unittest\n\nclass TestDemo(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
        self._write("tests/test_demo.py", good)
        self._commit_all("good")
        # init(.gitignore) 커밋이 있으므로 base는 HEAD~1
        self.assertEqual(resolve_base(self.cwd), "HEAD~1")

        # 1) 정상 상태 → PASS
        self.assertEqual(run_loop(self.cfg, cwd=self.cwd), 0)
        self.assertEqual(load_state(self.cwd)["status"], "passed")

        # 2) 테스트 고장 → FAIL + 피드백 파일
        broken = "import unittest\n\nclass TestDemo(unittest.TestCase):\n    def test_broken(self):\n        self.assertFalse(True)\n"
        self._write("tests/test_demo.py", broken)
        code = run_loop(self.cfg, cwd=self.cwd)
        self.assertEqual(code, 1)
        feedback = (self.cwd / FEEDBACK_FILE).read_text(encoding="utf-8")
        self.assertIn("test_broken", feedback)
        self.assertEqual(load_state(self.cwd)["attempt"], 1)

        # 3) 수정 → PASS → 상태 리셋
        fixed = "import unittest\n\nclass TestDemo(unittest.TestCase):\n    def test_fixed(self):\n        self.assertTrue(True)\n"
        self._write("tests/test_demo.py", fixed)
        self.assertEqual(run_loop(self.cfg, cwd=self.cwd), 0)
        self.assertEqual(load_state(self.cwd)["attempt"], 0)

    def test_secret_commit_fails_then_fixed(self):
        good = "import unittest\n\nclass TestDemo(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
        self._write("tests/test_demo.py", good)
        self._commit_all("good")
        self.assertEqual(run_loop(self.cfg, cwd=self.cwd), 0)

        # 시크릿이 든 변경 커밋 → secrets 검사 FAIL
        self._write("secrets.env", "api_key = \"sk-abc1234567890XYZ\"\n")
        self._commit_all("leak")
        code = run_loop(self.cfg, cwd=self.cwd)
        self.assertEqual(code, 1)
        verdict_json = json.loads((self.cwd / "data/gate_verdict.json").read_text(encoding="utf-8"))
        self.assertEqual(verdict_json["verdict"], "fail")
        secrets_check = verdict_json["checks"][3]
        self.assertEqual(secrets_check["status"], "fail")
        self.assertIn("REDACTED", " ".join(secrets_check["evidence"]))

        # 시크릿 제거 → PASS
        (self.cwd / "secrets.env").unlink()
        self._commit_all("remove-leak")
        self.assertEqual(run_loop(self.cfg, cwd=self.cwd), 0)

    def test_single_commit_base_fallback(self):
        """commit이 1개뿐이면 base가 root commit으로 폴백해야 한다."""
        tmp = tempfile.TemporaryDirectory()
        cwd = Path(tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=str(cwd), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(cwd), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(cwd), check=True)
        (cwd / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True)
        subprocess.run(["git", "commit", "-qm", "only"], cwd=str(cwd), check=True)
        root = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(cwd), check=True
        ).stdout.strip()
        self.assertEqual(resolve_base(cwd), root)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()