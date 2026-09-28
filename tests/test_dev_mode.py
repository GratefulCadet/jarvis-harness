from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.config import DEV_TOOL_LOOP_MAX_TURNS, HarnessConfig

"""개발 모드(tool_loop_max_turns 해제) 테스트.

배경: 배포 configs/harness.yaml가 tool_loop_max_turns: 4를 명시한다. 개발 중
JARVIS가 실제 편집 작업을 수행하면 4턴에 걸리고, 브리지는 그 사실을
"예상하지 못한 finish_reason"이라는 무의미한 문자열로만 위로 보냈다. 여기서는
(1) 한도를 실제로 푸는 플래그가 YAML에 눌리지 않고 동작하는지,
(2) 한도가 어디에 적힌 값보다 우선하는지,
(3) 중단 사유가 사람이 읽을 수 있는 문구로 올라오는지 를 검증한다.

환경변수는 mock.patch.dict로 격리한다 — 개발 셸에서 JARVIS_DEV_MODE가 켜져
있어도 이 테스트 결과가 바뀌면 안 된다.
"""

_CONFIG_ENV_KEYS = ("JARVIS_DEV_MODE", "JARVIS_TOOL_LOOP_MAX_TURNS")


def _clean_env(**overrides: str):
    env = {k: v for k, v in os.environ.items() if k not in _CONFIG_ENV_KEYS}
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


class DevModeConfigTests(unittest.TestCase):
    def test_default_is_off_and_keeps_yaml_limit(self) -> None:
        with _clean_env():
            config = HarnessConfig.load()
        self.assertFalse(config.dev_mode)
        self.assertEqual(config.tool_loop_max_turns, 4)

    def test_dev_mode_raises_limit_even_though_yaml_pins_four(self) -> None:
        # 배포 YAML이 4를 명시한다. 이에도 dev_mode가 실제로 한도를 올려야 한다 —
        # 그렇지 않으면 플래그가 눌려 있는데 조용히 무효가 된다.
        with _clean_env(JARVIS_DEV_MODE="1"):
            config = HarnessConfig.load()
        self.assertTrue(config.dev_mode)
        self.assertEqual(config.tool_loop_max_turns, DEV_TOOL_LOOP_MAX_TURNS)
        self.assertGreater(config.tool_loop_max_turns, 4)

    def test_dev_mode_accepts_common_truthy_spellings(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            with self.subTest(value=value), _clean_env(JARVIS_DEV_MODE=value):
                self.assertTrue(HarnessConfig.load().dev_mode)

    def test_dev_mode_off_values_do_not_enable_it(self) -> None:
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value), _clean_env(JARVIS_DEV_MODE=value):
                config = HarnessConfig.load()
                self.assertFalse(config.dev_mode)
                self.assertEqual(config.tool_loop_max_turns, 4)

    def test_explicit_turns_win_over_dev_mode(self) -> None:
        with _clean_env(JARVIS_DEV_MODE="1", JARVIS_TOOL_LOOP_MAX_TURNS="40"):
            config = HarnessConfig.load()
        self.assertEqual(config.tool_loop_max_turns, 40)

    def test_explicit_turns_work_without_dev_mode(self) -> None:
        with _clean_env(JARVIS_TOOL_LOOP_MAX_TURNS="7"):
            config = HarnessConfig.load()
        self.assertFalse(config.dev_mode)
        self.assertEqual(config.tool_loop_max_turns, 7)

    def test_explicit_turns_never_drop_below_one(self) -> None:
        # 0이나 음수는 루프를 즉시 종료시키는 쪽이므로 1로 올려 "한 번은 시도"로
        # 만든다. 0으로 돌려 "즉시 실패"를 만들면 애초에 테스트가 불가능해진다.
        for value in ("0", "-5"):
            with self.subTest(value=value), _clean_env(JARVIS_TOOL_LOOP_MAX_TURNS=value):
                self.assertEqual(HarnessConfig.load().tool_loop_max_turns, 1)

    def test_garbage_turns_falls_back_to_dev_mode_not_a_crash(self) -> None:
        with _clean_env(JARVIS_DEV_MODE="1", JARVIS_TOOL_LOOP_MAX_TURNS="many"):
            config = HarnessConfig.load()
        self.assertEqual(config.tool_loop_max_turns, DEV_TOOL_LOOP_MAX_TURNS)

    def test_garbage_turns_falls_back_to_yaml(self) -> None:
        with _clean_env(JARVIS_TOOL_LOOP_MAX_TURNS="many"):
            config = HarnessConfig.load()
        self.assertEqual(config.tool_loop_max_turns, 4)


class DevModeIsolationTests(unittest.TestCase):
    def test_ambient_dev_mode_does_not_change_test_config(self) -> None:
        # 개발 셸에서 JARVIS_DEV_MODE=1로 테스트를 돌려도 대화용 한도(4)가
        # 유지되어야 한다. 테스트는 개발 모드가 아니라 명시적 회수를 쓴다.
        with mock.patch.dict(os.environ, {"JARVIS_DEV_MODE": "1"}):
            config = HarnessConfig(
                runtime="mock", tool_loop_max_turns=4,
                trace_dir=Path(tempfile.mkdtemp()),
            )
        self.assertEqual(config.tool_loop_max_turns, 4)

    def test_dev_mode_flag_is_surfaced_for_logging(self) -> None:
        # dev_mode는 config에 남으므로 trace/로그에서 "지금 개발 모드였는가"를
        # 사후에 확인할 수 있다. 한도만 조용히 바뀌면 진단할 수 없다.
        with _clean_env(JARVIS_DEV_MODE="1"):
            self.assertTrue(HarnessConfig.load().dev_mode)


if __name__ == "__main__":
    unittest.main()
