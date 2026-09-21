"""Context selection, temporal filtering và secret filtering (T12).

Test dùng project mẫu dựng trong `tmp_path` từ ví dụ contract T02
(`docs/design/examples/linked_project_valid.json`): hai chương, một secret
author-only, và một world rule hiệu lực chương 100 để phát hiện leak.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_ai.core import lifecycle, storage
from novel_ai.core.context import (
    ContextError,
    build_co_create_context,
    build_long_plan_context,
    build_review_context,
    build_skeleton_context,
    build_writer_context,
)
from novel_ai.core.models import (
    ArtifactRevision,
    ArtifactStatus,
    ChapterMetadata,
    ChapterStatus,
    ContextMode,
    CurrentTimelineDocument,
    DependencyPin,
    PayloadSource,
    RelationshipStateDocument,
    SkeletonPayload,
    SourceType,
    ValidationResult,
    ValidationState,
    WorldRule,
    now_iso,
)
from novel_ai.core.project import Project

#: Sentinel lấy trực tiếp từ fixture T02.
POT_SECRET = "mảnh vật phẩm cổ"
BODY_SECRET = "liên quan tới bí mật của cái nồi"
PURPOSE_SECRET = "Gieo mầm vật phẩm có nguồn gốc bất thường"

_STAMP = "2026-09-19T10:00:00+07:00"


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def _revision(payload: dict[str, Any], *, revision: int = 1) -> ArtifactRevision:
    return ArtifactRevision(
        revision=revision,
        payload=payload,
        payload_source=PayloadSource(source_type=SourceType.llm, prompt_id="fixture"),
        dependency_pins=[],
        validation=_valid(),
        created_at=_STAMP,
        accepted_at=_STAMP,
        accepted_by="user",
    )


def _accepted_artifact(artifact_id: str, artifact_type: str, payload: dict[str, Any]):
    return lifecycle.new_artifact(artifact_type, artifact_id).model_copy(
        update={
            "status": ArtifactStatus.accepted,
            "accepted_revision": _revision(payload),
        }
    )


@pytest.fixture
def seeded_project(tmp_path: Path, t02_valid_document: dict[str, Any]) -> Project:
    """Project 2 chương + skeleton chương 1 + skeleton chương 2 accepted."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project = Project.create(projects_root, "Nồi Canh Bên Đường")
    document = t02_valid_document

    # --- Base Idea (markdown + metadata) ---------------------------------
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

    # --- Foundation + plans ----------------------------------------------
    for key in ("premise", "characters", "world_rules", "foreshadow", "long_plan", "short_plan"):
        raw = document["artifacts"][key]
        envelope = _accepted_artifact(key, raw["artifact_type"], raw["accepted_revision"]["payload"])
        storage.save_artifact(project, envelope, operation_id=f"op_seed_{key}")

    # --- Skeleton chương 1 (từ fixture) ----------------------------------
    raw_skeleton = document["artifacts"]["skeleton_ch_0001"]
    storage.save_artifact(
        project,
        _accepted_artifact(
            "skeleton_ch_0001", "skeleton", raw_skeleton["accepted_revision"]["payload"]
        ),
        operation_id="op_seed_skeleton_1",
    )
    # --- Skeleton chương 2 (dựng thêm để test state as-of) ---------------
    storage.save_artifact(
        project,
        _accepted_artifact(
            "skeleton_ch_0002",
            "skeleton",
            SkeletonPayload(
                chapter_id="ch_0002",
                chapter_number=2,
                global_constraints=["Không giải thích nguồn gốc cái nồi."],
                sections=[
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
            ).model_dump(mode="json"),
        ),
        operation_id="op_seed_skeleton_2",
    )

    # --- Chapters ---------------------------------------------------------
    for key, raw in document["chapters"].items():
        chapter = ChapterMetadata.model_validate(raw)
        if key == "ch_0002":
            chapter = chapter.model_copy(
                update={
                    "status": ChapterStatus.skeleton_ready,
                    "skeleton_pin": DependencyPin(
                        artifact_id="skeleton_ch_0002",
                        revision=1,
                        scope="skeleton",
                        chapter_id="ch_0002",
                    ),
                }
            )
        storage.save_chapter(project, chapter, operation_id=f"op_seed_{key}")

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
# Writer projection
# ---------------------------------------------------------------------------


def test_writer_context_for_chapter_one_has_no_secret_sentinel(seeded_project: Project) -> None:
    bundle = build_writer_context(seeded_project, chapter_id="ch_0001")
    serialized = json.dumps(bundle.payload, ensure_ascii=False)

    assert POT_SECRET not in serialized
    assert BODY_SECRET not in serialized
    assert PURPOSE_SECRET not in serialized
    assert "author_only" not in serialized
    assert "truth_author_only" not in serialized


def test_writer_context_keeps_writer_safe_surface_instruction(seeded_project: Project) -> None:
    bundle = build_writer_context(seeded_project, chapter_id="ch_0001")
    sections = bundle.payload["skeleton"]["sections"]

    second = sections[1]
    assert second["foreshadow_surfaces"] == [
        "Cái nồi có vẻ rỉ nhưng cứng bất thường; chỉ mô tả, không lý giải."
    ]
    # `purpose_visibility = planner_only` nên purpose không được gửi Writer.
    assert "purpose" not in second
    first = sections[0]
    assert first["purpose"] == "Chứng minh Base Idea: phản xạ cứu người thắng phản xạ tự cứu."


def test_writer_context_excludes_future_character_and_late_lore(seeded_project: Project) -> None:
    bundle = build_writer_context(seeded_project, chapter_id="ch_0001")

    assert bundle.included_ids["characters"] == ["char_0001"]
    assert bundle.included_ids["world_rules"] == ["rule_0001"]
    excluded = {
        (item.item_id, item.effective_from_chapter)
        for item in bundle.excluded_due_to_effective_chapter
    }
    assert ("char_0002", 2) in excluded
    assert ("rule_0100", 100) in excluded
    # Nhân vật hiệu lực chương 2 và lore chương 100 không nằm trong projection,
    # dù Skeleton có thể nhắc tên nhân vật trong lời cấm ("không để ... xuất hiện").
    assert [item["display_name"] for item in bundle.payload["characters"]] == ["Sở Dương"]
    assert [item["world_rule_id"] for item in bundle.payload["world_rules"]] == ["rule_0001"]
    assert "Cơ quan phòng chống tội phạm xuyên giới" not in json.dumps(
        bundle.payload, ensure_ascii=False
    )


@pytest.mark.parametrize("visibility", ["author_only", "planner_only", "skeleton_only"])
def test_writer_projection_never_sends_non_writer_safe_world_rule_content(
    seeded_project: Project, visibility: str
) -> None:
    """World rule không `writer_safe` không được đẩy `content` cho Writer.

    Fixture chỉ có một rule `author_only` và nó hiệu lực chương 100, nên bị lọc
    bởi `effective_from_chapter` và không chứng minh được gì về `visibility`. Test
    này đặt rule bí mật **hiệu lực từ chương 1** và `writer_projection = null` —
    đúng pattern fixture đang dùng — để bắt đường leak thật: trước đây
    `_world_rule_writer_projection` fallback thẳng sang `content` cho mọi
    visibility, nên secret vào thẳng prompt Writer mà `validate_writer_projection`
    vẫn báo hợp lệ (guard chỉ soi tên key, không phân loại giá trị string).
    """
    secret = "CO-QUAN-BI-MAT: giam sat vat pham xuyen gioi."
    envelope = storage.load_artifact(seeded_project, "world_rules")
    payload = envelope.accepted_revision.payload.model_copy(
        update={
            "world_rules": [
                *envelope.accepted_revision.payload.world_rules,
                WorldRule.model_validate(
                    {
                        "world_rule_id": "rule_0500",
                        "category": "Cơ quan bí mật",
                        "summary": "Cơ quan bí mật tồn tại từ chương 1.",
                        "content": secret,
                        "boundary": "Không tiết lộ cho tới khi có reveal.",
                        "effective_from_chapter": 1,
                        "visibility": visibility,
                        "writer_projection": None,
                        "author_only": {"reason": "truth chỉ tác giả biết"},
                    }
                ),
            ]
        }
    )
    accepted = envelope.model_copy(
        update={
            "accepted_revision": envelope.accepted_revision.model_copy(
                update={"revision": envelope.accepted_revision.revision + 1, "payload": payload}
            )
        }
    )
    storage.save_artifact(seeded_project, accepted, operation_id="op_add_secret_rule")

    bundle = build_writer_context(seeded_project, chapter_id="ch_0001")
    serialized = json.dumps(bundle.payload, ensure_ascii=False)

    assert "rule_0500" in bundle.included_ids["world_rules"]
    assert secret not in serialized
    project = bundle.payload["world_rules"]
    rule = next(item for item in project if item["world_rule_id"] == "rule_0500")
    assert "writer_projection" not in rule


def test_writer_projection_uses_explicit_writer_projection_for_author_only_rule(
    seeded_project: Project,
) -> None:
    """Rule `author_only` **có** `writer_projection` vẫn gửi bản diễn đạt an toàn."""
    envelope = storage.load_artifact(seeded_project, "world_rules")
    payload = envelope.accepted_revision.payload.model_copy(
        update={
            "world_rules": [
                *envelope.accepted_revision.payload.world_rules,
                WorldRule.model_validate(
                    {
                        "world_rule_id": "rule_0600",
                        "category": "Cơ quan bí mật",
                        "summary": "Cơ quan bí mật tồn tại từ chương 1.",
                        "content": "TRUTH-CHA-GIAU: nguồn gốc thật của cơ quan.",
                        "boundary": "Không tiết lộ.",
                        "effective_from_chapter": 1,
                        "visibility": "author_only",
                        "writer_projection": "Chỉ mô tả dấu hiệu bên ngoài, không giải thích.",
                        "author_only": {},
                    }
                ),
            ]
        }
    )
    accepted = envelope.model_copy(
        update={
            "accepted_revision": envelope.accepted_revision.model_copy(
                update={"revision": envelope.accepted_revision.revision + 1, "payload": payload}
            )
        }
    )
    storage.save_artifact(seeded_project, accepted, operation_id="op_add_safe_rule")

    bundle = build_writer_context(seeded_project, chapter_id="ch_0001")
    serialized = json.dumps(bundle.payload, ensure_ascii=False)
    rule = next(
        item for item in bundle.payload["world_rules"] if item["world_rule_id"] == "rule_0600"
    )

    assert "TRUTH-CHA-GIAU" not in serialized
    assert rule["writer_projection"] == "Chỉ mô tả dấu hiệu bên ngoài, không giải thích."


def test_chapter_two_context_includes_chapter_two_character_but_not_late_lore(
    seeded_project: Project,
) -> None:
    bundle = build_writer_context(seeded_project, chapter_id="ch_0002")

    assert bundle.included_ids["characters"] == ["char_0001", "char_0002"]
    assert bundle.included_ids["world_rules"] == ["rule_0001"]
    assert {item.item_id for item in bundle.excluded_due_to_effective_chapter} == {"rule_0100"}


def test_writer_context_uses_state_as_of_previous_chapter(seeded_project: Project) -> None:
    first = build_writer_context(seeded_project, chapter_id="ch_0001")
    second = build_writer_context(seeded_project, chapter_id="ch_0002")

    # Chương 1 dùng baseline rỗng: relationship chỉ cập nhật từ chương 1.
    assert first.payload["relationships_as_of"] == []
    assert first.payload["timeline_as_of"] == []
    assert first.payload["previous_final_summary"] is None
    # Chương 2 thấy state chương 1, nhưng không thấy final của chính nó.
    assert [item["relationship_id"] for item in second.payload["relationships_as_of"]] == [
        "rel_0001"
    ]
    assert [item["chapter_number"] for item in second.payload["timeline_as_of"]] == [1]
    assert second.payload["previous_final_summary"]["chapter_number"] == 1


def test_writer_context_is_deterministic(seeded_project: Project) -> None:
    first = build_writer_context(seeded_project, chapter_id="ch_0001")
    second = build_writer_context(seeded_project, chapter_id="ch_0001")

    assert first.projection_hash == second.projection_hash
    assert first.payload == second.payload


def test_writer_context_blocks_when_skeleton_missing(seeded_project: Project) -> None:
    """File skeleton tồn tại không có nghĩa artifact hoàn thành (invariant 29)."""
    envelope = storage.load_artifact(seeded_project, "skeleton_ch_0002")
    storage.save_artifact(
        seeded_project,
        envelope.model_copy(
            update={"status": ArtifactStatus.missing, "accepted_revision": None}
        ),
        operation_id="op_clear_skeleton_artifact",
    )

    with pytest.raises(ContextError) as excinfo:
        build_writer_context(seeded_project, chapter_id="ch_0002")

    assert excinfo.value.code in {"missing_dependency", "stale_dependency"}


def test_writer_context_blocks_when_chapter_has_no_skeleton_pin(seeded_project: Project) -> None:
    storage.save_chapter(
        seeded_project,
        storage.load_chapter(seeded_project, "ch_0002").model_copy(
            update={"skeleton_pin": None, "status": ChapterStatus.planned}
        ),
        operation_id="op_clear_skeleton_pin",
    )

    with pytest.raises(ContextError) as excinfo:
        build_writer_context(seeded_project, chapter_id="ch_0002")

    assert excinfo.value.code == "missing_dependency"


def test_writer_context_blocks_when_previous_chapter_not_final_reconciled(
    seeded_project: Project,
) -> None:
    storage.save_chapter(
        seeded_project,
        storage.load_chapter(seeded_project, "ch_0001").model_copy(
            update={"status": ChapterStatus.review_required}
        ),
        operation_id="op_regress_chapter_one",
    )

    with pytest.raises(ContextError) as excinfo:
        build_writer_context(seeded_project, chapter_id="ch_0002")

    assert excinfo.value.code == "stale_dependency"


def test_writer_context_blocks_stale_skeleton(seeded_project: Project) -> None:
    envelope = storage.load_artifact(seeded_project, "skeleton_ch_0001")
    storage.save_artifact(
        seeded_project,
        envelope.model_copy(update={"status": ArtifactStatus.stale}),
        operation_id="op_stale_skeleton",
    )

    with pytest.raises(ContextError) as excinfo:
        build_writer_context(seeded_project, chapter_id="ch_0001")

    assert excinfo.value.code == "missing_dependency"


def test_writer_context_reports_missing_chapter(seeded_project: Project) -> None:
    with pytest.raises(ContextError) as excinfo:
        build_writer_context(seeded_project, chapter_id="ch_0099")

    assert excinfo.value.code == "missing_chapter"


# ---------------------------------------------------------------------------
# Skeleton / plan context
# ---------------------------------------------------------------------------


def test_skeleton_context_uses_provisional_when_previous_chapter_not_final(
    seeded_project: Project,
) -> None:
    storage.save_chapter(
        seeded_project,
        storage.load_chapter(seeded_project, "ch_0001").model_copy(
            update={"status": ChapterStatus.review_required}
        ),
        operation_id="op_regress_chapter_one",
    )

    bundle = build_skeleton_context(seeded_project, chapter_id="ch_0002")

    assert bundle.mode is ContextMode.provisional
    assert bundle.payload["context_basis"]["mode"] == "provisional"
    assert bundle.payload["context_basis"]["actual_through_chapter"] == 0
    assert bundle.preparation_context is not None
    # Bridge chỉ lấy intent từ plan accepted, không tạo timeline giả.
    assert bundle.payload["timeline_as_of"] == []
    assert bundle.payload["relationships_as_of"] == []


def test_skeleton_context_is_actual_when_previous_chapter_final(seeded_project: Project) -> None:
    bundle = build_skeleton_context(seeded_project, chapter_id="ch_0002")

    assert bundle.mode is ContextMode.actual
    assert bundle.payload["context_basis"]["planned_bridge"] == []
    assert bundle.preparation_context is None
    assert [item["chapter_number"] for item in bundle.payload["timeline_as_of"]] == [1]


def test_long_plan_context_requires_premise(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project = Project.create(projects_root, "Truyện chưa có premise")

    with pytest.raises(ContextError) as excinfo:
        build_long_plan_context(project, planning_scope={"start": 1, "end": 7})

    assert excinfo.value.code == "missing_dependency"


def test_long_plan_context_filters_late_lore_from_scope(seeded_project: Project) -> None:
    bundle = build_long_plan_context(seeded_project, planning_scope={"start": 1, "end": 7})

    assert bundle.included_ids["world_rules"] == ["rule_0001"]
    assert {"rule_0100"} <= {
        item.item_id for item in bundle.excluded_due_to_effective_chapter
    }


def test_co_create_context_does_not_read_story_artifacts(seeded_project: Project) -> None:
    bundle = build_co_create_context(seeded_project, user_message="Thêm một dị bản.")

    assert set(bundle.payload) == {
        "language",
        "genre_prompt",
        "conversation_summary",
        "current_idea_state",
        "user_message",
    }
    assert "premise" not in bundle.payload


# ---------------------------------------------------------------------------
# Review và budget
# ---------------------------------------------------------------------------


def test_review_context_projection_has_no_secret(seeded_project: Project) -> None:
    bundle = build_review_context(
        seeded_project, chapter_id="ch_0001", prose_revision=2, prose_markdown="Bản nháp."
    )
    serialized = json.dumps(bundle.payload, ensure_ascii=False)

    assert BODY_SECRET not in serialized
    assert "author_only_notes" not in serialized
    assert bundle.payload["constraint_sources"]


def test_budget_reduction_never_silently_drops_hard_constraints(seeded_project: Project) -> None:
    full = build_writer_context(seeded_project, chapter_id="ch_0001", budget_chars=10**7)
    assert full.budget_report is not None
    assert full.budget_report.reduced == []

    limit = max(full.budget_report.estimated_chars - 200, 200)
    try:
        reduced = build_writer_context(seeded_project, chapter_id="ch_0001", budget_chars=limit)
    except ContextError as exc:
        assert exc.code == "context_budget_exceeded"
        assert exc.details["blocking_sections"], "Phải nêu phần P0 gây vượt budget."
    else:
        assert reduced.budget_report is not None
        assert reduced.budget_report.reduced
        for key in (
            "chapter_id",
            "chapter_number",
            "base_idea_constraints",
            "premise_constraints",
            "skeleton",
        ):
            assert key in reduced.payload, f"Budget cắt mất hard constraint P0 `{key}`"


def test_bundle_to_snapshot_records_context_evidence(seeded_project: Project) -> None:
    bundle = build_writer_context(seeded_project, chapter_id="ch_0001")

    snapshot = bundle.to_snapshot(created_from_action="generate_writer_draft")

    assert snapshot.for_chapter_id == "ch_0001"
    assert snapshot.effective_character_ids == ["char_0001"]
    assert snapshot.writer_projection_hash == bundle.projection_hash
    assert {item.item_id for item in snapshot.excluded_due_to_effective_chapter} == {
        "char_0002",
        "rule_0100",
    }
    assert now_iso()  # snapshot có created_at ISO
