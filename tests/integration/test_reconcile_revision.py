"""Integration test cho Finalize/Reconcile transaction (T17) và Revision/Retcon (T18).

Test chạy offline với `tmp_path`, seed project qua `storage`/`lifecycle` (không
phụ thuộc service của task khác), và dùng `FakeLLMClient` để mô phỏng response
hợp lệ, JSON sai, ID không tồn tại và timeout.

Bao phủ 10 case bắt buộc của T23 liên quan tới finalize/reconcile/retcon:
unlock hai chương, Auto Accept không bypass Human Review, accepted state không
đổi khi fail, crash + recovery, retry không nhân đôi, retcon giữ final cũ, revise
upstream chỉ đánh dấu stale, và reload sau mọi thao tác.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

import pytest

from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.context import ContextError, build_writer_context
from novel_ai.core.llm import FakeLLMClient, LLMTimeoutError
from novel_ai.core.models import (
    ArtifactEnvelope,
    ArtifactRevision,
    ArtifactStatus,
    ChapterMetadata,
    ChapterStatus,
    CurrentTimelineDocument,
    DependencyPin,
    HumanReviewRecord,
    PayloadSource,
    ProseRevision,
    ReconciliationPayload,
    ReconciliationStatus,
    RelationshipStateDocument,
    SkeletonPayload,
    SourceChange,
    SourceType,
    StructuredOutputError,
    ValidationResult,
    ValidationState,
    now_iso,
)
from novel_ai.core.project import Project
from novel_ai.services import (
    GuardError,
    ServiceError,
    StaleDependencyError,
    ValidationFailure,
)
from novel_ai.services import reconcile as reconcile_service
from novel_ai.services import revision as revision_service

_STAMP = "2026-09-19T12:00:00+07:00"
_PROPOSAL_BUILDER = Callable[[int, int, str], str]


# ---------------------------------------------------------------------------
# Seed project (copy cách seed của tests/unit/test_context.py)
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


def _seed_skeleton(
    project: Project, *, chapter_id: str, chapter_number: int, instruction: str
) -> None:
    storage.save_artifact(
        project,
        _accepted_artifact(
            f"skeleton_{chapter_id}",
            "skeleton",
            SkeletonPayload(
                chapter_id=chapter_id,
                chapter_number=chapter_number,
                global_constraints=["Không giải thích nguồn gốc cái nồi."],
                sections=[
                    {
                        "section_id": "section_0001",
                        "index": 1,
                        "type": "action",
                        "instruction": instruction,
                        "purpose": "Nối tiếp hệ quả chương trước.",
                        "purpose_visibility": "writer_safe",
                    }
                ],
            ).model_dump(mode="json"),
        ),
        operation_id=f"op_seed_skeleton_{chapter_id}",
    )


def _draft_markdown(chapter_number: int, revision: int = 1) -> str:
    return (
        f"# Chương {chapter_number}\n\n"
        f"Sở Dương bước tiếp trên sườn núi (bản r{revision}). "
        "Cái nồi sắt đen vẫn không móp khi gõ vào đá.\n"
    )


def _with_third_character(payload: dict[str, Any]) -> dict[str, Any]:
    """Thêm `char_0003` hiệu lực chương 3 để test relationship mới ở chương sau."""
    updated = json.loads(json.dumps(payload, ensure_ascii=False))
    third = json.loads(json.dumps(updated["characters"][1], ensure_ascii=False))
    third.update(
        {
            "character_id": "char_0003",
            "display_name": "Lão Trần",
            "aliases": ["Người giữ lửa"],
            "effective_from_chapter": 3,
        }
    )
    updated["characters"].append(third)
    return updated


def _seed_project(tmp_path: Path, t02_valid_document: dict[str, Any]) -> Project:
    """Project 3 chương: foundation + short plan + skeleton, prose do test tự seed."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project = Project.create(projects_root, "Nồi Canh Bên Đường")
    document = t02_valid_document

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
        payload = raw["accepted_revision"]["payload"]
        if key == "characters":
            payload = _with_third_character(payload)
        storage.save_artifact(
            project,
            _accepted_artifact(key, raw["artifact_type"], payload),
            operation_id=f"op_seed_{key}",
        )

    # Short Plan của fixture chỉ có 2 chương; thêm chương 3 cho test retcon 3 chương.
    short_plan = storage.load_artifact(project, "short_plan")
    payload = short_plan.accepted_revision.payload.model_copy(deep=True)
    chapter_three = payload.chapters[1].model_copy(
        update={
            "chapter_id": "ch_0003",
            "chapter_number": 3,
            "title": "Tro tàn dưới vực",
            "summary": "Sở Dương chạm mặt người giữ ánh lửa lần đầu.",
            "hook": "Hai người cùng nhận ra có thứ đang theo dõi.",
            "outline": ["Đối mặt trong bóng tối.", "Trao đổi điều kiện sống sót."],
            "relationship_changes": [],
            "chapter_goal": "Đưa quan hệ vào va chạm trực tiếp.",
            "planned_ending": "Hai người buộc phải đi cùng hướng.",
        }
    )
    payload = payload.model_copy(update={"chapters": [*payload.chapters, chapter_three]})
    storage.save_artifact(
        project,
        short_plan.model_copy(
            update={
                "accepted_revision": short_plan.accepted_revision.model_copy(
                    update={"payload": payload, "revision": 2}
                )
            }
        ),
        operation_id="op_seed_short_plan_3",
    )

    for chapter_id, number in (("ch_0001", 1), ("ch_0002", 2), ("ch_0003", 3)):
        _seed_skeleton(
            project,
            chapter_id=chapter_id,
            chapter_number=number,
            instruction=f"Viết cảnh mở đầu chương {number}.",
        )

    for chapter_id, raw in document["chapters"].items():
        chapter = ChapterMetadata.model_validate(raw)
        chapter = chapter.model_copy(
            update={
                "status": ChapterStatus.skeleton_ready,
                "skeleton_pin": DependencyPin(
                    artifact_id=f"skeleton_{chapter_id}",
                    revision=1,
                    scope="skeleton",
                    chapter_id=chapter_id,
                ),
                "drafts": [],
                "current_draft_revision": None,
                "human_review": None,
                "final_candidate": None,
                "final_revision": None,
                "reconciliation_pin": None,
            }
        )
        storage.save_chapter(project, chapter, operation_id=f"op_seed_{chapter_id}")

    short_plan_pin = DependencyPin(
        artifact_id="short_plan",
        revision=2,
        scope="short_plan",
        chapter_id="ch_0003",
    )
    storage.save_chapter(
        project,
        ChapterMetadata(
            chapter_id="ch_0003",
            chapter_number=3,
            title="Tro tàn dưới vực",
            status=ChapterStatus.skeleton_ready,
            previous_chapter_id="ch_0002",
            short_plan_pin=short_plan_pin,
            skeleton_pin=DependencyPin(
                artifact_id="skeleton_ch_0003",
                revision=1,
                scope="skeleton",
                chapter_id="ch_0003",
            ),
        ),
        operation_id="op_seed_ch_0003",
    )

    storage.save_timeline(
        project,
        CurrentTimelineDocument(latest_final_chapter=0, latest_consistent_chapter=0, entries=[]),
        operation_id="op_seed_timeline",
    )
    storage.save_relationships(
        project,
        RelationshipStateDocument(latest_consistent_chapter=0, relationships=[]),
        operation_id="op_seed_relationships",
    )
    return project


