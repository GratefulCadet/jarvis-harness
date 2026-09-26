from __future__ import annotations

import dataclasses
import os
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
    # Slice 2: chat_with_tools tool-call 루프의 최대 모델 호출 횟수 (§8.3-3 중단 기준)
    tool_loop_max_turns: int = 4
    # Slice 1: get_project_context 문맥 원천 (local-jarvis memory/). None이면 tool 미등록 안내.
    memory_dir: Path | None = None
    # Slice 4: create_task 저장 파일 (기본: <memory_dir>/tasks.md). None이면 memory_dir에서 유도.
    task_file: Path | None = None
    pages_dir: Path | None = None  # Knowledge Markdown pages 루트 (None = <memory_dir>/pages)
    # Context Discovery (§7): 승인된 파일 루트 — dict(name→path) 또는 'name=path,...'.
    # 기본 없음. JARVIS_FILE_ROOTS 환경변수가 우선한다.
    file_roots: dict[str, str] | str | None = None

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
        tools = raw.pop("tools", {}) or {}

        flat: dict[str, Any] = dict(raw)
        flat.update(
            {key: value for key, value in generation.items() if key in ("temperature", "num_predict")}
        )
        flat["trace_enabled"] = trace.get("enabled", True)
        flat["trace_dir"] = trace.get("dir", "data/traces")
        # 우선순위: 환경변수 > YAML > 기본값 (YAML 주석의 "재정의 가능"과 일치)
        flat["memory_dir"] = (
            os.environ.get("HARNESS_MEMORY_DIR")
            or tools.get("memory_dir")
            or None
        )
        flat["task_file"] = (
            os.environ.get("HARNESS_TASK_FILE")
            or tools.get("task_file")
            or None
        )
        flat["pages_dir"] = (
            os.environ.get("HARNESS_PAGES_DIR")
            or tools.get("pages_dir")
            or None
        )
        flat["file_roots"] = (
            os.environ.get("JARVIS_FILE_ROOTS")
            or tools.get("file_roots")
            or None
        )
        # 모델 엔드포인트도 같은 방식으로 재정의한다. 앱(GUI) 자동화 검증이
        # 실제 로컬 모델 서버를 건드리지 않고 격리된 서버를 볼 수 있게 하는
        # 것이 목적이며, 미설정 시 YAML/기본값을 그대로 쓴다.
        # (값이 없으면 flat에 None을 넣지 않는다 — dataclass 기본값을 지우지 않기 위해)
        for env_key, field_name in (
            ("HARNESS_RUNTIME", "runtime"),
            ("HARNESS_API_FORMAT", "api_format"),
            ("HARNESS_BASE_URL", "base_url"),
            ("HARNESS_MODEL", "model"),
        ):
            override = os.environ.get(env_key)
            if override:
                flat[field_name] = override

        known = {dataclass_field.name for dataclass_field in dataclasses.fields(cls)}
        values = {key: value for key, value in flat.items() if key in known}
        config = cls(**values)

        trace_dir = Path(config.trace_dir)
        if not trace_dir.is_absolute():
            config.trace_dir = PROJECT_ROOT / trace_dir
        if config.memory_dir:
            memory_dir = Path(config.memory_dir)
            config.memory_dir = memory_dir if memory_dir.is_absolute() else PROJECT_ROOT / memory_dir
        if config.task_file:
            task_file = Path(config.task_file)
            config.task_file = task_file if task_file.is_absolute() else PROJECT_ROOT / task_file
        if config.pages_dir:
            pages_dir = Path(config.pages_dir)
            config.pages_dir = pages_dir if pages_dir.is_absolute() else PROJECT_ROOT / pages_dir
        return config
