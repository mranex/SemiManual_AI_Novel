"""Helper seed + harness AppTest cho test UI chương (T21) và revision/recovery (T22).

File này **không** phải test (không khớp `test_*.py`). Nó gom ba nhóm helper:

1. **Seed project** trong `tmp_path` theo cách của `tests/conftest.py` +
   `t15_t16_support.py`: foundation/plan accepted, Skeleton chương 1–3 accepted,
   có thể seed prose + Human Review và chạy finalize/reconcile bằng **service thật**
   (không copy state giả).
2. **Payload JSON** cho `review.v1`, `reconcile.v1`, `retcon_impact.v1`.
3. **Harness AppTest**: render đúng một page qua `novel_ai.ui.layout.render_workspace`
   với `FakeLLMClient` script sẵn (kể cả kịch bản stream đứt), cùng helper
   fingerprint cây project / fingerprint canon.

Mọi thứ chạy offline; không gọi mạng, không dùng `projects/` thật.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from novel_ai.core import lifecycle, storage
from novel_ai.core.models import (
    ArtifactRevision,
    ArtifactStatus,
    ChapterMetadata,
    ChapterStatus,
    DependencyPin,
    HumanReviewRecord,
    PayloadSource,
    ProseRevision,
    SkeletonPayload,
    SourceType,
    ValidationResult,
    ValidationState,
)
from novel_ai.core.project import Project
from novel_ai.services import reconcile as reconcile_service
from novel_ai.ui import layout
from t15_t16_support import (  # noqa: F401 - dùng lại seed/payload của T15/T16
    SKELETON_CH1_REGENERATED,
    SKELETON_CH2_GENERATED,
    seed_project,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-09-19T10:00:00+07:00"

#: Prose complete dùng cho Writer draft (hai đoạn, không phải thông báo lỗi).
COMPLETE_PROSE = (
    "Sở Dương chống tay ngồi dậy. Cái nồi nằm ngay bên cạnh, lớp rỉ xốp bong ra "
    "dưới ngón tay anh.\n\n"
    "Anh nhặt hòn đá gõ vào thành nồi. Tiếng chạm khô và rõ, nhưng mặt nồi vẫn phẳng."
)

#: Bản user sửa sau review (dùng cho Save Draft ở workspace Review).
EDITED_PROSE = (
    "Sở Dương chống tay ngồi dậy. Cái nồi nằm ngay bên cạnh, lớp rỉ xốp bong ra "
    "dưới ngón tay anh.\n\n"
    "Anh nhặt hòn đá gõ vào thành nồi. Tiếng chạm khô và rõ, nhưng mặt nồi vẫn "
    "phẳng lạ thường. Anh ngồi im nghe nhịp thở của mình chậm lại."
)

#: Quote nguyên văn của `EDITED_PROSE` (evidence của AI Review phải verbatim).
EDITED_QUOTE = "Anh ngồi im nghe nhịp thở của mình chậm lại."


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def fingerprint_tree(root: Path) -> dict[str, str]:
    """Fingerprint mọi file trong project (dùng để assert rerun không ghi gì)."""
    return {
        str(path.relative_to(root)).replace("\\", "/"): storage.file_fingerprint(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


#: Phần state được coi là "canon/derived state" cho test impact report.
CANON_GLOBS: tuple[str, ...] = (
    "state/current_timeline.json",
    "state/relationships.json",
    "chapters/*/chapter.json",
    "chapters/*/final/*.md",
    "architect/*.json",
    "plans/*.json",
    "idea/*",
)


def fingerprint_canon(project: Project) -> dict[str, str]:
    """Fingerprint canon/derived state (không gồm raw/candidate/history)."""
    result: dict[str, str] = {}
    for pattern in CANON_GLOBS:
        for path in sorted(project.root.glob(pattern)):
            if path.is_file():
                relpath = str(path.relative_to(project.root)).replace("\\", "/")
                result[relpath] = storage.file_fingerprint(path)
    return result


# ---------------------------------------------------------------------------
# Payload JSON cho prompt v1
# ---------------------------------------------------------------------------


def review_report_json(
    *,
    chapter_id: str,
    prose_revision: int,
    quote: str | None = EDITED_QUOTE,
) -> str:
    """Report hợp lệ cho `review.v1` (quote phải là nguyên văn prose đang review)."""
    return json.dumps(
        {
            "chapter_id": chapter_id,
            "prose_revision": prose_revision,
            "issues": [
                {
                    "issue_id": "issue_0001",
                    "severity": "minor",
                    "category": "style_issue",
                    "source": {
                        "authority_kind": "skeleton",
                        "artifact_id": f"skeleton_{chapter_id}",
                        "revision": 1,
                        "field_path": "sections[0].instruction",
                    },
                    "evidence": {"prose_revision": prose_revision, "quote": quote},
                    "message": "Nhịp đoạn kết hơi chùng so với chỉ dẫn Skeleton.",
                    "suggested_action": "Siết lại một câu trước khi kết đoạn.",
                    "status": "open",
                }
            ],
            "summary": "Một ghi chú nhỏ về nhịp; không có mâu thuẫn cứng.",
        },
        ensure_ascii=False,
    )


def proposal_json(
    *,
    chapter_id: str,
    chapter_number: int,
    prose_revision: int,
    markdown_ref: str,
    timeline_status: str = "",
) -> str:
    """Proposal hợp lệ cho `reconcile.v1` (không relationship update ⇒ không cần ID)."""
    return json.dumps(
        {
            "chapter_id": chapter_id,
            "chapter_number": chapter_number,
            "source_final_candidate": {
                "prose_revision": prose_revision,
                "markdown_ref": markdown_ref,
            },
            "timeline": {
                "time": f"Ngày thứ {chapter_number}",
                "location": f"Vách đá chương {chapter_number}",
                "status": timeline_status
                or f"Sở Dương sống sót tới cuối chương {chapter_number}.",
            },
            "relationship_updates": [],
            "notes": [f"Trích xuất từ final manuscript chương {chapter_number}."],
        },
        ensure_ascii=False,
    )


def impact_report_json(
    *,
    item_kind: str,
    item_id: str,
    affected: list[dict[str, Any]] | None = None,
) -> str:
    """Report hợp lệ cho `retcon_impact.v1`."""
    return json.dumps(
        {
            "source_change": {
                "item_kind": item_kind,
                "item_id": item_id,
                "from_revision": 1,
                "to_candidate_revision": None,
            },
            "affected_items": affected
            or [
                {
                    "item_kind": "chapter",
                    "item_id": "ch_0002",
                    "reason": "State chương 2 dựa trên bản final cũ của chương 1.",
                    "severity": "major",
                    "suggested_action": "reconcile_downstream chương 2 theo thứ tự.",
                }
            ],
            "risk_summary": "Chỉ state derived bị ảnh hưởng; final prose chương sau không đổi.",
            "suggested_actions": ["Rebuild state downstream lần lượt từ chương 2."],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Seed prose + finalize/reconcile bằng service thật
# ---------------------------------------------------------------------------


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def accepted_artifact(artifact_id: str, artifact_type: str, payload: Any):
    """Envelope accepted tối thiểu cho test (không cần prompt/LLM)."""
    return lifecycle.new_artifact(artifact_type, artifact_id, now=NOW).model_copy(
        update={
            "status": ArtifactStatus.accepted,
            "accepted_revision": ArtifactRevision(
                revision=1,
                payload=payload,
                payload_source=PayloadSource(source_type=SourceType.user),
                dependency_pins=[],
                validation=_valid(),
                created_at=NOW,
                accepted_at=NOW,
                accepted_by="user",
            ),
        }
    )


def seed_skeleton(
    project: Project, *, chapter_id: str, chapter_number: int, instruction: str = ""
) -> None:
    """Accepted Skeleton cho một chapter (đủ điều kiện Writer/finalize)."""
    payload = SkeletonPayload(
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        global_constraints=["Không giải thích nguồn gốc cái nồi."],
        sections=[
            {
                "section_id": "section_0001",
                "index": 1,
                "type": "action",
                "instruction": instruction or f"Viết cảnh mở đầu chương {chapter_number}.",
                "purpose": "Nối tiếp hệ quả chương trước.",
                "purpose_visibility": "writer_safe",
            }
        ],
    )
    storage.save_artifact(
        project,
        accepted_artifact(f"skeleton_{chapter_id}", "skeleton", payload.model_dump(mode="json")),
        operation_id=f"op_seed_skeleton_{chapter_id}",
    )


def add_third_chapter(project: Project) -> None:
    """Thêm chương 3 (skeleton accepted + chapter metadata) cho test retcon 3 chương."""
    seed_skeleton(
        project,
        chapter_id="ch_0003",
        chapter_number=3,
        instruction="Sở Dương chạm mặt người giữ ánh lửa lần đầu.",
    )
    storage.save_chapter(
        project,
        ChapterMetadata(
            chapter_id="ch_0003",
            chapter_number=3,
            title="Tro tàn dưới vực",
            status=ChapterStatus.skeleton_ready,
            previous_chapter_id="ch_0002",
            short_plan_pin=DependencyPin(
                artifact_id="short_plan",
                revision=1,
                scope="short_plan",
                chapter_id="ch_0003",
            ),
            skeleton_pin=DependencyPin(
                artifact_id="skeleton_ch_0003",
                revision=1,
                scope="skeleton",
                chapter_id="ch_0003",
            ),
        ),
        operation_id="op_seed_ch_0003",
    )


def _draft_markdown(chapter_number: int, revision: int) -> str:
    return (
        f"# Chương {chapter_number}\n\n"
        f"Sở Dương bước tiếp trên sườn núi (bản r{revision}). "
        "Cái nồi sắt đen vẫn không móp khi gõ vào đá.\n"
    )


def seed_reviewed_draft(
    project: Project, *, chapter_id: str, revision: int = 1, reviewed: bool = True
) -> str:
    """Seed prose revision complete + Human Review hợp lệ; trả `markdown_ref`."""
    chapter = storage.load_chapter(project, chapter_id)
    assert chapter is not None, f"thiếu chapter {chapter_id}"
    relpath = f"chapters/{chapter_id}/drafts/draft_r{revision:04d}.md"
    storage.write_text_atomic(
        project.root / relpath,
        _draft_markdown(chapter.chapter_number, revision),
        operation_id=f"op_seed_draft_{chapter_id}_r{revision}",
    )
    updated = chapter.model_copy(
        update={
            "status": ChapterStatus.review_required,
            "drafts": [
                *chapter.drafts,
                ProseRevision(
                    revision=revision,
                    markdown_ref=relpath,
                    source_type=SourceType.llm,
                    is_complete=True,
                    created_at=NOW,
                    dependency_pins=[
                        DependencyPin(
                            artifact_id=f"skeleton_{chapter_id}", revision=1, scope="skeleton"
                        )
                    ],
                ),
            ],
            "current_draft_revision": revision,
            "human_review": (
                HumanReviewRecord(
                    prose_revision=revision,
                    reviewed_at=NOW,
                    reviewed_by="user",
                    notes="Seed test: đã review.",
                    valid_for_current_revision=True,
                )
                if reviewed
                else None
            ),
        }
    )
    storage.save_chapter(project, updated, operation_id=f"op_seed_draft_{chapter_id}")
    return relpath


def finalize_and_reconcile(project: Project, *, chapter_id: str) -> None:
    """Chạy đúng luồng service: finalize → generate proposal → accept (không qua UI)."""
    from novel_ai.core.llm import FakeLLMClient

    reconcile_service.finalize_chapter(project, chapter_id=chapter_id, now=NOW)
    chapter = storage.load_chapter(project, chapter_id)
    assert chapter is not None and chapter.final_candidate is not None
    proposal = proposal_json(
        chapter_id=chapter_id,
        chapter_number=chapter.chapter_number,
        prose_revision=chapter.final_candidate.prose_revision,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    generated = reconcile_service.generate_reconciliation(
        project, client=FakeLLMClient([proposal]), chapter_id=chapter_id, now=NOW
    )
    assert generated.data.get("reconciliation_status") == "draft", generated.data
    accepted = reconcile_service.accept_reconciliation(project, chapter_id=chapter_id, now=NOW)
    assert accepted.data.get("status") == "final_reconciled", accepted.data


def seed_fully_finalized(
    project: Project, *, chapters: tuple[str, ...] = ("ch_0001", "ch_0002", "ch_0003")
) -> None:
    """Seed các chapter đã `final_reconciled` bằng chính service (state thật)."""
    for chapter_id in chapters:
        seed_reviewed_draft(project, chapter_id=chapter_id, revision=1)
        finalize_and_reconcile(project, chapter_id=chapter_id)


def seed_chapter_project(tmp_path: Path, document: dict[str, Any], *, chapters: int = 2) -> Project:
    """Project 2–3 chương: foundation/plan accepted, Skeleton accepted, chưa có prose."""
    project = seed_project(
        tmp_path,
        document,
        chapter_one_status=ChapterStatus.skeleton_ready,
        chapter_two_status=ChapterStatus.skeleton_ready,
    )
    if chapters >= 3:
        add_third_chapter(project)
    return project


# ---------------------------------------------------------------------------
# Harness AppTest
# ---------------------------------------------------------------------------

_HARNESS = '''\
"""Harness tạm cho AppTest T21/T22: render đúng một page với client script sẵn.

