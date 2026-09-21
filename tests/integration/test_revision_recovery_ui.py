"""AppTest cho UI Revision/Retcon/Recovery (T22).

Chạy offline trong `tmp_path` với `FakeLLMClient`. Các test bám đúng yêu cầu T22:

1. Retcon chương đã final qua UI: hiển thị `retcon_state`, nói rõ final cũ vẫn canon,
   và `reset_consistency` đánh dấu stale chain downstream.
2. Dẫn người dùng reconcile downstream **lần lượt** (chương kế tiếp bấm được, chương
   sau bị khóa cho tới khi chương trước rebuild xong).
3. Hoàn tất retcon (finalize + reconcile) rồi rebuild chương 2/3 qua UI.
4. Recovery: banner pending transaction + `reconcile.recover` chạy được; read-only
   khi `storage.requires_manual_recovery` (không render action ghi nào).
5. Impact report **không** mutation canon (so fingerprint) và stale chain hiển thị.
6. History/snapshot phân biệt accepted cũ / candidate mới / `finalizing`.
7. Revise Base Idea qua UI: tạo accepted revision mới + đánh dấu stale downstream,
   bấm lặp không tạo revision thứ hai.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from novel_ai.core import storage
from novel_ai.core.llm import FakeLLMClient
from novel_ai.core.models import ArtifactStatus, ChapterStatus
from novel_ai.pages import reconcile as reconcile_page
from novel_ai.pages import review as review_page
from novel_ai.pages import revision as revision_page
from novel_ai.services import reconcile as reconcile_service
from novel_ai.services import revision as revision_service

from t21_t22_support import (
    button_disabled,
    click,
    client_of,
    fingerprint_canon,
    fingerprint_tree,
    impact_report_json,
    proposal_json,
    projects_root_for,
    make_apptest,
    rendered,
    seed_chapter_project,
    seed_fully_finalized,
    seed_reviewed_draft,
)


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    return projects_root_for(tmp_path)


@pytest.fixture
def apptest_factory(tmp_path: Path, projects_root: Path, monkeypatch: Any):
    return make_apptest(tmp_path, projects_root, monkeypatch)


def _final_text(project: Any, chapter_id: str) -> str:
    chapter = storage.load_chapter(project, chapter_id)
    assert chapter is not None and chapter.final_revision is not None
    return storage.read_text(project.root / chapter.final_revision.markdown_ref)


# ---------------------------------------------------------------------------
# 1. Retcon + stale chain
# ---------------------------------------------------------------------------


def test_retcon_ui_shows_state_and_stale_chain(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Start Retcon qua UI giữ final cũ làm canon; reset consistency đánh dấu chain."""
    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=3)
    seed_fully_finalized(project)
    later_prose = {cid: _final_text(project, cid) for cid in ("ch_0002", "ch_0003")}

    at = apptest_factory(project, "revision")
    assert not at.exception
    assert "Start Retcon" in rendered(at)
    click(at, revision_page.KEY_START_RETCON)
    assert not at.exception

    marker = revision_service.retcon_state(project, chapter_id="ch_0001")
    assert marker is not None and marker["final_still_canon"] is True
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    assert chapter.status is ChapterStatus.final_reconciled  # chưa rời final
    assert chapter.final_revision is not None and chapter.final_revision.revision == 1
    assert chapter.current_draft_revision == marker["retcon_draft_revision"]
    assert "vẫn là canon" in rendered(at) or "vẫn canon" in rendered(at)
    assert revision_page.KEY_RESET_CONSISTENCY in [b.key for b in at.button]

    click(at, revision_page.KEY_RESET_CONSISTENCY)
    assert not at.exception
    timeline = storage.load_timeline(project)
    assert timeline.latest_consistent_chapter == 1
    assert timeline.latest_final_chapter == 3  # không ghi lùi
    assert [entry.chapter_number for entry in timeline.entries if entry.stale] == [2, 3]

    text = rendered(at)
    assert "Stale chain" in text
    assert "Reconcile downstream chương 2" in text
    # Hướng dẫn lần lượt: chương 2 bấm được, chương 3 khóa tới khi chương 2 xong.
    assert button_disabled(at, f"{revision_page.KEY_BLOCKER_DOWNSTREAM_PREFIX}ch_0002") is False
    assert button_disabled(at, f"{revision_page.KEY_BLOCKER_DOWNSTREAM_PREFIX}ch_0003") is True
    # Final prose chương sau không bị rewrite (D004).
    assert {cid: _final_text(project, cid) for cid in ("ch_0002", "ch_0003")} == later_prose
    # Artifact stale downstream được dẫn review/reaccept.
    blockers = revision_service.downstream_blockers(project)
    assert any(item["kind"] == "artifact" for item in blockers)