def _seed_draft(
    project: Project, *, chapter_id: str, revision: int, reviewed: bool
) -> None:
    chapter = storage.load_chapter(project, chapter_id)
    number = chapter.chapter_number
    relpath = f"chapters/{chapter_id}/drafts/draft_r{revision:04d}.md"
    storage.write_text_atomic(
        project.root / relpath, _draft_markdown(number, revision), operation_id=f"op_seed_draft_{chapter_id}"
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
                    created_at=_STAMP,
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
                    reviewed_at=_STAMP,
                    reviewed_by="user",
                    notes="Test seed: đã review.",
                    valid_for_current_revision=True,
                )
                if reviewed
                else None
            ),
        }
    )
    storage.save_chapter(project, updated, operation_id=f"op_seed_draft_{chapter_id}")


def seed_reviewed_chapter(project: Project, *, chapter_id: str, revision: int = 1) -> None:
    """Seed draft complete + Human Review hợp lệ cho chapter."""
    _seed_draft(project, chapter_id=chapter_id, revision=revision, reviewed=True)


@pytest.fixture
def project(tmp_path: Path, t02_valid_document: dict[str, Any]) -> Project:
    return _seed_project(tmp_path, t02_valid_document)


# ---------------------------------------------------------------------------
# Proposal builder
# ---------------------------------------------------------------------------


def make_proposal(
    *,
    chapter_number: int,
    prose_revision: int,
    markdown_ref: str,
    updates: list[dict[str, Any]] | None = None,
    time: str | None = None,
    location: str | None = None,
    status: str | None = None,
    timeline_status: str | None = None,
) -> str:
    """JSON proposal hợp lệ cho `reconcile.v1`."""
    payload = {
        "chapter_id": f"ch_{chapter_number:04d}",
        "chapter_number": chapter_number,
        "source_final_candidate": {
            "prose_revision": prose_revision,
            "markdown_ref": markdown_ref,
        },
        "timeline": {
            "time": time or f"Đêm thứ {chapter_number}",
            "location": location or f"Vách đá chương {chapter_number}",
            "status": timeline_status
            or status
            or f"Sở Dương sống sót tới cuối chương {chapter_number}.",
        },
        "relationship_updates": updates or [],
        "notes": [f"Trích xuất từ bản final chương {chapter_number}."],
    }
    return json.dumps(payload, ensure_ascii=False)


def _client(proposal: str | Exception) -> FakeLLMClient:
    return FakeLLMClient([proposal])


# ---------------------------------------------------------------------------
# Fixture nâng cao: project đã finalize/reconcile 3 chương
# ---------------------------------------------------------------------------


def default_proposal_builder(chapter_number: int, prose_revision: int, markdown_ref: str) -> str:
    """Proposal theo chương: chương 2 tạo relationship mới, chương 3 update cặp đó."""
    updates: list[dict[str, Any]] = []
    if chapter_number == 2:
        updates = [
            {
                "character_ids": ["char_0001", "char_0002"],
                "current": "Lần đầu chạm mặt, còn dè chừng.",
            }
        ]
    elif chapter_number == 3:
        updates = [
            {
                "character_ids": ["char_0001", "char_0002"],
                "current": "Buộc phải đi cùng hướng, chưa tin nhau.",
            }
        ]
    return make_proposal(
        chapter_number=chapter_number,
        prose_revision=prose_revision,
        markdown_ref=markdown_ref,
        updates=updates,
    )


def finalize_and_reconcile(
    project: Project,
    *,
    chapter_id: str,
    proposal: str,
    client: FakeLLMClient | None = None,
) -> dict[str, Any]:
    """Chạy đúng luồng user: finalize → generate reconciliation → accept."""
    finalized = reconcile_service.finalize_chapter(project, chapter_id=chapter_id)
    used = client or _client(proposal)
    generated = reconcile_service.generate_reconciliation(
        project, client=used, chapter_id=chapter_id
    )
    if generated.data.get("auto_accepted"):
        return {"finalize": finalized, "generate": generated, "accept": None}
    accepted = reconcile_service.accept_reconciliation(project, chapter_id=chapter_id)
    return {"finalize": finalized, "generate": generated, "accept": accepted}


def seed_fully_finalized(
    project: Project,
    *,
    chapters: tuple[str, ...] = ("ch_0001", "ch_0002", "ch_0003"),
    proposal_builder: _PROPOSAL_BUILDER | None = None,
) -> dict[str, dict[str, Any]]:
    """Seed 3 chương đã final_reconciled bằng chính service (state thật)."""
    builder = proposal_builder or default_proposal_builder
    results: dict[str, dict[str, Any]] = {}
    for chapter_id in chapters:
        seed_reviewed_chapter(project, chapter_id=chapter_id, revision=1)
        chapter = storage.load_chapter(project, chapter_id)
        markdown_ref = f"chapters/{chapter_id}/drafts/draft_r0001.md"
        proposal = builder(chapter.chapter_number, 1, markdown_ref)
        results[chapter_id] = finalize_and_reconcile(
            project, chapter_id=chapter_id, proposal=proposal
        )
    return results


# ---------------------------------------------------------------------------
# 1. E2E hai chương (case 1 của T23)
# ---------------------------------------------------------------------------


def test_finalize_then_reconcile_unlocks_next_chapter_writer(project: Project) -> None:
    seed_reviewed_chapter(project, chapter_id="ch_0001")

    # Accepted state trước finalize: timeline rỗng, chương 2 bị khóa.
    assert storage.load_timeline(project).entries == []
    with pytest.raises(ContextError) as blocked:
        build_writer_context(project, chapter_id="ch_0002")
    assert blocked.value.code in {"missing_dependency", "stale_dependency"}

    finalized = reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.status is ChapterStatus.finalizing
    assert chapter.final_candidate is not None
    assert chapter.final_candidate.reconciliation_status is ReconciliationStatus.missing
    assert (project.root / chapter.final_candidate.markdown_ref).is_file()
    assert finalized.data["status"] == "finalizing"

    # Chương 2 vẫn khóa khi reconciliation chưa commit.
    assert storage.load_chapter(project, "ch_0002").status is ChapterStatus.skeleton_ready
    with pytest.raises(ContextError):
        build_writer_context(project, chapter_id="ch_0002")

    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
        timeline_status="Sở Dương tỉnh dậy bên cái nồi và bị kéo lên sườn núi.",
    )
    client = _client(proposal)
    generated = reconcile_service.generate_reconciliation(
        project, client=client, chapter_id="ch_0001"
    )
    assert generated.data["reconciliation_status"] == "draft", generated.data

    accepted = reconcile_service.accept_reconciliation(project, chapter_id="ch_0001")

    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.status is ChapterStatus.final_reconciled
    assert chapter.final_candidate is None
    assert chapter.final_revision is not None
    assert chapter.final_revision.source_prose_revision == 1
    assert chapter.reconciliation_pin is not None

    timeline = storage.load_timeline(project)
    assert [entry.chapter_number for entry in timeline.entries] == [1]
    assert timeline.entries[0].source_final_revision.revision == 1
    assert "Sở Dương tỉnh dậy" in timeline.entries[0].status
    assert timeline.latest_final_chapter == 1
    assert timeline.latest_consistent_chapter == 1

    # Chương 1 chỉ có char_0001 nên reconcile không tạo relationship pair nào.
    assert storage.load_relationships(project).relationships == []
    assert accepted.data["relationship_ids"] == []

    # Snapshot chuẩn bị cho chương 2 đã được tạo trong cùng transaction.
    assert accepted.data["snapshot_created"] is True
    assert storage.load_snapshots(project)

    bundle = build_writer_context(project, chapter_id="ch_0002")
    assert [entry["chapter_number"] for entry in bundle.payload["timeline_as_of"]] == [1]
    assert [item["character_id"] for item in bundle.payload["characters"]] == [
        "char_0001",
        "char_0002",
    ]


