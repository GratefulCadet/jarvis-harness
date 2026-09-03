from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "harness.yaml"


@dataclass
class HarnessConfig:
    """Runtime / model / trace 설정. docs/harness-design.md §7.1 참고.

    runtime: ollama | llama_cpp | openai | mock
    api_format: native_chat (ollama /api/chat) | openai_compat (/v1/chat/completions)
    """

    runtime: str = "ollama"
    model: str = "local-jarvis-qwen3:8b"
    api_format: str = "native_chat"
    base_url: str = "http://127.0.0.1:11434"
    profile: str = "desktop"
    timeout_s: float = 180.0
    temperature: float = 0.6
    num_predict: int = 240
    trace_enabled: bool = True
    trace_dir: Path = field(default_factory=lambda: Path("data/traces"))
    prompt_version: str = "v0"

    @classmethod
    def load(cls, path: Path | str | None = None) -> "HarnessConfig":
        config_path = Path(path) if path else DEFAULT_CONFIG_PATH
        raw: dict[str, Any] = {}
        if config_path.exists():
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded

        trace = raw.pop("trace", {}) or {}
        generation = raw.pop("generation", {}) or {}

        flat: dict[str, Any] = dict(raw)
        flat.update(
            {key: value for key, value in generation.items() if key in ("temperature", "num_predict")}
        )
        flat["trace_enabled"] = trace.get("enabled", True)
        flat["trace_dir"] = trace.get("dir", "data/traces")

        known = {dataclass_field.name for dataclass_field in dataclasses.fields(cls)}
        values = {key: value for key, value in flat.items() if key in known}
        config = cls(**values)

        trace_dir = Path(config.trace_dir)
        if not trace_dir.is_absolute():
            config.trace_dir = PROJECT_ROOT / trace_dir
        return config
