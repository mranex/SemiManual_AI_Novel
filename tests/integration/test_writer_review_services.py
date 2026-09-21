"""Integration test T16 — Human Review, AI Review, Rewrite Section.

Seed project và payload LLM dùng chung nằm ở `t15_t16_support.py` (không phải
file test). Test T15 nằm ở `test_skeleton_writer_services.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ai.core import lifecycle, storage
from novel_ai.core.llm import FakeLLMClient
from novel_ai.core.models import ChapterStatus, RewriteSectionRequest
from novel_ai.services import GuardError, ValidationFailure
from novel_ai.services import reviewer, writer
from t15_t16_support import (
    COMPLETE_PROSE,
    _complete_draft,
    _review_report,
    seed_project,
)

# ---------------------------------------------------------------------------
# 9. AI Review
# ---------------------------------------------------------------------------


def test_ai_review_binds_revision_and_rejects_fake_quote(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    _complete_draft(project)
    chapter_before = storage.load_chapter(project, "ch_0001")
    prose_ref = project.root / chapter_before.current_draft.markdown_ref
    prose_fingerprint = storage.file_fingerprint(prose_ref)
    client = FakeLLMClient([json.dumps(_review_report(), ensure_ascii=False)])

    result = reviewer.run_ai_review(project, client=client, chapter_id="ch_0001")

    assert result.data["prose_revision"] == 1
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.status is chapter_before.status is ChapterStatus.review_required
    assert chapter.current_draft_revision == 1
    assert chapter.ai_review_reports[-1].artifact_id == "review_report_ch_0001"
    assert chapter.ai_review_reports[-1].prose_revision == 1
    assert storage.file_fingerprint(prose_ref) == prose_fingerprint
    report = storage.load_artifact(project, "review_report_ch_0001")
    assert report is not None and report.accepted_revision is not None
    assert report.accepted_revision.payload.prose_revision == 1

    # Quote không có trong prose revision ⇒ từ chối, không thêm ref mới.
    client.queue(
        json.dumps(_review_report(quote="Câu này không hề có trong prose."), ensure_ascii=False)
    )
    with pytest.raises(ValidationFailure) as excinfo:
        reviewer.run_ai_review(project, client=client, chapter_id="ch_0001")
    assert any(issue.code == "quote_not_verbatim" for issue in excinfo.value.result.errors)

    client.queue(json.dumps(_review_report(prose_revision=99), ensure_ascii=False))
    with pytest.raises(ValidationFailure) as excinfo:
        reviewer.run_ai_review(project, client=client, chapter_id="ch_0001")
    assert excinfo.value.code == "review_revision_mismatch"
    assert storage.load_chapter(project, "ch_0001").ai_review_reports == (
        chapter.ai_review_reports
    )


# ---------------------------------------------------------------------------
# 10. Human Review
# ---------------------------------------------------------------------------


def test_human_review_invalidated_by_edit_and_old_review_cannot_finalize(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    _complete_draft(project)

    reviewer.mark_reviewed(project, chapter_id="ch_0001", notes="Đã đọc và sửa câu dán nhãn.")

    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.human_review is not None
    assert chapter.human_review.prose_revision == 1
    assert chapter.human_review.valid_for_current_revision is True
    check = reviewer.ready_to_finalize(project, chapter_id="ch_0001")
    assert check.ready is True, check.reasons
    assert check.has_draft and check.is_complete and check.human_review_valid
    assert check.skeleton_fresh and check.previous_chapter_ok
    assert check.pending_edits is False

    # Sửa draft sau review ⇒ review cũ mất hiệu lực.
    writer.save_draft(
        project, chapter_id="ch_0001", text=COMPLETE_PROSE + "\n\nSở Dương thử lần nữa."
    )

    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.current_draft_revision == 2
    assert chapter.human_review is not None
    assert chapter.human_review.prose_revision == 1
    assert chapter.human_review.valid_for_current_revision is False
    check = reviewer.ready_to_finalize(project, chapter_id="ch_0001")
    assert check.ready is False
    assert check.pending_edits is True
    assert check.human_review_valid is False
    assert any("Human Review" in reason for reason in check.reasons)
    guard = lifecycle.guard_finalize(project, "ch_0001")
    assert guard.allowed is False
    assert guard.code == "human_review_stale"


# ---------------------------------------------------------------------------
# 11. Rewrite Section
# ---------------------------------------------------------------------------


def test_rewrite_candidate_and_apply_guards(tmp_path: Path, t02_valid_document) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    _complete_draft(project)
    reviewer.mark_reviewed(project, chapter_id="ch_0001")
    prose = writer.draft_markdown(project, "ch_0001", 1)
    target = "Anh nhặt hòn đá gõ vào thành nồi."
    replacement = "Anh nhặt hòn đá, gõ thử một nhịp vào thành nồi."
    request = RewriteSectionRequest(
        chapter_id="ch_0001",
        prose_revision=1,
        section_id="section_0002",
        selected_text=target,
        instruction="Thêm nhịp thao tác, giữ nguyên sự kiện.",
        constraints=["Không đổi sự kiện.", "Không thêm nhân vật mới."],
    )
    client = FakeLLMClient()

    # Replacement rỗng bị từ chối.
    client.queue(json.dumps({"replacement_markdown": "   ", "notes": [], "changed_intent": False}))
    with pytest.raises(ValidationFailure) as excinfo:
        reviewer.rewrite_section(project, client=client, request=request)
    assert any(issue.code == "empty_replacement" for issue in excinfo.value.result.errors)

    # Candidate hợp lệ: chỉ trả trong ActionResult.data, không tạo revision.
    client.queue(
        json.dumps(
            {
                "replacement_markdown": replacement,
                "notes": ["Giữ intent cũ."],
                "changed_intent": False,
            },
            ensure_ascii=False,
        )
    )
    result = reviewer.rewrite_section(project, client=client, request=request)
    assert result.data["replacement_markdown"] == replacement
    assert result.data["target_text"] == target
    assert result.data["changed_intent"] is False
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.current_draft_revision == 1
    assert writer.draft_markdown(project, "ch_0001", 1) == prose

    # `expected_revision` lệch ⇒ từ chối.
    with pytest.raises(GuardError) as excinfo:
        reviewer.apply_rewrite(
            project,
            chapter_id="ch_0001",
            replacement_markdown=replacement,
            target_text=target,
            expected_revision=7,
        )
    assert excinfo.value.code == "stale_replacement"

    # Target không có trong prose revision ⇒ từ chối.
    with pytest.raises(GuardError) as excinfo:
        reviewer.apply_rewrite(
            project,
            chapter_id="ch_0001",
            replacement_markdown=replacement,
            target_text="Đoạn không tồn tại trong prose.",
            expected_revision=1,
        )
    assert excinfo.value.code == "target_not_found"

    # Replacement rỗng / no-op ⇒ từ chối.
    with pytest.raises(GuardError) as excinfo:
        reviewer.apply_rewrite(
            project,
            chapter_id="ch_0001",
            replacement_markdown="   ",
            target_text=target,
            expected_revision=1,
        )
    assert excinfo.value.code == "empty_replacement"
    with pytest.raises(GuardError) as excinfo:
        reviewer.apply_rewrite(
            project,
            chapter_id="ch_0001",
            replacement_markdown=target,
            target_text=target,
            expected_revision=1,
        )
    assert excinfo.value.code == "no_op_replacement"

    # Apply thành công: revision mới, review cũ mất hiệu lực, không đổi ngoài scope.
    applied = reviewer.apply_rewrite(
        project,
        chapter_id="ch_0001",
        replacement_markdown=replacement,
        target_text=target,
        expected_revision=1,
    )

    assert applied.data["previous_revision"] == 1
    assert applied.data["revision"] == 2
    assert applied.data["human_review_invalidated"] is True
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.current_draft_revision == 2
    assert chapter.human_review is not None
    assert chapter.human_review.valid_for_current_revision is False
    new_prose = writer.draft_markdown(project, "ch_0001", 2)
    assert new_prose == prose.replace(target, replacement, 1)
    assert target not in new_prose
    assert reviewer.ready_to_finalize(project, chapter_id="ch_0001").ready is False
