from __future__ import annotations

import argparse

from harness.client import HarnessClient
from harness.config import HarnessConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harness Slice 1 check — get_project_context를 실제 JARVIS memory로 검증 (LLM 호출 없음)."
    )
    parser.add_argument("--config", type=str, default=None, help="config YAML 경로")
    parser.add_argument(
        "--project",
        type=str,
        default="local-jarvis",
        help="조회할 프로젝트 id (기본: local-jarvis)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = HarnessConfig.load(args.config)
    client = HarnessClient(config)

    print(f"[memory_dir={config.memory_dir}]")
    print("등록된 tool:", ", ".join(sorted(tool.name for tool in client.tools.schemas())))

    result = client.execute_tool("get_project_context", {"project_id": args.project})
    if not result.ok:
        print(f"실패: {result.error}")
        return 1

    data = result.data
    print(f"ok — project_id={data['project_id']!r} title={data['title']!r}")
    for file in data["files"]:
        preview = file["content"].strip().splitlines()
        first = preview[0] if preview else ""
        print(f"  [{file['kind']:7}] {file['name']} ({file['chars']}자) — {first}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
