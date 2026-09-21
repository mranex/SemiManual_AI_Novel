"""Artifact lifecycle, guard backend và stale propagation (T09).

Bao phủ: candidate/accept/reject/stale/reaccept, chống accept lặp và accept
candidate cũ, guard Writer/Review/Finalize, và stale propagation theo bảng
storage.md mục 10.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from novel_ai.core import lifecycle, storage
from novel_ai.core.lifecycle import (
    STALE_CHANGE_BASE_IDEA_REVISE,
    STALE_CHANGE_CHARACTERS_APPEND,
    STALE_CHANGE_WORLD_RULES_APPEND,
    GuardBlockedError,
    LifecycleError,
)
from novel_ai.core.models import (
    ArtifactStatus,
    ChapterMetadata,
    ChapterStatus,
    DependencyPin,
    HumanReviewRecord,
    PayloadSource,
    ProseRevision,
    SourceType,
    ValidationIssue,
    ValidationResult,
    ValidationState,
)
from novel_ai.core.project import Project
from novel_ai.core.storage import StaleCandidateError

_STAMP = "2026-09-19T12:00:00+07:00"


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def _invalid() -> ValidationResult:
    return ValidationResult(
        state=ValidationState.invalid,
        errors=[
            ValidationIssue(
                path="/payload/title", code="missing_field", message="Thiếu field bắt buộc."
            )
        ],
    )


def _source(prompt_id: str = "architect.premise.v1") -> PayloadSource:
    return PayloadSource(source_type=SourceType.llm, prompt_id=prompt_id)


def _new_project(tmp_path: Path) -> Project:
    return Project.create(tmp_path / "projects", "Nồi Canh Bên Đường")


def _accept_artifact(
    project: Project,
    artifact_type: str,
    artifact_id: str,
    payload: Any,
    *,
    dependency_pins: tuple[DependencyPin, ...] = (),
) -> Any:
    envelope = lifecycle.new_artifact(artifact_type, artifact_id)
    candidate = lifecycle.set_candidate(
        envelope,
        payload,
        source=_source(f"{artifact_type}.v1"),
        dependency_pins=dependency_pins,
        validation=_valid(),
    )
    accepted = lifecycle.accept_candidate(candidate, validation=_valid(), now=_STAMP)
    storage.save_artifact(project, accepted, operation_id=f"op_{artifact_id}")
    return accepted


def _skeleton_payload(chapter_id: str, chapter_number: int, **section: Any) -> dict[str, Any]:
    base = {
        "section_id": "section_0001",
        "index": 1,
        "type": "action",
        "instruction": "Viết cảnh mở đầu.",
        "purpose": "Thiết lập tình huống.",
    }
    base.update(section)
    return {
        "chapter_id": chapter_id,
        "chapter_number": chapter_number,
        "global_constraints": [],
        "sections": [base],
    }


def _chapter(
    chapter_id: str,
    chapter_number: int,
    status: ChapterStatus,
    *,
    previous: str | None = None,
    skeleton_pin: DependencyPin | None = None,
    complete_draft: bool = False,
    human_review: HumanReviewRecord | None = None,
) -> ChapterMetadata:
    drafts = []
    current = None
    if complete_draft:
        drafts = [
            ProseRevision(
                revision=1,
                markdown_ref="draft_r0001.md",
                source_type=SourceType.llm,
                is_complete=True,
                created_at=_STAMP,
            )
        ]
        current = 1
    return ChapterMetadata(
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        title=f"Chương {chapter_number}",
        status=status,
        previous_chapter_id=previous,
        short_plan_pin=DependencyPin(
            artifact_id="short_plan", revision=1, scope="short_plan", chapter_id=chapter_id
        ),
        skeleton_pin=skeleton_pin,
        drafts=drafts,
        current_draft_revision=current,
        human_review=human_review,
    )


def _skeleton_pin(chapter_id: str, revision: int = 1) -> DependencyPin:
    return DependencyPin(
        artifact_id=f"skeleton_{chapter_id}",
        revision=revision,
        scope="skeleton",
        chapter_id=chapter_id,
    )


# ---------------------------------------------------------------------------
# Transition
# ---------------------------------------------------------------------------


def test_artifact_id_for_matches_contract() -> None:
    assert lifecycle.artifact_id_for("premise") == "premise"
    assert lifecycle.artifact_id_for("short_plan") == "short_plan"
    assert lifecycle.artifact_id_for("skeleton", chapter_id="ch_0001") == "skeleton_ch_0001"
    assert (
        lifecycle.artifact_id_for("review_report", chapter_id="ch_0002")
        == "review_report_ch_0002"
    )
    assert (
        lifecycle.artifact_id_for("reconciliation", chapter_id="ch_0001")
        == "reconciliation_ch_0001"
    )
    assert (
        lifecycle.artifact_id_for("rolling_patch", chapter_id="arc_0001")
        == "rolling_patch_arc_0001"
    )
    assert (
        lifecycle.artifact_id_for("impact_report", chapter_id="ch_0001")
        == "impact_report_ch_0001"
    )


def test_artifact_id_for_requires_scope() -> None:
    with pytest.raises(LifecycleError):
        lifecycle.artifact_id_for("skeleton")
    with pytest.raises(LifecycleError):
        lifecycle.artifact_id_for("rolling_patch")
    with pytest.raises(LifecycleError):
        lifecycle.artifact_id_for("khong_co_type")


def test_new_artifact_is_missing() -> None:
    envelope = lifecycle.new_artifact("premise", "premise")

    assert envelope.status is ArtifactStatus.missing
    assert envelope.accepted_revision is None
    assert envelope.candidate_revision is None
    assert envelope.stale_reasons == []


def test_set_candidate_keeps_accepted_and_bumps_revision(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    accepted = _accept_artifact(project, "premise", "premise", {"title": "A", "logline": "L"})
    loaded = storage.load_artifact(project, "premise")

    candidate = lifecycle.set_candidate(
        loaded, {"title": "B", "logline": "L"}, source=_source(), validation=_valid()
    )

    assert candidate.status is ArtifactStatus.draft
    assert candidate.accepted_revision.revision == accepted.accepted_revision.revision == 1
    assert candidate.accepted_revision.payload.title == "A"
    assert candidate.candidate_revision.revision == 2
    assert candidate.candidate_revision.payload.title == "B"
    # Envelope đầu vào không bị mutate.
    assert loaded.candidate_revision is None


def test_set_candidate_reuses_revision_for_same_id_map_retry() -> None:
    envelope = lifecycle.new_artifact("long_plan", "long_plan")
    source = PayloadSource(
        source_type=SourceType.llm, prompt_id="long_plan.v1", id_map={"tmp_arc_1": "arc_0001"}
    )
    first = lifecycle.set_candidate(envelope, _long_plan_payload(), source=source)

    retry = lifecycle.set_candidate(first, _long_plan_payload(title="Khác"), source=source)

    assert retry.candidate_revision.revision == first.candidate_revision.revision == 1

    new_source = PayloadSource(source_type=SourceType.llm, prompt_id="long_plan.v1")
    assert lifecycle.set_candidate(retry, _long_plan_payload(), source=new_source).candidate_revision.revision == 1


def test_set_candidate_rejects_payload_out_of_contract() -> None:
    envelope = lifecycle.new_artifact("premise", "premise")

    with pytest.raises(LifecycleError) as excinfo:
        lifecycle.set_candidate(envelope, {"logline": "thiếu title"}, source=_source())

    assert excinfo.value.code == "invalid_candidate_payload"
    assert envelope.candidate_revision is None


def test_accept_candidate_moves_candidate_and_clears_stale() -> None:
    envelope = lifecycle.new_artifact("premise", "premise")
    candidate = lifecycle.set_candidate(
        envelope, {"title": "A", "logline": "L"}, source=_source(), validation=_valid()
    )
    marked = lifecycle.mark_stale(
        lifecycle.accept_candidate(candidate, validation=_valid(), now=_STAMP),
        source_artifact_id="base_idea",
        source_revision=2,
        reason="Base Idea đổi",
    )
    assert marked.status is ArtifactStatus.stale

    refreshed = lifecycle.set_candidate(
        marked, {"title": "B", "logline": "L"}, source=_source(), validation=_valid()
    )
    accepted = lifecycle.accept_candidate(refreshed, validation=_valid(), now=_STAMP)

    assert accepted.status is ArtifactStatus.accepted
    assert accepted.candidate_revision is None
    assert accepted.accepted_revision.revision == 2
    assert accepted.accepted_revision.accepted_at == _STAMP
    assert accepted.accepted_revision.accepted_by == "user"
    assert accepted.stale_reasons == []
    # Accepted cũ vẫn đọc được trong envelope trước đó (audit).
    assert marked.accepted_revision.revision == 1


def test_accept_candidate_requires_candidate() -> None:
    envelope = lifecycle.new_artifact("premise", "premise")

    with pytest.raises(LifecycleError) as excinfo:
        lifecycle.accept_candidate(envelope)

    assert excinfo.value.code == "missing_candidate"


def test_accept_candidate_refuses_invalid_validation() -> None:
    envelope = lifecycle.new_artifact("premise", "premise")
    candidate = lifecycle.set_candidate(
        envelope, {"title": "A", "logline": "L"}, source=_source(), validation=_invalid()
    )

    with pytest.raises(LifecycleError) as excinfo:
        lifecycle.accept_candidate(candidate, validation=_invalid())

    assert excinfo.value.code == "invalid_candidate"
    assert candidate.accepted_revision is None


def test_accept_candidate_refuses_unchecked_validation() -> None:
    """Candidate `not_checked` không được vào canon (F-B6 của review T24).

    `ValidationResult()` mặc định là `not_checked`; trước đây guard chỉ chặn
    `invalid` nên backend không tự enforce "validate trước khi merge".
    """
    envelope = lifecycle.new_artifact("premise", "premise")
    candidate = lifecycle.set_candidate(
        envelope, {"title": "A", "logline": "L"}, source=_source()
    )
    assert candidate.candidate_revision.validation.state is ValidationState.not_checked

    with pytest.raises(LifecycleError) as excinfo:
        lifecycle.accept_candidate(candidate, now=_STAMP)

    assert excinfo.value.code == "validation_required"
    assert candidate.accepted_revision is None

    # Validate thật rồi accept thì đi qua bình thường.
    accepted = lifecycle.accept_candidate(candidate, validation=_valid(), now=_STAMP)
    assert accepted.status is ArtifactStatus.accepted


def test_reject_candidate_keeps_accepted_revision() -> None:
    never_accepted = lifecycle.new_artifact("premise", "premise")
    with_candidate = lifecycle.set_candidate(
        never_accepted, {"title": "A", "logline": "L"}, source=_source()
    )
    rejected = lifecycle.reject_candidate(with_candidate)
    assert rejected.status is ArtifactStatus.missing
    assert rejected.candidate_revision is None

    accepted = lifecycle.accept_candidate(
        lifecycle.set_candidate(
            never_accepted, {"title": "A", "logline": "L"}, source=_source()
        ),
        validation=_valid(),
        now=_STAMP,
    )
    rejected_with_accepted = lifecycle.reject_candidate(
        lifecycle.set_candidate(accepted, {"title": "B", "logline": "L"}, source=_source())
    )
    assert rejected_with_accepted.status is ArtifactStatus.rejected
    assert rejected_with_accepted.accepted_revision.revision == 1
    assert rejected_with_accepted.candidate_revision is None


def test_mark_stale_records_reason_and_range() -> None:
    accepted = lifecycle.accept_candidate(
        lifecycle.set_candidate(
            lifecycle.new_artifact("premise", "premise"),
            {"title": "A", "logline": "L"},
            source=_source(),
        ),
        validation=_valid(),
        now=_STAMP,
    )

    marked = lifecycle.mark_stale(
        accepted,
        source_artifact_id="base_idea",
        source_revision=3,
        reason="Base Idea đổi",
        affected_range=(2, 4),
        now=_STAMP,
    )

    assert marked.status is ArtifactStatus.stale
    assert marked.accepted_revision.revision == 1
    reason = marked.stale_reasons[-1]
    assert reason.source_artifact_id == "base_idea"
    assert reason.source_revision == 3
    assert reason.affected_range.start == 2 and reason.affected_range.end == 4
    assert reason.can_reaccept is True

    # Chưa từng accepted thì giữ nguyên status thay vì tự thành stale.
    pending = lifecycle.set_candidate(
        lifecycle.new_artifact("premise", "premise"),
        {"title": "A", "logline": "L"},
        source=_source(),
    )
    assert (
        lifecycle.mark_stale(
            pending, source_artifact_id="base_idea", source_revision=1, reason="x"
        ).status
        is ArtifactStatus.draft
    )


def test_reaccept_refreshes_pins_and_revision() -> None:
    accepted = lifecycle.accept_candidate(
        lifecycle.set_candidate(
            lifecycle.new_artifact("premise", "premise"),
            {"title": "A", "logline": "L"},
            source=_source(),
        ),
        validation=_valid(),
        now=_STAMP,
    )
    marked = lifecycle.mark_stale(
        accepted, source_artifact_id="base_idea", source_revision=2, reason="Base Idea đổi"
    )

    refreshed = lifecycle.reaccept(
        marked,
        dependency_pins=[DependencyPin(artifact_id="base_idea", revision=2, scope="base_idea")],
        now=_STAMP,
    )

    assert refreshed.status is ArtifactStatus.accepted
    assert refreshed.stale_reasons == []
    assert refreshed.accepted_revision.revision == 2
    assert refreshed.accepted_revision.dependency_pins[0].revision == 2
    assert refreshed.accepted_revision.payload.title == "A"


def test_reaccept_requires_accepted_revision() -> None:
    with pytest.raises(LifecycleError) as excinfo:
        lifecycle.reaccept(lifecycle.new_artifact("premise", "premise"))

    assert excinfo.value.code == "missing_accepted"


def test_reaccept_refuses_content_that_no_longer_validates() -> None:
    payload = _long_plan_payload(duplicate_arc=True)
    accepted = lifecycle.accept_candidate(
        lifecycle.set_candidate(
            lifecycle.new_artifact("long_plan", "long_plan"), payload, source=_source()
        ),
        validation=_valid(),
        now=_STAMP,
    )
    marked = lifecycle.mark_stale(
        accepted, source_artifact_id="premise", source_revision=2, reason="Premise đổi"
    )

    with pytest.raises(LifecycleError) as excinfo:
        lifecycle.reaccept(marked)

    assert excinfo.value.code == "reaccept_validation_failed"


def test_stale_pin_mismatches() -> None:
    pins = [
        DependencyPin(artifact_id="premise", revision=1, scope="premise"),
        DependencyPin(artifact_id="base_idea", revision=1, scope="base_idea"),
        DependencyPin(artifact_id="current_timeline", revision=1, scope="timeline_as_of"),
    ]

    assert lifecycle.stale_pin_mismatches(pins, {"premise": 1, "base_idea": 2}) == [
        "base_idea: pin r1 nhưng accepted hiện tại là r2 (scope base_idea)"
    ]
    assert lifecycle.stale_pin_mismatches(pins, {}) == []


def test_stale_dependencies_reports_non_accepted_status(
    t09_linked_project: Project,
) -> None:
    pins = [
        DependencyPin(artifact_id="premise", revision=1, scope="premise"),
        DependencyPin(artifact_id="base_idea", revision=1, scope="base_idea"),
        DependencyPin(artifact_id="current_timeline", revision=1, scope="timeline_as_of"),
    ]
    assert lifecycle.stale_dependencies(t09_linked_project, pins) == []

    lifecycle.mark_downstream_stale(
        t09_linked_project,
        source_artifact_id="base_idea",
        source_revision=2,
        change=STALE_CHANGE_BASE_IDEA_REVISE,
    )

    reasons = lifecycle.stale_dependencies(t09_linked_project, pins)

    assert len(reasons) == 1
    assert "premise" in reasons[0]
    assert "stale" in reasons[0]


# ---------------------------------------------------------------------------
# Accept lặp và candidate cũ
# ---------------------------------------------------------------------------


def test_repeated_accept_same_operation_does_not_duplicate_revision(tmp_path: Path) -> None:
    """Bấm lại đúng action accept không tạo accepted revision thứ hai.

    Ghi candidate (`set_candidate`) và accept là **hai write khác nhau**, nên dùng
    hai `operation_id` khác nhau — đúng như tầng service làm (mỗi bước có
    `operation_id` riêng). Retry cùng action accept với cùng `operation_id` là
    no-op; retry cùng `operation_id` nhưng nội dung khác bị từ chối
    (`test_same_operation_id_with_different_content_is_rejected`).
    """
    project = _new_project(tmp_path)
    envelope = lifecycle.new_artifact("premise", "premise")
    candidate = lifecycle.set_candidate(
        envelope, {"title": "A", "logline": "L"}, source=_source(), validation=_valid()
    )
    storage.save_artifact(project, candidate, operation_id="op_premise_candidate")
    accepted = lifecycle.accept_candidate(candidate, validation=_valid(), now=_STAMP)
    storage.save_artifact(project, accepted, operation_id="op_premise_accept")

    # Retry cùng action/operation (UI rerun hoặc người dùng bấm lại).
    retried = lifecycle.accept_candidate(candidate, validation=_valid(), now=_STAMP)
    storage.save_artifact(project, retried, operation_id="op_premise_accept")

    loaded = storage.load_artifact(project, "premise")
    assert loaded.accepted_revision.revision == 1
    assert loaded.candidate_revision is None
    assert len(storage.list_artifact_ids(project)) == 1
    history_manifest = storage.read_json(
        project.paths.history_dir / "op_premise_accept" / "manifest.json"
    )
    assert len(history_manifest["write_set"]) == 1


def test_old_candidate_is_rejected_as_stale(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _accept_artifact(project, "premise", "premise", {"title": "A", "logline": "L"})

    first = storage.load_artifact(project, "premise")
    candidate = lifecycle.set_candidate(
        first, {"title": "B", "logline": "L"}, source=_source(), validation=_valid()
    )
    acceptance = lifecycle.accept_candidate(candidate, validation=_valid(), now=_STAMP)
    storage.save_artifact(project, acceptance, operation_id="op_premise_r2")

    with pytest.raises(StaleCandidateError):
        storage.save_artifact(project, candidate, operation_id="op_premise_stale")
    with pytest.raises(StaleCandidateError):
        storage.save_artifact(project, first, operation_id="op_premise_stale_accepted")

    # Accepted r2 trên disk không bị thay bằng candidate cũ.
    loaded = storage.load_artifact(project, "premise")
    assert loaded.accepted_revision.revision == 2
    assert loaded.accepted_revision.payload.title == "B"


def test_candidate_with_stale_dependency_pin_is_rejected(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _accept_artifact(project, "long_plan", "long_plan", _long_plan_payload())

    first = storage.load_artifact(project, "long_plan")
    regeneration = lifecycle.set_candidate(
        first, _long_plan_payload(title="Tập 1 (sửa)"), source=_source(), validation=_valid()
    )
    storage.save_artifact(
        project,
        lifecycle.accept_candidate(regeneration, validation=_valid(), now=_STAMP),
        operation_id="op_long_plan_r2",
    )

    short_plan = lifecycle.set_candidate(
        lifecycle.new_artifact("short_plan", "short_plan"),
        {
            "arc_id": "arc_0001",
            "chapters": [
                {
                    "chapter_id": "ch_0001",
                    "chapter_number": 1,
                    "title": "Đường vắng",
                    "summary": "Mở đầu.",
                    "chapter_goal": "Thiết lập.",
                }
            ],
        },
        source=_source("short_plan.v1"),
        dependency_pins=[
            DependencyPin(artifact_id="long_plan", revision=1, scope="long_plan")
        ],
        validation=_valid(),
    )

    with pytest.raises(StaleCandidateError) as excinfo:
        storage.save_artifact(project, short_plan, operation_id="op_short_plan")

    assert "long_plan" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def test_guard_result_raise_if_blocked() -> None:
    blocked = lifecycle.GuardResult(False, "skeleton_missing", ["Chưa có Skeleton."])
    with pytest.raises(GuardBlockedError) as excinfo:
        blocked.raise_if_blocked()

    assert excinfo.value.code == "skeleton_missing"
    assert excinfo.value.reasons == ["Chưa có Skeleton."]
    assert excinfo.value.details["reasons"] == ["Chưa có Skeleton."]

    lifecycle.GuardResult(True, "ok", []).raise_if_blocked()


def test_guard_writer_blocks_when_skeleton_missing(t09_linked_project: Project) -> None:
    result = lifecycle.guard_writer(t09_linked_project, "ch_0002")

    assert result.allowed is False
    assert result.code == "skeleton_missing"
    with pytest.raises(GuardBlockedError) as excinfo:
        result.raise_if_blocked()
    assert excinfo.value.code == "skeleton_missing"


def test_guard_writer_blocks_chapter_two_when_chapter_one_not_final(
    t09_linked_project: Project,
) -> None:
    _accept_artifact(
        t09_linked_project, "skeleton", "skeleton_ch_0002", _skeleton_payload("ch_0002", 2)
    )
    storage.save_chapter(
        t09_linked_project,
        storage.load_chapter(t09_linked_project, "ch_0002").model_copy(
            update={"skeleton_pin": _skeleton_pin("ch_0002"), "status": ChapterStatus.skeleton_ready}
        ),
        operation_id="op_pin_ch2",
    )
    assert lifecycle.guard_writer(t09_linked_project, "ch_0002").allowed is True

    storage.save_chapter(
        t09_linked_project,
        storage.load_chapter(t09_linked_project, "ch_0001").model_copy(
            update={"status": ChapterStatus.review_required}
        ),
        operation_id="op_regress_ch1",
    )

    result = lifecycle.guard_writer(t09_linked_project, "ch_0002")

    assert result.allowed is False
    assert result.code == "previous_chapter_not_finalized"
    assert any("final_reconciled" in reason for reason in result.reasons)

    with pytest.raises(GuardBlockedError) as excinfo:
        result.raise_if_blocked()
    assert excinfo.value.code == "previous_chapter_not_finalized"


def test_guard_writer_allows_chapter_one_with_accepted_skeleton(
    t09_linked_project: Project,
) -> None:
    result = lifecycle.guard_writer(t09_linked_project, "ch_0001")

    assert result.allowed is True
    assert result.code == "ok"
    assert any("Skeleton" in reason for reason in result.reasons)


def test_guard_writer_blocks_stale_skeleton(t09_linked_project: Project) -> None:
    envelope = storage.load_artifact(t09_linked_project, "skeleton_ch_0001")
    storage.save_artifact(
        t09_linked_project,
        envelope.model_copy(update={"status": ArtifactStatus.stale}),
        operation_id="op_stale_skeleton",
    )

    result = lifecycle.guard_writer(t09_linked_project, "ch_0001")

    assert result.allowed is False
    assert result.code == "skeleton_stale"


def test_guard_writer_blocks_skeleton_pin_mismatch(t09_linked_project: Project) -> None:
    envelope = storage.load_artifact(t09_linked_project, "skeleton_ch_0001")
    storage.save_artifact(
        t09_linked_project,
        envelope.model_copy(
            update={
                "accepted_revision": envelope.accepted_revision.model_copy(
                    update={"revision": 2}
                )
            }
        ),
        operation_id="op_skeleton_r2",
    )
    storage.save_chapter(
        t09_linked_project,
        storage.load_chapter(t09_linked_project, "ch_0001").model_copy(
            update={"skeleton_pin": _skeleton_pin("ch_0001", revision=1)}
        ),
        operation_id="op_pin_r1",
    )

    result = lifecycle.guard_skeleton_for_chapter(t09_linked_project, "ch_0001")

    assert result.allowed is False
    assert result.code == "skeleton_pin_mismatch"


def test_guard_writer_reports_missing_chapter(t09_linked_project: Project) -> None:
    result = lifecycle.guard_writer(t09_linked_project, "ch_0099")

    assert result.allowed is False
    assert result.code == "chapter_missing"


def test_guard_requires_skeleton_pin_even_when_skeleton_accepted(
    t09_linked_project: Project,
) -> None:
    """`skeleton_pin = None` phải bị chặn, không được coi là "Skeleton hợp lệ".

    Trước đây toàn bộ check pin nằm trong `if chapter.skeleton_pin is not None`, nên
    một `chapter.json` không có pin (dữ liệu cũ, import tay, hoặc metadata bị sửa)
    vẫn qua `guard_writer`/`guard_finalize` chỉ vì envelope Skeleton `accepted`.
    Đó là đường vòng guard khi gọi service trực tiếp: `finalize_chapter` và
    `reconcile` chỉ dùng `guard_skeleton_for_chapter`, không dùng `build_writer_context`.
    """
    storage.save_chapter(
        t09_linked_project,
        storage.load_chapter(t09_linked_project, "ch_0001").model_copy(
            update={"skeleton_pin": None}
        ),
        operation_id="op_clear_skeleton_pin",
    )

    for guard in (
        lifecycle.guard_skeleton_for_chapter(t09_linked_project, "ch_0001"),
        lifecycle.guard_writer(t09_linked_project, "ch_0001"),
    ):
        assert guard.allowed is False
        assert guard.code == "skeleton_pin_missing"

    # Chapter 1 trong fixture đã `final_reconciled`, nên Finalize bị chặn ở gate
    # retcon trước khi tới check pin — vẫn là chặn, nhưng mã lý do khác.
    finalize = lifecycle.guard_finalize(t09_linked_project, "ch_0001")
    assert finalize.allowed is False
    assert finalize.code == "chapter_already_final"


def test_guard_requires_pin_after_skeleton_regenerated(
    t09_linked_project: Project,
) -> None:
    """Skeleton lên revision mới mà pin không đổi thì phải chặn (kể cả pin cũ là None)."""
    storage.save_chapter(
        t09_linked_project,
        storage.load_chapter(t09_linked_project, "ch_0001").model_copy(
            update={"skeleton_pin": None}
        ),
        operation_id="op_clear_pin_before_regen",
    )
    envelope = storage.load_artifact(t09_linked_project, "skeleton_ch_0001")
    storage.save_artifact(
        t09_linked_project,
        envelope.model_copy(
            update={
                "accepted_revision": envelope.accepted_revision.model_copy(
                    update={"revision": envelope.accepted_revision.revision + 1}
                )
            }
        ),
        operation_id="op_skeleton_r2_no_pin",
    )

    assert lifecycle.guard_finalize(t09_linked_project, "ch_0001").allowed is False


def test_state_chain_guard_blocks_after_retcon_lowered_consistency(
    t09_linked_project: Project,
) -> None:
    """Guard chain phải dùng `latest_consistent_chapter`, không lấy `max`.

    Sau retcon, `latest_consistent_chapter` bị hạ xuống chương retcon trong khi
    `latest_final_chapter` giữ nguyên (không ghi lùi state). Nếu guard lấy `max`
    hai field thì nó luôn bằng `latest_final_chapter` và guard trở nên vô hiệu:
    Writer sẽ chạy trên state chain cũ.

    Kịch bản: state nhất quán tới chương 1, nhưng chapter 2 và 3 đã
    `final_reconciled`. Viết chapter 4 cần chain tới chương 3 ⇒ phải bị chặn.
    """
    timeline = storage.load_timeline(t09_linked_project).model_copy(
        update={"latest_final_chapter": 3, "latest_consistent_chapter": 1}
    )
    storage.save_timeline(t09_linked_project, timeline, operation_id="op_lower_consistency")

    _accept_artifact(
        t09_linked_project, "skeleton", "skeleton_ch_0004", _skeleton_payload("ch_0004", 4)
    )
    for number, previous, status in (
        (2, "ch_0001", ChapterStatus.final_reconciled),
        (3, "ch_0002", ChapterStatus.final_reconciled),
        (4, "ch_0003", ChapterStatus.skeleton_ready),
    ):
        chapter_id = f"ch_{number:04d}"
        if number != 4:
            _accept_artifact(
                t09_linked_project,
                "skeleton",
                f"skeleton_{chapter_id}",
                _skeleton_payload(chapter_id, number),
            )
        base = storage.load_chapter(t09_linked_project, "ch_0002")
        metadata = base.model_copy(
            update={
                "chapter_id": chapter_id,
                "chapter_number": number,
                "title": f"Chương {number}",
                "previous_chapter_id": previous,
                "status": status,
                "skeleton_pin": _skeleton_pin(chapter_id),
            }
        )
        storage.save_chapter(t09_linked_project, metadata, operation_id=f"op_seed_{chapter_id}")

    storage.save_chapter(
        t09_linked_project,
        storage.load_chapter(t09_linked_project, "ch_0001").model_copy(
            update={"status": ChapterStatus.final_reconciled}
        ),
        operation_id="op_final_ch1_for_chain",
    )

    result = lifecycle.guard_writer(t09_linked_project, "ch_0004")

    assert result.allowed is False
    assert result.code == "timeline_not_consistent"


def test_guard_review_and_finalize(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _accept_artifact(project, "skeleton", "skeleton_ch_0001", _skeleton_payload("ch_0001", 1))

    empty = _chapter("ch_0001", 1, ChapterStatus.planned, skeleton_pin=_skeleton_pin("ch_0001"))
    storage.save_chapter(project, empty, operation_id="op_ch1_empty")
    assert lifecycle.guard_review(project, "ch_0001").code == "draft_missing"
    assert lifecycle.guard_finalize(project, "ch_0001").code == "draft_missing"

    partial = storage.load_chapter(project, "ch_0001")
    assert partial is not None
    storage.save_chapter(
        project,
        partial.model_copy(
            update={
                "status": ChapterStatus.draft,
                "drafts": [
                    ProseRevision(
                        revision=1,
                        markdown_ref="draft_r0001.md",
                        source_type=SourceType.llm,
                        is_complete=False,
                        created_at=_STAMP,
                    )
                ],
                "current_draft_revision": 1,
            }
        ),
        operation_id="op_ch1_partial",
    )
    assert lifecycle.guard_review(project, "ch_0001").code == "draft_incomplete"

    ready = storage.load_chapter(project, "ch_0001")
    assert ready is not None
    storage.save_chapter(
        project,
        ready.model_copy(
            update={
                "status": ChapterStatus.review_required,
                "drafts": [ready.drafts[0].model_copy(update={"is_complete": True})],
            }
        ),
        operation_id="op_ch1_ready",
    )
    assert lifecycle.guard_review(project, "ch_0001").allowed is True
    assert lifecycle.guard_finalize(project, "ch_0001").code == "human_review_missing"

    reviewed = storage.load_chapter(project, "ch_0001")
    assert reviewed is not None
    storage.save_chapter(
        project,
        reviewed.model_copy(
            update={
                "human_review": HumanReviewRecord(
                    prose_revision=1, reviewed_at=_STAMP, valid_for_current_revision=False
                )
            }
        ),
        operation_id="op_ch1_review_invalid",
    )
    assert lifecycle.guard_finalize(project, "ch_0001").code == "human_review_stale"

    valid_review = storage.load_chapter(project, "ch_0001")
    assert valid_review is not None
    storage.save_chapter(
        project,
        valid_review.model_copy(
            update={
                "human_review": HumanReviewRecord(
                    prose_revision=1, reviewed_at=_STAMP, valid_for_current_revision=True
                )
            }
        ),
        operation_id="op_ch1_review_valid",
    )
    assert lifecycle.guard_finalize(project, "ch_0001").allowed is True


def test_guard_finalize_blocks_chapter_already_final(t09_linked_project: Project) -> None:
    result = lifecycle.guard_finalize(t09_linked_project, "ch_0001")

    assert result.allowed is False
    assert result.code == "chapter_already_final"


# ---------------------------------------------------------------------------
# Stale propagation
# ---------------------------------------------------------------------------


def test_mark_downstream_stale_for_base_idea_revision(
    t09_linked_project: Project,
) -> None:
    marked = lifecycle.mark_downstream_stale(
        t09_linked_project,
        source_artifact_id="base_idea",
        source_revision=2,
        change=STALE_CHANGE_BASE_IDEA_REVISE,
    )

    assert {"premise", "characters", "world_rules", "foreshadow", "long_plan", "short_plan"} <= set(
        marked
    )
    # Skeleton là derived artifact nên bị đánh dấu; final prose không bị rewrite.
    assert "skeleton_ch_0001" in marked
    assert storage.load_artifact(t09_linked_project, "skeleton_ch_0001").status is (
        ArtifactStatus.stale
    )
    # Review của chapter đã final chỉ là historical report, không đánh dấu lại.
    assert "review_report_ch_0001" not in marked
    assert "reconciliation_ch_0001" not in marked

    premise = storage.load_artifact(t09_linked_project, "premise")
    assert premise.status is ArtifactStatus.stale
    assert premise.accepted_revision.revision == 1
    assert premise.stale_reasons[-1].source_artifact_id == "base_idea"
    assert t09_linked_project.paths.chapter_json("ch_0001").is_file()

    again = lifecycle.mark_downstream_stale(
        t09_linked_project,
        source_artifact_id="base_idea",
        source_revision=2,
        change=STALE_CHANGE_BASE_IDEA_REVISE,
    )
    assert again == []


def test_mark_downstream_stale_rejects_unknown_change(t09_linked_project: Project) -> None:
    with pytest.raises(LifecycleError) as excinfo:
        lifecycle.mark_downstream_stale(
            t09_linked_project,
            source_artifact_id="premise",
            source_revision=1,
            change="khong_co_change",
        )

    assert excinfo.value.code == "unknown_stale_change"


def test_append_marks_only_artifacts_effective_from_that_chapter(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    _accept_artifact(
        project,
        "world_rules",
        "world_rules",
        {
            "world_rules": [
                {
                    "world_rule_id": "rule_0001",
                    "category": "Hệ thống",
                    "summary": "Luật hiện tại.",
                    "content": "Nội dung.",
                    "effective_from_chapter": 1,
                }
            ]
        },
    )
    current = storage.load_artifact(project, "world_rules")
    appended = lifecycle.set_candidate(
        current,
        {
            "world_rules": [
                {
                    "world_rule_id": "rule_0001",
                    "category": "Hệ thống",
                    "summary": "Luật hiện tại.",
                    "content": "Nội dung.",
                    "effective_from_chapter": 1,
                },
                {
                    "world_rule_id": "rule_0100",
                    "category": "Cơ quan bí mật",
                    "summary": "Lore tương lai.",
                    "content": "Nội dung.",
                    "effective_from_chapter": 100,
                },
            ]
        },
        source=_source("architect.world_rules.v1"),
        validation=_valid(),
    )
    storage.save_artifact(
        project,
        lifecycle.accept_candidate(appended, validation=_valid(), now=_STAMP),
        operation_id="op_world_rules_r2",
    )
    _accept_artifact(
        project,
        "skeleton",
        "skeleton_ch_0005",
        _skeleton_payload("ch_0005", 5, world_rule_ids=["rule_0100"]),
    )
    _accept_artifact(
        project,
        "skeleton",
        "skeleton_ch_0100",
        _skeleton_payload("ch_0100", 100, world_rule_ids=["rule_0100"]),
    )

    marked = lifecycle.mark_downstream_stale(
        project,
        source_artifact_id="world_rules",
        source_revision=2,
        change=STALE_CHANGE_WORLD_RULES_APPEND,
    )

    # Chương 5 không bị ảnh hưởng vì lore chỉ hiệu lực từ chương 100.
    assert marked == ["skeleton_ch_0100"]
    stale_skeleton = storage.load_artifact(project, "skeleton_ch_0100")
    assert stale_skeleton.status is ArtifactStatus.stale
    assert stale_skeleton.stale_reasons[-1].affected_range.start == 100
    assert storage.load_artifact(project, "skeleton_ch_0005").status is ArtifactStatus.accepted


def test_append_character_marks_plan_and_skeleton_referencing_it(
    t09_linked_project: Project,
) -> None:
    _accept_artifact(
        t09_linked_project,
        "skeleton",
        "skeleton_ch_0002",
        _skeleton_payload("ch_0002", 2, character_ids=["char_0001", "char_0002"]),
    )

    marked = lifecycle.mark_downstream_stale(
        t09_linked_project,
        source_artifact_id="characters",
        source_revision=2,
        change=STALE_CHANGE_CHARACTERS_APPEND,
    )

    assert "skeleton_ch_0002" in marked
    assert "short_plan" in marked
    assert "long_plan" in marked
    # Skeleton chương 1 tham chiếu char_0001 (hiệu lực chương 1) nên cũng stale,
    # nhưng final prose vẫn giữ nguyên.
    assert "skeleton_ch_0001" in marked
    assert "review_report_ch_0001" not in marked


# ---------------------------------------------------------------------------
# Helper payload
# ---------------------------------------------------------------------------


def _long_plan_payload(*, title: str = "Tập 1", duplicate_arc: bool = False) -> dict[str, Any]:
    arc = {
        "arc_id": "arc_0001",
        "title": "Arc 1",
        "chapter_range": {"start": 1, "end": 7},
        "goal": "Mục tiêu arc",
        "core_conflict": "Xung đột",
        "start_state": "Đầu",
        "end_state": "Cuối",
    }
    arcs = [arc]
    if duplicate_arc:
        arcs.append(dict(arc, title="Arc trùng ID"))
    return {
        "volumes": [
            {
                "volume_id": "vol_0001",
                "title": title,
                "theme": "Chủ đề",
                "goal": "Mục tiêu",
                "arcs": arcs,
            }
        ]
    }