def test_reconcile_chapter_two_creates_relationship_with_backend_id(project: Project) -> None:
    """Pair mới ở chương 2 được backend cấp `relationship_id`."""
    seed_fully_finalized(project, chapters=("ch_0001", "ch_0002"))

    relationships = storage.load_relationships(project)
    assert len(relationships.relationships) == 1
    created = relationships.relationships[0]
    assert created.relationship_id.startswith("rel_")
    assert created.character_ids == ["char_0001", "char_0002"]
    assert created.last_updated_chapter == 2
    assert [item.chapter_number for item in created.history] == [2]
    assert relationships.latest_consistent_chapter == 2


# ---------------------------------------------------------------------------
# 2. Auto Accept không bypass Human Review (case 2)
# ---------------------------------------------------------------------------


def test_auto_accept_structured_does_not_bypass_human_review(project: Project) -> None:
    project.config = project.config.model_copy(update={"auto_accept_structured": True})
    _seed_draft(project, chapter_id="ch_0001", revision=1, reviewed=False)

    with pytest.raises(GuardError) as blocked:
        reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    assert blocked.value.code == "human_review_missing"
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.review_required
    assert storage.load_timeline(project).entries == []

    # Xác nhận rõ trong chính action Finalize mới đi tiếp được.
    finalized = reconcile_service.finalize_chapter(
        project, chapter_id="ch_0001", confirm_review=True
    )
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.status is ChapterStatus.finalizing
    assert chapter.human_review is not None
    assert chapter.human_review.prose_revision == 1
    assert chapter.human_review.valid_for_current_revision is True
    assert finalized.data["human_review_confirmed_in_action"] is True

    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    client = _client(proposal)
    generated = reconcile_service.generate_reconciliation(
        project, client=client, chapter_id="ch_0001"
    )
    assert generated.data["auto_accepted"] is True
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.final_reconciled
    assert len(storage.load_timeline(project).entries) == 1


# ---------------------------------------------------------------------------
# 3. Accepted state không đổi khi fail (case 3, 6, 10, 11)
# ---------------------------------------------------------------------------


def _accepted_snapshot(project: Project) -> dict[str, Any]:
    """Ảnh chụp **accepted state**: timeline, relationship và final manuscript."""
    chapter = storage.load_chapter(project, "ch_0001")
    return {
        "timeline": storage.load_timeline(project).model_dump(mode="json"),
        "relationships": storage.load_relationships(project).model_dump(mode="json"),
        "final_revision": chapter.final_revision.model_dump(mode="json")
        if chapter.final_revision
        else None,
        "final_markdown": (
            storage.read_text(project.root / chapter.final_revision.markdown_ref)
            if chapter.final_revision
            else None
        ),
    }


def _finalizing_chapter(project: Project) -> ChapterMetadata:
    seed_reviewed_chapter(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    return storage.load_chapter(project, "ch_0001")


def test_invalid_json_keeps_chapter_finalizing_and_state_unchanged(project: Project) -> None:
    chapter = _finalizing_chapter(project)
    before = _accepted_snapshot(project)

    result = reconcile_service.generate_reconciliation(
        project, client=_client("{ không phải JSON"), chapter_id="ch_0001"
    )

    assert result.data["reconciliation_status"] == "failed"
    assert result.data["raw_output_ref"]
    assert (project.root / result.data["raw_output_ref"]).is_file()
    assert (project.root / result.data["error_record_ref"]).is_file()
    assert _accepted_snapshot(project) == before
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.finalizing
    assert chapter.final_candidate is not None
    with pytest.raises(ContextError):
        build_writer_context(project, chapter_id="ch_0002")


def test_unknown_character_id_is_rejected_without_state_change(project: Project) -> None:
    _finalizing_chapter(project)
    before = _accepted_snapshot(project)
    chapter = storage.load_chapter(project, "ch_0001")

    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
        updates=[
            {
                "character_ids": ["char_0001", "char_9999"],
                "current": "Quan hệ với người không tồn tại.",
            }
        ],
    )
    result = reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )

    assert result.data["reconciliation_status"] == "failed"
    codes = {item["code"] for item in result.data["errors"]}
    assert "unknown_reference" in codes
    assert _accepted_snapshot(project) == before
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.finalizing


def test_relationship_id_mismatched_pair_is_rejected(project: Project) -> None:
    _finalizing_chapter(project)
    before = _accepted_snapshot(project)
    chapter = storage.load_chapter(project, "ch_0001")

    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
        updates=[
            {
                "relationship_id": "rel_0001",
                "character_ids": ["char_0001", "char_0002"],
                "current": "Quan hệ không tồn tại trong state.",
            }
        ],
    )
    result = reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )

    assert result.data["reconciliation_status"] == "failed"
    codes = {item["code"] for item in result.data["errors"]}
    assert "unknown_reference" in codes
    assert _accepted_snapshot(project) == before


def test_entity_not_effective_in_chapter_is_rejected(project: Project) -> None:
    """`char_0002` hiệu lực từ chương 2 nên không được dùng trong reconcile chương 1."""
    _finalizing_chapter(project)
    before = _accepted_snapshot(project)
    chapter = storage.load_chapter(project, "ch_0001")

    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
        updates=[
            {
                "character_ids": ["char_0001", "char_0002"],
                "current": "Quan hệ với nhân vật chưa hiệu lực.",
            }
        ],
    )
    result = reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )

    assert result.data["reconciliation_status"] == "failed"
    codes = {item["code"] for item in result.data["errors"]}
    assert "effective_from_future" in codes
    assert _accepted_snapshot(project) == before


def test_chapter_number_mismatch_is_rejected(project: Project) -> None:
    _finalizing_chapter(project)
    before = _accepted_snapshot(project)
    chapter = storage.load_chapter(project, "ch_0001")

    proposal = json.dumps(
        {
            "chapter_id": "ch_0001",
            "chapter_number": 2,
            "source_final_candidate": {
                "prose_revision": 1,
                "markdown_ref": chapter.final_candidate.markdown_ref,
            },
            "timeline": {"time": "Đêm 2", "location": "Vực", "status": "Sai chương."},
            "relationship_updates": [],
            "notes": [],
        },
        ensure_ascii=False,
    )
    result = reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )

    assert result.data["reconciliation_status"] == "failed"
    codes = {item["code"] for item in result.data["errors"]}
    assert "chapter_mismatch" in codes
    assert _accepted_snapshot(project) == before


def test_llm_timeout_keeps_finalizing_and_saves_nothing(project: Project) -> None:
    _finalizing_chapter(project)
    before = _accepted_snapshot(project)
    client = FakeLLMClient([LLMTimeoutError("timeout giả lập")])

    with pytest.raises(Exception) as excinfo:
        reconcile_service.generate_reconciliation(
            project, client=client, chapter_id="ch_0001"
        )

    assert getattr(excinfo.value, "code", "") in {"llm_timeout", "llm_unavailable"}
    assert _accepted_snapshot(project) == before
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.finalizing
    assert storage.load_artifact(project, "reconciliation_ch_0001") is None
    assert storage.needs_recovery(project) is False


def test_proposal_from_old_prose_revision_is_stale(project: Project) -> None:
    """Proposal trỏ prose revision khác final candidate bị từ chối ở accept."""
    chapter = _finalizing_chapter(project)
    proposal = make_proposal(
        chapter_number=1,
        prose_revision=9,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    result = reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )
    assert result.data["reconciliation_status"] == "failed"
    assert "stale_candidate" in {item["code"] for item in result.data["errors"]}


def test_accept_rejects_proposal_when_input_state_changed(project: Project) -> None:
    seed_reviewed_chapter(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )
    before = _accepted_snapshot(project)

    # Upstream foundation đổi sau khi proposal được tạo.
    premise = storage.load_artifact(project, "premise")
    storage.save_artifact(
        project,
        premise.model_copy(
            update={
                "accepted_revision": premise.accepted_revision.model_copy(
                    update={"revision": 2}
                )
            }
        ),
        operation_id="op_bump_premise",
    )

    with pytest.raises(StaleDependencyError):
        reconcile_service.accept_reconciliation(project, chapter_id="ch_0001")

    assert _accepted_snapshot(project) == before
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.finalizing