# ---------------------------------------------------------------------------
# 2+3. Hoàn tất retcon rồi rebuild downstream lần lượt qua UI
# ---------------------------------------------------------------------------


def test_retcon_completion_and_downstream_rebuild_via_ui(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Finalize retcon ở Review → accept ở Reconcile → rebuild chương 2, 3 ở Revision."""
    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=3)
    seed_fully_finalized(project)
    later_prose = {cid: _final_text(project, cid) for cid in ("ch_0002", "ch_0003")}

    # --- Revision: Start Retcon rồi đánh dấu stale chain downstream ------------
    at = apptest_factory(project, "revision")
    assert not at.exception
    click(at, revision_page.KEY_START_RETCON)
    assert not at.exception
    marker = revision_service.retcon_state(project, chapter_id="ch_0001")
    assert marker is not None and marker["final_still_canon"] is True
    click(at, revision_page.KEY_RESET_CONSISTENCY)
    assert not at.exception
    assert storage.load_timeline(project).latest_consistent_chapter == 1
    assert [entry.chapter_number for entry in storage.load_timeline(project).entries if entry.stale] == [2, 3]

    # --- Review: chapter final_reconciled + retcon mở → finalize bản retcon -----
    at = apptest_factory(project, "review")
    assert not at.exception
    text = rendered(at)
    assert "đang retcon" in text
    assert "vẫn là canon" in text or "vẫn canon" in text
    click(at, review_page.KEY_FINALIZE)
    assert not at.exception
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.status is ChapterStatus.finalizing
    assert chapter.final_candidate is not None
    # Final cũ vẫn là canon: final_revision chưa đổi.
    assert chapter.final_revision is not None and chapter.final_revision.revision == 1

    # --- Reconcile: generate + accept proposal cho bản retcon -------------------
    proposal = proposal_json(
        chapter_id="ch_0001",
        chapter_number=1,
        prose_revision=chapter.final_candidate.prose_revision,
        markdown_ref=chapter.final_candidate.markdown_ref,
        timeline_status="Sở Dương sống sót nhưng mất cái nồi (bản retcon).",
    )
    at = apptest_factory(project, "reconcile", responses=[proposal])
    assert not at.exception
    click(at, reconcile_page.KEY_GENERATE)
    assert not at.exception
    click(at, reconcile_page.KEY_ACCEPT)
    assert not at.exception
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.status is ChapterStatus.final_reconciled
    assert chapter.final_revision is not None and chapter.final_revision.revision == 2

    # --- Revision: rebuild chương 2 rồi chương 3 qua UI -------------------------
    ch2 = storage.load_chapter(project, "ch_0002")
    ch3 = storage.load_chapter(project, "ch_0003")
    assert ch2 is not None and ch2.final_revision is not None
    assert ch3 is not None and ch3.final_revision is not None
    responses = [
        proposal_json(
            chapter_id="ch_0002",
            chapter_number=2,
            prose_revision=ch2.final_revision.source_prose_revision,
            markdown_ref=ch2.final_revision.markdown_ref,
        ),
        proposal_json(
            chapter_id="ch_0003",
            chapter_number=3,
            prose_revision=ch3.final_revision.source_prose_revision,
            markdown_ref=ch3.final_revision.markdown_ref,
        ),
    ]
    at = apptest_factory(project, "revision", responses=responses)
    assert not at.exception
    click(at, f"{revision_page.KEY_BLOCKER_DOWNSTREAM_PREFIX}ch_0002")
    assert not at.exception
    assert len(client_of(at).calls) == 1
    # Sau khi chương 2 xong, chương 3 là bước kế tiếp và bấm được.
    assert button_disabled(at, f"{revision_page.KEY_BLOCKER_DOWNSTREAM_PREFIX}ch_0003") is False
    click(at, f"{revision_page.KEY_BLOCKER_DOWNSTREAM_PREFIX}ch_0003")
    assert not at.exception
    assert len(client_of(at).calls) == 2

    assert revision_service.downstream_blockers(project) == []
    timeline = storage.load_timeline(project)
    assert [entry.chapter_number for entry in timeline.entries] == [1, 2, 3]
    assert not any(entry.stale for entry in timeline.entries)
    assert timeline.latest_consistent_chapter == 3
    # Không nhân đôi entry khi rebuild.
    assert len(timeline.entries) == 3
    # Final prose chương 2/3 vẫn không bị rewrite (D004).
    assert {cid: _final_text(project, cid) for cid in ("ch_0002", "ch_0003")} == later_prose


# ---------------------------------------------------------------------------
# 4. Recovery
# ---------------------------------------------------------------------------


def _crash_during_reconcile_commit(project: Any, monkeypatch: Any) -> None:
    """Đẩy một transaction vào trạng thái pending bằng lỗi ghi giả lập."""
    real_replace = os.replace
    target = project.paths.chapter_json("ch_0001")

    def _replace(src: Any, dst: Any) -> None:
        if Path(dst) == target:
            raise OSError("crash giả lập khi commit")
        real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", _replace)
        with pytest.raises(Exception):
            reconcile_service.accept_reconciliation(project, chapter_id="ch_0001")


def _pending_project(tmp_path: Path, document: dict[str, Any], monkeypatch: Any) -> Any:
    project = seed_chapter_project(tmp_path, document, chapters=2)
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
    _crash_during_reconcile_commit(project, monkeypatch)
    assert storage.needs_recovery(project) is True
    return project


def test_recovery_banner_and_recover_button(
    apptest_factory: Any,
    tmp_path: Path,
    t02_valid_document: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """Pending transaction hiển thị ở Revision và `reconcile.recover` chạy được."""
    project = _pending_project(tmp_path, t02_valid_document, monkeypatch)
    pending = storage.pending_operation_ids(project)
    assert pending and storage.requires_manual_recovery(project) is False

    at = apptest_factory(project, "revision")
    assert not at.exception
    text = rendered(at)
    assert "transaction dở" in text
    assert pending[0] in text
    assert revision_page.KEY_RECOVER in [b.key for b in at.button]

    click(at, revision_page.KEY_RECOVER)
    assert not at.exception
    assert storage.needs_recovery(project) is False
    assert storage.pending_operation_ids(project) == []
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None and chapter.status is ChapterStatus.final_reconciled
    assert "Recovery hoàn tất" in rendered(at)
    # Recovery lần hai idempotent, vẫn không pending.
    assert reconcile_service.recover(project).status == storage.RECOVERY_CLEAN


def test_revision_page_is_read_only_when_manual_recovery_required(
    apptest_factory: Any,
    tmp_path: Path,
    t02_valid_document: dict[str, Any],
    monkeypatch: Any,
) -> None:
    """`storage.requires_manual_recovery` → UI read-only, không render action ghi."""
    project = _pending_project(tmp_path, t02_valid_document, monkeypatch)
    operation_id = storage.pending_operation_ids(project)[0]
    staged = project.paths.ops_pending_dir / operation_id / "staged"
    for item in sorted(staged.rglob("*"), reverse=True):
        if item.is_file():
            item.unlink()
    assert storage.requires_manual_recovery(project) is True
    before = fingerprint_tree(project.root)

    at = apptest_factory(project, "revision")
    assert not at.exception
    text = rendered(at)
    assert "read-only" in text
    assert "kiểm tra thủ công" in text or "kiểm tra `.ops/pending/` và `history/`" in text

    keys = [b.key for b in at.button]
    assert revision_page.KEY_RECOVER not in keys
    assert revision_page.KEY_START_RETCON not in keys
    assert revision_page.KEY_REVISE_BASE not in keys
    assert revision_page.KEY_IMPACT_RUN not in keys
    # Render read-only không ghi file nào.
    assert fingerprint_tree(project.root) == before


# ---------------------------------------------------------------------------
# 5. Impact report không mutation
# ---------------------------------------------------------------------------


def test_impact_report_does_not_mutate_canon(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Chạy impact report: chỉ lưu report; canon/derived state không đổi."""
    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=3)
    seed_fully_finalized(project)
    canon_before = fingerprint_canon(project)

    at = apptest_factory(
        project,
        "revision",
        responses=[impact_report_json(item_kind="chapter", item_id="ch_0001")],
    )
    assert not at.exception
    at.text_input(key=revision_page.KEY_IMPACT_ID).set_value("ch_0001")
    at.run(timeout=180)
    assert not at.exception
    click(at, revision_page.KEY_IMPACT_RUN)
    assert not at.exception
    assert len(client_of(at).calls) == 1

    envelope = storage.load_artifact(project, "impact_report_ch_0001")
    assert envelope is not None and envelope.candidate_revision is not None
    assert envelope.candidate_revision.payload.affected_items
    text = rendered(at)
    assert "ch_0002" in text
    assert "State chương 2 dựa trên bản final cũ" in text
    # Không mutation canon: timeline, relationship, chapter metadata, final prose, plan.
    assert fingerprint_canon(project) == canon_before
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.final_reconciled
    assert not any(entry.stale for entry in storage.load_timeline(project).entries)


