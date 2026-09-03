from __future__ import annotations

from typing import Callable, TypeAlias

from harness.config import HarnessConfig
from harness.models import RuntimeAdapter

AdapterFactory: TypeAlias = Callable[[HarnessConfig], RuntimeAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}


def register_adapter(key: str) -> Callable[[AdapterFactory], AdapterFactory]:
    def decorator(factory: AdapterFactory) -> AdapterFactory:
        _REGISTRY[key] = factory
        return factory

    return decorator


def create_adapter(config: HarnessConfig) -> RuntimeAdapter:
    key = f"{config.runtime}:{config.api_format}"
    factory = _REGISTRY.get(key)
    if factory is None:
        raise ValueError(
            f"등록되지 않은 adapter: {key!r} (runtime={config.runtime!r}, api_format={config.api_format!r}). "
            f"사용 가능: {sorted(_REGISTRY)}"
        )
    return factory(config)


# 하위 adapter 모듈을 import해 registry를 채운다.
from harness.adapters import mock, ollama, openai_compat  # noqa: E402,F401