# ---------------------------------------------------------------------------
# 4+5. Crash injection, recovery và retry không nhân đôi (case 5, 6)
# ---------------------------------------------------------------------------


def _prepare_accepted_proposal(project: Project) -> tuple[ChapterMetadata, str, str]:
    seed_reviewed_chapter(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )
    return chapter, chapter.final_candidate.markdown_ref, "op_commit_crash"


def _crash_at_paths(paths: list[Path], *, after: int | None = None):
    """`os.replace` giả lập lỗi ở lần ghi thứ `after` cho các path chỉ định."""
    real = os.replace
    wanted = [Path(path) for path in paths]
    calls = {"count": 0}

    def _replace(src: object, dst: object) -> None:
        dst_path = Path(dst)
        if dst_path in wanted:
            calls["count"] += 1
            if after is not None and calls["count"] == after:
                raise OSError("lỗi ghi đĩa giả lập")
            if after is None:
                raise OSError("lỗi ghi đĩa giả lập")
        real(src, dst)

    return _replace


@pytest.mark.parametrize(
    "crash_target",
    ["chapter", "reconcile_artifact", "markdown", "timeline"],
)
def test_crash_during_commit_recovers_to_consistent_state(
    project: Project, monkeypatch: pytest.MonkeyPatch, crash_target: str
) -> None:
    chapter, markdown_ref, operation_id = _prepare_accepted_proposal(project)
    target_path = {
        "chapter": project.paths.chapter_json("ch_0001"),
        "reconcile_artifact": project.paths.reconcile_dir("ch_0001")
        / "reconciliation_ch_0001.json",
        "markdown": project.root / markdown_ref,
        "timeline": project.paths.timeline_json,
    }[crash_target]
    before_final = storage.read_text(project.root / markdown_ref)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", _crash_at_paths([target_path]))
        with pytest.raises(Exception):
            reconcile_service.accept_reconciliation(
                project, chapter_id="ch_0001", operation_id=operation_id
            )

    # Không bao giờ cần người xử lý tay cho lỗi ghi đĩa mô phỏng.
    assert storage.requires_manual_recovery(project) is False
    if storage.needs_recovery(project):
        # Crash giữa commit: state chưa commit nên Writer chương sau vẫn khóa.
        with pytest.raises(ContextError):
            build_writer_context(project, chapter_id="ch_0002")
        report = reconcile_service.recover(project)
        assert report.status == "recovered"
        assert storage.pending_operation_ids(project) == []
        assert storage.read_lock(project) is None
    else:
        # Lỗi ở target đầu tiên: manifest abort sạch, không target nào đổi.
        assert storage.pending_operation_ids(project) == []
        assert storage.read_lock(project) is None
        assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.finalizing
        # Retry sau abort phải dùng operation_id mới (op cũ đã abort trong .ops/done).
        retried = reconcile_service.accept_reconciliation(
            project, chapter_id="ch_0001", operation_id=f"{operation_id}.retry"
        )
        assert retried.data.get("replayed") is not True

    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.status is ChapterStatus.final_reconciled
    assert chapter.final_candidate is None
    timeline = storage.load_timeline(project)
    assert [entry.chapter_number for entry in timeline.entries] == [1]
    assert validation.validate_artifact_payload(
        "reconciliation", storage.load_artifact(project, "reconciliation_ch_0001").accepted_revision.payload
    ).state is ValidationState.valid
    assert storage.read_text(project.root / markdown_ref) == before_final
    assert storage.needs_recovery(project) is False

    # Recovery lần hai idempotent: không nhân đôi timeline/relationship.
    second = reconcile_service.recover(project)
    assert second.status == "clean"
    assert second.applied_paths == []
    assert [entry.chapter_number for entry in storage.load_timeline(project).entries] == [1]
    assert len(storage.load_relationships(project).relationships) <= 1
    # Không để lại pending manifest/lock trong project tạm sau recovery.
    assert storage.pending_operation_ids(project) == []
    assert list(project.paths.ops_pending_dir.rglob("manifest.json")) == []
    assert project.paths.lock_path.exists() is False
    assert storage.is_locked(project) is False


