from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.adapters.base import create_adapter
from harness.client import HarnessClient
from harness.config import HarnessConfig, PROJECT_ROOT


def _config(trace_dir: Path) -> HarnessConfig:
    config = HarnessConfig(runtime="mock", trace_dir=trace_dir)
    return config


class ConfigTests(unittest.TestCase):
    def test_default_config_loads(self) -> None:
        config = HarnessConfig.load()
        self.assertEqual(config.runtime, "ollama")
        self.assertEqual(config.model, "local-jarvis-qwen3:8b")
        self.assertEqual(config.api_format, "native_chat")
        self.assertTrue(config.trace_dir.is_absolute())

    def test_default_config_exists_at_documented_path(self) -> None:
        self.assertTrue((PROJECT_ROOT / "configs" / "harness.yaml").exists())


class MockRoundtripTests(unittest.TestCase):
    def test_mock_chat_returns_and_records_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _config(Path(temp) / "traces")
            client = HarnessClient(config)

            response = client.chat([{"role": "user", "content": "안녕"}])

            self.assertTrue(response.content.startswith("[mock:"))
            self.assertIsNotNone(response.trace_id)

            written = list((Path(temp) / "traces").glob("*.json"))
            self.assertEqual(len(written), 1)
            entry = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(entry["trace_id"], response.trace_id)
            self.assertEqual(entry["request"]["messages"][0]["content"], "안녕")
            self.assertIn("e2e", entry["latency_ms"])
            self.assertEqual(entry["runtime"], "mock")

    def test_create_adapter_rejects_unknown_key(self) -> None:
        config = HarnessConfig(runtime="nope", api_format="nope")
        with self.assertRaises(ValueError):
            create_adapter(config)

    def test_trace_disabled_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _config(Path(temp) / "traces")
            config.trace_enabled = False
            client = HarnessClient(config)
            response = client.chat([{"role": "user", "content": "hi"}])
            self.assertIsNotNone(response.content)
            self.assertEqual(list((Path(temp) / "traces").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
