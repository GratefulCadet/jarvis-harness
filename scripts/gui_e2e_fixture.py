"""
GUI end-to-end 검증용 격리 상태 fixture (Electron 실제 앱 구동용).

왜 Python인가: canonical 상태 파일(JSON 레지스트리·projects.md)의 형식은
하네스가 이미 파싱한다. 테스트가 손으로 JSON을 쓰면 파서가 바뀌면 조용히
깨진다 — 그래서 실제 writer 클래스(WorkspaceRootRegistry, ProjectWorkspaces,
MemoryContextReader 규약)로 만든다. 형식은 저절로 맞는다.

만드는 것:
  1) memory 디렉터리 + projects.md (## Active 아래 프로젝트 1개)
  2) 실제 폴더 1개를 WorkspaceRoot로 등록 (device_path는 임시 폴더)
  3) 그 프로젝트의 primary workspace를 그 root로 지정
  4) 폴더 안에 실제 파일 1개 (Active File이 될 대상)

앱은 이 상태에서 "WORKSPACE / FILES"에 파일을 보여주고, 사용자가 그것을 열면
Active File이 된다. 그 뒤의 링크 흐름은 GUI 테스트가 담당한다.

사용:
  python -m scripts.gui_e2e_fixture --state-dir <dir> --workspace-dir <dir> \
      --project-id jarvis-app

출력(JSON, stdout): {"project_id", "root_id", "file_path", "state_dir", ...}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.tools.project_workspaces import (
    ProjectWorkspaces,
    default_project_workspaces_file,
)
from harness.tools.workspace_roots import (
    WorkspaceRoots,
    default_workspace_roots_file,
)

PROJECTS_MD_TEMPLATE = """# Projects

## Active

- {project_id}: {title}

## Notes

(테스트 fixture — 실제 사용자 state가 아니다)
"""

SAMPLE_FILE = """# Experiment notes

GUI end-to-end 검증이 opened target.
이 파일을 Active File로 연 뒤, 만든 task에 연결하면
resume briefing이 이 파일을 관련 자료로 실어야 한다.
"""


def _ensure_state_files(state_dir: Path) -> None:
    """STATE_FILES 관례대로 빈 파일을 먼저 만들어 둔다.

    bridge는 상태 파일이 없으면 scratch→사용자 state 마이그레이션을 시도하므로,
    존재해도 되는 빈 파일을 먼저 만들어 그 경로를 건드리지 않게 한다.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "projects.md",
        "tasks.md",
        "workspace_roots.json",
        "project_workspaces.json",
        "resource_links.json",
        "file_refs.json",
        "page_identity.json",
    ):
        path = state_dir / name
        if not path.exists():
            path.write_text("" if name.endswith(".json") else "", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--project-id", default="jarvis-app")
    parser.add_argument("--title", default="GUI E2E 검증 프로젝트")
    parser.add_argument("--file-name", default="experiment-notes.md")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir).resolve()
    workspace_dir = Path(args.workspace_dir).resolve()

    _ensure_state_files(state_dir)

    # 1) projects.md — MemoryContextReader가 읽는 `## Active` 규약.
    (state_dir / "projects.md").write_text(
        PROJECTS_MD_TEMPLATE.format(project_id=args.project_id, title=args.title),
        encoding="utf-8",
    )

    # 2) 실제 폴더를 WorkspaceRoot로 등록 (root_id는 basename slug로 생성된다).
    workspace_dir.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceRoots(
        default_workspace_roots_file(state_dir), memory_dir=state_dir
    )
    registered = registry.register(workspace_dir, display_name="gui-e2e-workspace")
    root_id = registered["root"]["id"]

    # 3) 파일 1개 — 사용자가 열 Active File.
    sample = workspace_dir / args.file_name
    if not sample.exists():
        sample.write_text(SAMPLE_FILE, encoding="utf-8")

    # 4) project → root 관계. 이 단계는 root가 "이 디바이스에 승인된 root"여야
    #    하므로 registry가 만든 root_id를 그대로 넘긴다(경로 금지 계약).
    workspaces = ProjectWorkspaces(
        default_project_workspaces_file(state_dir),
        memory_dir=state_dir,
        roots={root_id: workspace_dir},
    )
    workspaces.set_project_primary_workspace(args.project_id, root_id)

    print(
        json.dumps(
            {
                "state_dir": str(state_dir),
                "workspace_dir": str(workspace_dir),
                "project_id": args.project_id,
                "root_id": root_id,
                "file_name": args.file_name,
                "file_path": str(sample),
                "file_roots_env": f"{root_id}={workspace_dir}",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