def test_retry_same_operation_after_crash_does_not_duplicate_timeline(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    _chapter, markdown_ref, operation_id = _prepare_accepted_proposal(project)
    before_text = storage.read_text(project.root / markdown_ref)

    paths = [
        project.paths.chapter_json("ch_0001"),
        project.paths.timeline_json,
        project.paths.relationships_json,
    ]
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", _crash_at_paths(paths, after=2))
        with pytest.raises(Exception):
            reconcile_service.accept_reconciliation(
                project, chapter_id="ch_0001", operation_id=operation_id
            )

    report = reconcile_service.recover(project)
    assert report.status == "recovered"
    committed_timeline = storage.load_timeline(project).model_dump(mode="json")
    committed_relationships = storage.load_relationships(project).model_dump(mode="json")

    # Retry cùng operation_id sau khi recovery: pending op phải được recover trước.
    assert storage.pending_operation_ids(project) == []

    retry = reconcile_service.accept_reconciliation(
        project, chapter_id="ch_0001", operation_id=operation_id
    )
    assert retry.data.get("replayed") is True
    assert storage.load_timeline(project).model_dump(mode="json") == committed_timeline
    assert (
        storage.load_relationships(project).model_dump(mode="json") == committed_relationships
    )
    assert len(storage.load_timeline(project).entries) == 1
    assert storage.read_text(project.root / markdown_ref) == before_text


def test_history_before_snapshot_survives_crash_and_recovery(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`history/<op>/before/**` phải giữ nội dung **trước** commit, không bị ghi đè.

    `_record_history_for_transaction` từng được gọi lần hai sau khi đã replace xong
    mọi target, nên bản `before` bị copy đè bằng nội dung **sau** commit. Hệ quả:
    không thể audit/khôi phục accepted revision cũ sau crash + recovery.
    """
    chapter = seed_reviewed_chapter(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    finalizing = storage.load_chapter(project, "ch_0001")
    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=finalizing.final_candidate.markdown_ref,
    )
    reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )

    operation_id = "op_history_before"
    chapter_before = storage.read_text(project.paths.chapter_json("ch_0001"))
    with monkeypatch.context() as patch:
        patch.setattr(
            os,
            "replace",
            _crash_at_paths([project.paths.timeline_json, project.paths.chapter_json("ch_0001")]),
        )
        with pytest.raises(Exception):
            reconcile_service.accept_reconciliation(
                project, chapter_id="ch_0001", operation_id=operation_id
            )

    report = reconcile_service.recover(project)
    assert report.ok
    assert storage.pending_operation_ids(project) == []
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.final_reconciled

    before_path = (
        project.paths.history_dir
        / operation_id
        / "before"
        / "chapters"
        / "ch_0001"
        / "chapter.json"
    )
    assert before_path.is_file()
    assert before_path.read_text(encoding="utf-8") == chapter_before
    assert storage.read_json(before_path)["status"] == ChapterStatus.finalizing.value


def test_accept_twice_same_operation_id_is_idempotent(project: Project) -> None:
    chapter, _markdown_ref, operation_id = _prepare_accepted_proposal(project)

    first = reconcile_service.accept_reconciliation(
        project, chapter_id="ch_0001", operation_id=operation_id
    )
    timeline_after_first = storage.load_timeline(project).model_dump(mode="json")
    relationships_after_first = storage.load_relationships(project).model_dump(mode="json")

    second = reconcile_service.accept_reconciliation(
        project, chapter_id="ch_0001", operation_id=operation_id
    )

    assert second.data.get("replayed") is True
    assert second.operation_id == first.operation_id
    assert storage.load_timeline(project).model_dump(mode="json") == timeline_after_first
    assert (
        storage.load_relationships(project).model_dump(mode="json")
        == relationships_after_first
    )
    assert len(storage.load_timeline(project).entries) == 1


def test_retry_reconcile_after_accept_does_not_call_llm(project: Project) -> None:
    chapter, _markdown_ref, operation_id = _prepare_accepted_proposal(project)
    reconcile_service.accept_reconciliation(
        project, chapter_id="ch_0001", operation_id=operation_id
    )
    unused_client = FakeLLMClient([])

    result = reconcile_service.retry_reconcile(
        project, client=unused_client, chapter_id="ch_0001"
    )

    assert result.data.get("already_accepted") is True
    assert unused_client.calls == []
    assert len(storage.load_timeline(project).entries) == 1


def test_reject_and_cancel_keep_accepted_state(project: Project) -> None:
    seed_reviewed_chapter(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )
    accepted_state = _accepted_snapshot(project)

    rejected = reconcile_service.reject_reconciliation(project, chapter_id="ch_0001")
    assert rejected.data["status"] == "finalizing"
    assert _accepted_snapshot(project) == accepted_state

    cancelled = reconcile_service.cancel_finalizing(project, chapter_id="ch_0001")
    updated = storage.load_chapter(project, "ch_0001")
    assert cancelled.data["final_candidate"] is None
    assert updated.final_candidate is None
    assert updated.status is ChapterStatus.review_required
    assert updated.current_draft_revision == 1
    # Accepted state (timeline/relationship/final manuscript) không đổi.
    assert _accepted_snapshot(project) == accepted_state
    assert storage.load_timeline(project).entries == []
    assert storage.load_relationships(project).relationships == []


# ---------------------------------------------------------------------------
# 6. Retcon 3 chương (case 8 / T18)
# ---------------------------------------------------------------------------


def _fingerprint_files(project: Project, *, skip: set[str] | None = None) -> dict[str, str]:
    skipped = skip or set()
    result: dict[str, str] = {}
    for path in sorted(project.root.rglob("*")):
        if not path.is_file() or ".locks" in path.parts:
            continue
        relpath = str(path.relative_to(project.root)).replace("\\", "/")
        if relpath in skipped or ".tmp." in relpath:
            continue
        result[relpath] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def test_retcon_chapter_one_keeps_later_prose_and_blocks_writer(
    project: Project,
) -> None:
    seed_fully_finalized(project)
    later_prose = {
        chapter_id: storage.read_text(
            project.root / storage.load_chapter(project, chapter_id).final_revision.markdown_ref
        )
        for chapter_id in ("ch_0002", "ch_0003")
    }
    timeline_before = storage.load_timeline(project)
    assert timeline_before.latest_final_chapter == 3

    started = revision_service.start_retcon(project, chapter_id="ch_0001")
    assert started.data["final_still_canon"] is True
    chapter = storage.load_chapter(project, "ch_0001")
    # Final cũ vẫn là canon: chapter KHÔNG rời `final_reconciled`.
    assert chapter.final_revision is not None
    assert chapter.status is ChapterStatus.final_reconciled
    assert chapter.status is not ChapterStatus.draft
    assert chapter.current_draft_revision == 2
    assert started.data["retcon_marker_ref"] == "chapters/ch_0001/retcon/state.json"
    assert started.data["retcon_draft_revision"] == 2
    marker = revision_service.retcon_state(project, chapter_id="ch_0001")
    assert marker is not None and marker["final_still_canon"] is True
    # Draft retcon được copy từ final, chưa đụng final manuscript.
    retcon_text = storage.read_text(project.root / started.data["retcon_draft_markdown_ref"])
    assert retcon_text == storage.read_text(project.root / chapter.final_revision.markdown_ref)
    assert (project.root / started.data["retcon_marker_ref"]).is_file()

    # Final prose của chương 2/3 không bị đụng.
    for chapter_id, text in later_prose.items():
        markdown_ref = storage.load_chapter(project, chapter_id).final_revision.markdown_ref
        assert storage.read_text(project.root / markdown_ref) == text

    revision_service.reset_consistency_after_retcon(project, chapter_number=1)

    timeline = storage.load_timeline(project)
    assert timeline.latest_consistent_chapter == 1
    assert timeline.latest_final_chapter == 3  # không ghi lùi
    assert all(entry.chapter_number != 1 or not entry.stale for entry in timeline.entries)
    assert [entry.chapter_number for entry in timeline.entries if entry.stale] == [2, 3]

    relationships = storage.load_relationships(project)
    assert relationships.latest_consistent_chapter == 1
    assert all(item.stale for item in relationships.relationships if item.last_updated_chapter > 1)
    # `current` không bị ghi lùi về state chương 1.
    for item in relationships.relationships:
        if item.history:
            assert item.current == item.history[-1].current

    # Chain state sau chương 1 bị đánh dấu stale: Arbiter/UI thấy blocker và
    # context chương 2 chỉ còn dùng được state tới chương 1.
    blockers = revision_service.downstream_blockers(project)
    assert {item["chapter_number"] for item in blockers if item["kind"] == "timeline_entry"} == {
        2,
        3,
    }
    assert any(item["kind"] == "artifact" for item in blockers)
    bundle_ch2 = build_writer_context(project, chapter_id="ch_0002")
    assert [entry["chapter_number"] for entry in bundle_ch2.payload["timeline_as_of"]] == [1]

    # Reload lại từ disk: state vẫn đúng (case 10 của T23).
    assert storage.load_timeline(project).latest_consistent_chapter == 1
    assert storage.load_chapter(project, "ch_0001").final_revision.revision == 1


def test_reconcile_downstream_reopens_writer_in_order(project: Project) -> None:
    seed_fully_finalized(project)
    revision_service.start_retcon(project, chapter_id="ch_0001")
    revision_service.reset_consistency_after_retcon(project, chapter_number=1)

    # Chương 2 vẫn chạy được vì chương 1 **chưa** mất `final_reconciled` (final cũ
    # vẫn là canon), nhưng state chain chỉ còn tin cậy tới chương 1.
    bundle_ch2 = build_writer_context(project, chapter_id="ch_0002")
    assert bundle_ch2.for_chapter_number == 2
    assert [entry["chapter_number"] for entry in bundle_ch2.payload["timeline_as_of"]] == [1]
    assert storage.load_timeline(project).latest_consistent_chapter == 1

    # Retcon chương 1 hoàn tất: finalize + reconcile bản retcon.
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001", confirm_review=True)
    retcon_chapter = storage.load_chapter(project, "ch_0001")
    assert retcon_chapter.status is ChapterStatus.finalizing
    retcon_markdown_ref = retcon_chapter.final_candidate.markdown_ref
    retcon_proposal = make_proposal(
        chapter_number=1,
        prose_revision=retcon_chapter.final_candidate.prose_revision,
        markdown_ref=retcon_markdown_ref,
        timeline_status="Sở Dương sống sót nhưng mất cái nồi ở vách đá (bản retcon).",
    )
    reconcile_service.generate_reconciliation(
        project, client=_client(retcon_proposal), chapter_id="ch_0001"
    )
    reconcile_service.accept_reconciliation(project, chapter_id="ch_0001")
    rebuilt_chapter_one = storage.load_chapter(project, "ch_0001")
    assert rebuilt_chapter_one.status is ChapterStatus.final_reconciled
    assert rebuilt_chapter_one.final_revision.revision == 2
    assert "bản retcon" in storage.load_timeline(project).entries[0].status

    # Chương 2/3 bị stale bởi retcon; rebuild theo thứ tự để mở lại Writer.
    blockers = revision_service.downstream_blockers(project)
    assert any(item["kind"] in {"timeline_entry", "artifact"} for item in blockers)

    results = []
    for chapter_id in ("ch_0002", "ch_0003"):
        chapter = storage.load_chapter(project, chapter_id)
        markdown_ref = chapter.final_revision.markdown_ref
        proposal = default_proposal_builder(chapter.chapter_number, 1, markdown_ref)
        results.append(
            revision_service.reconcile_downstream(
                project, client=_client(proposal), chapter_id=chapter_id
            )
        )

    assert all(result.data.get("rebuilt_downstream") is True for result in results)
    timeline = storage.load_timeline(project)
    assert [entry.chapter_number for entry in timeline.entries] == [1, 2, 3]
    assert not any(entry.stale for entry in timeline.entries)
    assert all(
        not item.stale for item in storage.load_relationships(project).relationships
    )
    # Không nhân đôi entry khi rebuild lại cùng chương.
    assert len(timeline.entries) == 3
    assert timeline.latest_consistent_chapter == 3

    assert revision_service.downstream_blockers(project) == []
    bundle = build_writer_context(project, chapter_id="ch_0002")
    assert bundle.for_chapter_number == 2
    assert [entry["chapter_number"] for entry in bundle.payload["timeline_as_of"]] == [1]


def test_stale_artifact_can_be_reaccepted_after_upstream_revise(project: Project) -> None:
    revision_service.revise_premise(
        project,
        payload={
            "title": "Nồi Canh Bên Đường",
            "logline": "Bản premise đã revise.",
            "dramatic_question": "Giữ nguyên tắc khi thân thể không nghe lời?",
            "themes": ["quyền lựa chọn"],
            "tone_contract": ["Tiếng Việt tự nhiên."],
            "hard_constraints": ["Không reveal nguồn gốc cái nồi trong chương 1."],
            "non_goals": [],
        },
    )
    assert storage.load_artifact(project, "short_plan").status is ArtifactStatus.stale

    # Nội dung accepted vẫn dựa trên premise cũ (không pin premise) nên reaccept
    # không được phép: backend giữ `stale` và yêu cầu regenerate.
    with pytest.raises(ServiceError) as blocked:
        revision_service.reaccept_stale(project, artifact_id="short_plan")
    assert blocked.value.code in {"stale_dependency", "reaccept_validation_failed"}
    assert storage.load_artifact(project, "short_plan").status is ArtifactStatus.stale

    # Khi accepted revision đã pin đúng bản mới của nguồn, reaccept cập nhật pins
    # và đưa artifact về `accepted`.
    skeleton = storage.load_artifact(project, "skeleton_ch_0001")
    repinned = skeleton.model_copy(
        update={
            "accepted_revision": skeleton.accepted_revision.model_copy(
                update={
                    "dependency_pins": [
                        *skeleton.accepted_revision.dependency_pins,
                        DependencyPin(artifact_id="premise", revision=2, scope="premise"),
                    ]
                }
            )
        }
    )
    storage.save_artifact(project, repinned, operation_id="op_repin_skeleton")

    accepted = revision_service.reaccept_stale(project, artifact_id="skeleton_ch_0001")
    assert accepted.data["status"] == "accepted"
    rewritten = storage.load_artifact(project, "skeleton_ch_0001")
    assert rewritten.status is ArtifactStatus.accepted
    assert rewritten.accepted_revision.revision == 2
    assert {pin.artifact_id for pin in rewritten.accepted_revision.dependency_pins} >= {
        "premise",
        "short_plan",
    }
    assert rewritten.stale_reasons == []


def test_reaccept_stale_keeps_stale_when_scope_invalid(project: Project) -> None:
    """JSON/schema/ID không còn pass thì artifact giữ `stale`, không thành accepted."""
    envelope = storage.load_artifact(project, "skeleton_ch_0001")
    storage.save_artifact(
        project,
        envelope.model_copy(
            update={"accepted_revision": envelope.accepted_revision.model_copy(
                update={"payload": envelope.accepted_revision.payload.model_copy(
                    update={"sections": [envelope.accepted_revision.payload.sections[0].model_copy(
                        update={"character_ids": ["char_9999"]}
                    )]}
                )}
            )}
        ),
        operation_id="op_break_skeleton",
    )
    stale = lifecycle.mark_stale(
        storage.load_artifact(project, "skeleton_ch_0001"),
        source_artifact_id="premise",
        source_revision=2,
        reason="premise_revise",
        now=_STAMP,
    )
    storage.save_artifact(project, stale, operation_id="op_stale_skeleton")

    with pytest.raises(ValidationFailure) as excinfo:
        revision_service.reaccept_stale(project, artifact_id="skeleton_ch_0001")

    assert excinfo.value.code == "reaccept_validation_failed"
    assert storage.load_artifact(project, "skeleton_ch_0001").status is ArtifactStatus.stale


# ---------------------------------------------------------------------------
# 7+8. Revise Premise / Base Idea (case 7)
# ---------------------------------------------------------------------------


def test_revise_premise_marks_plans_and_skeleton_stale_but_keeps_final_prose(
    project: Project,
) -> None:
    seed_fully_finalized(project, chapters=("ch_0001",))
    final_text = storage.read_text(
        project.root / storage.load_chapter(project, "ch_0001").final_revision.markdown_ref
    )
    timeline_before = storage.load_timeline(project).model_dump(mode="json")

    result = revision_service.revise_premise(
        project,
        payload={
            "title": "Nồi Canh Bên Đường",
            "logline": "Sở Dương phải chọn giữa phản xạ nghề và mạng mình.",
            "dramatic_question": "Giữ nguyên tắc khi thân thể không nghe lời?",
            "themes": ["quyền lựa chọn", "lòng tốt"],
            "tone_contract": ["Tiếng Việt tự nhiên."],
            "hard_constraints": ["Không reveal nguồn gốc cái nồi trong chương 1."],
            "non_goals": [],
        },
    )

    assert result.data["revision"] == 2
    assert "long_plan" in result.data["stale_artifact_ids"]
    assert "short_plan" in result.data["stale_artifact_ids"]
    # Chapter 1 đã final nên Skeleton của nó không bị đánh dấu stale
    # (storage.md mục 10: Final Manuscript/final chapter không bị stale).
    assert "skeleton_ch_0001" not in result.data["stale_artifact_ids"]
    assert storage.load_artifact(project, "long_plan").status is ArtifactStatus.stale
    assert storage.load_artifact(project, "skeleton_ch_0001").status is ArtifactStatus.accepted
    # Base Idea và Final Manuscript không đổi.
    assert storage.load_co_create(project).base_idea.revision == 1
    assert (
        storage.read_text(
            project.root / storage.load_chapter(project, "ch_0001").final_revision.markdown_ref
        )
        == final_text
    )
    assert storage.load_timeline(project).model_dump(mode="json") == timeline_before
    # Dữ liệu stale không bị xóa.
    assert storage.load_artifact(project, "short_plan").accepted_revision is not None


def test_revise_premise_marks_non_final_skeleton_stale(project: Project) -> None:
    """Skeleton của chương **chưa** final bị stale khi Premise đổi."""
    revision_service.revise_premise(
        project,
        payload={
            "title": "Nồi Canh Bên Đường",
            "logline": "Bản premise đã revise.",
            "dramatic_question": "Giữ nguyên tắc khi thân thể không nghe lời?",
            "themes": ["quyền lựa chọn"],
            "tone_contract": ["Tiếng Việt tự nhiên."],
            "hard_constraints": ["Không reveal nguồn gốc cái nồi trong chương 1."],
            "non_goals": [],
        },
    )

    assert storage.load_artifact(project, "skeleton_ch_0001").status is ArtifactStatus.stale
    assert storage.load_artifact(project, "skeleton_ch_0002").status is ArtifactStatus.stale
    assert storage.load_artifact(project, "skeleton_ch_0003").status is ArtifactStatus.stale
    # Không xóa dữ liệu: accepted revision vẫn đọc được.
    assert storage.load_artifact(project, "skeleton_ch_0001").accepted_revision is not None
    # Writer bị chặn vì Skeleton stale.
    with pytest.raises(ContextError) as blocked:
        build_writer_context(project, chapter_id="ch_0001")
    assert blocked.value.code == "missing_dependency"


def test_revise_base_idea_marks_foundation_stale_without_deleting_data(
    project: Project,
) -> None:
    before_premise = storage.load_artifact(project, "premise").accepted_revision.revision

    result = revision_service.revise_base_idea(
        project,
        markdown="Base Idea v2: Sở Dương tỉnh dậy và phải chọn cứu người hay giữ mạng.",
    )

    assert result.data["revision"] == 2
    assert (project.root / "idea/base_idea.md").read_text(encoding="utf-8").startswith(
        "Base Idea v2"
    )
    assert storage.load_co_create(project).base_idea.revision == 2
    for artifact_id in ("premise", "characters", "world_rules", "foreshadow", "long_plan", "short_plan"):
        envelope = storage.load_artifact(project, artifact_id)
        assert envelope.status is ArtifactStatus.stale, artifact_id
        assert envelope.accepted_revision is not None
        assert envelope.accepted_revision.payload is not None
    assert storage.load_artifact(project, "premise").accepted_revision.revision == before_premise
    assert storage.load_artifact(project, "skeleton_ch_0001").status is ArtifactStatus.stale


def test_revise_premise_rejects_invalid_payload_without_mutation(project: Project) -> None:
    before = storage.load_artifact(project, "premise").model_dump(mode="json")

    with pytest.raises(ValidationFailure):
        revision_service.revise_premise(project, payload={"title": "Thiếu field"})

    assert storage.load_artifact(project, "premise").model_dump(mode="json") == before


def _valid_premise_payload() -> dict[str, Any]:
    return {
        "title": "Nồi Canh Bên Đường",
        "logline": "Bản premise đã revise.",
        "dramatic_question": "Giữ nguyên tắc khi thân thể không nghe lời?",
        "themes": ["quyền lựa chọn"],
        "tone_contract": ["Tiếng Việt tự nhiên."],
        "hard_constraints": ["Không reveal nguồn gốc cái nồi trong chương 1."],
        "non_goals": [],
    }


def _crash_on_target(paths: list[Path], *, after: int):
    """`os.replace` giả lập lỗi ở lần replace **target thật** thứ `after`.

    Ghi vào `.ops/pending/.../staged/...` không tính: đó là bước staging, không
    phải bước commit vào file thật. Nhờ vậy test đánh trúng đúng điểm crash
    trong `commit_operation` (storage.md mục 7).
    """
    real = os.replace
    wanted = {Path(path) for path in paths}
    calls = {"count": 0}
    pending_marker = f"{os.sep}.ops{os.sep}"

    def _replace(src: object, dst: object) -> None:
        dst_path = Path(dst)
        if dst_path in wanted and pending_marker not in str(dst_path):
            calls["count"] += 1
            if calls["count"] == after:
                raise OSError("lỗi ghi đĩa giả lập")
        real(src, dst)

    return _replace


def test_failed_revision_commit_aborts_cleanly_and_releases_lock(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T18: commit revision lỗi ở target đầu tiên phải abort sạch, không giữ lock.

    Lỗi ghi ở target đầu tiên khiến `commit_operation` abort hoàn toàn: accepted
    cũ nguyên vẹn, lock được release và không còn pending operation. Nếu bước
    abort hỏng, lock + pending manifest bị bỏ lại và mọi write action sau đó của
    project bị chặn cho tới khi recovery tay.
    """
    seed_fully_finalized(project, chapters=("ch_0001",))
    before = storage.load_artifact(project, "premise").model_dump(mode="json")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", _crash_at_paths([project.paths.premise_json]))
        with pytest.raises(Exception):
            revision_service.revise_premise(
                project, payload=_valid_premise_payload(), operation_id="op_revise_fail"
            )

    assert storage.load_artifact(project, "premise").model_dump(mode="json") == before
    assert storage.pending_operation_ids(project) == []
    assert storage.read_lock(project) is None
    assert storage.is_locked(project) is False
    assert storage.needs_recovery(project) is False

    # Project vẫn ghi được ngay sau lỗi, không cần recovery tay.
    retried = revision_service.revise_premise(
        project, payload=_valid_premise_payload(), operation_id="op_revise_retry"
    )
    assert retried.data["revision"] == 2


def test_revise_base_idea_crash_between_targets_recovers_without_duplication(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T18: crash giữa hai target của `revise_base_idea` phải recovery được.

    `revise_base_idea` ghi `idea/base_idea.md` rồi `idea/base_idea.meta.json`. Crash ở
    target thứ hai để lại state nửa cũ nửa mới, nên `_abort_quietly` của service
    phải chấp nhận rằng abort sạch là bất khả và để `recover_pending()` hoàn tất
    commit. Recovery không nhân đôi và release lock; retry cùng `operation_id`
    không tạo revision thứ ba.

    Giới hạn đã biết: metadata `revision` của Base Idea nằm trong `co_create.json`
    và được ghi **sau** transaction (`storage.save_co_create`), nên crash giữa
    transaction để lại `base_idea.md` đã ghi trong khi revision metadata chưa lên.
    Đây là gap đã có của T18, không phải hành vi mới; test ghi nhận đúng thực tế
    thay vì che nó.
    """
    before_revision = storage.load_co_create(project).base_idea.revision
    new_markdown = "Base Idea v2: Sở Dương phải chọn giữa cứu người và giữ mạng."

    with monkeypatch.context() as patch:
        patch.setattr(
            os,
            "replace",
            _crash_on_target(
                [project.paths.base_idea_md, project.paths.base_idea_meta_json], after=2
            ),
        )
        with pytest.raises(Exception):
            revision_service.revise_base_idea(
                project,
                markdown=new_markdown,
                operation_id="op_base_idea_crash",
            )

    # Crash giữa commit: dấu vết pending + lock còn lại đúng như thiết kế.
    assert storage.needs_recovery(project) is True

    report = storage.recover_pending(project)
    assert report.status == "recovered"
    assert storage.pending_operation_ids(project) == []
    assert storage.read_lock(project) is None
    assert storage.is_locked(project) is False

    # Commit đã hoàn tất đúng nội dung đã stage, không nhân đôi file.
    assert (project.root / "idea/base_idea.md").read_text(encoding="utf-8") == new_markdown
    meta = storage.read_json(project.paths.base_idea_meta_json)
    assert meta["revision"] == before_revision + 1

    # Recovery lần hai idempotent.
    assert storage.recover_pending(project).status == "clean"

    # Retry cùng operation_id trả kết quả cũ; không có revision thứ ba nào được ghi.
    replay = revision_service.revise_base_idea(
        project,
        markdown=new_markdown,
        operation_id="op_base_idea_crash",
    )
    assert replay.data.get("replayed") is True
    assert storage.read_json(project.paths.base_idea_meta_json)["revision"] == before_revision + 1
    assert (project.root / "idea/base_idea.md").read_text(encoding="utf-8") == new_markdown


# ---------------------------------------------------------------------------
# 9. Impact report không mutation
# ---------------------------------------------------------------------------


def test_impact_report_only_adds_report_files(project: Project) -> None:
    seed_fully_finalized(project)
    source_change = SourceChange(
        item_kind="chapter_final",
        item_id="ch_0001",
        from_revision=1,
        to_candidate_revision=2,
    )
    report = {
        "source_change": source_change.model_dump(mode="json"),
        "affected_items": [
            {
                "item_kind": "skeleton",
                "item_id": "skeleton_ch_0002",
                "reason": "Bản retcon đổi vị trí đồ vật mà Skeleton chương 2 còn dựa vào.",
                "severity": "major",
                "suggested_action": "Review lại Skeleton chương 2.",
            }
        ],
        "risk_summary": "Chỉ ảnh hưởng trong phạm vi hai chương đã cấp.",
        "suggested_actions": ["Review Skeleton chương 2; không tự rewrite prose."],
    }
    client = _client(json.dumps(report, ensure_ascii=False))
    before = _fingerprint_files(project)
    timeline_before = storage.load_timeline(project).model_dump(mode="json")
    artifacts_before = {
        artifact_id: storage.load_artifact(project, artifact_id).status
        for artifact_id in storage.list_artifact_ids(project)
    }

    result = revision_service.generate_impact_report(
        project,
        client=client,
        source_change=source_change,
        before_content="An mang theo phong thư.",
        after_content="An để phong thư trong ngăn kéo.",
        analysis_scope="chương 1-2",
    )

    assert result.artifact_id == "impact_report_ch_0001"
    report_envelope = storage.load_artifact(project, result.artifact_id)
    assert report_envelope.status is ArtifactStatus.draft
    assert report_envelope.candidate_revision is not None
    assert not report_envelope.candidate_revision.dependency_pins == []

    after = _fingerprint_files(project)
    # File được phép thêm/đổi: raw output, report candidate, error record và
    # bookkeeping của transaction (history/, .ops/) — **không** phải story state.
    allowed_prefixes = (
        "raw/",
        "history/",
        ".ops/",
        ".locks/",
        "chapters/ch_0001/impact/",
    )
    report_relpath = "chapters/ch_0001/impact/impact_report_ch_0001.json"
    changed = {
        relpath
        for relpath, digest in after.items()
        if before.get(relpath) != digest
        and not relpath.startswith(allowed_prefixes)
        and relpath != report_relpath
    }
    added = {
        relpath
        for relpath in after
        if relpath not in before
        and not relpath.startswith(allowed_prefixes)
        and relpath != report_relpath
    }
    assert changed == set()
    assert added == set()
    assert storage.load_timeline(project).model_dump(mode="json") == timeline_before
    for artifact_id, status in artifacts_before.items():
        assert storage.load_artifact(project, artifact_id).status is status, artifact_id


def test_impact_report_invalid_json_does_not_mutate(project: Project) -> None:
    seed_fully_finalized(project)
    before = _fingerprint_files(project)
    timeline_before = storage.load_timeline(project).model_dump(mode="json")
    relationships_before = storage.load_relationships(project).model_dump(mode="json")

    result = revision_service.generate_impact_report(
        project,
        client=_client('{"source_change": "sai shape"}'),
        source_change=SourceChange(item_kind="chapter_final", item_id="ch_0001"),
    )

    assert result.data["error_record_ref"]
    assert (project.root / result.data["raw_output_ref"]).is_file()
    assert storage.load_artifact(project, "impact_report_ch_0001") is None

    after = _fingerprint_files(project)
    allowed_prefixes = ("raw/", "history/", ".ops/", ".locks/", "chapters/ch_0001/impact/")
    unexpected = {
        relpath
        for relpath in after
        if relpath not in before and not relpath.startswith(allowed_prefixes)
    }
    assert unexpected == set()
    assert storage.load_timeline(project).model_dump(mode="json") == timeline_before
    assert (
        storage.load_relationships(project).model_dump(mode="json") == relationships_before
    )
    assert storage.load_chapter(project, "ch_0001").final_revision is not None


def test_edit_reconciliation_candidate_validates_manually_edited_json(
    project: Project,
) -> None:
    """User sửa JSON tay: validate lại, accepted state không đổi."""
    _finalizing_chapter(project)
    before = _accepted_snapshot(project)
    chapter = storage.load_chapter(project, "ch_0001")
    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
        timeline_status="Bản gốc từ LLM.",
    )
    reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )

    # JSON sửa tay sai ID → từ chối, candidate cũ giữ nguyên.
    with pytest.raises(ValidationFailure):
        reconcile_service.edit_reconciliation_candidate(
            project,
            chapter_id="ch_0001",
            payload={
                "chapter_id": "ch_0001",
                "chapter_number": 1,
                "source_final_candidate": {
                    "prose_revision": 1,
                    "markdown_ref": chapter.final_candidate.markdown_ref,
                },
                "timeline": {
                    "time": "Đêm 1",
                    "location": "Vách đá",
                    "status": "Bản sửa tay sai.",
                },
                "relationship_updates": [
                    {"character_ids": ["char_0001", "char_9999"], "current": "Sai ID."}
                ],
                "notes": [],
            },
        )
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.finalizing
    assert _accepted_snapshot(project) == before

    # JSON sửa tay hợp lệ → candidate mới, vẫn chờ user accept.
    edited = reconcile_service.edit_reconciliation_candidate(
        project,
        chapter_id="ch_0001",
        payload={
            "chapter_id": "ch_0001",
            "chapter_number": 1,
            "source_final_candidate": {
                "prose_revision": 1,
                "markdown_ref": chapter.final_candidate.markdown_ref,
            },
            "timeline": {
                "time": "Đêm 1",
                "location": "Vách đá",
                "status": "Bản người dùng sửa tay.",
            },
            "relationship_updates": [],
            "notes": ["Sửa tay sau khi LLM trả thiếu chi tiết."],
        },
    )
    assert edited.artifact_id == "reconciliation_ch_0001"
    assert storage.load_artifact(project, "reconciliation_ch_0001").status is ArtifactStatus.draft
    assert _accepted_snapshot(project) == before

    accepted = reconcile_service.accept_reconciliation(project, chapter_id="ch_0001")
    assert accepted.data["status"] == "final_reconciled"
    assert storage.load_timeline(project).entries[0].status == "Bản người dùng sửa tay."


def test_auto_accept_structured_invalid_proposal_keeps_finalizing(project: Project) -> None:
    project.config = project.config.model_copy(update={"auto_accept_structured": True})
    seed_reviewed_chapter(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")

    proposal = make_proposal(
        chapter_number=2,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    result = reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )

    assert result.data.get("auto_accepted") is None
    assert result.data["reconciliation_status"] == "failed"
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.finalizing
    assert storage.load_timeline(project).entries == []


# ---------------------------------------------------------------------------
# Recovery helper + pending state cho UI/Arbiter
# ---------------------------------------------------------------------------


def test_pending_reconciliation_reports_finalizing_state(project: Project) -> None:
    seed_reviewed_chapter(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    proposal = make_proposal(
        chapter_number=1,
        prose_revision=1,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    reconcile_service.generate_reconciliation(
        project, client=_client(proposal), chapter_id="ch_0001"
    )

    pending = reconcile_service.pending_reconciliation(project, chapter_id="ch_0001")
    assert pending is not None
    assert pending["status"] == "finalizing"
    assert pending["reconciliation_status"] == "draft"
    assert pending["final_candidate"]["prose_revision"] == 1
    assert pending["needs_recovery"] is False

    reconcile_service.accept_reconciliation(project, chapter_id="ch_0001")
    assert reconcile_service.pending_reconciliation(project, chapter_id="ch_0001")[
        "status"
    ] == "final_reconciled"
    assert reconcile_service.pending_reconciliation(project, chapter_id="ch_9999") is None
