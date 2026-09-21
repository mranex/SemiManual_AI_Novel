"""Helper seed dùng chung cho test T15 (Skeleton/Writer) và T16 (Review/Rewrite).

File này **không** phải test (không khớp `test_*.py` nên pytest không collect).
Nó seed project trong `tmp_path` theo đúng cách của `tests/unit/test_context.py`
(fixture contract T02 `linked_project_valid.json`): hai chương, một secret
author-only và một world rule hiệu lực chương 100 để phát hiện leak. Chapter 1
được đặt ở `skeleton_ready` để Writer mở được, chapter 2 giữ `planned`/
`skeleton_ready` để kiểm tra guard chương trước.

Mọi LLM call trong test đều đi qua `FakeLLMClient` (offline, deterministic).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from novel_ai.core import lifecycle, storage
from novel_ai.core.context import ContextMode, build_writer_context
from novel_ai.core.llm import STREAM_INTERRUPTED, FakeLLMClient, LLMError
from novel_ai.core.models import (
    ArtifactRevision,
    ArtifactStatus,
    ChapterMetadata,
    ChapterStatus,
    CurrentTimelineDocument,
    DependencyPin,
    PayloadSource,
    RelationshipStateDocument,
    RewriteSectionRequest,
    SourceType,
    ValidationResult,
    ValidationState,
)
from novel_ai.core.project import Project
from novel_ai.services import (
    GuardError,
    LLMUnavailableError,
    StaleDependencyError,
    ValidationFailure,
)
from novel_ai.services import reviewer, skeleton, writer

STAMP = "2026-09-19T10:00:00+07:00"

#: Sentinel lấy trực tiếp từ fixture T02 (xem `tests/unit/test_context.py`).
POT_SECRET = "mảnh vật phẩm cổ"
BODY_SECRET = "liên quan tới bí mật của cái nồi"
PURPOSE_SECRET = "Gieo mầm vật phẩm có nguồn gốc bất thường"
LATE_LORE = "Cơ quan phòng chống tội phạm xuyên giới"

COMPLETE_PROSE = (
    "Sở Dương chống tay ngồi dậy. Cái nồi nằm ngay bên cạnh, lớp rỉ xốp bong ra "
    "dưới ngón tay anh.\n\n"
    "Anh nhặt hòn đá gõ vào thành nồi. Tiếng chạm khô và rõ, nhưng mặt nồi vẫn phẳng."
)


# ---------------------------------------------------------------------------
# Seed project
# ---------------------------------------------------------------------------


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def _revision(payload: dict[str, Any], *, revision: int = 1) -> ArtifactRevision:
    return ArtifactRevision(
        revision=revision,
        payload=payload,
        payload_source=PayloadSource(source_type=SourceType.llm, prompt_id="fixture"),
        dependency_pins=[],
        validation=_valid(),
        created_at=STAMP,
        accepted_at=STAMP,
        accepted_by="user",
    )


def _accepted_artifact(artifact_id: str, artifact_type: str, payload: dict[str, Any]):
    return lifecycle.new_artifact(artifact_type, artifact_id).model_copy(
        update={
            "status": ArtifactStatus.accepted,
            "accepted_revision": _revision(payload),
        }
    )


def _pin(artifact_id: str, chapter_id: str, *, revision: int = 1) -> DependencyPin:
    return DependencyPin(
        artifact_id=artifact_id,
        revision=revision,
        scope="skeleton",
        chapter_id=chapter_id,
    )


def _chapter(
    chapter_id: str,
    number: int,
    title: str,
    *,
    status: ChapterStatus,
    previous: str | None,
    skeleton_pin: DependencyPin | None,
) -> ChapterMetadata:
    return ChapterMetadata(
        chapter_id=chapter_id,
        chapter_number=number,
        title=title,
        status=status,
        previous_chapter_id=previous,
        short_plan_pin=DependencyPin(
            artifact_id="short_plan",
            revision=1,
            scope="short_plan",
            chapter_id=chapter_id,
        ),
        skeleton_pin=skeleton_pin,
    )


#: Skeleton chương 2 (dựng thêm như test_context) để test guard chương trước.
SKELETON_CH2_PAYLOAD: dict[str, Any] = {
    "chapter_id": "ch_0002",
    "chapter_number": 2,
    "global_constraints": ["Không giải thích nguồn gốc cái nồi."],
    "sections": [
        {
            "section_id": "section_0001",
            "index": 1,
            "type": "action",
            "instruction": "Sở Dương thoát khỏi vách đá bằng phản xạ nghề nghiệp.",
            "purpose": "Nối tiếp hệ quả chương 1.",
            "purpose_visibility": "writer_safe",
        },
        {
            "section_id": "section_0002",
            "index": 2,
            "type": "foreshadow",
            "instruction": "Nghe động tĩnh của người giữ ánh lửa.",
            "purpose": PURPOSE_SECRET,
            "purpose_visibility": "planner_only",
            "author_only_notes": ["Không gửi truth của fs_0001."],
        },
    ],
}


def seed_project(
    tmp_path: Path,
    document: dict[str, Any],
    *,
    chapter_one_status: ChapterStatus = ChapterStatus.skeleton_ready,
    chapter_two_status: ChapterStatus = ChapterStatus.skeleton_ready,
    chapter_two_skeleton: bool = True,
) -> Project:
    """Project hai chương có foundation/plan accepted và skeleton chương 1 accepted."""
    projects_root = tmp_path / "projects"
    project = Project.create(projects_root, "Nồi Canh Bên Đường")

    storage.write_text_atomic(
        project.paths.base_idea_md,
        "Sở Dương tỉnh dậy ở Thương Ngô giới với cái nồi sắt đen.",
        operation_id="op_seed_base_idea",
    )
    storage.write_json_atomic(
        project.paths.base_idea_meta_json,
        document["co_create"]["base_idea"],
        operation_id="op_seed_base_idea",
    )
    storage.write_json_atomic(
        project.paths.co_create_json,
        document["co_create"],
        operation_id="op_seed_co_create",
    )

    for key in ("premise", "characters", "world_rules", "foreshadow", "long_plan", "short_plan"):
        raw = document["artifacts"][key]
        storage.save_artifact(
            project,
            _accepted_artifact(key, raw["artifact_type"], raw["accepted_revision"]["payload"]),
            operation_id=f"op_seed_{key}",
        )

    raw_skeleton = document["artifacts"]["skeleton_ch_0001"]
    storage.save_artifact(
        project,
        _accepted_artifact(
            "skeleton_ch_0001", "skeleton", raw_skeleton["accepted_revision"]["payload"]
        ),
        operation_id="op_seed_skeleton_1",
    )
    if chapter_two_skeleton:
        storage.save_artifact(
            project,
            _accepted_artifact("skeleton_ch_0002", "skeleton", SKELETON_CH2_PAYLOAD),
            operation_id="op_seed_skeleton_2",
        )

    storage.save_chapter(
        project,
        _chapter(
            "ch_0001",
            1,
            "Đường vắng",
            status=chapter_one_status,
            previous=None,
            skeleton_pin=_pin("skeleton_ch_0001", "ch_0001"),
        ),
        operation_id="op_seed_ch_0001",
    )
    storage.save_chapter(
        project,
        _chapter(
            "ch_0002",
            2,
            "Ánh lửa dưới thung lũng",
            status=chapter_two_status if chapter_two_skeleton else ChapterStatus.planned,
            previous="ch_0001",
            skeleton_pin=_pin("skeleton_ch_0002", "ch_0002")
            if chapter_two_skeleton
            else None,
        ),
        operation_id="op_seed_ch_0002",
    )

    storage.save_timeline(
        project,
        CurrentTimelineDocument.model_validate(document["state"]["current_timeline"]),
        operation_id="op_seed_timeline",
    )
    storage.save_relationships(
        project,
        RelationshipStateDocument.model_validate(document["state"]["relationships"]),
        operation_id="op_seed_relationships",
    )
    return project


# ---------------------------------------------------------------------------
# Payload LLM dùng lại nhiều test
# ---------------------------------------------------------------------------


#: Skeleton chương 1 regenerate: ID tạm cho section mới để test mapping.
SKELETON_CH1_REGENERATED: dict[str, Any] = {
    "chapter_id": "ch_0001",
    "chapter_number": 1,
    "global_constraints": ["Không giải thích nguồn gốc cái nồi."],
    "writer_context_policy": {"include_author_only": False, "include_future_plan": False},
    "sections": [
        {
            "section_id": "tmp_section_1",
            "index": 1,
            "type": "action",
            "instruction": "Sở Dương cấp cứu người bị tai nạn trên đường vắng rồi chết.",
            "purpose": "Chứng minh Base Idea: phản xạ cứu người thắng phản xạ tự cứu.",
            "purpose_visibility": "writer_safe",
            "writer_notes": ["Cụ thể hóa thao tác cấp cứu, không hô khẩu hiệu."],
            "required_beats": ["ép tim ngoài lồng ngực", "đèn xe tải"],
            "forbidden_moves": ["Không viết cảnh chết thành kinh dị máu me."],
            "character_ids": ["char_0001"],
            "world_rule_ids": ["rule_0001"],
            "foreshadow_ids": [],
        },
        {
            "section_id": "tmp_section_2",
            "index": 2,
            "type": "foreshadow",
            "instruction": "Mô tả cái nồi sắt đen rỉ xốp nhưng không móp khi bị gõ bằng đá.",
            "purpose": PURPOSE_SECRET,
            "purpose_visibility": "planner_only",
            "author_only_notes": ["Không gửi truth_author_only của fs_0001 cho Writer."],
            "required_beats": ["nồi rỉ xốp như bùn khô", "không móp khi gõ đá"],
            "forbidden_moves": ["Không giải thích nguồn gốc cái nồi."],
            "character_ids": ["char_0001"],
            "world_rule_ids": ["rule_0001"],
            "foreshadow_ids": ["fs_0001"],
            "foreshadow_surfaces": [
                {
                    "foreshadow_id": "fs_0001",
                    "surface_instruction": "Cái nồi có vẻ rỉ nhưng cứng bất thường; chỉ mô tả, không lý giải.",
                    "reveal_policy": "hint_only",
                }
            ],
        },
    ],
}

#: Skeleton chương 2 với ID tạm (pool của chương 2 còn trống).
SKELETON_CH2_GENERATED: dict[str, Any] = {
    "chapter_id": "ch_0002",
    "chapter_number": 2,
    "global_constraints": ["Không giải thích nguồn gốc cái nồi."],
    "writer_context_policy": {"include_author_only": False, "include_future_plan": False},
    "sections": [
        {
            "section_id": "tmp_section_1",
            "index": 1,
            "type": "action",
            "instruction": "Sở Dương thoát khỏi vách đá bằng phản xạ nghề nghiệp.",
            "purpose": "Nối tiếp hệ quả chương 1.",
            "purpose_visibility": "writer_safe",
            "character_ids": ["char_0001"],
            "world_rule_ids": ["rule_0001"],
        },
        {
            "section_id": "tmp_section_2",
            "index": 2,
            "type": "foreshadow",
            "instruction": "Nghe động tĩnh của người giữ ánh lửa.",
            "purpose": PURPOSE_SECRET,
            "purpose_visibility": "planner_only",
            "author_only_notes": ["Không gửi truth."],
            "character_ids": ["char_0001", "char_0002"],
            "foreshadow_ids": ["fs_0001"],
            "foreshadow_surfaces": [
                {
                    "foreshadow_id": "fs_0001",
                    "surface_instruction": "Chỉ mô tả hiện tượng, không giải thích nguồn gốc.",
                    "reveal_policy": "hint_only",
                }
            ],
        },
    ],
}


def _review_report(
    *,
    chapter_id: str = "ch_0001",
    prose_revision: int = 1,
    quote: str | None = "Anh nhặt hòn đá gõ vào thành nồi.",
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "issue_id": "issue_0001",
        "severity": "minor",
        "category": "style_issue",
        "source": {
            "authority_kind": "skeleton",
            "artifact_id": "skeleton_ch_0001",
            "revision": 1,
            "field_path": "sections[0].instruction",
        },
        "evidence": {"prose_revision": prose_revision, "quote": quote},
        "message": "Nhịp đoạn mở hơi nhanh so với chỉ dẫn Skeleton.",
        "suggested_action": "Thêm một câu phản ứng trước khi kiểm tra cái nồi.",
        "status": "open",
    }
    return {
        "chapter_id": chapter_id,
        "prose_revision": prose_revision,
        "issues": [issue],
        "summary": "Một ghi chú nhỏ về nhịp; không có mâu thuẫn cứng.",
    }


def _complete_draft(project: Project) -> None:
    """Sinh một prose draft complete cho chương 1."""
    client = FakeLLMClient([COMPLETE_PROSE])
    writer.generate_draft(project, client=client, chapter_id="ch_0001")
