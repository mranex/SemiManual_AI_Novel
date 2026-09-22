"""Nghiệm thu MVP end-to-end và regression cross-module (T23).

Bổ sung cho test theo từng task: file này chạy **chuỗi service thật** qua nhiều
module (seed → writer → review → finalize → reconcile → chương sau) và kiểm 10
case bắt buộc ở `IMPLEMENTATION_PLAN.md` mục 8, cùng invariant hard của
`docs/design/workflow.md` mục 7.

Toàn bộ offline: `FakeLLMClient`, `tmp_path`, không mạng, không live API.
Helper seed dùng lại `t15_t16_support.seed_project` (cùng file thư mục nên
import trực tiếp được).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from novel_ai.core import lifecycle, storage
from novel_ai.core.context import (
    ContextError,
    build_writer_context,
)
from novel_ai.core.llm import FakeLLMClient, LLMError
from novel_ai.core.models import (
    ArtifactStatus,
    ChapterStatus,
    SourceChange,
    SourceType,
)
from novel_ai.core.project import Project
from novel_ai.services import (
    GuardError,
    LLMUnavailableError,
    ServiceError,
    ValidationFailure,
    architect,
    reconcile,
    reviewer,
    revision,
    short_planner,
    skeleton,
    writer,
)
from novel_ai.core.prompts import PromptRegistry, render_prompt
from t15_t16_support import (
    COMPLETE_PROSE,
    LATE_LORE,
    BODY_SECRET,
    POT_SECRET,
    PURPOSE_SECRET,
    SKELETON_CH2_GENERATED,
    seed_project,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _reconcile_payload(project: Project, *, chapter_id: str = "ch_0001") -> dict[str, Any]:
    """Payload reconcile hợp lệ dựng từ final candidate thật (không bịa binding)."""
    chapter = storage.load_chapter(project, chapter_id)
    assert chapter is not None and chapter.final_candidate is not None
    return {
        "chapter_id": chapter_id,
        "chapter_number": chapter.chapter_number,
        "source_final_candidate": {
            "prose_revision": chapter.final_candidate.prose_revision,
            "markdown_ref": chapter.final_candidate.markdown_ref,
        },
        "timeline": {
            "time": "Đêm đầu sau khi tỉnh dậy",
            "location": "Sườn Vạn Linh sơn",
            "status": "Sở Dương tỉnh dậy bên cái nồi sắt đen và bị thân thể cưỡng chế đi lên sống núi.",
        },
        # char_0002 hiệu lực chương 2 nên không được xuất hiện trong chương 1.
        "relationship_updates": [],
        "notes": ["Không trích xuất nguồn gốc cái nồi thành world rule mới."],
    }


def _drive_chapter_one(project: Project, *, operation_prefix: str = "op") -> None:
    """writer → human review → finalize → reconcile accept cho chương 1."""
    client = FakeLLMClient([COMPLETE_PROSE])
    writer.generate_draft(project, client=client, chapter_id="ch_0001")
    reviewer.mark_reviewed(project, chapter_id="ch_0001", notes="Đã đọc và duyệt.")
    reconcile.finalize_chapter(project, chapter_id="ch_0001")
    payload = _reconcile_payload(project)
    reconcile.generate_reconciliation(
        project,
        client=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
        chapter_id="ch_0001",
    )
    reconcile.accept_reconciliation(project, chapter_id="ch_0001")


def _tree_fingerprint(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): storage.file_fingerprint(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# Case 1 — chương 2 chỉ unlock sau khi chương 1 final_reconciled
# ---------------------------------------------------------------------------


def test_case1_two_chapter_flow_unlocks_writer_only_after_reconcile(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(
        tmp_path,
        t02_valid_document,
        chapter_one_status=ChapterStatus.skeleton_ready,
        chapter_two_status=ChapterStatus.planned,
        chapter_two_skeleton=False,
    )

    # Trước khi chương 1 final_reconciled: Writer chương 2 bị khóa ở backend,
    # và guard phải chặn TRƯỚC khi gọi LLM.
    blocked_client = FakeLLMClient([COMPLETE_PROSE])
    with pytest.raises(ServiceError):
        writer.generate_draft(project, client=blocked_client, chapter_id="ch_0002")
    assert blocked_client.calls == []

    _drive_chapter_one(project)

    chapter_one = storage.load_chapter(project, "ch_0001")
    assert chapter_one is not None
    assert chapter_one.status is ChapterStatus.final_reconciled
    assert chapter_one.final_revision is not None

    # Chương 2: Skeleton candidate provisional/actual → accept → Writer chạy được.
    skeleton_client = FakeLLMClient([json.dumps(SKELETON_CH2_GENERATED, ensure_ascii=False)])
    skeleton.generate(project, client=skeleton_client, chapter_id="ch_0002")
    skeleton.accept(project, chapter_id="ch_0002")

    second_client = FakeLLMClient([COMPLETE_PROSE])
    writer.generate_draft(project, client=second_client, chapter_id="ch_0002")
    assert len(second_client.calls) == 1

    chapter_two = storage.load_chapter(project, "ch_0002")
    assert chapter_two is not None
    assert chapter_two.status is ChapterStatus.review_required
    # State chương 1 đã commit và chương 2 dùng được nó.
    assert [entry.chapter_number for entry in storage.load_timeline(project).entries] == [1]


# ---------------------------------------------------------------------------
# Case 2 — Auto Accept không finalize prose, không thay Human Review
# ---------------------------------------------------------------------------


def test_case2_auto_accept_structured_never_finalizes_prose(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(
        tmp_path,
        t02_valid_document,
        chapter_one_status=ChapterStatus.skeleton_ready,
        chapter_two_skeleton=False,
    )
    project.update_config(auto_accept_structured=True)
    project = Project.open(project.root.parent, project.config.project_id)
    assert project.config.auto_accept_structured is True
    latest_final_before = storage.load_timeline(project).latest_final_chapter

    # Structured output hợp lệ được auto accept...
    architect.generate(
        project,
        client=FakeLLMClient(
            [json.dumps({"title": "T", "logline": "Logline auto accept."}, ensure_ascii=False)]
        ),
        artifact_type="premise",
    )
    premise = storage.load_artifact(project, "premise")
    assert premise is not None
    assert premise.status is ArtifactStatus.accepted
    assert premise.accepted_revision is not None

    # ...nhưng prose vẫn đi qua đúng đường draft → human review.
    writer.generate_draft(project, client=FakeLLMClient([COMPLETE_PROSE]), chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert chapter.status is ChapterStatus.review_required
    assert chapter.final_candidate is None
    assert chapter.final_revision is None

    with pytest.raises(GuardError):
        reconcile.finalize_chapter(project, chapter_id="ch_0001")

    check = reviewer.ready_to_finalize(project, chapter_id="ch_0001")
    assert check.ready is False
    assert check.human_review_valid is False

    # Sau Human Review tường minh thì Finalize chạy, vẫn không tự reconcile.
    reviewer.mark_reviewed(project, chapter_id="ch_0001")
    reconcile.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert chapter.status is ChapterStatus.finalizing
    assert storage.load_timeline(project).latest_final_chapter == latest_final_before


# ---------------------------------------------------------------------------
# Case 3 — accepted không đổi khi regenerate lỗi / candidate chưa accept
# ---------------------------------------------------------------------------


def test_case3_accepted_state_survives_regenerate_failure(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)

    before = storage.file_fingerprint(project.paths.premise_json)

    with pytest.raises(LLMUnavailableError):
        architect.generate(
            project,
            client=FakeLLMClient([LLMError("provider chết")]),
            artifact_type="premise",
        )

    after = storage.file_fingerprint(project.paths.premise_json)
    assert before == after
    envelope = storage.load_artifact(project, "premise")
    assert envelope is not None
    assert envelope.status is ArtifactStatus.accepted
    assert envelope.accepted_revision is not None
    assert envelope.accepted_revision.revision == 1

    # Candidate mới chưa accept cũng không thay accepted.
    with pytest.raises(ValidationFailure):
        architect.generate(
            project,
            client=FakeLLMClient(["{not json"]),
            artifact_type="premise",
        )
    assert storage.load_artifact(project, "premise").accepted_revision.revision == 1


# ---------------------------------------------------------------------------
# Case 4 — temporal leak + secret leak trong context/prompt chương sớm
# ---------------------------------------------------------------------------


def test_case4_late_lore_and_author_secrets_never_reach_early_prompts(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)

    bundle = build_writer_context(project, chapter_id="ch_0001")
    registry = PromptRegistry.load(repo_root=REPO_ROOT)
    rendered = render_prompt(
        registry,
        "writer.v1",
        inputs=bundle.payload,
        repo_root=REPO_ROOT,
        config=project.config,
    )
    prompt_text = "\n".join(message.content for message in rendered.messages)

    for secret in (POT_SECRET, BODY_SECRET, PURPOSE_SECRET, LATE_LORE):
        assert secret not in prompt_text, f"Leak `{secret}` trong prompt Writer chương 1"

    excluded = {item.item_id for item in bundle.excluded_due_to_effective_chapter}
    assert {"char_0002", "rule_0100"} <= excluded

    # Chương 20: lore hiệu lực 100 vẫn không được chọn (case được nêu trong plan).
    from novel_ai.core.validation import ReferenceIndex, validate_effective_selection

    index = ReferenceIndex(
        world_rule_ids=frozenset({"rule_0001", "rule_0100"}),
        world_rule_effective={"rule_0001": 1, "rule_0100": 100},
    )
    issues = validate_effective_selection(
        for_chapter_number=20,
        index=index,
        world_rule_ids=["rule_0001", "rule_0100"],
    )
    assert [(issue.path, issue.code) for issue in issues] == [
        ("/payload/effective_world_rule_ids/1", "effective_from_future")
    ]


# ---------------------------------------------------------------------------
# Case 5 — retry không merge/sinh request trùng
# ---------------------------------------------------------------------------


def test_case5_retry_finalize_and_reconcile_do_not_duplicate_state(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)
    writer.generate_draft(project, client=FakeLLMClient([COMPLETE_PROSE]), chapter_id="ch_0001")
    reviewer.mark_reviewed(project, chapter_id="ch_0001")
    reconcile.finalize_chapter(project, chapter_id="ch_0001", operation_id="op_finalize_once")

    payload = _reconcile_payload(project)
    reconcile.generate_reconciliation(
        project,
        client=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
        chapter_id="ch_0001",
        operation_id="op_reconcile_once",
    )
    reconcile.accept_reconciliation(project, chapter_id="ch_0001", operation_id="op_reconcile_accept")

    timeline_after = len(storage.load_timeline(project).entries)
    relationships_after = len(storage.load_relationships(project).relationships)
    assert timeline_after == 1

    # Retry cùng operation_id: replay, không nhân đôi.
    try:
        reconcile.accept_reconciliation(
            project, chapter_id="ch_0001", operation_id="op_reconcile_accept"
        )
    except ServiceError:
        pass
    assert len(storage.load_timeline(project).entries) == timeline_after
    assert len(storage.load_relationships(project).relationships) == relationships_after

    # Retry reconcile proposal sau khi đã accepted cũng không thêm entry.
    try:
        reconcile.retry_reconcile(
            project, client=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]), chapter_id="ch_0001"
        )
    except ServiceError:
        pass
    assert len(storage.load_timeline(project).entries) == timeline_after
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.status is ChapterStatus.final_reconciled


# ---------------------------------------------------------------------------
# Case 6 — crash giữa commit nhiều file + recovery, không unlock sớm
# ---------------------------------------------------------------------------


def test_case6_crash_mid_commit_recovers_consistently_and_unlocks_late(
    tmp_path: Path, t02_valid_document: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)
    writer.generate_draft(project, client=FakeLLMClient([COMPLETE_PROSE]), chapter_id="ch_0001")
    reviewer.mark_reviewed(project, chapter_id="ch_0001")
    reconcile.finalize_chapter(project, chapter_id="ch_0001")
    payload = _reconcile_payload(project)
    reconcile.generate_reconciliation(
        project,
        client=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
        chapter_id="ch_0001",
    )

    real_replace = os.replace
    state = {"crashed": False}

    def flaky_replace(src: object, dst: object, *args: object, **kwargs: object) -> None:
        # Chỉ tiêm lỗi vào **target thật** của commit (không phải file staged
        # trong `.ops/pending`), và `chapter.json` được commit CUỐI CÙNG (T17):
        # crash tại đây nghĩa là các target khác đã replace nhưng chapter chưa
        # được đánh dấu final.
        target = str(dst)
        if (
            not state["crashed"]
            and ".ops" not in target
            and target.endswith("chapter.json")
        ):
            state["crashed"] = True
            raise OSError("crash được tiêm trước khi chapter.json được replace")
        real_replace(src, dst, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("novel_ai.core.storage.os.replace", flaky_replace)
    with pytest.raises(Exception):
        reconcile.accept_reconciliation(project, chapter_id="ch_0001")
    monkeypatch.undo()
    assert state["crashed"] is True

    assert storage.needs_recovery(project) is True
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert chapter.status is not ChapterStatus.final_reconciled

    report = reconcile.recover(project)
    # `RecoveryReport.manual_required` là danh sách op cần người xử lý, rỗng = tự recovery được.
    assert not report.manual_required
    assert storage.needs_recovery(project) is False

    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert chapter.status is ChapterStatus.final_reconciled
    assert len(storage.load_timeline(project).entries) == 1

    # Recovery lần hai không nhân đôi state.
    reconcile.recover(project)
    assert len(storage.load_timeline(project).entries) == 1


# ---------------------------------------------------------------------------
# Case 7 — revise Premise làm stale planning, giữ nguyên Final Manuscript
# ---------------------------------------------------------------------------


def test_case7_revise_premise_marks_planning_stale_but_keeps_final_manuscript(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)
    _drive_chapter_one(project)

    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.final_revision is not None
    final_path = project.paths.final_dir("ch_0001") / Path(chapter.final_revision.markdown_ref).name
    final_before = storage.file_fingerprint(final_path)

    revision.revise_premise(
        project,
        payload={
            "title": "Nồi Canh Bên Đường",
            "logline": "Bản logline đã sửa để kiểm tra stale.",
            "hard_constraints": ["Không reveal nguồn gốc cái nồi trong chương 1."],
        },
    )

    short_plan = storage.load_artifact(project, "short_plan")
    assert short_plan is not None
    assert short_plan.status is ArtifactStatus.stale
    assert short_plan.accepted_revision is not None, "Accepted cũ phải còn cho audit"

    assert storage.file_fingerprint(final_path) == final_before
    chapter_after = storage.load_chapter(project, "ch_0001")
    assert chapter_after is not None
    assert chapter_after.status is ChapterStatus.final_reconciled


# ---------------------------------------------------------------------------
# Case 8 — retcon chương cũ dùng state đúng thời điểm, không ghi lùi current state
# ---------------------------------------------------------------------------


def test_case8_retcon_old_chapter_does_not_roll_back_later_state(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)
    _drive_chapter_one(project)

    timeline_before = storage.load_timeline(project)
    final_before = storage.file_fingerprint(
        project.paths.final_dir("ch_0001")
        / Path(storage.load_chapter(project, "ch_0001").final_revision.markdown_ref).name
    )

    revision.start_retcon(project, chapter_id="ch_0001")
    retcon = revision.retcon_state(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert retcon is not None and retcon.get("final_still_canon") is True
    # Final cũ vẫn là canon cho tới khi commit retcon hoàn tất (D004).
    assert chapter.final_revision is not None
    assert storage.file_fingerprint(
        project.paths.final_dir("ch_0001") / Path(chapter.final_revision.markdown_ref).name
    ) == final_before

    revision.reset_consistency_after_retcon(project, chapter_number=1)

    timeline_after = storage.load_timeline(project)
    assert timeline_after.latest_final_chapter == timeline_before.latest_final_chapter
    assert timeline_after.latest_consistent_chapter == 1
    # State chương 1 vẫn đọc được as-of chính nó; không bị thay bằng state mới hơn.
    chapter_one_state = [
        entry for entry in timeline_after.entries if entry.chapter_number <= 1
    ]
    assert chapter_one_state and chapter_one_state[0].chapter_id == "ch_0001"

    # Writer chương 2 vẫn bị chặn cho tới khi downstream được review/rebuilt.
    blocked = FakeLLMClient([COMPLETE_PROSE])
    with pytest.raises(ServiceError):
        writer.generate_draft(project, client=blocked, chapter_id="ch_0002")
    assert blocked.calls == []

    blockers = revision.downstream_blockers(project)
    assert isinstance(blockers, list)


# ---------------------------------------------------------------------------
# Case 9 — Rolling Plan không sửa chương final/foundation/Long Plan
# ---------------------------------------------------------------------------


def test_case9_rolling_patch_cannot_touch_final_chapter_or_foundation(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)
    _drive_chapter_one(project)

    eligible = short_planner.eligible_chapters_for_rolling(project)
    assert "ch_0001" not in {item["chapter_id"] for item in eligible}

    foundation_before = {
        key: storage.file_fingerprint(getattr(project.paths, f"{key}_json"))
        for key in ("premise", "characters", "world_rules", "foreshadow")
    }
    long_plan_before = storage.file_fingerprint(project.paths.long_plan_json)
    final_before = storage.file_fingerprint(
        project.paths.final_dir("ch_0001")
        / Path(storage.load_chapter(project, "ch_0001").final_revision.markdown_ref).name
    )

    bad_patch = {
        "status": "adjust",
        "reviewed_chapter_range": {"start": 1, "end": 1},
        "short_plan_changes": [
            {
                "chapter_id": "ch_0001",
                "changes": {"summary": "Sửa chương đã final."},
                "reason": "Muốn đổi chương đã final.",
            }
        ],
        "deviations": [],
        "relationship_plan_changes": [],
        "blocked_by_authority": [],
    }
    with pytest.raises(ServiceError):
        short_planner.generate_rolling(
            project,
            client=FakeLLMClient([json.dumps(bad_patch, ensure_ascii=False)]),
            arc_id="arc_0001",
        )

    assert storage.file_fingerprint(project.paths.long_plan_json) == long_plan_before
    assert {
        key: storage.file_fingerprint(getattr(project.paths, f"{key}_json"))
        for key in ("premise", "characters", "world_rules", "foreshadow")
    } == foundation_before
    assert storage.file_fingerprint(
        project.paths.final_dir("ch_0001")
        / Path(storage.load_chapter(project, "ch_0001").final_revision.markdown_ref).name
    ) == final_before

    # Config tắt replan quan hệ: proposal đổi hướng quan hệ phải bị từ chối.
    project.update_config(allow_relationship_replan=False)
    reopened = Project.open(project.root.parent, project.config.project_id)
    relationship_patch = {
        "status": "adjust",
        "reviewed_chapter_range": {"start": 1, "end": 1},
        "deviations": [],
        "short_plan_changes": [],
        "relationship_plan_changes": [
            {
                "chapter_id": "ch_0002",
                "relationship_changes": [
                    {
                        "character_ids": ["char_0001", "char_0002"],
                        "arc_direction": "dè chừng",
                        "target_state": "Hợp tác",
                        "notes": "",
                    }
                ],
                "reason": "Muốn đổi nhịp.",
            }
        ],
        "blocked_by_authority": [],
    }
    if "ch_0002" in {item["chapter_id"] for item in short_planner.eligible_chapters_for_rolling(reopened)}:
        with pytest.raises(ServiceError):
            short_planner.generate_rolling(
                reopened,
                client=FakeLLMClient([json.dumps(relationship_patch, ensure_ascii=False)]),
                arc_id="arc_0001",
            )
    assert len(storage.load_relationships(reopened).relationships) == 1


# ---------------------------------------------------------------------------
# Case 10 — đóng/mở lại app vẫn thấy accepted, candidate, partial draft, pending
# ---------------------------------------------------------------------------


def test_case10_state_survives_close_and_reopen(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(
        tmp_path,
        t02_valid_document,
        chapter_one_status=ChapterStatus.skeleton_ready,
        chapter_two_status=ChapterStatus.planned,
        chapter_two_skeleton=False,
    )

    # partial draft bằng stream đứt
    from novel_ai.core.llm import STREAM_INTERRUPTED

    partial_client = FakeLLMClient()
    partial_client.queue_stream(COMPLETE_PROSE[:40], STREAM_INTERRUPTED)
    writer.generate_draft(project, client=partial_client, chapter_id="ch_0001", stream=True)

    # candidate chưa accept (Skeleton chương 2)
    skeleton.generate(
        project,
        client=FakeLLMClient([json.dumps(SKELETON_CH2_GENERATED, ensure_ascii=False)]),
        chapter_id="ch_0002",
    )

    chapter_before = storage.load_chapter(project, "ch_0001")
    assert chapter_before is not None
    assert chapter_before.current_draft is not None
    assert chapter_before.current_draft.is_complete is False
    candidate_before = storage.load_artifact(project, "skeleton_ch_0002")
    assert candidate_before is not None and candidate_before.candidate_revision is not None

    reopened = Project.open(project.root.parent, project.config.project_id)

    chapter_after = storage.load_chapter(reopened, "ch_0001")
    assert chapter_after is not None
    assert chapter_after.current_draft is not None
    assert chapter_after.current_draft.is_complete is False
    assert chapter_after.status is ChapterStatus.draft

    candidate_after = storage.load_artifact(reopened, "skeleton_ch_0002")
    assert candidate_after is not None
    assert candidate_after.candidate_revision is not None
    assert candidate_after.candidate_revision.revision == candidate_before.candidate_revision.revision

    premise = storage.load_artifact(reopened, "premise")
    assert premise is not None and premise.status is ArtifactStatus.accepted

    # Partial draft không được finalize (không nhét lỗi thành manuscript).
    with pytest.raises(GuardError):
        reconcile.finalize_chapter(reopened, chapter_id="ch_0001")


# ---------------------------------------------------------------------------
# Bổ sung: invariant "downstream không sửa upstream" và "plan không phải canon"
# ---------------------------------------------------------------------------


def test_writer_output_never_mutates_upstream_artifacts(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)

    watched = {
        key: storage.file_fingerprint(getattr(project.paths, f"{key}_json"))
        for key in ("premise", "characters", "world_rules", "foreshadow", "long_plan", "short_plan")
    }
    timeline_before = storage.file_fingerprint(project.paths.timeline_json)
    relationships_before = storage.file_fingerprint(project.paths.relationships_json)

    writer.generate_draft(project, client=FakeLLMClient([COMPLETE_PROSE]), chapter_id="ch_0001")

    for key, fingerprint in watched.items():
        assert storage.file_fingerprint(getattr(project.paths, f"{key}_json")) == fingerprint, key
    assert storage.file_fingerprint(project.paths.timeline_json) == timeline_before
    assert storage.file_fingerprint(project.paths.relationships_json) == relationships_before


def test_accepting_short_plan_does_not_write_actual_state(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Plan là ý định tương lai: accept plan không được ghi timeline/relationship."""
    project = seed_project(
        tmp_path,
        t02_valid_document,
        chapter_one_status=ChapterStatus.planned,
        chapter_two_skeleton=False,
    )
    timeline_before_fingerprint = storage.file_fingerprint(project.paths.timeline_json)
    latest_final_before = storage.load_timeline(project).latest_final_chapter
    # D016: request Short Plan cần contract viết đủ; seed default viết của project.
    project.update_config(default_pov="ngôi ba giới hạn", default_length_guidance="1500 từ")

    short_planner.generate(
        project,
        client=FakeLLMClient(
            [
                json.dumps(
                    t02_valid_document["artifacts"]["short_plan"]["accepted_revision"]["payload"],
                    ensure_ascii=False,
                )
            ]
        ),
        arc_id="arc_0001",
        assigned_chapters=[
            {"chapter_id": "ch_0001", "chapter_number": 1},
            {"chapter_id": "ch_0002", "chapter_number": 2},
        ],
    )
    short_planner.accept(project)

    assert storage.file_fingerprint(project.paths.timeline_json) == timeline_before_fingerprint
    assert storage.load_timeline(project).latest_final_chapter == latest_final_before