`FakeLLMClient` được giữ trong `st.session_state` để mọi rerun của cùng một AppTest
dùng **đúng một** client; nhờ vậy test đếm được số lần gọi LLM.
"""

import json
import os

import streamlit as st

from novel_ai.config import load_config
from novel_ai.core.llm import STREAM_INTERRUPTED, FakeLLMClient
from novel_ai.core.project import Project
from novel_ai.ui import layout


def _stream_item(item):
    if item == "interrupted":
        return STREAM_INTERRUPTED
    return str(item)


config = load_config(
    env={{"NOVEL_AI_PROJECTS_ROOT": os.environ["NOVEL_AI_TEST_PROJECTS_ROOT"]}},
    repo_root={repo_root!r},
)
_project = Project.open(config.projects_root, os.environ["NOVEL_AI_TEST_SLUG"])
CLIENT = st.session_state.get("__t21_client")
if CLIENT is None:
    CLIENT = FakeLLMClient(json.loads(os.environ.get("NOVEL_AI_TEST_RESPONSES") or "[]"))
    script = json.loads(os.environ.get("NOVEL_AI_TEST_STREAM") or "[]")
    if script:
        CLIENT.queue_stream(*[_stream_item(item) for item in script])
    st.session_state["__t21_client"] = CLIENT
ctx = layout.AppContext(
    app_config=config,
    project=_project,
    registry=layout.load_prompt_registry(config)[0],
    workspace=os.environ["NOVEL_AI_TEST_WORKSPACE"],
    llm_client=CLIENT,
    llm_error=None,
)
layout.render_workspace(ctx)
'''


def projects_root_for(tmp_path: Path) -> Path:
    """Data root tạm cho một test (`tmp_path/projects`)."""
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_apptest(
    tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """Dựng factory AppTest cho một workspace (dùng trong fixture của test file).

    Factory nhận `(project, workspace, responses=None, stream=None)` và trả `AppTest`
    đã `run()` một lần. `FakeLLMClient` được giữ trong session state của AppTest để
    mọi rerun dùng đúng một client (đếm được số lần gọi LLM).
    """
    harness = tmp_path / "t21_harness.py"
    harness.write_text(_HARNESS.format(repo_root=str(REPO_ROOT)), encoding="utf-8")
    monkeypatch.setenv("NOVEL_AI_TEST_PROJECTS_ROOT", str(projects_root))

    def build(
        project: Project,
        workspace: str,
        responses: list[Any] | None = None,
        *,
        stream: list[Any] | None = None,
    ) -> AppTest:
        monkeypatch.setenv("NOVEL_AI_TEST_SLUG", project.slug)
        monkeypatch.setenv("NOVEL_AI_TEST_WORKSPACE", workspace)
        monkeypatch.setenv(
            "NOVEL_AI_TEST_RESPONSES", json.dumps(responses or [], ensure_ascii=False)
        )
        monkeypatch.setenv("NOVEL_AI_TEST_STREAM", json.dumps(stream or [], ensure_ascii=False))
        at = AppTest.from_file(str(harness))
        at.session_state["novel_ai_open_project"] = project.slug
        at.session_state["novel_ai_workspace_nav"] = layout.label_for_workspace(workspace)
        at.run(timeout=180)
        return at

    return build


def client_of(at: AppTest) -> Any:
    """`FakeLLMClient` của lần run gần nhất (harness gán vào `__main__.CLIENT`)."""
    import sys

    return getattr(sys.modules["__main__"], "CLIENT")


def click(at: AppTest, key: str) -> None:
    """Bấm một button theo key rồi chạy lại."""
    at.button(key=key).click()
    at.run(timeout=180)


def button_disabled(at: AppTest, key: str) -> bool:
    """True nếu button đang `disabled` (AppTest 1.41 chưa expose thuộc tính này)."""
    button = at.button(key=key)
    proto = getattr(button, "proto", None)
    return bool(getattr(proto, "disabled", False))


def rendered(at: AppTest) -> str:
    """Toàn bộ text hiển thị (markdown/caption/info/warning/error/success/code/text)."""
    parts: list[str] = []
    for name in (
        "markdown",
        "caption",
        "info",
        "warning",
        "error",
        "success",
        "code",
        "text",
        "subheader",
    ):
        elements = getattr(at, name, None)
        if elements is None:  # pragma: no cover - phụ thuộc phiên bản AppTest
            continue
        for element in elements:
            value = getattr(element, "value", None)
            if value is not None:
                parts.append(str(value))
    return "\n".join(parts)