# ---------------------------------------------------------------------------
# 6. History/snapshot
# ---------------------------------------------------------------------------


def test_history_panel_distinguishes_versions(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """History/snapshot hiển thị accepted cũ, candidate mới (retcon) và `finalizing`."""
    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=3)
    seed_fully_finalized(project, chapters=("ch_0001", "ch_0002"))
    revision_service.start_retcon(project, chapter_id="ch_0001")
    # ch_0003 về `finalizing` để đủ ba loại phiên bản.
    seed_reviewed_draft(project, chapter_id="ch_0003")
    reconcile_service.finalize_chapter(project, chapter_id="ch_0003")

    at = apptest_factory(project, "revision")
    assert not at.exception
    text = rendered(at)
    assert "accepted prose r1" in text
    assert "final manuscript r1" in text
    assert "candidate mới prose r2" in text
    assert "finalizing (chờ commit) prose r1" in text
    assert "đang retcon: final cũ vẫn canon" in text
    assert "snapshot_" in text
    assert "History operation" in text or "`save_chapter`" in text


# ---------------------------------------------------------------------------
# 7. Revise Base Idea
# ---------------------------------------------------------------------------


def test_revise_base_idea_marks_downstream_stale_and_is_not_duplicated(
    apptest_factory: Any, tmp_path: Path, t02_valid_document: dict[str, Any]
) -> None:
    """Revise Base Idea qua UI: accepted revision mới + stale downstream; bấm lặp vô hại."""
    project = seed_chapter_project(tmp_path, t02_valid_document, chapters=2)
    assert storage.load_artifact(project, "short_plan").status is ArtifactStatus.accepted

    at = apptest_factory(project, "revision")
    assert not at.exception
    new_text = "Base Idea bản revise: Sở Dương mất trí nhớ và giữ cái nồi.\n"
    at.text_area(key=revision_page.KEY_BASE_IDEA_TEXT).set_value(new_text)
    at.run(timeout=180)
    assert not at.exception
    click(at, revision_page.KEY_REVISE_BASE)
    assert not at.exception

    meta = storage.read_json(project.paths.base_idea_meta_json)
    assert meta["revision"] == 2
    assert storage.read_text(project.paths.base_idea_md) == new_text
    assert storage.load_artifact(project, "short_plan").status is ArtifactStatus.stale
    assert "stale" in rendered(at)

    # Bấm lặp (không đổi nội dung): không tạo revision thứ hai.
    click(at, revision_page.KEY_REVISE_BASE)
    assert not at.exception
    assert storage.read_json(project.paths.base_idea_meta_json)["revision"] == 2
    assert storage.read_text(project.paths.base_idea_md) == new_text
