"""Integration test cho T14 — Long Plan, Short Plan và Rolling Plan services.

Test chạy offline với `FakeLLMClient`, project dựng trong `tmp_path`. Foundation
được seed accepted trực tiếp qua `storage`/`lifecycle` (mẫu của
`tests/unit/test_context.py`) để test tập trung vào planner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.llm import FakeLLMClient
from novel_ai.core.models import (
    ArtifactRevision,
    ArtifactStatus,
    ChapterStatus,
    CurrentTimelineDocument,
    FinalRevision,
    PayloadSource,
    ProseRevision,
    SourceType,
    ValidationResult,
    ValidationState,
)
from novel_ai.core.project import Project
from novel_ai.services import GuardError, ValidationFailure
from novel_ai.services import co_create, long_planner, short_planner

NOW = "2026-09-19T10:00:00+07:00"


# ---------------------------------------------------------------------------
# Fixture cục bộ
# ---------------------------------------------------------------------------


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def _make_project(
    tmp_path: Path,
    *,
    auto_accept: bool = False,
    allow_relationship_replan: bool = True,
) -> Project:
    projects_root = tmp_path / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    project = Project.create(projects_root, "Truyện Test T14")
    changes: dict[str, Any] = {"allow_relationship_replan": allow_relationship_replan}
    if auto_accept:
        changes["auto_accept_structured"] = True
    project.update_config(**changes)
    co_create.finalize_base_idea(project, markdown="Base idea test T14.\n", now=NOW)
    return project


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
            "description": "Nhân vật test.",
            "traits": [],
            "voice": "",
            "known_history": "",
        },
        "writer_profile": {},
        "author_only": {},
        "future_direction": {},
    }


def _accepted_artifact(
    project: Project,
    artifact_id: str,
    artifact_type: str,
    payload: dict[str, Any],
    *,
    revision: int = 1,
) -> None:
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


def _seed_foundation(project: Project) -> None:
    _accepted_artifact(
        project,
        "premise",
        "premise",
        {
            "title": "Nồi Canh Bên Đường",
            "logline": "L",
            "dramatic_question": "Q",
            "themes": [],
            "tone_contract": [],
            "hard_constraints": [],
            "non_goals": [],
        },
    )
    _accepted_artifact(
        project,
        "characters",
        "characters",
        {"characters": [_character("char_0001", "Sở Dương"), _character("char_0002", "Tiêu")]},
    )
    _accepted_artifact(project, "world_rules", "world_rules", {"world_rules": []})
    _accepted_artifact(project, "foreshadow", "foreshadow", {"foreshadows": []})


def _long_plan_payload(
    *, arc_range: tuple[int, int] = (1, 2), arc_id: str = "arc_0001"
) -> dict[str, Any]:
    return {
        "volumes": [
            {
                "volume_id": "vol_0001",
                "title": "Quyển 1",
                "theme": "quyền lựa chọn",
                "goal": "sống qua đêm đầu",
                "arcs": [
                    {
                        "arc_id": arc_id,
                        "title": "Đêm đầu",
                        "chapter_range": {"start": arc_range[0], "end": arc_range[1]},
                        "goal": "g",
                        "core_conflict": "c",
                        "start_state": "s",
                        "end_state": "e",
                        "major_reveals": [],
                        "character_ids": ["char_0001", "char_0002"],
                        "world_rule_ids": [],
                        "foreshadow_ids": [],
                        "relationship_directions": [],
                    }
                ],
            }
        ],
        "global_threads": [],
    }


def _chapter(
    chapter_id: str,
    number: int,
    *,
    summary: str | None = None,
    with_relationship: bool = False,
) -> dict[str, Any]:
    return {
        "chapter_id": chapter_id,
        "chapter_number": number,
        "title": f"Chương {number}",
        "summary": summary or f"Tóm tắt {number}",
        "hook": "",
        "outline": [
            {"language": "vi", "pov": "ngôi ba giới hạn", "length_guidance": "1500 từ"},
            f"Beat {number}",
        ],
        "character_ids": ["char_0001", "char_0002"],
        "world_rule_ids": [],
        "foreshadow_ids": [],
        "threads": [],
        "relationship_changes": (
            [
                {
                    "character_ids": ["char_0001", "char_0002"],
                    "arc_direction": "chưa gặp → dò xét",
                    "target_state": "dè chừng",
                    "notes": "",
                }
            ]
            if with_relationship
            else []
        ),
        "chapter_goal": f"Mục tiêu {number}",
        "planned_ending": f"Kết {number}",
    }


def _accept_long_plan(
    project: Project, *, arc_range: tuple[int, int] = (1, 2), arc_id: str = "arc_0001"
) -> None:
    client = FakeLLMClient(
        [
            json.dumps(
                _long_plan_payload(arc_range=arc_range, arc_id=arc_id), ensure_ascii=False
            )
        ]
    )
    long_planner.generate(
        project, client=client, operation_id="op_long_gen", now=NOW
    )
    envelope = storage.load_artifact(project, "long_plan")
    if envelope is not None and envelope.accepted_revision is None:
        long_planner.accept(project, operation_id="op_long_acc", now=NOW)


def _arc(project: Project):
    """`ArcPlan` đầu tiên của Long Plan accepted (helper test)."""
    from novel_ai.core.models import LongPlanPayload

    envelope = storage.load_artifact(project, "long_plan")
    assert envelope is not None and envelope.accepted_revision is not None
    payload = envelope.accepted_revision.payload
    if not isinstance(payload, LongPlanPayload):
        payload = LongPlanPayload.model_validate(payload.model_dump(mode="json"))
    return payload.volumes[0].arcs[0]


def _accept_short_plan(
    project: Project, *, with_relationship_on_second: bool = True
) -> list[dict[str, Any]]:
    assigned = short_planner.assign_chapter_ids(project, 2)
    payload = {
        "arc_id": "arc_0001",
        "chapters": [
            _chapter(assigned[0]["chapter_id"], assigned[0]["chapter_number"]),
            _chapter(
                assigned[1]["chapter_id"],
                assigned[1]["chapter_number"],
                with_relationship=with_relationship_on_second,
            ),
        ],
    }
    client = FakeLLMClient([json.dumps(payload, ensure_ascii=False)])
    short_planner.generate(
        project,
        client=client,
        arc_id="arc_0001",
        assigned_chapters=assigned,
        operation_id="op_short_gen",
        now=NOW,
    )
    envelope = storage.load_artifact(project, "short_plan")
    if envelope is not None and envelope.accepted_revision is None:
        short_planner.accept(project, operation_id="op_short_acc", now=NOW)
    return assigned


def _seed_skeleton(project: Project, chapter_id: str, number: int) -> None:
    _accepted_artifact(
        project,
        f"skeleton_{chapter_id}",
        "skeleton",
        {
            "chapter_id": chapter_id,
            "chapter_number": number,
            "global_constraints": [],
            "sections": [
                {
                    "section_id": "section_0001",
                    "index": 1,
                    "type": "action",
                    "instruction": "Hành động test.",
                    "purpose": "Nối hệ quả.",
                    "purpose_visibility": "writer_safe",
                    "character_ids": ["char_0001"],
                }
            ],
        },
    )


def _mark_chapter_final(project: Project, chapter_id: str, number: int) -> None:
    chapter = storage.load_chapter(project, chapter_id)
    assert chapter is not None
    relpath = f"chapters/{chapter_id}/final.md"
    storage.write_markdown(project, relpath, f"Final prose chương {number}.\n", operation_id=f"op_final_{chapter_id}")
    storage.save_chapter(
        project,
        chapter.model_copy(
            update={
                "status": ChapterStatus.final_reconciled,
                "final_revision": FinalRevision(
                    revision=1,
                    source_prose_revision=1,
                    markdown_ref=relpath,
                    reconciled_at=NOW,
                ),
            }
        ),
        operation_id=f"op_final_meta_{chapter_id}",
    )
    storage.save_timeline(
        project,
        CurrentTimelineDocument(
            latest_final_chapter=number, latest_consistent_chapter=number
        ),
        operation_id=f"op_final_timeline_{chapter_id}",
    )


def _rolling_patch(
    chapter_id: str,
    *,
    changes: dict[str, Any] | None = None,
    relationship_changes: list[dict[str, Any]] | None = None,
    reviewed_end: int = 1,
) -> dict[str, Any]:
    patch: dict[str, Any] = {
        "status": "adjust",
        "reviewed_chapter_range": {"start": 1, "end": reviewed_end},
        "deviations": [
            {
                "chapter_id": "ch_0001",
                "planned": "Kết ở vách đá.",
                "actual": "Kết trên sống núi.",
                "evidence": "Timeline ch_0001.",
            }
        ],
        "short_plan_changes": [],
        "relationship_plan_changes": [],
        "blocked_by_authority": [],
    }
    if changes is not None:
        patch["short_plan_changes"].append(
            {
                "chapter_id": chapter_id,
                "changes": changes,
                "reason": "Nối actual với future intent.",
            }
        )
    if relationship_changes is not None:
        patch["relationship_plan_changes"].append(
            {
                "chapter_id": chapter_id,
                "relationship_changes": relationship_changes,
                "reason": "Chỉnh nhịp quan hệ theo actual.",
            }
        )
    return patch


def _prepare_rolling_project(
    tmp_path: Path, *, allow_relationship_replan: bool = True
) -> tuple[Project, list[dict[str, Any]]]:
    project = _make_project(tmp_path, allow_relationship_replan=allow_relationship_replan)
    _seed_foundation(project)
    _accept_long_plan(project)
    assigned = _accept_short_plan(project)
    _seed_skeleton(project, assigned[1]["chapter_id"], 2)
    _mark_chapter_final(project, assigned[0]["chapter_id"], 1)
    return project, assigned


# ---------------------------------------------------------------------------
# 9. Long Plan → Short Plan → ChapterMetadata
# ---------------------------------------------------------------------------


def test_long_plan_then_short_plan_creates_chapter_metadata(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _seed_foundation(project)

    long_result = long_planner.generate(
        project,
        client=FakeLLMClient([json.dumps(_long_plan_payload(), ensure_ascii=False)]),
        operation_id="op_long_gen",
        now=NOW,
    )
    assert long_result.data["status"] == "draft"
    assert long_result.data["planning_scope"] == {"start": 1, "end": 3}
    long_planner.accept(project, operation_id="op_long_acc", now=NOW)
    accepted_long = storage.load_artifact(project, "long_plan")
    assert accepted_long.status is ArtifactStatus.accepted
    assert accepted_long.accepted_revision.payload.volumes[0].arcs[0].arc_id == "arc_0001"

    assigned = short_planner.assign_chapter_ids(project, 2)
    assert assigned == [
        {"chapter_id": "ch_0001", "chapter_number": 1},
        {"chapter_id": "ch_0002", "chapter_number": 2},
    ]
    payload = {
        "arc_id": "arc_0001",
        "chapters": [
            _chapter("ch_0001", 1),
            _chapter("ch_0002", 2, with_relationship=True),
        ],
    }
    short_planner.generate(
        project,
        client=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
        arc_id="arc_0001",
        assigned_chapters=assigned,
        operation_id="op_short_gen",
        now=NOW,
    )
    short_planner.accept(project, operation_id="op_short_acc", now=NOW)

    accepted_short = storage.load_artifact(project, "short_plan")
    assert accepted_short.status is ArtifactStatus.accepted
    assert accepted_short.accepted_revision.revision == 1
    first = storage.load_chapter(project, "ch_0001")
    second = storage.load_chapter(project, "ch_0002")
    assert first.chapter_number == 1
    assert first.previous_chapter_id is None
    assert first.status is ChapterStatus.planned
    assert first.short_plan_pin.artifact_id == "short_plan"
    assert first.short_plan_pin.revision == 1
    assert first.short_plan_pin.scope == "short_plan"
    assert first.short_plan_pin.chapter_id == "ch_0001"
    assert second.chapter_number == 2
    assert second.previous_chapter_id == "ch_0001"
    assert second.short_plan_pin.chapter_id == "ch_0002"


def test_long_plan_regenerate_keeps_accepted(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _seed_foundation(project)
    _accept_long_plan(project)
    before = storage.load_artifact(project, "long_plan")
    assert before.accepted_revision.payload.volumes[0].arcs[0].arc_id == "arc_0001"

    long_planner.generate(
        project,
        client=FakeLLMClient(
            [
                json.dumps(
                    _long_plan_payload(arc_range=(1, 3), arc_id="arc_0002"),
                    ensure_ascii=False,
                )
            ]
        ),
        action="regenerate",
        operation_id="op_long_regen",
        now=NOW,
    )

    after = storage.load_artifact(project, "long_plan")
    assert after.status is ArtifactStatus.draft
    assert after.accepted_revision.revision == 1
    assert after.accepted_revision.payload.volumes[0].arcs[0].arc_id == "arc_0001"
    assert after.candidate_revision.payload.volumes[0].arcs[0].arc_id == "arc_0002"


def test_short_plan_generate_defaults_assigned_chapters_from_arc(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _seed_foundation(project)
    _accept_long_plan(project, arc_range=(1, 2))

    result = short_planner.generate(
        project,
        client=FakeLLMClient(
            [
                json.dumps(
                    {
                        "arc_id": "arc_0001",
                        "chapters": [_chapter("ch_0001", 1), _chapter("ch_0002", 2)],
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        arc_id="arc_0001",
        operation_id="op_short_default_gen",
        now=NOW,
    )

    assert result.data["assigned_chapters"] == [
        {"chapter_id": "ch_0001", "chapter_number": 1},
        {"chapter_id": "ch_0002", "chapter_number": 2},
    ]


# ---------------------------------------------------------------------------
# 10. Short Plan tham chiếu arc không có trong Long Plan
# ---------------------------------------------------------------------------


def test_short_plan_accept_rejects_arc_not_in_long_plan(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _seed_foundation(project)
    _accept_long_plan(project)

    payload = {
        "arc_id": "arc_9999",
        "chapters": [_chapter("ch_0001", 1), _chapter("ch_0002", 2)],
    }
    envelope = lifecycle.new_artifact("short_plan", "short_plan")
    envelope = lifecycle.set_candidate(
        envelope,
        payload,
        source=PayloadSource(source_type=SourceType.llm, operation_id="op_bad_short"),
        dependency_pins=[],
        validation=_valid(),
        now=NOW,
    )
    storage.save_artifact(project, envelope, operation_id="op_bad_short")

    with pytest.raises(GuardError) as info:
        short_planner.accept(project, operation_id="op_bad_short_acc", now=NOW)

    assert info.value.code == "unknown_arc"
    # Validator độc lập cũng từ chối arc không resolve trong Long Plan accepted.
    index = validation.build_reference_index(
        long_plan_arcs=[
            arc
            for volume in storage.load_artifact(project, "long_plan").accepted_revision.payload.volumes
            for arc in volume.arcs
        ]
    )
    result = validation.validate_artifact_payload(
        "short_plan", payload, context=validation.ValidationContext(index=index)
    )
    assert not result.is_valid
    assert any(issue.code == "unknown_reference" for issue in result.errors)
    # Accepted Short Plan chưa được tạo.
    stored = storage.load_artifact(project, "short_plan")
    assert stored is not None and stored.accepted_revision is None


# ---------------------------------------------------------------------------
# 11. Rolling: eligible, apply, chapter final
# ---------------------------------------------------------------------------


def test_eligible_chapters_for_rolling_excludes_final(tmp_path: Path) -> None:
    project, assigned = _prepare_rolling_project(tmp_path)

    eligible = short_planner.eligible_chapters_for_rolling(project)

    assert eligible == [{"chapter_id": assigned[1]["chapter_id"], "chapter_number": 2}]

    _mark_chapter_final(project, assigned[1]["chapter_id"], 2)
    assert short_planner.eligible_chapters_for_rolling(project) == []


def test_rolling_accept_updates_short_plan_and_marks_stale(tmp_path: Path) -> None:
    project, assigned = _prepare_rolling_project(tmp_path)
    second = assigned[1]["chapter_id"]

    generated = short_planner.generate_rolling(
        project,
        client=FakeLLMClient(
            [
                json.dumps(
                    _rolling_patch(second, changes={"summary": "Tóm tắt nối actual."}),
                    ensure_ascii=False,
                )
            ]
        ),
        operation_id="op_roll_gen",
        now=NOW,
    )

    patch_envelope = storage.load_artifact(project, "rolling_patch_arc_0001")
    assert patch_envelope.status is ArtifactStatus.draft
    assert patch_envelope.candidate_revision is not None
    assert generated.data["proposal_status"] == "adjust"
    assert generated.data["eligible_chapters"] == [
        {"chapter_id": second, "chapter_number": 2}
    ]
    # Proposal chưa được apply: Short Plan accepted giữ nguyên.
    assert (
        storage.load_artifact(project, "short_plan").accepted_revision.revision == 1
    )

    applied = short_planner.accept_rolling(project, operation_id="op_roll_acc", now=NOW)

    assert applied.data["revision"] == 2
    assert applied.data["changed_chapters"] == [second]
    plan = storage.load_artifact(project, "short_plan")
    assert plan.status is ArtifactStatus.accepted
    chapters = {chapter.chapter_id: chapter for chapter in plan.accepted_revision.payload.chapters}
    assert chapters[assigned[0]["chapter_id"]].summary == "Tóm tắt 1"
    assert chapters[second].summary == "Tóm tắt nối actual."
    # Chapter bị ảnh hưởng → skeleton liên quan stale; chapter final không đổi.
    assert (
        storage.load_artifact(project, f"skeleton_{second}").status is ArtifactStatus.stale
    )
    assert storage.load_chapter(project, assigned[0]["chapter_id"]).status is (
        ChapterStatus.final_reconciled
    )
    # Rolling proposal trở thành dấu vết accepted và không còn candidate.
    applied_patch = storage.load_artifact(project, "rolling_patch_arc_0001")
    assert applied_patch.status is ArtifactStatus.accepted
    assert applied_patch.candidate_revision is None


def test_rolling_proposal_targeting_final_or_unknown_chapter_rejected(tmp_path: Path) -> None:
    project, assigned = _prepare_rolling_project(tmp_path)
    first = assigned[0]["chapter_id"]

    with pytest.raises(ValidationFailure) as info:
        short_planner.generate_rolling(
            project,
            client=FakeLLMClient(
                [
                    json.dumps(
                        _rolling_patch(first, changes={"summary": "Sửa chương đã final."}),
                        ensure_ascii=False,
                    )
                ]
            ),
            operation_id="op_roll_final",
            now=NOW,
        )
    assert any(issue.code == "out_of_scope" for issue in info.value.result.errors)
    assert storage.load_artifact(project, "rolling_patch_arc_0001") is None

    with pytest.raises(ValidationFailure) as info_unknown:
        short_planner.generate_rolling(
            project,
            client=FakeLLMClient(
                [
                    json.dumps(
                        _rolling_patch("ch_9999", changes={"summary": "Chapter lạ."}),
                        ensure_ascii=False,
                    )
                ]
            ),
            operation_id="op_roll_unknown",
            now=NOW,
        )
    codes = {issue.code for issue in info_unknown.value.result.errors}
    assert {"unknown_reference", "out_of_scope"} & codes


def test_rolling_reject_keeps_short_plan(tmp_path: Path) -> None:
    project, assigned = _prepare_rolling_project(tmp_path)
    second = assigned[1]["chapter_id"]
    short_planner.generate_rolling(
        project,
        client=FakeLLMClient(
            [
                json.dumps(
                    _rolling_patch(second, changes={"summary": "Đề xuất."}), ensure_ascii=False
                )
            ]
        ),
        operation_id="op_roll_gen_reject",
        now=NOW,
    )

    short_planner.reject_rolling(project, operation_id="op_roll_reject", now=NOW)

    patch_envelope = storage.load_artifact(project, "rolling_patch_arc_0001")
    assert patch_envelope.candidate_revision is None
    plan = storage.load_artifact(project, "short_plan")
    assert plan.status is ArtifactStatus.accepted
    assert plan.accepted_revision.revision == 1


# ---------------------------------------------------------------------------
# 12. allow_relationship_replan
# ---------------------------------------------------------------------------


def test_relationship_replan_disabled_rejects_relationship_and_indirect_change(
    tmp_path: Path,
) -> None:
    project, assigned = _prepare_rolling_project(tmp_path, allow_relationship_replan=False)
    second = assigned[1]["chapter_id"]

    with pytest.raises(ValidationFailure) as info:
        short_planner.generate_rolling(
            project,
            client=FakeLLMClient(
                [
                    json.dumps(
                        _rolling_patch(
                            second,
                            relationship_changes=[
                                {
                                    "character_ids": ["char_0001", "char_0002"],
                                    "arc_direction": "dè chừng → hợp tác",
                                    "target_state": "hợp tác",
                                    "notes": "",
                                }
                            ],
                        ),
                        ensure_ascii=False,
                    )
                ]
            ),
            operation_id="op_roll_rel_false",
            now=NOW,
        )
    codes = {issue.code for issue in info.value.result.errors}
    assert "relationship_replan_disabled" in codes

    with pytest.raises(ValidationFailure) as indirect:
        short_planner.generate_rolling(
            project,
            client=FakeLLMClient(
                [
                    json.dumps(
                        _rolling_patch(second, changes={"outline": ["Beat mới."]}),
                        ensure_ascii=False,
                    )
                ]
            ),
            operation_id="op_roll_indirect",
            now=NOW,
        )
    indirect_codes = {issue.code for issue in indirect.value.result.errors}
    assert "relationship_replan_disabled_indirect" in indirect_codes
    assert storage.load_artifact(project, "rolling_patch_arc_0001") is None


def test_relationship_replan_allowed_when_config_true(tmp_path: Path) -> None:
    project, assigned = _prepare_rolling_project(tmp_path, allow_relationship_replan=True)
    second = assigned[1]["chapter_id"]

    short_planner.generate_rolling(
        project,
        client=FakeLLMClient(
            [
                json.dumps(
                    _rolling_patch(
                        second,
                        relationship_changes=[
                            {
                                "character_ids": ["char_0001", "char_0002"],
                                "arc_direction": "dè chừng → phối hợp có điều kiện",
                                "target_state": "phối hợp có điều kiện",
                                "notes": "Chỉ đổi bước đi, giữ đích arc.",
                            }
                        ],
                    ),
                    ensure_ascii=False,
                )
            ]
        ),
        operation_id="op_roll_rel_true",
        now=NOW,
    )
    short_planner.accept_rolling(project, operation_id="op_roll_rel_true_acc", now=NOW)

    plan = storage.load_artifact(project, "short_plan")
    chapters = {chapter.chapter_id: chapter for chapter in plan.accepted_revision.payload.chapters}
    assert chapters[second].relationship_changes[0].target_state == "phối hợp có điều kiện"
    # Không sinh current relationship state từ plan.
    assert storage.load_relationships(project).relationships == []


# ---------------------------------------------------------------------------
# Bổ sung: guard trước LLM và auto accept
# ---------------------------------------------------------------------------


def test_long_plan_requires_premise_before_llm(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    client = FakeLLMClient([json.dumps(_long_plan_payload(), ensure_ascii=False)])

    with pytest.raises(GuardError):
        long_planner.generate(project, client=client, now=NOW)

    assert client.calls == []


def test_short_plan_requires_long_plan_before_llm(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _seed_foundation(project)
    client = FakeLLMClient(["{}"])

    with pytest.raises(GuardError) as info:
        short_planner.generate(project, client=client, arc_id="arc_0001", now=NOW)

    assert info.value.code == "missing_dependency"
    assert client.calls == []


def test_short_plan_auto_accept_creates_chapter_metadata(tmp_path: Path) -> None:
    """Auto Accept chỉ áp cho candidate `actual`: chương trước phải đã reconcile."""
    project = _make_project(tmp_path, auto_accept=True)
    _seed_foundation(project)
    _accept_long_plan(project, arc_range=(1, 2))
    first = short_planner.assign_chapter_ids(project, 1)
    short_planner.generate(
        project,
        client=FakeLLMClient(
            [
                json.dumps(
                    {"arc_id": "arc_0001", "chapters": [_chapter("ch_0001", 1)]},
                    ensure_ascii=False,
                )
            ]
        ),
        arc_id="arc_0001",
        assigned_chapters=first,
        operation_id="op_short_auto_ch1",
        now=NOW,
    )
    assert storage.load_chapter(project, "ch_0001") is not None
    _mark_chapter_final(project, "ch_0001", 1)

    # Chương 2 lập sau khi chương 1 đã `final_reconciled` ⇒ basis là actual.
    assigned = short_planner.resolve_assigned_chapters(project, _arc(project))
    assert assigned == [{"chapter_id": "ch_0002", "chapter_number": 2}]

    result = short_planner.generate(
        project,
        client=FakeLLMClient(
            [
                json.dumps(
                    {
                        "arc_id": "arc_0001",
                        "chapters": [_chapter("ch_0002", 2)],
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        arc_id="arc_0001",
        assigned_chapters=assigned,
        operation_id="op_short_auto",
        now=NOW,
    )

    assert result.data["context_mode"] == "actual"
    assert result.data["auto_accepted"] is True
    plan = storage.load_artifact(project, "short_plan")
    assert plan.status is ArtifactStatus.accepted
    assert storage.load_chapter(project, "ch_0002").previous_chapter_id == "ch_0001"


# ---------------------------------------------------------------------------
# Provisional: candidate chuẩn bị trước không được vào canon (F-B1/F-B2 của T24)
# ---------------------------------------------------------------------------


def _provisional_short_plan_candidate(tmp_path: Path) -> tuple[Project, list[dict[str, Any]]]:
    """Short Plan 2 chương đã accepted, rồi generate lại khi chương 1 đã có prose.

    Chương 1 có prose nhưng chưa reconcile ⇒ basis của chương 2 là `provisional`
    và bridge vẫn mô tả chương 1 như một ý định — tức candidate đã bị actual vượt
    qua, không được accept.
    """
    project = _make_project(tmp_path)
    _seed_foundation(project)
    _accept_long_plan(project)
    assigned = _accept_short_plan(project)
    chapter_one = storage.load_chapter(project, assigned[0]["chapter_id"])
    assert chapter_one is not None
    storage.save_chapter(
        project,
        chapter_one.model_copy(
            update={
                "status": ChapterStatus.review_required,
                "drafts": [
                    ProseRevision(
                        revision=1,
                        markdown_ref=f"chapters/{chapter_one.chapter_id}/drafts/draft_r0001.md",
                        source_type=SourceType.llm,
                        is_complete=True,
                        created_at=NOW,
                    )
                ],
                "current_draft_revision": 1,
            }
        ),
        operation_id="op_regress_chapter_one",
    )

    payload = {
        "arc_id": "arc_0001",
        "chapters": [
            _chapter(assigned[0]["chapter_id"], assigned[0]["chapter_number"], summary="Bản mới 1"),
            _chapter(assigned[1]["chapter_id"], assigned[1]["chapter_number"], summary="Bản mới 2"),
        ],
    }
    result = short_planner.generate(
        project,
        client=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
        arc_id="arc_0001",
        assigned_chapters=assigned,
        action="regenerate",
        operation_id="op_short_provisional",
        now=NOW,
    )
    assert result.data["context_mode"] == "provisional"
    return project, assigned


def test_short_plan_accept_keeps_chapter_status_of_chapter_with_prose(
    tmp_path: Path,
) -> None:
    """Accept Short Plan không hạ chapter đã có prose về `planned` (F-A4 của T24).

    Metadata phải khớp dữ liệu thật trên disk: chương 2 đang `review_required` với
    prose r1 vẫn giữ nguyên lifecycle sau khi accept lại Short Plan (chỉ chương 1
    được lập lại), không bị ghi ngược thành `planned`.
    """
    project = _make_project(tmp_path)
    _seed_foundation(project)
    _accept_long_plan(project)
    _accept_short_plan(project)
    chapter_two = storage.load_chapter(project, "ch_0002")
    assert chapter_two is not None
    storage.save_chapter(
        project,
        chapter_two.model_copy(
            update={
                "status": ChapterStatus.review_required,
                "drafts": [
                    ProseRevision(
                        revision=1,
                        markdown_ref="chapters/ch_0002/drafts/draft_r0001.md",
                        source_type=SourceType.llm,
                        is_complete=True,
                        created_at=NOW,
                    )
                ],
                "current_draft_revision": 1,
            }
        ),
        operation_id="op_seed_prose_ch2",
    )

    payload = {
        "arc_id": "arc_0001",
        "chapters": [_chapter("ch_0001", 1, summary="Lập lại chương 1")],
    }
    result = short_planner.generate(
        project,
        client=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
        arc_id="arc_0001",
        assigned_chapters=[{"chapter_id": "ch_0001", "chapter_number": 1}],
        action="regenerate",
        operation_id="op_short_replan",
        now=NOW,
    )
    assert result.data["context_mode"] == "actual"
    short_planner.accept(project, operation_id="op_short_replan_accept", now=NOW)

    chapter_after = storage.load_chapter(project, "ch_0002")
    assert chapter_after is not None
    assert chapter_after.status is ChapterStatus.review_required
    assert chapter_after.current_draft_revision == 1
    assert len(chapter_after.drafts) == 1
    # Chapter 1 (được lập lại) gắn pin của Short Plan accepted mới.
    accepted = storage.load_artifact(project, "short_plan").accepted_revision
    chapter_one = storage.load_chapter(project, "ch_0001")
    assert chapter_one is not None and chapter_one.short_plan_pin is not None
    assert chapter_one.short_plan_pin.revision == accepted.revision


def test_short_plan_provisional_candidate_carries_preparation_context(
    tmp_path: Path,
) -> None:
    """Candidate provisional ghi `preparation_context` ngay trong revision."""
    project, _assigned = _provisional_short_plan_candidate(tmp_path)

    envelope = storage.load_artifact(project, "short_plan")
    assert envelope is not None and envelope.candidate_revision is not None
    preparation = envelope.candidate_revision.preparation_context
    assert preparation is not None
    assert preparation.context_basis.mode.value == "provisional"
    assert preparation.context_basis.actual_through_chapter == 0
    # Bridge lấy intent từ plan accepted, không bịa timeline.
    assert [item.chapter_number for item in preparation.context_basis.planned_bridge] == [1]


def test_short_plan_provisional_candidate_is_not_acceptable(tmp_path: Path) -> None:
    """Accept thủ công candidate provisional bị backend từ chối (D014)."""
    project, _assigned = _provisional_short_plan_candidate(tmp_path)
    accepted_before = storage.load_artifact(project, "short_plan").accepted_revision.revision

    with pytest.raises(GuardError) as info:
        short_planner.accept(project, operation_id="op_short_accept_provisional", now=NOW)

    assert info.value.code == "provisional_candidate_not_writer_ready"
    envelope = storage.load_artifact(project, "short_plan")
    # Accepted cũ giữ nguyên, candidate vẫn còn cho user regenerate/review.
    assert envelope.accepted_revision.revision == accepted_before
    assert envelope.candidate_revision is not None
    assert envelope.status is ArtifactStatus.draft


def test_short_plan_actual_candidate_is_acceptable_after_previous_chapter_final(
    tmp_path: Path,
) -> None:
    """Sau khi chương 1 `final_reconciled`, candidate chỉ còn chương 2 và là `actual`."""
    project = _make_project(tmp_path)
    _seed_foundation(project)
    _accept_long_plan(project)
    assigned = _accept_short_plan(project)
    _mark_chapter_final(project, assigned[0]["chapter_id"], assigned[0]["chapter_number"])

    # Chương 1 đã final: plan của nó là canon, chỉ còn chương 2 được lập lại.
    chapter_two = storage.load_chapter(project, "ch_0002")
    assert chapter_two is not None
    remaining = [
        {"chapter_id": chapter_two.chapter_id, "chapter_number": chapter_two.chapter_number}
    ]
    payload = {
        "arc_id": "arc_0001",
        "chapters": [
            _chapter("ch_0002", 2, summary="Bám theo actual chương 1"),
        ],
    }
    result = short_planner.generate(
        project,
        client=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
        arc_id="arc_0001",
        assigned_chapters=remaining,
        action="regenerate",
        operation_id="op_short_actual",
        now=NOW,
    )

    assert result.data["context_mode"] == "actual"
    envelope = storage.load_artifact(project, "short_plan")
    assert envelope.candidate_revision.preparation_context is None
    # Chapter đã final không bị đưa vào candidate mới.
    assert [item.chapter_id for item in envelope.candidate_revision.payload.chapters] == [
        "ch_0002"
    ]
    accepted = short_planner.accept(project, operation_id="op_short_accept_actual", now=NOW)
    assert accepted.data["status"] == ArtifactStatus.accepted.value
    # Plan của chương 1 (đã final) được merge lại nguyên vẹn.
    final_plan = storage.load_artifact(project, "short_plan").accepted_revision.payload
    assert sorted(item.chapter_id for item in final_plan.chapters) == ["ch_0001", "ch_0002"]
