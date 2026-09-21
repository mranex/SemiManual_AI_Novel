"""AppTest cho UI Skeleton/Writer/Review/Reconcile (T21).

Toàn bộ chạy offline trong `tmp_path` với `FakeLLMClient`. Mỗi bước lớn là một
AppTest riêng (mỗi lần `at.run()` + bấm nút) để tránh test giòn; phần nào chỉ kiểm
ở tầng service được ghi rõ trong từng test.

Nhóm kiểm tra:

1. Happy path chương 1 → chương 2 qua UI thật (Skeleton accept → Writer → Save →
   AI Review → Human Review → Finalize → accept reconciliation → chương 1
   `final_reconciled` → Writer chương 2 chạy được với state mới).
2. Partial stream: UI hiện partial, không có nút Finalize khả dụng; reload vẫn thấy
   partial.
3. Reload giữa `finalizing`: proposal còn đó và chương sau vẫn khóa.
4. JSON reconcile sai: UI báo lỗi, accepted state không đổi, editor giữ nội dung.
5. Rerun thuần không ghi file trên cả 5 page.
6. Bấm lặp/double submit: không gọi API lần hai, không tạo revision thứ hai.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_ai.core import storage
from novel_ai.core.llm import FakeLLMClient
from novel_ai.core.models import ArtifactStatus, ChapterStatus
from novel_ai.pages import _chapter_ui, _common
from novel_ai.pages import reconcile as reconcile_page
from novel_ai.pages import review as review_page
from novel_ai.pages import skeleton as skeleton_page
from novel_ai.pages import writer as writer_page
from novel_ai.services import reconcile as reconcile_service

from t21_t22_support import (
    COMPLETE_PROSE,
    EDITED_PROSE,
    SKELETON_CH1_REGENERATED,
    button_disabled,
    click,
    client_of,
    fingerprint_canon,
    fingerprint_tree,
    proposal_json,
    rendered,
    review_report_json,
    seed_chapter_project,
)

REVIEW_EDITOR_KEY = f"novel_ai_review_prose_ch_0001"
RECONCILE_EDITOR_KEY = _common.editor_key("reconciliation", workspace="reconcile")


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    from t21_t22_support import projects_root_for

    return projects_root_for(tmp_path)


@pytest.fixture
def apptest_factory(tmp_path: Path, projects_root: Path, monkeypatch: Any):
    from t21_t22_support import make_apptest

    return make_apptest(tmp_path, projects_root, monkeypatch)


# ---------------------------------------------------------------------------
# 1. Happy path chương 1 → chương 2
# ---------------------------------------------------------------------------


def test_ui_flow_chapter_one_to_two(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Luồng chương đầy đủ qua UI; mỗi bước một AppTest + một lần bấm nút."""
    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)

    # --- Skeleton: generate candidate mới rồi accept qua UI -----------------
    at = apptest_factory(
        project,
        "skeleton",
        responses=[json.dumps(SKELETON_CH1_REGENERATED, ensure_ascii=False)],
    )
    assert not at.exception
    click(at, skeleton_page.KEY_GENERATE)
    assert not at.exception
    assert len(client_of(at).calls) == 1
    envelope = storage.load_artifact(project, "skeleton_ch_0001")
    assert envelope is not None and envelope.candidate_revision is not None
    assert envelope.accepted_revision is not None
    assert envelope.accepted_revision.revision == 1  # accepted cũ chưa đổi
    click(at, skeleton_page.KEY_ACCEPT)
    assert not at.exception
    envelope = storage.load_artifact(project, "skeleton_ch_0001")
    assert envelope is not None and envelope.accepted_revision is not None
    assert envelope.accepted_revision.revision == 2
    assert envelope.candidate_revision is None
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.skeleton_ready

    # --- Writer: gate cho phép, generate draft complete ---------------------
    assert _chapter_ui.writer_gate(project, "ch_0001").allowed is True
    # Chương 2 vẫn khóa vì chương 1 chưa `final_reconciled`.
    blocked_gate = _chapter_ui.writer_gate(project, "ch_0002")
    assert blocked_gate.allowed is False
    assert "previous_chapter_not_finalized" in blocked_gate.code

    at = apptest_factory(project, "writer", responses=[COMPLETE_PROSE])
    assert not at.exception
    assert "Backend cho phép Writer" in rendered(at)
    click(at, writer_page.KEY_RUN)
    assert not at.exception
    assert len(client_of(at).calls) == 1
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert chapter.status is ChapterStatus.review_required
    assert chapter.current_draft is not None and chapter.current_draft.is_complete is True
    assert chapter.current_draft.source_type.value == "llm"

    # --- Review: Save Draft (user sửa prose) --------------------------------
    at = apptest_factory(project, "review")
    assert not at.exception
    at.text_area(key=REVIEW_EDITOR_KEY).set_value(EDITED_PROSE)
    at.run(timeout=180)
    assert not at.exception
    click(at, review_page.KEY_PROSE_SAVE)
    assert not at.exception
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.current_draft_revision == 2
    assert storage.read_text(project.root / chapter.current_draft.markdown_ref) == EDITED_PROSE

    # --- Review: AI Review (report hỗ trợ, quote nguyên văn) ----------------
    at = apptest_factory(
        project,
        "review",
        responses=[review_report_json(chapter_id="ch_0001", prose_revision=2)],
    )
    click(at, review_page.KEY_AI_RUN)
    assert not at.exception
    assert "Nhịp đoạn kết" in rendered(at)
    assert "Anh ngồi im nghe nhịp thở" in rendered(at)  # evidence.quote hiển thị
    report = storage.load_artifact(project, "review_report_ch_0001")
    assert report is not None and report.status is ArtifactStatus.accepted
    # Report không phải gate: chapter vẫn review_required và chưa có final candidate.
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.status is ChapterStatus.review_required
    assert chapter.final_candidate is None

    # --- Review: Human Review gắn đúng prose revision -----------------------
    click(at, review_page.KEY_HUMAN_REVIEW)
    assert not at.exception
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.human_review is not None
    assert chapter.human_review.prose_revision == 2
    assert chapter.human_review.valid_for_current_revision is True

    # --- Review: Finalize → `finalizing`, chương sau vẫn khóa ---------------
    click(at, review_page.KEY_FINALIZE)
    assert not at.exception
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.status is ChapterStatus.finalizing
    assert chapter.final_candidate is not None
    assert "vẫn khóa" in rendered(at)
    assert storage.load_chapter(project, "ch_0002").status is ChapterStatus.skeleton_ready
    assert _chapter_ui.writer_gate(project, "ch_0002").allowed is False

    # --- Reconcile: generate proposal rồi accept (transaction nhiều file) ---
    assert chapter.final_candidate is not None
    proposal = proposal_json(
        chapter_id="ch_0001",
        chapter_number=1,
        prose_revision=chapter.final_candidate.prose_revision,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    at = apptest_factory(project, "reconcile", responses=[proposal])
    assert not at.exception
    click(at, reconcile_page.KEY_GENERATE)
    assert not at.exception
    assert len(client_of(at).calls) == 1
    artifact = storage.load_artifact(project, "reconciliation_ch_0001")
    assert artifact is not None and artifact.candidate_revision is not None
    click(at, reconcile_page.KEY_ACCEPT)
    assert not at.exception
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.status is ChapterStatus.final_reconciled
    assert chapter.final_candidate is None and chapter.final_revision is not None
    timeline = storage.load_timeline(project)
    assert [entry.chapter_number for entry in timeline.entries] == [1]
    assert "final_reconciled" in rendered(at)

    # --- Writer chương 2 chạy được với state mới ---------------------------
    at = apptest_factory(project, "writer", responses=[COMPLETE_PROSE])
    at.selectbox(key=writer_page.KEY_CHAPTER).set_value("ch_0002")
    at.run(timeout=180)
    assert not at.exception
    assert "Backend cho phép Writer" in rendered(at)
    click(at, writer_page.KEY_RUN)
    assert not at.exception
    assert len(client_of(at).calls) == 1
    chapter_two = storage.load_chapter(project, "ch_0002")
    assert chapter_two is not None
    assert chapter_two.status is ChapterStatus.review_required
    assert chapter_two.current_draft is not None and chapter_two.current_draft.is_complete is True


# ---------------------------------------------------------------------------
# 2. Partial stream
# ---------------------------------------------------------------------------


def test_partial_stream_blocks_finalize_and_survives_reload(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Stream đứt qua UI: partial hiển thị, không Finalize được, reload vẫn partial."""
    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)
    partial_text = "Sở Dương chống tay ngồi dậy. Cái nồi nằm ngay bên cạnh."

    at = apptest_factory(
        project, "writer", stream=[partial_text, "interrupted"]
    )
    assert not at.exception
    click(at, writer_page.KEY_RUN)
    assert not at.exception
    assert len(client_of(at).calls) == 1

    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert chapter.status is ChapterStatus.draft
    assert chapter.current_draft is not None
    assert chapter.current_draft.is_complete is False
    text = rendered(at)
    assert "partial" in text.lower()
    assert "không finalize được" in text

    # Reload (session state mới, không client script): vẫn thấy partial từ file.
    at2 = apptest_factory(project, "writer")
    assert not at2.exception
    assert "partial" in rendered(at2).lower()
    assert client_of(at2).calls == []

    # Review page: nút Finalize tồn tại nhưng bị disable; backend guard cũng từ chối.
    at3 = apptest_factory(project, "review")
    assert not at3.exception
    assert button_disabled(at3, review_page.KEY_FINALIZE) is True
    assert "không finalize được" in rendered(at3)
    check = _chapter_ui.prose_text  # sanity: helper đọc prose vẫn dùng được
    assert check(project, "ch_0001", chapter.current_draft_revision) == partial_text


# ---------------------------------------------------------------------------
# 3. Reload giữa `finalizing`
# ---------------------------------------------------------------------------


def test_reload_during_finalizing_keeps_pending_proposal(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Mở lại app giữa `finalizing`: proposal pending hiển thị, chương sau vẫn khóa."""
    from t21_t22_support import seed_reviewed_draft

    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)
    seed_reviewed_draft(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.final_candidate is not None
    proposal = proposal_json(
        chapter_id="ch_0001",
        chapter_number=1,
        prose_revision=chapter.final_candidate.prose_revision,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    reconcile_service.generate_reconciliation(
        project, client=FakeLLMClient([proposal]), chapter_id="ch_0001"
    )

    at = apptest_factory(project, "reconcile")
    assert not at.exception
    text = rendered(at)
    assert "finalizing" in text
    assert "vẫn khóa" in text
    assert "ch_0001" in at.text_area(key=RECONCILE_EDITOR_KEY).value
    assert "reconciliation" in text
    # Chương sau vẫn khóa cho tới khi accept.
    assert _chapter_ui.writer_gate(project, "ch_0002").allowed is False
    assert storage.load_chapter(project, "ch_0002").status is ChapterStatus.skeleton_ready
    # Bấm accept trên app vừa reload vẫn đi tiếp đúng bước.
    click(at, reconcile_page.KEY_ACCEPT)
    assert not at.exception
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.final_reconciled


# ---------------------------------------------------------------------------
# 4. JSON reconcile sai
# ---------------------------------------------------------------------------


def test_invalid_reconcile_json_keeps_accepted_state(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """JSON sai (parse hoặc schema) chỉ hiện lỗi; accepted state và candidate giữ nguyên."""
    from t21_t22_support import seed_reviewed_draft

    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)
    seed_reviewed_draft(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.final_candidate is not None
    proposal = proposal_json(
        chapter_id="ch_0001",
        chapter_number=1,
        prose_revision=chapter.final_candidate.prose_revision,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    reconcile_service.generate_reconciliation(
        project, client=FakeLLMClient([proposal]), chapter_id="ch_0001"
    )
    canon_before = fingerprint_canon(project)
    envelope_before = storage.load_artifact(project, "reconciliation_ch_0001")
    assert envelope_before is not None and envelope_before.candidate_revision is not None
    candidate_revision_before = envelope_before.candidate_revision.revision

    at = apptest_factory(project, "reconcile")
    assert not at.exception

    # (a) JSON không parse được.
    at.text_area(key=RECONCILE_EDITOR_KEY).set_value("{không phải json")
    at.run(timeout=180)
    click(at, reconcile_page.KEY_EDIT_JSON)
    assert not at.exception
    assert "JSON không hợp lệ" in rendered(at)
    assert at.text_area(key=RECONCILE_EDITOR_KEY).value == "{không phải json"

    # (b) JSON parse được nhưng sai schema.
    at.text_area(key=RECONCILE_EDITOR_KEY).set_value('{"chapter_id": "ch_0001"}')
    at.run(timeout=180)
    click(at, reconcile_page.KEY_EDIT_JSON)
    assert not at.exception
    text = rendered(at)
    assert "không qua validation" in text or "không khớp schema" in text
    assert at.text_area(key=RECONCILE_EDITOR_KEY).value == '{"chapter_id": "ch_0001"}'

    assert fingerprint_canon(project) == canon_before
    envelope_after = storage.load_artifact(project, "reconciliation_ch_0001")
    assert envelope_after is not None and envelope_after.candidate_revision is not None
    assert envelope_after.candidate_revision.revision == candidate_revision_before
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.finalizing


# ---------------------------------------------------------------------------
# 5. Auto Accept không thay Human Review/Finalize
# ---------------------------------------------------------------------------


def test_auto_accept_does_not_finalize_or_skip_human_review(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Auto Accept structured bật: UI không tự Finalize và không bỏ Human Review."""
    from t21_t22_support import seed_reviewed_draft

    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)
    project.update_config(auto_accept_structured=True)
    project.reload()
    seed_reviewed_draft(project, chapter_id="ch_0001", reviewed=False)

    at = apptest_factory(project, "review")
    assert not at.exception
    assert button_disabled(at, review_page.KEY_FINALIZE) is False  # draft complete
    timeline_before = storage.load_timeline(project).model_dump(mode="json")
    click(at, review_page.KEY_FINALIZE)

    assert not at.exception
    text = rendered(at)
    assert "Human Review" in text
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert chapter.status is ChapterStatus.review_required
    assert chapter.final_candidate is None
    assert chapter.human_review is None
    assert storage.load_timeline(project).model_dump(mode="json") == timeline_before

    # Human Review vẫn phải là action rõ ràng của người dùng.
    click(at, review_page.KEY_HUMAN_REVIEW)
    assert not at.exception
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.human_review is not None


# ---------------------------------------------------------------------------
# 6. Rerun thuần không ghi file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workspace", ["skeleton", "writer", "review", "reconcile", "revision"])
def test_pure_rerun_does_not_write_files(
    apptest_factory: Any,
    tmp_path: Path,
    t02_valid_document: dict[str, Any],
    workspace: str,
) -> None:
    """Không bấm gì: hai lần `at.run()` không đổi fingerprint và không gọi LLM."""
    from t21_t22_support import seed_fully_finalized

    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)
    seed_fully_finalized(project, chapters=("ch_0001", "ch_0002"))
    before = fingerprint_tree(project.root)
    assert before

    at = apptest_factory(project, workspace)
    assert not at.exception
    at.run(timeout=180)
    at.run(timeout=180)

    assert not at.exception
    assert fingerprint_tree(project.root) == before
    assert client_of(at).calls == []


# ---------------------------------------------------------------------------
# 7. Bấm lặp / double submit
# ---------------------------------------------------------------------------


def test_repeat_click_does_not_call_llm_twice(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Bấm Generate Writer hai lần: lần hai bị chặn, không có draft/revision thứ hai."""
    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)
    at = apptest_factory(project, "writer", responses=[COMPLETE_PROSE])
    assert not at.exception
    click(at, writer_page.KEY_RUN)
    after_first = fingerprint_tree(project.root)
    assert len(client_of(at).calls) == 1

    click(at, writer_page.KEY_RUN)

    assert not at.exception
    assert len(client_of(at).calls) == 1
    assert fingerprint_tree(project.root) == after_first
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and len(chapter.drafts) == 1
    assert "bỏ qua" in rendered(at)


def test_repeat_save_draft_does_not_create_second_revision(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Save Draft hai lần với cùng nội dung: chỉ một prose revision mới."""
    from t21_t22_support import seed_reviewed_draft

    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)
    seed_reviewed_draft(project, chapter_id="ch_0001")

    at = apptest_factory(project, "review")
    assert not at.exception
    at.text_area(key=REVIEW_EDITOR_KEY).set_value(EDITED_PROSE)
    at.run(timeout=180)
    click(at, review_page.KEY_PROSE_SAVE)
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.current_draft_revision == 2
    after_first = fingerprint_tree(project.root)

    click(at, review_page.KEY_PROSE_SAVE)

    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert chapter.current_draft_revision == 2
    assert [draft.revision for draft in chapter.drafts] == [1, 2]
    assert fingerprint_tree(project.root) == after_first
    assert "bỏ qua" in rendered(at)


def test_repeated_reconcile_generate_is_idempotent_at_ui_level(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Generate reconciliation hai lần: không gọi LLM lần hai, candidate không tăng revision."""
    from t21_t22_support import seed_reviewed_draft

    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)
    seed_reviewed_draft(project, chapter_id="ch_0001")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0001")
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.final_candidate is not None
    proposal = proposal_json(
        chapter_id="ch_0001",
        chapter_number=1,
        prose_revision=chapter.final_candidate.prose_revision,
        markdown_ref=chapter.final_candidate.markdown_ref,
    )
    at = apptest_factory(project, "reconcile", responses=[proposal])
    click(at, reconcile_page.KEY_GENERATE)
    assert not at.exception
    assert len(client_of(at).calls) == 1
    artifact = storage.load_artifact(project, "reconciliation_ch_0001")
    assert artifact is not None and artifact.candidate_revision is not None
    revision_after_first = artifact.candidate_revision.revision
    after_first = fingerprint_tree(project.root)

    click(at, reconcile_page.KEY_GENERATE)

    assert not at.exception
    assert len(client_of(at).calls) == 1
    artifact = storage.load_artifact(project, "reconciliation_ch_0001")
    assert artifact is not None and artifact.candidate_revision is not None
    assert artifact.candidate_revision.revision == revision_after_first
    assert fingerprint_tree(project.root) == after_first
    assert "bỏ qua" in rendered(at)
