"""Integration test T15 — Skeleton và Writer services.

Seed project và payload LLM dùng chung nằm ở `t15_t16_support.py` (không phải
file test). Test T16 nằm ở `test_writer_review_services.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ai.core import storage
from novel_ai.core.context import ContextMode, build_writer_context
from novel_ai.core.llm import STREAM_INTERRUPTED, FakeLLMClient, LLMError
from novel_ai.core.models import (
    ArtifactStatus,
    ChapterStatus,
    SourceType,
)
from novel_ai.services import (
    GuardError,
    LLMUnavailableError,
    StaleDependencyError,
    ValidationFailure,
)
from novel_ai.services import reviewer, skeleton, writer
from t15_t16_support import (
    BODY_SECRET,
    COMPLETE_PROSE,
    LATE_LORE,
    POT_SECRET,
    PURPOSE_SECRET,
    SKELETON_CH1_REGENERATED,
    SKELETON_CH2_GENERATED,
    _complete_draft,
    seed_project,
)

# ---------------------------------------------------------------------------
# 1. Guard trước khi gọi LLM
# ---------------------------------------------------------------------------


def test_writer_guard_blocks_before_llm(tmp_path: Path, t02_valid_document) -> None:
    client = FakeLLMClient()

    # (a) Thiếu Skeleton accepted cho chương 2.
    project = seed_project(
        tmp_path / "missing_skeleton", t02_valid_document, chapter_two_skeleton=False
    )
    with pytest.raises(GuardError) as excinfo:
        writer.generate_draft(project, client=client, chapter_id="ch_0002")
    assert excinfo.value.code in {"missing_dependency", "stale_dependency"}
    assert client.calls == []
    assert storage.load_chapter(project, "ch_0002").drafts == []

    # (b) Chương trước chưa `final_reconciled`.
    project = seed_project(
        tmp_path / "previous_open",
        t02_valid_document,
        chapter_one_status=ChapterStatus.review_required,
    )
    with pytest.raises(StaleDependencyError) as excinfo:
        writer.generate_draft(project, client=client, chapter_id="ch_0002")
    assert excinfo.value.code == "stale_dependency"
    assert client.calls == []

    # (c) Chương trước đang `finalizing`.
    project = seed_project(
        tmp_path / "previous_finalizing",
        t02_valid_document,
        chapter_one_status=ChapterStatus.finalizing,
    )
    with pytest.raises(StaleDependencyError) as excinfo:
        writer.generate_draft(project, client=client, chapter_id="ch_0002")
    assert excinfo.value.code == "stale_dependency"
    assert client.calls == []

    # (d) Chapter không tồn tại.
    with pytest.raises(GuardError) as excinfo:
        writer.generate_draft(project, client=client, chapter_id="ch_0099")
    assert excinfo.value.code == "chapter_missing"
    assert client.calls == []


def test_writer_unlocks_chapter_two_after_previous_final_reconciled(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(
        tmp_path, t02_valid_document, chapter_one_status=ChapterStatus.final_reconciled
    )
    client = FakeLLMClient([COMPLETE_PROSE])

    result = writer.generate_draft(project, client=client, chapter_id="ch_0002")

    assert result.data["is_complete"] is True
    assert storage.load_chapter(project, "ch_0002").status is ChapterStatus.review_required


# ---------------------------------------------------------------------------
# 2. Happy path
# ---------------------------------------------------------------------------


def test_short_plan_to_skeleton_to_writer_draft_review_required(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(tmp_path, t02_valid_document)

    # Short Plan accepted + Skeleton accepted đã có, chapter ở `skeleton_ready`.
    assert skeleton.load_accepted(project, "ch_0001") is not None
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.skeleton_ready

    client = FakeLLMClient([COMPLETE_PROSE])
    result = writer.generate_draft(project, client=client, chapter_id="ch_0001")

    assert result.data["is_complete"] is True
    assert result.data["stream_status"] == "completed"
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.status is ChapterStatus.review_required
    assert chapter.current_draft_revision == 1
    assert chapter.current_draft is not None and chapter.current_draft.is_complete is True
    assert writer.draft_markdown(project, "ch_0001", 1).strip() == COMPLETE_PROSE.strip()
    assert len(client.calls) == 1
    assert writer.latest_operation_for(project, "ch_0001")["revision"] == 1


# ---------------------------------------------------------------------------
# 3. Stream đứt + Continue
# ---------------------------------------------------------------------------


def test_stream_interrupt_keeps_partial_then_continue_completes(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    client = FakeLLMClient()
    client.queue_stream("Sở Dương chống tay ngồi dậy. Cái nồi nằm bên cạnh.", STREAM_INTERRUPTED)
    chunks = []

    result = writer.generate_draft(
        project, client=client, chapter_id="ch_0001", stream=True, on_chunk=chunks.append
    )

    assert result.data["is_complete"] is False
    assert result.data["stream_status"] == "partial"
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.status is ChapterStatus.draft
    assert chapter.status is not ChapterStatus.review_required
    assert chapter.current_draft is not None and chapter.current_draft.is_complete is False
    partial = writer.draft_markdown(project, "ch_0001", 1)
    assert partial.startswith("Sở Dương chống tay ngồi dậy.")
    assert chunks[-1].status == "partial"

    # Đọc lại từ disk vẫn thấy partial.
    reloaded = storage.load_chapter(project, "ch_0001")
    assert reloaded.current_draft is not None
    assert reloaded.current_draft.is_complete is False
    assert reviewer.ready_to_finalize(project, chapter_id="ch_0001").ready is False

    # Continue nối đúng tail của draft cũ và hoàn tất được.
    client.queue_stream("Anh nhặt hòn đá gõ vào thành nồi. Tiếng chạm khô và rõ.")
    result2 = writer.continue_draft(project, client=client, chapter_id="ch_0001", stream=True)

    assert result2.data["is_complete"] is True
    chapter2 = storage.load_chapter(project, "ch_0001")
    assert chapter2.status is ChapterStatus.review_required
    assert chapter2.current_draft_revision == 2
    prose2 = writer.draft_markdown(project, "ch_0001", 2)
    assert prose2.startswith("Sở Dương chống tay ngồi dậy.")
    assert "Anh nhặt hòn đá gõ vào thành nồi." in prose2
    # Prompt Continue nhận tail của draft hiện tại cùng Skeleton/context.
    continue_input = client.calls[-1].messages[1].content
    assert "Sở Dương chống tay ngồi dậy." in continue_input
    assert "section_0002" in continue_input


# ---------------------------------------------------------------------------
# 4. Output rỗng/chỉ báo lỗi
# ---------------------------------------------------------------------------


def test_empty_or_error_only_output_does_not_open_review(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    client = FakeLLMClient(
        ["", "Xin lỗi, tôi không thể viết chương này vì thiếu dữ liệu về nhân vật."]
    )

    empty = writer.generate_draft(project, client=client, chapter_id="ch_0001")
    assert empty.data["is_complete"] is False
    assert empty.data["reason"] == "no_prose_output"
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.status is ChapterStatus.skeleton_ready
    assert chapter.current_draft is None
    assert reviewer.ready_to_finalize(project, chapter_id="ch_0001").ready is False

    notice = writer.generate_draft(project, client=client, chapter_id="ch_0001")
    assert notice.data["reason"] == "no_prose_output"
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.status is not ChapterStatus.review_required
    assert chapter.current_draft is None


# ---------------------------------------------------------------------------
# 5. Regenerate / discard / save
# ---------------------------------------------------------------------------


def test_regenerate_failure_keeps_state_and_duplicate_operation_is_idempotent(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    client = FakeLLMClient([COMPLETE_PROSE])
    writer.generate_draft(project, client=client, chapter_id="ch_0001")
    before = storage.load_chapter(project, "ch_0001")

    # Regenerate thất bại ở LLM: không revision mới, final/accepted không đổi.
    client.queue(LLMError("network down"))
    with pytest.raises(LLMUnavailableError):
        writer.regenerate_draft(project, client=client, chapter_id="ch_0001")
    after = storage.load_chapter(project, "ch_0001")
    assert after.drafts == before.drafts
    assert after.current_draft_revision == before.current_draft_revision
    assert after.final_revision is None
    assert after.final_candidate is None

    # Duplicate operation_id: gọi lần hai không gọi LLM và không tạo revision mới.
    client.queue(COMPLETE_PROSE)
    op_id = "op_writer_duplicate_0001"
    first = writer.generate_draft(
        project, client=client, chapter_id="ch_0001", operation_id=op_id
    )
    calls_after_first = len(client.calls)
    second = writer.generate_draft(
        project, client=client, chapter_id="ch_0001", operation_id=op_id
    )
    assert len(client.calls) == calls_after_first
    assert second.data["revision"] == first.data["revision"]
    assert len(storage.load_chapter(project, "ch_0001").drafts) == 2

    # Discard: storage T09 chặn lùi `current_draft_revision`, nên chỉ bỏ được
    # revision cũ; bỏ chính revision đang hiển thị bị từ chối.
    with pytest.raises(GuardError) as excinfo:
        writer.discard_draft(project, chapter_id="ch_0001", revision=2)
    assert excinfo.value.code == "draft_is_current"
    discarded = writer.discard_draft(project, chapter_id="ch_0001")
    assert discarded.data["revision"] == 1
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.draft_revision(1) is None
    assert chapter.current_draft_revision == 2
    assert chapter.status is ChapterStatus.review_required

    # Save draft người dùng sửa: revision mới, is_complete suy theo nội dung.
    saved = writer.save_draft(
        project, chapter_id="ch_0001", text=COMPLETE_PROSE + "\n\nSở Dương thử lần nữa."
    )
    assert saved.data["is_complete"] is True
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter.current_draft_revision == 3
    assert chapter.status is ChapterStatus.review_required
    assert chapter.current_draft.source_type is SourceType.user

    partial = writer.save_draft(project, chapter_id="ch_0001", text="   ")
    assert partial.data["is_complete"] is False
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.draft


def test_regenerate_is_blocked_on_final_chapter(tmp_path: Path, t02_valid_document) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    _complete_draft(project)
    storage.save_chapter(
        project,
        storage.load_chapter(project, "ch_0001").model_copy(
            update={"status": ChapterStatus.final_reconciled}
        ),
        operation_id="op_force_chapter_one_final",
    )
    chapter_before = storage.load_chapter(project, "ch_0001")
    client = FakeLLMClient([COMPLETE_PROSE])

    with pytest.raises(GuardError) as excinfo:
        writer.regenerate_draft(project, client=client, chapter_id="ch_0001")

    assert excinfo.value.code == "chapter_already_final"
    assert client.calls == []
    assert storage.load_chapter(project, "ch_0001").drafts == chapter_before.drafts


# ---------------------------------------------------------------------------
# 6. Writer context không chứa secret
# ---------------------------------------------------------------------------


def test_writer_prompt_and_bundle_have_no_author_only_secret(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    client = FakeLLMClient([COMPLETE_PROSE])
    writer.generate_draft(project, client=client, chapter_id="ch_0001")

    prompt_input = client.calls[0].messages[1].content
    bundle = build_writer_context(project, chapter_id="ch_0001")
    serialized = json.dumps(bundle.payload, ensure_ascii=False)

    for secret in (POT_SECRET, BODY_SECRET, PURPOSE_SECRET, LATE_LORE):
        assert secret not in prompt_input
        assert secret not in serialized
    for field in ("truth_author_only", "author_only_notes", "future_direction", "planned_payoff"):
        assert field not in prompt_input
        assert field not in serialized
    # `purpose` planner_only của section 2 không được gửi Writer.
    assert "purpose" not in bundle.payload["skeleton"]["sections"][1]


# ---------------------------------------------------------------------------
# 7. Skeleton: mapping ID tạm, provisional, accept/reject/regenerate
# ---------------------------------------------------------------------------


def test_skeleton_maps_temporary_ids_and_refuses_provisional_accept(
    tmp_path: Path, t02_valid_document
) -> None:
    # Chương 1 chưa `final_reconciled` ⇒ basis của chương 2 là provisional.
    project = seed_project(
        tmp_path,
        t02_valid_document,
        chapter_one_status=ChapterStatus.review_required,
        chapter_two_skeleton=False,
    )
    # Bật Auto Accept để chứng minh Skeleton vẫn không bị auto accept.
    project.update_config(auto_accept_structured=True)
    client = FakeLLMClient([json.dumps(SKELETON_CH2_GENERATED, ensure_ascii=False)])

    generated = skeleton.generate(project, client=client, chapter_id="ch_0002")

    envelope = storage.load_artifact(project, "skeleton_ch_0002")
    assert envelope is not None
    assert envelope.status is ArtifactStatus.draft
    assert envelope.accepted_revision is None
    assert envelope.candidate_revision is not None
    assert envelope.candidate_revision.payload_source.id_map == {
        "tmp_section_1": "section_0001",
        "tmp_section_2": "section_0002",
    }
    dumped = json.dumps(envelope.candidate_revision.payload.model_dump(mode="json"), ensure_ascii=False)
    assert "tmp_" not in dumped
    assert generated.data["mode"] == ContextMode.provisional.value

    chapter_two = storage.load_chapter(project, "ch_0002")
    assert chapter_two.preparation_context is not None
    assert chapter_two.preparation_context.context_basis.mode is ContextMode.provisional
    assert chapter_two.status is ChapterStatus.planned
    assert chapter_two.skeleton_pin is None

    # Candidate provisional không được accept.
    with pytest.raises(GuardError) as excinfo:
        skeleton.accept(project, chapter_id="ch_0002")
    assert excinfo.value.code == "provisional_candidate_not_writer_ready"
    assert storage.load_artifact(project, "skeleton_ch_0002").status is ArtifactStatus.draft

    # Regenerate chương 1 giữ accepted cũ và map ID tạm.
    accepted_before = storage.load_artifact(project, "skeleton_ch_0001").accepted_revision
    client.queue(json.dumps(SKELETON_CH1_REGENERATED, ensure_ascii=False))
    skeleton.generate(project, client=client, chapter_id="ch_0001")
    envelope_one = storage.load_artifact(project, "skeleton_ch_0001")
    assert envelope_one.accepted_revision == accepted_before
    assert envelope_one.status is ArtifactStatus.draft
    assert envelope_one.candidate_revision.revision == 2
    assert envelope_one.candidate_revision.payload_source.id_map == {
        "tmp_section_1": "section_0003",
        "tmp_section_2": "section_0004",
    }
    assert "tmp_" not in json.dumps(
        envelope_one.candidate_revision.payload.model_dump(mode="json"), ensure_ascii=False
    )

    accepted = skeleton.accept(project, chapter_id="ch_0001")
    assert accepted.data["status"] == ChapterStatus.skeleton_ready.value
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.skeleton_ready
    payload = skeleton.load_accepted(project, "ch_0001")
    assert payload is not None
    assert [section.section_id for section in payload.sections] == [
        "section_0003",
        "section_0004",
    ]

    # Sau khi chương 1 final, regenerate trên actual rồi accept được.
    storage.save_chapter(
        project,
        storage.load_chapter(project, "ch_0001").model_copy(
            update={"status": ChapterStatus.final_reconciled}
        ),
        operation_id="op_force_chapter_one_final",
    )
    client.queue(json.dumps(SKELETON_CH2_GENERATED, ensure_ascii=False))
    regenerated = skeleton.generate(project, client=client, chapter_id="ch_0002")
    assert regenerated.data["mode"] == ContextMode.actual.value
    assert storage.load_chapter(project, "ch_0002").preparation_context is None

    accepted_two = skeleton.accept(project, chapter_id="ch_0002")
    assert accepted_two.data["status"] == ChapterStatus.skeleton_ready.value
    assert storage.load_chapter(project, "ch_0002").skeleton_pin is not None


def test_skeleton_reject_keeps_accepted_and_generate_does_not_auto_accept(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    accepted_before = storage.load_artifact(project, "skeleton_ch_0001").accepted_revision
    client = FakeLLMClient([json.dumps(SKELETON_CH1_REGENERATED, ensure_ascii=False)])

    generated = skeleton.generate(project, client=client, chapter_id="ch_0001")

    assert generated.validation is not None and generated.validation.is_valid
    envelope = storage.load_artifact(project, "skeleton_ch_0001")
    assert envelope.status is ArtifactStatus.draft
    assert envelope.accepted_revision == accepted_before

    rejected = skeleton.reject(project, chapter_id="ch_0001")

    assert rejected.data["revision"] == 2
    envelope = storage.load_artifact(project, "skeleton_ch_0001")
    assert envelope.candidate_revision is None
    assert envelope.accepted_revision == accepted_before
    assert envelope.status is ArtifactStatus.rejected

    # ID tạm không được đi vào accepted payload.
    assert not any(
        section.section_id.startswith("tmp_") for section in accepted_before.payload.sections
    )
    assert skeleton.assign_section_ids(project, "ch_0001", 2) == [
        "section_0003",
        "section_0004",
    ]


def test_skeleton_edit_candidate_requires_stable_ids_and_can_be_accepted(
    tmp_path: Path, t02_valid_document
) -> None:
    project = seed_project(tmp_path, t02_valid_document)

    # ID tạm không được đi vào bản sửa tay (chỉ output LLM mới được backend map).
    with pytest.raises(ValidationFailure) as excinfo:
        skeleton.edit_candidate(
            project, chapter_id="ch_0001", payload=SKELETON_CH1_REGENERATED
        )
    assert excinfo.value.code == "temporary_id_not_allowed"

    edited = json.loads(json.dumps(SKELETON_CH1_REGENERATED, ensure_ascii=False))
    edited["sections"][0]["section_id"] = "section_0003"
    edited["sections"][1]["section_id"] = "section_0004"
    result = skeleton.edit_candidate(project, chapter_id="ch_0001", payload=edited)

    envelope = storage.load_artifact(project, "skeleton_ch_0001")
    assert envelope is not None and envelope.candidate_revision is not None
    assert envelope.status is ArtifactStatus.draft
    assert envelope.candidate_revision.payload_source.source_type is SourceType.user
    assert result.validation is not None and result.validation.is_valid

    accepted = skeleton.accept(project, chapter_id="ch_0001")
    assert accepted.data["status"] == ChapterStatus.skeleton_ready.value
    payload = skeleton.load_accepted(project, "ch_0001")
    assert payload is not None
    assert [section.section_id for section in payload.sections] == [
        "section_0003",
        "section_0004",
    ]


# ---------------------------------------------------------------------------
# 8. Writer không mutate plan/foundation/timeline/relationship
# ---------------------------------------------------------------------------

def test_writer_does_not_mutate_upstream_files(tmp_path: Path, t02_valid_document) -> None:
    project = seed_project(tmp_path, t02_valid_document)
    tracked = [
        project.paths.project_json,
        project.paths.premise_json,
        project.paths.characters_json,
        project.paths.world_rules_json,
        project.paths.foreshadow_json,
        project.paths.long_plan_json,
        project.paths.short_plan_json,
        project.paths.timeline_json,
        project.paths.relationships_json,
    ]
    before = {str(path): storage.file_fingerprint(path) for path in tracked}
    assert all(value for value in before.values())

    _complete_draft(project)

    after = {str(path): storage.file_fingerprint(path) for path in tracked}
    assert after == before