def test_ai_review_report_never_changes_prose_or_chapter_status(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)
    writer.generate_draft(project, client=FakeLLMClient([COMPLETE_PROSE]), chapter_id="ch_0001")

    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.current_draft is not None
    prose_before = writer.draft_markdown(project, "ch_0001", chapter.current_draft.revision)
    status_before = chapter.status

    report = {
        "chapter_id": "ch_0001",
        "prose_revision": chapter.current_draft.revision,
        "summary": "Không có vấn đề cứng.",
        "issues": [],
    }
    reviewer.run_ai_review(
        project,
        client=FakeLLMClient([json.dumps(report, ensure_ascii=False)]),
        chapter_id="ch_0001",
    )

    chapter_after = storage.load_chapter(project, "ch_0001")
    assert chapter_after is not None
    assert chapter_after.status is status_before
    assert chapter_after.final_revision is None
    assert (
        writer.draft_markdown(project, "ch_0001", chapter_after.current_draft.revision)
        == prose_before
    )


def test_impact_report_is_report_only(
    tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    project = seed_project(tmp_path, t02_valid_document, chapter_two_skeleton=False)
    _drive_chapter_one(project)

    before = _tree_fingerprint(project.root)
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
                "reason": "Retcon có thể đổi trạng thái trước chương 2.",
                "severity": "major",
                "suggested_action": "Review lại Skeleton chương 2.",
            }
        ],
        "risk_summary": "Chỉ cần đánh dấu stale, không rewrite prose.",
        "suggested_actions": ["Reaccept Short Plan nếu hướng arc không đổi."],
    }
    revision.generate_impact_report(
        project,
        client=FakeLLMClient([json.dumps(report, ensure_ascii=False)]),
        source_change=source_change,
    )

    after = _tree_fingerprint(project.root)
    changed = {
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    }
    assert changed, "Impact report phải được lưu lại"
    assert all(
        path.startswith(("raw/", "chapters/ch_0001/retcon/", "architect/impact", "chapters/ch_0001/impact", "state/"))
        or "impact" in path
        or path.startswith("history/")
        for path in changed
    ), f"Impact report không được mutation ngoài phạm vi report: {sorted(changed)}"
