"""Arbiter rule-based: gợi ý next step, không mutate, không gọi LLM (T19)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from novel_ai.core import lifecycle, storage
from novel_ai.core.models import (
    ArtifactStatus,
    ChapterMetadata,
    ChapterStatus,
    DependencyPin,
    FinalCandidate,
    HumanReviewRecord,
    PayloadSource,
    ProseRevision,
    ReconciliationStatus,
    SourceType,
    ValidationResult,
    ValidationState,
)
from novel_ai.core.project import Project
from novel_ai.ui import arbiter
from novel_ai.ui.arbiter import analyze, summarize

_STAMP = "2026-09-19T10:00:00+07:00"


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def _accept(project: Project, artifact_id: str, artifact_type: str, payload: dict) -> None:
    """Seed một artifact accepted qua lifecycle thật (không tự ghép envelope)."""
    envelope = lifecycle.new_artifact(artifact_type, artifact_id)
    candidate = lifecycle.set_candidate(
        envelope,
        payload,
        source=PayloadSource(source_type=SourceType.llm, prompt_id=f"{artifact_type}.v1"),
        validation=_valid(),
    )
    accepted = lifecycle.accept_candidate(candidate, validation=_valid())
    storage.save_artifact(project, accepted, operation_id=f"op_{artifact_id}")


def _chapter(
    project: Project,
    *,
    chapter_id: str,
    chapter_number: int,
    status: ChapterStatus,
    previous_id: str | None = None,
    drafts: list[ProseRevision] | None = None,
    current_draft_revision: int | None = None,
    human_review: HumanReviewRecord | None = None,
    final_candidate: FinalCandidate | None = None,
    skeleton: bool = False,
) -> ChapterMetadata:
    chapter = ChapterMetadata(
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        title=f"Chương {chapter_number}",
        status=status,
        previous_chapter_id=previous_id,
        short_plan_pin=DependencyPin(artifact_id="short_plan", revision=1, scope="short_plan", chapter_id=chapter_id),
        skeleton_pin=DependencyPin(
            artifact_id=f"skeleton_{chapter_id}", revision=1, scope="skeleton", chapter_id=chapter_id
        )
        if skeleton
        else None,
        drafts=list(drafts or []),
        current_draft_revision=current_draft_revision,
        human_review=human_review,
        final_candidate=final_candidate,
    )
    storage.save_chapter(project, chapter, operation_id=f"op_{chapter_id}")
    return chapter


def _draft(revision: int, *, complete: bool = True) -> ProseRevision:
    return ProseRevision(
        revision=revision,
        markdown_ref=f"draft_r{revision:04d}.md",
        source_type=SourceType.llm,
        is_complete=complete,
        created_at=_STAMP,
    )


def _codes(report) -> set[str]:
    return {item.code for item in report.suggestions}


def _write_base_idea(project: Project) -> None:
    storage.write_text_atomic(
        project.paths.base_idea_md, "Base idea accepted.", operation_id="op_base_idea"
    )


def _seed_foundation_and_plans(project: Project) -> None:
    """Chấp nhận đủ foundation + plan để không còn gợi ý generate sớm hơn."""
    _write_base_idea(project)
    for artifact_id, artifact_type in (
        ("premise", "premise"),
        ("characters", "characters"),
        ("world_rules", "world_rules"),
        ("foreshadow", "foreshadow"),
        ("long_plan", "long_plan"),
        ("short_plan", "short_plan"),
    ):
        _accept(project, artifact_id, artifact_type, _minimal_payload(artifact_type))


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "projects"
    root.mkdir()
    return Project.create(root, "Nồi Canh Bên Đường")


def test_new_project_suggests_finalize_base_idea(project: Project) -> None:
    report = analyze(project)

    assert report.next_step is not None
    assert report.next_step.code == "finalize_base_idea"
    assert report.next_step.blocking is True
    assert report.next_step.workspace == "co_create"


def test_after_base_idea_suggests_foundation_generation(project: Project) -> None:
    storage.write_text_atomic(
        project.paths.base_idea_md, "Base idea.", operation_id="op_base_idea"
    )

    report = analyze(project)

    assert "generate_premise" in _codes(report)
    assert report.next_step.code.startswith("generate_")


def test_draft_candidate_blocks_until_user_review(project: Project) -> None:
    storage.write_text_atomic(project.paths.base_idea_md, "Base idea.", operation_id="op_base_idea")
    envelope = lifecycle.new_artifact("premise", "premise")
    candidate = lifecycle.set_candidate(
        envelope,
        {"title": "T", "logline": "L"},
        source=PayloadSource(source_type=SourceType.llm, prompt_id="architect.premise.v1"),
        validation=_valid(),
    )
    storage.save_artifact(project, candidate, operation_id="op_premise_candidate")

    report = analyze(project)

    assert "review_premise_candidate" in _codes(report)
    assert report.next_step.code == "review_premise_candidate"
    assert report.next_step.blocking is True


def test_stale_artifact_marks_project_needing_review(project: Project) -> None:
    storage.write_text_atomic(project.paths.base_idea_md, "Base idea.", operation_id="op_base_idea")
    _accept(project, "premise", "premise", {"title": "T", "logline": "L"})
    envelope = storage.load_artifact(project, "premise")
    stale = lifecycle.mark_stale(
        envelope,
        source_artifact_id="base_idea",
        source_revision=2,
        reason="Base Idea đổi",
    )
    storage.save_artifact(project, stale, operation_id="op_premise_stale")

    report = analyze(project)

    assert "reaccept_premise" in _codes(report)
    assert "premise" in report.stale_artifact_ids
    assert report.next_step.code == "reaccept_premise"
    assert report.next_step.blocking is True


def test_chapter_without_skeleton_suggests_skeleton_generation(project: Project) -> None:
    _write_base_idea(project)
    for artifact_id, artifact_type in (
        ("premise", "premise"),
        ("characters", "characters"),
        ("world_rules", "world_rules"),
        ("foreshadow", "foreshadow"),
        ("long_plan", "long_plan"),
        ("short_plan", "short_plan"),
    ):
        _accept(project, artifact_id, artifact_type, _minimal_payload(artifact_type))
    _chapter(project, chapter_id="ch_0001", chapter_number=1, status=ChapterStatus.planned)

    report = analyze(project)

    assert "generate_skeleton" in _codes(report)
    assert report.next_step.code == "generate_skeleton"


def test_skeleton_ready_chapter_suggests_writer_draft(project: Project) -> None:
    _seed_foundation_and_plans(project)
    _chapter(project, chapter_id="ch_0001", chapter_number=1, status=ChapterStatus.skeleton_ready, skeleton=True)

    report = analyze(project)

    assert "generate_writer_draft" in _codes(report)


def test_partial_draft_suggests_continue_and_blocks(project: Project) -> None:
    _seed_foundation_and_plans(project)
    _chapter(
        project,
        chapter_id="ch_0001",
        chapter_number=1,
        status=ChapterStatus.draft,
        skeleton=True,
        drafts=[_draft(1, complete=False)],
        current_draft_revision=1,
    )

    report = analyze(project)

    assert report.next_step.code == "continue_partial_draft"
    assert report.next_step.blocking is True


def test_review_required_with_invalid_human_review_needs_review(project: Project) -> None:
    _seed_foundation_and_plans(project)
    _chapter(
        project,
        chapter_id="ch_0001",
        chapter_number=1,
        status=ChapterStatus.review_required,
        skeleton=True,
        drafts=[_draft(1), _draft(2)],
        current_draft_revision=2,
        human_review=HumanReviewRecord(
            prose_revision=1,
            reviewed_at=_STAMP,
            valid_for_current_revision=False,
        ),
    )

    report = analyze(project)

    assert report.next_step.code == "human_review_required"
    assert report.next_step.blocking is True


def test_review_required_with_valid_review_suggests_finalize(project: Project) -> None:
    _seed_foundation_and_plans(project)
    _chapter(
        project,
        chapter_id="ch_0001",
        chapter_number=1,
        status=ChapterStatus.review_required,
        skeleton=True,
        drafts=[_draft(1)],
        current_draft_revision=1,
        human_review=HumanReviewRecord(prose_revision=1, reviewed_at=_STAMP),
    )

    report = analyze(project)

    assert report.next_step.code == "finalize_chapter"


def test_finalizing_chapter_keeps_next_chapter_locked(project: Project) -> None:
    _seed_foundation_and_plans(project)
    _chapter(
        project,
        chapter_id="ch_0001",
        chapter_number=1,
        status=ChapterStatus.finalizing,
        skeleton=True,
        drafts=[_draft(1)],
        current_draft_revision=1,
        final_candidate=FinalCandidate(
            prose_revision=1,
            markdown_ref="final_r0001.md",
            created_at=_STAMP,
            reconciliation_status=ReconciliationStatus.draft,
        ),
    )
    _chapter(project, chapter_id="ch_0002", chapter_number=2, status=ChapterStatus.planned, previous_id="ch_0001")

    report = analyze(project)

    assert report.next_step.code == "reconcile_chapter"
    assert report.next_step.blocking is True
    assert "generate_writer_draft" not in _codes(report)
    assert report.chapter_status["ch_0002"] == "planned"


def test_rolling_reminder_when_due(project: Project) -> None:
    _seed_foundation_and_plans(project)
    _chapter(project, chapter_id="ch_0001", chapter_number=1, status=ChapterStatus.final_reconciled, skeleton=True)
    _chapter(
        project,
        chapter_id="ch_0002",
        chapter_number=2,
        status=ChapterStatus.final_reconciled,
        previous_id="ch_0001",
        skeleton=True,
    )
    _chapter(
        project,
        chapter_id="ch_0003",
        chapter_number=3,
        status=ChapterStatus.final_reconciled,
        previous_id="ch_0002",
        skeleton=True,
    )
    _chapter(project, chapter_id="ch_0004", chapter_number=4, status=ChapterStatus.planned, previous_id="ch_0003")

    report = analyze(project)

    assert report.rolling_due is True
    assert "rolling_plan_due" in _codes(report)
    # Rolling chỉ là nhắc, không phải gate cứng.
    rolling = next(item for item in report.suggestions if item.code == "rolling_plan_due")
    assert rolling.blocking is False


def test_pending_operation_short_circuits_with_recovery_suggestion(project: Project) -> None:
    pending_dir = project.paths.ops_pending_dir / "op_pending_0001"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "manifest.json").write_text(
        json.dumps(
            {
                "operation_id": "op_pending_0001",
                "operation_type": "finalize_reconcile",
                "status": "committing",
                "created_at": _STAMP,
                "updated_at": _STAMP,
            }
        ),
        encoding="utf-8",
    )

    report = analyze(project)

    assert report.needs_recovery is True
    assert report.pending_operation_ids == ["op_pending_0001"]
    assert report.next_step.code == "recover_pending_operation"
    assert report.next_step.priority == 0
    # Không gợi ý bước workflow khi transaction còn dở.
    assert "finalize_base_idea" not in _codes(report)


def test_manual_recovery_marks_project_read_only(project: Project) -> None:
    pending_dir = project.paths.ops_pending_dir / "op_pending_0002"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "manifest.json").write_text(
        json.dumps(
            {
                "operation_id": "op_pending_0002",
                "operation_type": "finalize_reconcile",
                "status": "needs_manual_recovery",
                "created_at": _STAMP,
                "updated_at": _STAMP,
            }
        ),
        encoding="utf-8",
    )

    report = analyze(project)

    assert report.read_only is True
    assert report.next_step.code == "manual_recovery_required"
    assert "thủ công" in summarize(report)


def test_analyze_does_not_modify_project_files(project: Project) -> None:
    _write_base_idea(project)
    _accept(project, "premise", "premise", {"title": "T", "logline": "L"})
    _chapter(project, chapter_id="ch_0001", chapter_number=1, status=ChapterStatus.planned)

    before = _tree_fingerprint(project.root)
    analyze(project)
    analyze(project)
    after = _tree_fingerprint(project.root)

    assert before == after


def _tree_fingerprint(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _minimal_payload(artifact_type: str) -> dict:
    if artifact_type == "short_plan":
        return {
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
        }
    if artifact_type == "long_plan":
        return {
            "volumes": [
                {
                    "volume_id": "vol_0001",
                    "title": "Tập 1",
                    "theme": "Chủ đề",
                    "goal": "Mục tiêu",
                    "arcs": [
                        {
                            "arc_id": "arc_0001",
                            "title": "Arc 1",
                            "chapter_range": {"start": 1, "end": 7},
                            "goal": "Mục tiêu arc",
                            "core_conflict": "Xung đột",
                            "start_state": "Đầu",
                            "end_state": "Cuối",
                        }
                    ],
                }
            ]
        }
    if artifact_type == "premise":
        return {"title": "T", "logline": "L"}
    if artifact_type == "characters":
        return {"characters": []}
    if artifact_type == "world_rules":
        return {"world_rules": []}
    if artifact_type == "foreshadow":
        return {"foreshadows": []}
    raise AssertionError(f"Không có payload mẫu cho {artifact_type}")


# ---------------------------------------------------------------------------
# T32 — nhãn compact cho control Arbiter
# ---------------------------------------------------------------------------


def test_badge_summary_counts_blockers_stale_and_recovery() -> None:
    report = arbiter.ArbiterReport(
        project_id="proj_0001",
        current_chapter=1,
        suggestions=[
            arbiter.ArbiterSuggestion(
                code="generate_skeleton", label="Generate Skeleton", workspace="skeleton",
                reason="r", blocking=True,
            ),
            arbiter.ArbiterSuggestion(
                code="rolling_plan_due", label="Rolling Plan review", workspace="short_plan",
                reason="r",
            ),
        ],
        stale_artifact_ids=["short_plan"],
        rolling_due=True,
    )

    summary = arbiter.badge_summary(report)

    assert "1 blocker" in summary
    assert "1 stale" in summary
    assert "rolling due" in summary
    assert arbiter.compact_label(report) == f"Arbiter · {summary}"


def test_badge_summary_reports_recovery_and_no_blocker_state() -> None:
    quiet = arbiter.ArbiterReport(project_id="proj_0001", current_chapter=1)
    assert arbiter.badge_summary(quiet) == "không blocker"

    recovery = arbiter.ArbiterReport(
        project_id="proj_0001",
        current_chapter=1,
        needs_recovery=True,
        pending_operation_ids=["op_1"],
    )
    assert arbiter.badge_summary(recovery) == "cần recovery"

    read_only = arbiter.ArbiterReport(
        project_id="proj_0001",
        current_chapter=1,
        needs_recovery=True,
        read_only=True,
    )
    assert arbiter.badge_summary(read_only) == "read-only"
