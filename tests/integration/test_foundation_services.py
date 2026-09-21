"""Integration test cho T13 — Co-create và Architect services.

Test chạy offline với `FakeLLMClient` và project dựng trong `tmp_path`. Cách seed
project theo mẫu `tests/unit/test_context.py` (không dùng chung module mới, không
sửa `tests/conftest.py`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_ai.core import lifecycle, storage
from novel_ai.core.llm import FakeLLMClient, LLMTimeoutError
from novel_ai.core.models import (
    ArtifactRevision,
    ArtifactStatus,
    ChapterMetadata,
    ChapterStatus,
    DependencyPin,
    FinalRevision,
    PayloadSource,
    SourceType,
    ValidationResult,
    ValidationState,
)
from novel_ai.core.project import Project
from novel_ai.services import (
    ActionResult,
    GuardError,
    LLMUnavailableError,
    StaleDependencyError,
    ValidationFailure,
)
from novel_ai.services import architect, co_create

NOW = "2026-09-19T10:00:00+07:00"


# ---------------------------------------------------------------------------
# Fixture cục bộ
# ---------------------------------------------------------------------------


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def _make_project(tmp_path: Path, *, auto_accept: bool = False) -> Project:
    projects_root = tmp_path / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    project = Project.create(projects_root, "Truyện Test T13")
    if auto_accept:
        project.update_config(auto_accept_structured=True)
    return project


def _idea_state() -> dict[str, Any]:
    return {
        "genre": "xianxia hài nhẹ",
        "tone": "hài khô",
        "protagonist": "Sở Dương",
        "core_concept": "Bác sĩ cấp cứu tỉnh dậy trong thân xác lạ với cái nồi sắt đen.",
        "setting": "Thương Ngô giới",
        "conflict": "Giữ quyền lựa chọn trong thân thể bị cưỡng chế.",
        "constraints": ["Không reveal nguồn gốc cái nồi ở chương 1."],
        "open_questions": ["Vì sao cái nồi ở cạnh anh?"],
    }


def _co_create_response(message: str = "Brief hiện tại.") -> str:
    return json.dumps(
        {"message": message, "idea_state": _idea_state()}, ensure_ascii=False
    )


def _premise_payload(title: str = "Nồi Canh Bên Đường") -> dict[str, Any]:
    return {
        "title": title,
        "logline": "Sở Dương tỉnh dậy ở Thương Ngô giới với cái nồi sắt đen.",
        "dramatic_question": "Anh có giữ được nguyên tắc cứu người không?",
        "themes": ["quyền lựa chọn"],
        "tone_contract": ["Tiếng Việt tự nhiên."],
        "hard_constraints": ["Không reveal nguồn gốc cái nồi trong chương 1."],
        "non_goals": [],
    }


def _character(character_id: str, name: str, effective: int = 1) -> dict[str, Any]:
    return {
        "character_id": character_id,
        "display_name": name,
        "aliases": [],
        "role": "protagonist",
        "tier": "core",
        "effective_from_chapter": effective,
        "status": "accepted",
        "public_profile": {
            "description": "Bác sĩ cấp cứu.",
            "traits": ["điềm tĩnh"],
            "voice": "mỉa mai ngắn",
            "known_history": "",
        },
        "writer_profile": {},
        "author_only": {},
        "future_direction": {},
    }


def _world_rule(rule_id: str, effective: int = 1) -> dict[str, Any]:
    return {
        "world_rule_id": rule_id,
        "category": "hệ thống",
        "summary": "Cưỡng chế thân thể.",
        "content": "Thân thể bị kéo đi theo lệnh thu thập.",
        "boundary": "",
        "effective_from_chapter": effective,
        "visibility": "writer_safe",
        "writer_projection": "Thân thể bị kéo đi.",
        "author_only": {},
    }


def _foreshadow(foreshadow_id: str, effective: int = 1) -> dict[str, Any]:
    return {
        "foreshadow_id": foreshadow_id,
        "label": "Nguồn gốc cái nồi",
        "truth_author_only": "Bí mật chưa reveal.",
        "planned_planting": [],
        "planned_payoff": None,
        "effective_from_chapter": effective,
        "writer_visibility": "skeleton_only",
        "status": "active",
    }


def _accepted_artifact(
    project: Project,
    artifact_id: str,
    artifact_type: str,
    payload: dict[str, Any],
    *,
    revision: int = 1,
) -> None:
    """Seed một artifact accepted trực tiếp (bỏ qua LLM) cho test stale/append."""
    envelope = lifecycle.new_artifact(artifact_type, artifact_id).model_copy(
        update={
            "status": ArtifactStatus.accepted,
            "accepted_revision": ArtifactRevision(
                revision=revision,
                payload=payload,
                payload_source=PayloadSource(source_type=SourceType.user),
                dependency_pins=[],
                validation=_valid(),
                created_at=NOW,
                accepted_at=NOW,
                accepted_by="user",
            ),
        }
    )
    storage.save_artifact(project, envelope, operation_id=f"op_seed_{artifact_id}")


def _accepted_base_idea(project: Project) -> None:
    co_create.finalize_base_idea(project, markdown="Base idea test.\n", now=NOW)


def _accepted_premise(project: Project, *, title: str = "Nồi Canh Bên Đường") -> None:
    client = FakeLLMClient([json.dumps(_premise_payload(title), ensure_ascii=False)])
    architect.generate(
        project, client=client, artifact_type="premise", operation_id="op_premise_gen", now=NOW
    )
    architect.accept(project, artifact_type="premise", operation_id="op_premise_acc", now=NOW)


def _seed_plan_chain(project: Project) -> None:
    """Seed accepted foundation + plan + skeleton để test append/stale.

    Long Plan/Short Plan/Skeleton tham chiếu `char_0001`/`rule_0001`/`fs_0001`
    (foundation có hiệu lực chương 1) nên append entry mới hiệu lực chương 1 phải
    đánh dấu chúng stale.
    """
    _accepted_artifact(
        project,
        "characters",
        "characters",
        {"characters": [_character("char_0001", "Sở Dương")]},
    )
    _accepted_artifact(
        project,
        "world_rules",
        "world_rules",
        {"world_rules": [_world_rule("rule_0001")]},
    )
    _accepted_artifact(
        project,
        "foreshadow",
        "foreshadow",
        {"foreshadows": [_foreshadow("fs_0001")]},
    )
    _accepted_artifact(
        project,
        "long_plan",
        "long_plan",
        {
            "volumes": [
                {
                    "volume_id": "vol_0001",
                    "title": "Quyển 1",
                    "theme": "quyền lựa chọn",
                    "goal": "sống qua đêm đầu",
                    "arcs": [
                        {
                            "arc_id": "arc_0001",
                            "title": "Đêm đầu",
                            "chapter_range": {"start": 1, "end": 2},
                            "goal": "g",
                            "core_conflict": "c",
                            "start_state": "s",
                            "end_state": "e",
                            "major_reveals": [],
                            "character_ids": ["char_0001"],
                            "world_rule_ids": ["rule_0001"],
                            "foreshadow_ids": ["fs_0001"],
                            "relationship_directions": [],
                        }
                    ],
                }
            ],
            "global_threads": [],
        },
    )
    _accepted_artifact(
        project,
        "short_plan",
        "short_plan",
        {
            "arc_id": "arc_0001",
            "chapters": [
                _short_chapter("ch_0001", 1),
                _short_chapter("ch_0002", 2),
            ],
        },
    )
    chapter_one = _chapter_metadata(project, "ch_0001", 1, status=ChapterStatus.final_reconciled)
    storage.save_chapter(
        project,
        chapter_one.model_copy(
            update={
                "final_revision": FinalRevision(
                    revision=1,
                    source_prose_revision=1,
                    markdown_ref="chapters/ch_0001/final.md",
                    reconciled_at=NOW,
                )
            }
        ),
        operation_id="op_seed_ch_0001",
    )
    storage.save_chapter(
        project, _chapter_metadata(project, "ch_0002", 2), operation_id="op_seed_ch_0002"
    )
    storage.write_markdown(
        project,
        "chapters/ch_0001/final.md",
        "Final prose chương 1.\n",
        operation_id="op_seed_final_prose",
    )
    _accepted_artifact(
        project,
        "skeleton_ch_0001",
        "skeleton",
        _skeleton_payload("ch_0001", 1),
    )
    _accepted_artifact(
        project,
        "skeleton_ch_0002",
        "skeleton",
        _skeleton_payload("ch_0002", 2),
    )


def _short_chapter(chapter_id: str, number: int) -> dict[str, Any]:
    return {
        "chapter_id": chapter_id,
        "chapter_number": number,
        "title": f"Chương {number}",
        "summary": f"Tóm tắt {number}",
        "hook": "",
        "outline": [{"language": "vi", "pov": "ngôi ba", "length_guidance": "1500 từ"}],
        "character_ids": ["char_0001"],
        "world_rule_ids": ["rule_0001"],
        "foreshadow_ids": ["fs_0001"],
        "threads": [],
        "relationship_changes": [],
        "chapter_goal": "Mục tiêu",
        "planned_ending": "Kết",
    }


def _chapter_metadata(
    project: Project,
    chapter_id: str,
    number: int,
    *,
    status: ChapterStatus = ChapterStatus.planned,
) -> ChapterMetadata:
    return ChapterMetadata(
        chapter_id=chapter_id,
        chapter_number=number,
        title=f"Chương {number}",
        status=status,
        previous_chapter_id=None if number <= 1 else "ch_0001",
        short_plan_pin=DependencyPin(
            artifact_id="short_plan",
            revision=1,
            scope="short_plan",
            chapter_id=chapter_id,
        ),
    )


def _skeleton_payload(chapter_id: str, number: int) -> dict[str, Any]:
    return {
        "chapter_id": chapter_id,
        "chapter_number": number,
        "global_constraints": [],
        "sections": [
            {
                "section_id": "section_0001",
                "index": 1,
                "type": "action",
                "instruction": "Sở Dương thoát khỏi vách đá.",
                "purpose": "Nối hệ quả.",
                "purpose_visibility": "writer_safe",
                "character_ids": ["char_0001"],
                "world_rule_ids": ["rule_0001"],
                "foreshadow_ids": ["fs_0001"],
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. Co-create turn → Finalize Base Idea
# ---------------------------------------------------------------------------


def test_co_create_turn_then_finalize_base_idea(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    client = FakeLLMClient([_co_create_response("Đây là brief đầy đủ.")])

    turn = co_create.run_turn(
        project, client=client, user_message="Tôi muốn truyện tu tiên nhẹ.", now=NOW
    )

    assert turn.data["status"] == "working"
    document = storage.load_co_create(project)
    assert document is not None
    assert document.status.value == "working"
    assert [message.role for message in document.messages] == ["user", "assistant"]
    assert document.idea_state is not None and document.idea_state.genre.startswith("xianxia")
    assert document.base_idea is None

    result = co_create.finalize_base_idea(project, now=NOW)

    assert result.data["status"] == "accepted"
    reloaded = storage.load_co_create(project)
    assert reloaded is not None and reloaded.status.value == "finalized"
    assert reloaded.base_idea is not None
    assert reloaded.base_idea.status is ArtifactStatus.accepted
    assert project.paths.base_idea_md.is_file()
    assert project.paths.base_idea_meta_json.is_file()
    assert reloaded.idea_state is not None and reloaded.idea_state.genre.startswith("xianxia")


def test_run_turn_blocked_after_finalize_until_explicit_reopen(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    co_create.run_turn(
        project, client=FakeLLMClient([_co_create_response()]), user_message="Mở đầu.", now=NOW
    )
    co_create.finalize_base_idea(project, now=NOW)

    blocked_client = FakeLLMClient([_co_create_response()])
    with pytest.raises(GuardError) as info:
        co_create.run_turn(project, client=blocked_client, user_message="Thêm ý.", now=NOW)
    assert info.value.code == "co_create_finalized"
    assert blocked_client.calls == []

    reopened = co_create.set_working_state(project, idea_state=_idea_state(), now=NOW)
    assert reopened.data["status"] == "working"
    assert reopened.data["base_idea_status"] == "accepted"

    after = co_create.run_turn(
        project, client=FakeLLMClient([_co_create_response("Brief mới.")]), user_message="Thêm ý.", now=NOW
    )
    assert after.data["status"] == "working"
    # Canon Base Idea không đổi khi chỉ reopen working state.
    assert storage.load_co_create(project).base_idea.revision == 1


def test_finalize_base_idea_requires_minimum_payload(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    with pytest.raises(GuardError) as info:
        co_create.finalize_base_idea(project, now=NOW)
    assert info.value.code == "base_idea_incomplete"
    assert storage.load_co_create(project).status.value == "working"


# ---------------------------------------------------------------------------
# 2. Generate → accept, Auto Accept on/off
# ---------------------------------------------------------------------------


def test_generate_and_accept_premise_round_trip(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    client = FakeLLMClient([json.dumps(_premise_payload(), ensure_ascii=False)])

    generated = architect.generate(project, client=client, artifact_type="premise", now=NOW)

    assert generated.data["status"] == "draft"
    envelope = storage.load_artifact(project, "premise")
    assert envelope is not None and envelope.candidate_revision is not None
    assert envelope.accepted_revision is None

    accepted = architect.accept(project, artifact_type="premise", now=NOW)

    assert accepted.data["status"] == "accepted"
    reloaded = storage.load_artifact(project, "premise")
    assert reloaded is not None
    assert reloaded.status is ArtifactStatus.accepted
    assert reloaded.accepted_revision is not None
    assert reloaded.accepted_revision.payload.title == "Nồi Canh Bên Đường"
    assert reloaded.candidate_revision is None
    assert len(client.calls) == 1


def test_auto_accept_structured_on_and_off(tmp_path: Path) -> None:
    enabled = _make_project(tmp_path / "on", auto_accept=True)
    _accepted_base_idea(enabled)
    architect.generate(
        enabled,
        client=FakeLLMClient([json.dumps(_premise_payload(), ensure_ascii=False)]),
        artifact_type="premise",
        now=NOW,
    )
    enabled_envelope = storage.load_artifact(enabled, "premise")
    assert enabled_envelope.status is ArtifactStatus.accepted
    assert enabled_envelope.accepted_revision.accepted_by == "auto_accept"

    disabled = _make_project(tmp_path / "off")
    _accepted_base_idea(disabled)
    assert architect.auto_accept_enabled(disabled) is False
    architect.generate(
        disabled,
        client=FakeLLMClient([json.dumps(_premise_payload(), ensure_ascii=False)]),
        artifact_type="premise",
        now=NOW,
    )
    disabled_envelope = storage.load_artifact(disabled, "premise")
    assert disabled_envelope.status is ArtifactStatus.draft
    assert disabled_envelope.accepted_revision is None


# ---------------------------------------------------------------------------
# 3. Regenerate giữ accepted
# ---------------------------------------------------------------------------


def test_regenerate_keeps_accepted_and_adds_candidate(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    _accepted_premise(project)
    before = storage.load_artifact(project, "premise")
    assert before is not None and before.accepted_revision is not None

    architect.generate(
        project,
        client=FakeLLMClient([json.dumps(_premise_payload("Tên mới"), ensure_ascii=False)]),
        artifact_type="premise",
        action="regenerate",
        operation_id="op_regen",
        now=NOW,
    )

    after = storage.load_artifact(project, "premise")
    assert after is not None
    assert after.status is ArtifactStatus.draft
    assert after.accepted_revision is not None
    assert after.accepted_revision.revision == before.accepted_revision.revision
    assert after.accepted_revision.payload.title == "Nồi Canh Bên Đường"
    assert after.candidate_revision is not None
    assert after.candidate_revision.payload.title == "Tên mới"


# ---------------------------------------------------------------------------
# 4 + 5. Lỗi LLM / schema: accepted không đổi, raw + error được lưu
# ---------------------------------------------------------------------------


def test_invalid_json_keeps_accepted_and_records_raw_and_error(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    _accepted_premise(project)
    client = FakeLLMClient(["đây không phải JSON"])

    with pytest.raises(ValidationFailure) as info:
        architect.generate(
            project,
            client=client,
            artifact_type="premise",
            action="regenerate",
            operation_id="op_bad_json",
            now=NOW,
        )

    assert info.value.result is not None and info.value.result.errors
    envelope = storage.load_artifact(project, "premise")
    assert envelope.status is ArtifactStatus.accepted
    assert envelope.accepted_revision.revision == 1
    assert envelope.accepted_revision.payload.title == "Nồi Canh Bên Đường"
    assert envelope.candidate_revision is None

    raw_files = list(project.paths.raw_dir.rglob("*_premise.txt"))
    assert raw_files and "đây không phải JSON" in raw_files[0].read_text(encoding="utf-8")
    error_files = list(project.paths.raw_dir.rglob("*_premise.error.json"))
    assert error_files
    error_payload = json.loads(error_files[0].read_text(encoding="utf-8"))
    assert error_payload["errors"]
    assert error_payload["raw_output_ref"].endswith("_premise.txt")


def test_llm_timeout_keeps_accepted_and_records_error(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    _accepted_premise(project)
    client = FakeLLMClient([LLMTimeoutError("timeout giả lập")])

    with pytest.raises(LLMUnavailableError) as info:
        architect.generate(
            project,
            client=client,
            artifact_type="premise",
            action="regenerate",
            operation_id="op_timeout",
            now=NOW,
        )

    assert info.value.code == "llm_timeout"
    assert info.value.details["error_ref"].endswith("_premise.llm.error.json")
    envelope = storage.load_artifact(project, "premise")
    assert envelope.status is ArtifactStatus.accepted
    assert envelope.candidate_revision is None
    error_files = list(project.paths.raw_dir.rglob("*_premise.llm.error.json"))
    assert error_files
    assert json.loads(error_files[0].read_text(encoding="utf-8"))["code"] == "llm_timeout"


# ---------------------------------------------------------------------------
# 6. Accept lặp
# ---------------------------------------------------------------------------


def test_repeated_accept_with_same_operation_id_does_not_add_revision(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    architect.generate(
        project,
        client=FakeLLMClient([json.dumps(_premise_payload(), ensure_ascii=False)]),
        artifact_type="premise",
        operation_id="op_gen_once",
        now=NOW,
    )

    first = architect.accept(
        project, artifact_type="premise", operation_id="op_accept_once", now=NOW
    )
    second = architect.accept(
        project, artifact_type="premise", operation_id="op_accept_once", now=NOW
    )

    assert first.data["revision"] == 1
    assert second.data["idempotent"] is True
    envelope = storage.load_artifact(project, "premise")
    assert envelope.accepted_revision.revision == 1
    assert envelope.candidate_revision is None

    with pytest.raises(GuardError):
        architect.accept(project, artifact_type="premise", now=NOW)


# ---------------------------------------------------------------------------
# 7. Candidate từ revision cũ
# ---------------------------------------------------------------------------


def test_accept_rejects_candidate_created_from_old_premise_revision(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    _accepted_premise(project)
    pool = architect.assign_ids(project, "characters", 1)
    architect.generate(
        project,
        client=FakeLLMClient(
            [json.dumps({"characters": [_character(pool[0], "Sở Dương")]}, ensure_ascii=False)]
        ),
        artifact_type="characters",
        assigned_ids=pool,
        operation_id="op_char_gen",
        now=NOW,
    )

    # Accepted Premise đổi revision sau khi candidate Characters đã tạo.
    architect.generate(
        project,
        client=FakeLLMClient([json.dumps(_premise_payload("Bản 2"), ensure_ascii=False)]),
        artifact_type="premise",
        action="regenerate",
        operation_id="op_premise_regen",
        now=NOW,
    )
    architect.accept(project, artifact_type="premise", operation_id="op_premise_acc2", now=NOW)
    assert storage.load_artifact(project, "premise").accepted_revision.revision == 2

    with pytest.raises(StaleDependencyError):
        architect.accept(project, artifact_type="characters", operation_id="op_char_acc", now=NOW)

    envelope = storage.load_artifact(project, "characters")
    assert envelope.accepted_revision is None
    assert envelope.candidate_revision is not None


# ---------------------------------------------------------------------------
# 8. Append với effective_from_chapter
# ---------------------------------------------------------------------------


def _envelope_status(project: Project, artifact_id: str) -> ArtifactStatus:
    envelope = storage.load_artifact(project, artifact_id)
    assert envelope is not None
    return envelope.status


def test_append_future_entry_does_not_stale_past(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    _seed_plan_chain(project)

    for artifact_type, payload in (
        ("characters", {"characters": [_character("char_0002", "Người tương lai", 100)]}),
        ("world_rules", {"world_rules": [_world_rule("rule_0002", 100)]}),
        ("foreshadow", {"foreshadows": [_foreshadow("fs_0002", 100)]}),
    ):
        result = architect.append_entries(
            project,
            artifact_type=artifact_type,
            payload=payload,
            effective_from_chapter=100,
            operation_id=f"op_append_future_{artifact_type}",
            now=NOW,
        )
        assert result.data["stale_marked"] == []

    assert _envelope_status(project, "long_plan") is ArtifactStatus.accepted
    assert _envelope_status(project, "short_plan") is ArtifactStatus.accepted
    assert _envelope_status(project, "skeleton_ch_0002") is ArtifactStatus.accepted
    assert (
        storage.read_text(project.root / "chapters/ch_0001/final.md")
        == "Final prose chương 1.\n"
    )
    stored = storage.load_artifact(project, "characters")
    assert stored.accepted_revision.payload.characters[1].effective_from_chapter == 100


def test_append_current_entry_marks_downstream_stale_only(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    _seed_plan_chain(project)
    before_revision = storage.load_artifact(project, "short_plan").accepted_revision.revision

    result = architect.append_entries(
        project,
        artifact_type="characters",
        payload={"characters": [_character("char_0003", "Người hiện tại", 1)]},
        effective_from_chapter=1,
        operation_id="op_append_now",
        now=NOW,
    )

    assert set(result.data["stale_marked"]) >= {"long_plan", "short_plan", "skeleton_ch_0002"}
    assert _envelope_status(project, "long_plan") is ArtifactStatus.stale
    assert _envelope_status(project, "short_plan") is ArtifactStatus.stale
    assert _envelope_status(project, "skeleton_ch_0002") is ArtifactStatus.stale
    # Chỉ đánh dấu stale: final prose, chapter metadata và state không bị rewrite.
    assert (
        storage.read_text(project.root / "chapters/ch_0001/final.md")
        == "Final prose chương 1.\n"
    )
    assert storage.load_chapter(project, "ch_0001").status is ChapterStatus.final_reconciled
    assert storage.load_timeline(project).entries == []
    assert storage.load_relationships(project).relationships == []
    # Chỉ đánh dấu stale, không rewrite nội dung accepted.
    plan = storage.load_artifact(project, "short_plan")
    assert plan.accepted_revision.revision == before_revision
    assert plan.accepted_revision.payload.chapters[0].summary == "Tóm tắt 1"


def test_append_rejects_duplicate_entry_id(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    _seed_plan_chain(project)

    with pytest.raises(ValidationFailure):
        architect.append_entries(
            project,
            artifact_type="characters",
            payload={"characters": [_character("char_0001", "Trùng ID")]},
            effective_from_chapter=2,
            operation_id="op_append_dup",
            now=NOW,
        )


# ---------------------------------------------------------------------------
# 13. Guard trước LLM
# ---------------------------------------------------------------------------


def test_generate_premise_without_base_idea_blocks_before_llm(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    client = FakeLLMClient([json.dumps(_premise_payload(), ensure_ascii=False)])

    with pytest.raises(GuardError) as info:
        architect.generate(project, client=client, artifact_type="premise", now=NOW)

    assert info.value.code == "base_idea_missing"
    assert client.calls == []
    assert storage.load_artifact(project, "premise") is None


def test_generate_characters_requires_premise_before_llm(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    client = FakeLLMClient([json.dumps({"characters": []}, ensure_ascii=False)])

    with pytest.raises(GuardError):
        architect.generate(project, client=client, artifact_type="characters", now=NOW)

    assert client.calls == []


def test_assign_ids_avoids_accepted_and_candidate_ids(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    _accepted_premise(project)
    pool = architect.assign_ids(project, "characters", 2)
    architect.generate(
        project,
        client=FakeLLMClient(
            [json.dumps({"characters": [_character(pool[0], "A")]}, ensure_ascii=False)]
        ),
        artifact_type="characters",
        assigned_ids=[pool[0]],
        operation_id="op_char_candidate",
        now=NOW,
    )

    next_pool = architect.assign_ids(project, "characters", 2)

    assert pool == ["char_0001", "char_0002"]
    assert next_pool == ["char_0002", "char_0003"]


def test_edit_candidate_then_reject_keeps_accepted(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    _accepted_premise(project)

    edited = architect.edit_candidate(
        project,
        artifact_type="premise",
        payload=_premise_payload("Bản user sửa"),
        operation_id="op_edit",
        now=NOW,
    )
    assert edited.data["status"] == "draft"
    assert storage.load_artifact(project, "premise").accepted_revision.payload.title == (
        "Nồi Canh Bên Đường"
    )

    architect.reject(project, artifact_type="premise", operation_id="op_reject", now=NOW)

    envelope = storage.load_artifact(project, "premise")
    assert envelope.candidate_revision is None
    # Lifecycle T09: reject candidate khi đã có accepted → status `rejected`, nội
    # dung accepted cũ vẫn giữ nguyên (không xóa dữ liệu).
    assert envelope.status is ArtifactStatus.rejected
    assert envelope.accepted_revision.payload.title == "Nồi Canh Bên Đường"


def test_action_result_dataclass_is_returned(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _accepted_base_idea(project)
    result = architect.generate(
        project,
        client=FakeLLMClient([json.dumps(_premise_payload(), ensure_ascii=False)]),
        artifact_type="premise",
        operation_id="op_result",
        now=NOW,
    )
    assert isinstance(result, ActionResult)
    assert result.operation_id == "op_result"
    assert result.artifact_id == "premise"
    assert result.validation is not None and result.validation.is_valid
    assert result.data["raw_output_ref"].startswith("raw/")
