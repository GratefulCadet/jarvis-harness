from __future__ import annotations

import argparse

from harness.client import HarnessClient
from harness.config import HarnessConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harness Slice 0 smoke — 로컬 모델 1회 왕복.")
    parser.add_argument("--config", type=str, default=None, help="config YAML 경로")
    parser.add_argument(
        "--text",
        type=str,
        default="한 문장으로 인사하고, 지금 네가 무엇인지 설명해줘.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="실제 Ollama 대신 mock adapter 사용 (오프라인 검증)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    config = HarnessConfig.load(args.config)
    if args.mock:
        config.runtime = "mock"
        config.api_format = "native_chat"

    client = HarnessClient(config)
    response = client.chat(
        [{"role": "user", "content": args.text}],
        metadata={"source": "smoke"},
    )

    print(f"[runtime={config.runtime} model={config.model}]")
    print(f"응답: {response.content}")
    print(f"trace_id: {response.trace_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
