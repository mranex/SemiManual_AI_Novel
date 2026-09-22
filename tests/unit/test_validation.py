"""Validation behavior cho contract T02 (T08).

Test kiểm tra hành vi sản phẩm, không lặp lại implementation:

- ví dụ hợp lệ của contract phải validate `valid` (trừ case đã biết là sai);
- case sai có path/code ổn định để UI/service hiển thị theo field;
- version chưa hỗ trợ bị từ chối và dữ liệu không bị mutate;
- guard Writer projection, plan-vs-state và Auto Accept chạy độc lập.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from novel_ai.core.models import ChapterPlan, LongPlanPayload, RelationshipState, Severity
from novel_ai.core.validation import (
    DocumentError,
    ReferenceIndex,
    UnsupportedSchemaVersion,
    ValidationContext,
    build_reference_index,
    ensure_supported_schema_version,
    find_secret_text,
    long_plan_horizon_issues,
    parse_artifact_document,
    parse_model,
    partition_by_effective_chapter,
    validate_artifact_payload,
    validate_auto_accept_scope,
    validate_effective_selection,
    validate_plan_does_not_mutate_state,
    validate_writer_projection,
)


# ---------------------------------------------------------------------------
# Helper dùng chung
# ---------------------------------------------------------------------------


def _index_from_spec(spec: dict[str, Any] | None) -> ReferenceIndex | None:
    if not spec:
        return None
    return ReferenceIndex(
        character_ids=frozenset(spec.get("character_ids", [])),
        world_rule_ids=frozenset(spec.get("world_rule_ids", [])),
        foreshadow_ids=frozenset(spec.get("foreshadow_ids", [])),
        arc_ids=frozenset(spec.get("arc_ids", [])),
        volume_ids=frozenset(spec.get("volume_ids", [])),
        chapter_ids=frozenset(spec.get("chapter_ids", [])),
        relationship_ids=frozenset(spec.get("relationship_ids", [])),
        character_effective=spec.get("character_effective", {}),
        world_rule_effective=spec.get("world_rule_effective", {}),
        foreshadow_effective=spec.get("foreshadow_effective", {}),
        chapter_numbers=spec.get("chapter_numbers", {}),
        relationship_pairs={
            key: tuple(value) for key, value in spec.get("relationship_pairs", {}).items()
        },
    )


def _context_from_spec(spec: dict[str, Any] | None) -> ValidationContext:
    spec = spec or {}
    eligible = spec.get("eligible_chapter_ids")
    return ValidationContext(
        index=_index_from_spec(spec.get("index")),
        chapter_number=spec.get("chapter_number"),
        eligible_chapter_ids=frozenset(eligible) if eligible is not None else None,
        allow_relationship_replan=spec.get("allow_relationship_replan"),
        reviewed_chapter_range=(
            (spec["reviewed_chapter_range"]["start"], spec["reviewed_chapter_range"]["end"])
            if spec.get("reviewed_chapter_range")
            else None
        ),
    )


def _assert_errors(errors, expected: list[dict[str, str]]) -> None:
    pairs = {(issue.path, issue.code) for issue in errors}
    for item in expected:
        assert (item["path"], item["code"]) in pairs, (
            f"Thiếu lỗi {item} trong {sorted(pairs)}"
        )


def _full_index(t02_valid_document: dict[str, Any]) -> ReferenceIndex:
    """Index dựng từ chính các artifact accepted trong ví dụ contract T02."""
    from novel_ai.core.models import (
        ArcPlan,
        ChapterPlan,
        Character,
        ForeshadowEntry,
        WorldRule,
    )

    artifacts = t02_valid_document["artifacts"]
    characters = [
        Character.model_validate(item)
        for item in artifacts["characters"]["accepted_revision"]["payload"]["characters"]
    ]
    rules = [
        WorldRule.model_validate(item)
        for item in artifacts["world_rules"]["accepted_revision"]["payload"]["world_rules"]
    ]
    foreshadows = [
        ForeshadowEntry.model_validate(item)
        for item in artifacts["foreshadow"]["accepted_revision"]["payload"]["foreshadows"]
    ]
    arcs = [
        ArcPlan.model_validate(arc)
        for volume in artifacts["long_plan"]["accepted_revision"]["payload"]["volumes"]
        for arc in volume["arcs"]
    ]
    chapters = [
        ChapterPlan.model_validate(item)
        for item in artifacts["short_plan"]["accepted_revision"]["payload"]["chapters"]
    ]
    from novel_ai.core.validation import parse_relationship_document

    relationships = parse_relationship_document(t02_valid_document["state"]["relationships"])
    return build_reference_index(
        characters=characters,
        world_rules=rules,
        foreshadows=foreshadows,
        long_plan_arcs=arcs,
        short_plan_chapters=chapters,
        relationships=relationships,
    )


# ---------------------------------------------------------------------------
# Happy path trên ví dụ contract
# ---------------------------------------------------------------------------


def test_valid_contract_examples_validate_clean_except_known_historical_case(
    t02_valid_document: dict[str, Any],
) -> None:
    """Mọi payload accepted trong ví dụ phải valid, trừ case đã ghi là sai lịch sử.

    `reconciliation_ch_0001` cập nhật `char_0002` (hiệu lực chương 2) trong
    chương 1. Prompt-catalog mục 8.2 ghi rõ T08/T12/T17 phải reject case này,
    nên nó được assert riêng bên dưới thay vì được coi là hợp lệ.
    """
    index = _full_index(t02_valid_document)
    artifacts = t02_valid_document["artifacts"]
    known_invalid = {"reconciliation_ch_0001"}

    checked = 0
    for key, raw in artifacts.items():
        if raw["accepted_revision"] is None or key in known_invalid:
            continue
        result = validate_artifact_payload(
            raw["artifact_type"],
            raw["accepted_revision"]["payload"],
            context=ValidationContext(index=index),
        )
        assert result.errors == [], f"{key} không valid: {result.errors}"
        checked += 1
    assert checked >= 7


def test_historical_reconciliation_example_is_rejected_for_future_character(
    t02_valid_document: dict[str, Any],
) -> None:
    raw = t02_valid_document["artifacts"]["reconciliation_ch_0001"]["accepted_revision"]["payload"]
    result = validate_artifact_payload(
        "reconciliation", raw, context=ValidationContext(index=_full_index(t02_valid_document))
    )

    _assert_errors(
        result.errors,
        [
            {
                "path": "/payload/relationship_updates/0/character_ids/1",
                "code": "effective_from_future",
            }
        ],
    )


# ---------------------------------------------------------------------------
# Case âm của T02
# ---------------------------------------------------------------------------


def test_t02_invalid_examples_produce_expected_path_and_code(
    t02_invalid_cases: list[dict[str, Any]],
) -> None:
    index = ReferenceIndex(
        character_ids=frozenset({"char_0001"}),
        world_rule_ids=frozenset({"rule_0001", "rule_0100"}),
        world_rule_effective={"rule_0001": 1, "rule_0100": 100},
        chapter_numbers={"ch_0001": 1},
        chapter_ids=frozenset({"ch_0001"}),
    )
    handlers = {
        "relationship_uses_display_names": lambda payload: _display_name_rejection(payload),
        "future_lore_leaks_into_past_context": lambda payload: validate_effective_selection(
            for_chapter_number=payload["for_chapter_number"],
            index=index,
            world_rule_ids=payload["effective_world_rule_ids"],
        ),
        "writer_context_contains_author_secret": lambda payload: validate_writer_projection(
            payload
        ).errors,
        "short_plan_mutates_current_relationship": lambda payload: validate_plan_does_not_mutate_state(
            payload
        ).errors,
        "reconciliation_chapter_mismatch": lambda payload: validate_artifact_payload(
            "reconciliation",
            # Case T02 chỉ nhắm sai chapter_number; bổ sung field còn thiếu để
            # lỗi shape không che mất lỗi cross-field cần kiểm.
            {
                **payload,
                "source_final_candidate": {"prose_revision": 1, "markdown_ref": "final_r0001.md"},
                "notes": [],
            },
            context=_context_from_spec({"index": {"chapter_ids": ["ch_0001"], "chapter_numbers": {"ch_0001": 1}}}),
        ).errors,
        "auto_accept_prose_finalize": lambda payload: validate_auto_accept_scope(
            auto_accept_structured=payload["auto_accept_structured"],
            output_kind="markdown",
            requested_transition=payload["requested_transition"],
        ).errors,
    }

    assert set(handlers) == {case["case_id"] for case in t02_invalid_cases}
    for case in t02_invalid_cases:
        issues = handlers[case["case_id"]](case["payload"])
        _assert_errors(issues, case["expected_errors"])


def _display_name_rejection(payload: dict[str, Any]):
    """RelationshipState phải dùng `character_ids`, không dùng `character_a/b`."""
    try:
        parse_model(RelationshipState, payload, base_path="/payload")
    except DocumentError as exc:
        return [
            issue.model_copy(
                update={
                    "code": "display_name_as_foreign_key",
                    "message": "Relationship must use character_ids, not display names.",
                }
            )
            if issue.path.endswith(("/character_a", "/character_b"))
            else issue
            for issue in exc.errors
        ]
    return []


# ---------------------------------------------------------------------------
# Case âm bổ sung của T08
# ---------------------------------------------------------------------------


def test_t08_negative_cases_produce_expected_path_and_code(
    t08_negative_cases: dict[str, Any],
) -> None:
    for case in t08_negative_cases["cases"]:
        context = _context_from_spec(case.get("context"))
        result = validate_artifact_payload(
            case["artifact_type"], case["payload"], context=context
        )
        _assert_errors(result.errors, case["expected_errors"])


def test_t08_helper_cases_produce_expected_result(t08_negative_cases: dict[str, Any]) -> None:
    for case in t08_negative_cases["helper_cases"]:
        result = validate_auto_accept_scope(**case["kwargs"])
        _assert_errors(result.errors, case["expected_errors"])


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


def test_unsupported_envelope_schema_version_is_rejected_without_mutating_input() -> None:
    document = {
        "schema_version": 99,
        "artifact_id": "premise",
        "artifact_type": "premise",
        "status": "accepted",
        "accepted_revision": None,
        "candidate_revision": None,
        "stale_reasons": [],
        "history_refs": [],
    }
    snapshot = copy.deepcopy(document)

    with pytest.raises(UnsupportedSchemaVersion) as excinfo:
        parse_artifact_document(document)

    assert excinfo.value.errors[0].code == "unsupported_schema_version"
    assert "99" in excinfo.value.errors[0].message
    assert document == snapshot


def test_missing_schema_version_is_rejected() -> None:
    with pytest.raises(UnsupportedSchemaVersion) as excinfo:
        ensure_supported_schema_version({"project_id": "proj_0001"}, {2}, document_name="project.json")

    assert excinfo.value.errors[0].code == "missing_schema_version"


def test_project_schema_version_one_is_rejected() -> None:
    from novel_ai.core.validation import parse_project_config

    with pytest.raises(UnsupportedSchemaVersion) as excinfo:
        parse_project_config(
            {
                "schema_version": 1,
                "project_id": "proj_0001",
                "title": "Truyện",
                "created_at": "2026-09-19T09:00:00+07:00",
                "updated_at": "2026-09-19T09:00:00+07:00",
            }
        )

    assert excinfo.value.errors[0].code == "unsupported_schema_version"


def test_unknown_artifact_type_is_rejected() -> None:
    with pytest.raises(DocumentError) as excinfo:
        parse_artifact_document(
            {
                "schema_version": 1,
                "artifact_id": "x",
                "artifact_type": "not_a_type",
                "status": "draft",
                "accepted_revision": None,
                "candidate_revision": None,
                "stale_reasons": [],
                "history_refs": [],
            }
        )

    assert excinfo.value.errors[0].code == "unknown_artifact_type"


# ---------------------------------------------------------------------------
# Field-level errors và tính không phá dữ liệu
# ---------------------------------------------------------------------------


def test_field_errors_point_at_exact_json_pointer() -> None:
    payload = {
        "characters": [
            {"character_id": "char_0001", "display_name": "Sở Dương", "role": "protagonist", "public_profile": {}},
            {"character_id": "char_0002", "role": "ally", "public_profile": {}},
        ]
    }

    result = validate_artifact_payload("characters", payload)

    codes = {(issue.path, issue.code) for issue in result.errors}
    assert ("/payload/characters/1/display_name", "missing_field") in codes


def test_validation_returns_invalid_result_instead_of_raising() -> None:
    result = validate_artifact_payload("skeleton", {"chapter_id": "ch_0001"})

    assert result.state.value == "invalid"
    assert result.errors
    assert all(issue.severity is Severity.blocking for issue in result.errors)


def _skeleton_with_surface(foreshadow_id: str, *, with_foreshadow_ids: bool) -> dict[str, Any]:
    section: dict[str, Any] = {
        "section_id": "section_0001",
        "index": 1,
        "type": "action",
        "instruction": "Mở cảnh.",
        "purpose": "Nối hệ quả.",
        "purpose_visibility": "writer_safe",
        "foreshadow_surfaces": [
            {
                "foreshadow_id": foreshadow_id,
                "surface_instruction": "Chỉ mô tả, không lý giải.",
                "reveal_policy": "hint_only",
            }
        ],
    }
    if with_foreshadow_ids:
        section["foreshadow_ids"] = [foreshadow_id]
    return {
        "chapter_id": "ch_0001",
        "chapter_number": 1,
        "global_constraints": [],
        "sections": [section],
    }


def test_skeleton_surface_rejects_foreshadow_from_future_chapter() -> None:
    """`foreshadow_surfaces[].foreshadow_id` cũng phải kiểm hiệu lực chương (F-A9).

    Trước đây chỉ `foreshadow_ids` truyền `chapter_number` cho `check_reference`,
    nên surface trỏ foreshadow hiệu lực chương 100 vẫn `valid` ở chương 1 và
    `surface_instruction` của nó đi thẳng vào projection Writer (đường leak lore
    tương lai).
    """
    index = ReferenceIndex(
        character_ids=frozenset({"char_0001"}),
        foreshadow_ids=frozenset({"fs_0001", "fs_0100"}),
        foreshadow_effective={"fs_0001": 1, "fs_0100": 100},
        chapter_ids=frozenset({"ch_0001"}),
        chapter_numbers={"ch_0001": 1},
    )
    context = ValidationContext(index=index, chapter_number=1)

    surface_only = validate_artifact_payload(
        "skeleton", _skeleton_with_surface("fs_0100", with_foreshadow_ids=False), context=context
    )
    declared = validate_artifact_payload(
        "skeleton", _skeleton_with_surface("fs_0100", with_foreshadow_ids=True), context=context
    )

    _assert_errors(
        surface_only.errors,
        [
            {
                "path": "/payload/sections/0/foreshadow_surfaces/0/foreshadow_id",
                "code": "effective_from_future",
            }
        ],
    )
    assert any(
        issue.path == "/payload/sections/0/foreshadow_ids/0" for issue in declared.errors
    )
    # Foreshadow đã hiệu lực thì surface hợp lệ.
    current = validate_artifact_payload(
        "skeleton", _skeleton_with_surface("fs_0001", with_foreshadow_ids=False), context=context
    )
    assert current.errors == []


def test_validation_does_not_mutate_input_payload() -> None:
    payload = {
        "characters": [
            {"character_id": "char_0001", "display_name": "Sở Dương", "role": "protagonist", "public_profile": {}}
        ]
    }
    snapshot = copy.deepcopy(payload)

    validate_artifact_payload("characters", payload)

    assert payload == snapshot


# ---------------------------------------------------------------------------
# Effective chapter
# ---------------------------------------------------------------------------


def test_partition_by_effective_chapter_reports_excluded_entries() -> None:
    from novel_ai.core.models import WorldRule

    rules = [
        WorldRule(world_rule_id="rule_0001", category="a", summary="s", content="c", effective_from_chapter=1),
        WorldRule(world_rule_id="rule_0100", category="b", summary="s", content="c", effective_from_chapter=100),
    ]

    included, excluded = partition_by_effective_chapter(
        rules,
        chapter_number=20,
        kind="world_rule",
        id_of=lambda item: item.world_rule_id,
        effective_of=lambda item: item.effective_from_chapter,
    )

    assert [item.world_rule_id for item in included] == ["rule_0001"]
    assert excluded == [
        {"item_kind": "world_rule", "item_id": "rule_0100", "effective_from_chapter": 100}
    ]


def test_secret_text_scan_finds_paths_and_ignores_missing_sentinel() -> None:
    projection = {
        "skeleton": {
            "sections": [
                {"instruction": "Mô tả cái nồi.", "author_only_notes": ["Cái nồi là mảnh vật phẩm cổ."]}
            ]
        }
    }

    found = find_secret_text(projection, ["mảnh vật phẩm cổ"])

    assert found == ["/payload/skeleton/sections/0/author_only_notes/0"]
    assert find_secret_text(projection, ["không xuất hiện"]) == []


# ---------------------------------------------------------------------------
# Guard Writer projection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leaked_field",
    ["author_only", "truth_author_only", "future_direction", "author_only_notes", "planned_payoff"],
)
def test_writer_projection_rejects_author_only_fields_at_any_depth(leaked_field: str) -> None:
    projection = {"characters": [{"character_id": "char_0001", leaked_field: {"secret": "x"}}]}

    result = validate_writer_projection(projection)

    assert result.state.value == "invalid"
    assert result.errors[0].code == "secret_leak"
    assert result.errors[0].path == f"/payload/characters/0/{leaked_field}"


def test_writer_projection_rejects_planner_only_purpose() -> None:
    projection = {
        "skeleton": {
            "sections": [
                {
                    "section_id": "section_0002",
                    "instruction": "Mô tả cái nồi.",
                    "purpose": "Gieo mầm cổ vật.",
                    "purpose_visibility": "planner_only",
                }
            ]
        }
    }

    result = validate_writer_projection(projection)

    assert {(issue.path, issue.code) for issue in result.errors} == {
        ("/payload/skeleton/sections/0/purpose", "secret_leak")
    }


def test_writer_projection_accepts_writer_safe_surface_instruction() -> None:
    projection = {
        "skeleton": {
            "sections": [
                {
                    "section_id": "section_0002",
                    "instruction": "Mô tả cái nồi.",
                    "purpose": "Gieo mầm vật phẩm bất thường.",
                    "purpose_visibility": "writer_safe",
                    "foreshadow_surfaces": [
                        {
                            "foreshadow_id": "fs_0001",
                            "surface_instruction": "Chỉ mô tả, không lý giải.",
                            "reveal_policy": "hint_only",
                        }
                    ],
                }
            ]
        }
    }

    assert validate_writer_projection(projection).state.value == "valid"


# ---------------------------------------------------------------------------
# Plan không mutate state
# ---------------------------------------------------------------------------


def test_plan_payload_with_actual_state_is_rejected() -> None:
    result = validate_plan_does_not_mutate_state(
        {"chapter_id": "ch_0002", "relationship_updates": [{"relationship_id": "rel_0001", "current": "Tin nhau"}]}
    )

    assert {(issue.path, issue.code) for issue in result.errors} >= {
        ("/payload/relationship_updates/0/current", "plan_actual_state_mix")
    }


def test_short_plan_with_future_direction_is_accepted() -> None:
    plan = ChapterPlan.model_validate(
        {
            "chapter_id": "ch_0002",
            "chapter_number": 2,
            "title": "Ánh lửa",
            "summary": "Kế hoạch.",
            "chapter_goal": "Mục tiêu.",
            "relationship_changes": [
                {
                    "character_ids": ["char_0001", "char_0002"],
                    "arc_direction": "dè chừng",
                    "target_state": "Chưa tin nhau",
                }
            ],
        }
    )

    assert validate_plan_does_not_mutate_state(plan.model_dump(mode="json")).state.value == "valid"


# ---------------------------------------------------------------------------
# Auto Accept
# ---------------------------------------------------------------------------


def test_auto_accept_never_applies_to_prose_or_human_review() -> None:
    for output_kind in ("markdown", "prose", "human_review", "review_confirmation"):
        result = validate_auto_accept_scope(
            auto_accept_structured=True, output_kind=output_kind
        )
        assert result.state.value == "invalid"
        assert result.errors[0].code == "auto_accept_cannot_finalize_prose"


def test_auto_accept_disabled_allows_manual_prose_path() -> None:
    assert (
        validate_auto_accept_scope(auto_accept_structured=False, output_kind="markdown").state.value
        == "valid"
    )


# ---------------------------------------------------------------------------
# T30 — Long Plan complete horizon
# ---------------------------------------------------------------------------


def _long_plan(
    ranges: list[tuple[int, int]], *, splits: list[int] | None = None
) -> LongPlanPayload:
    arcs = [
        {
            "arc_id": f"arc_{index:04d}",
            "title": f"Arc {index}",
            "chapter_range": {"start": low, "end": high},
            "goal": "g",
            "core_conflict": "c",
            "start_state": "s",
            "end_state": "e",
            "major_reveals": [],
            "character_ids": [],
            "world_rule_ids": [],
            "foreshadow_ids": [],
            "relationship_directions": [],
        }
        for index, (low, high) in enumerate(ranges, start=1)
    ]
    if splits is None:
        chunks = [arcs]
    else:
        assert sum(splits) == len(arcs), "splits phải phủ hết arc"
        chunks = []
        cursor = 0
        for size in splits:
            chunks.append(arcs[cursor : cursor + size])
            cursor += size
    payload_volumes = [
        {
            "volume_id": f"vol_{index + 1:04d}",
            "title": f"Quyển {index + 1}",
            "theme": "t",
            "goal": "g",
            "arcs": chunk,
        }
        for index, chunk in enumerate(chunks)
    ]
    return LongPlanPayload.model_validate(
        {"volumes": payload_volumes, "global_threads": []}
    )


def _codes(payload: LongPlanPayload, scope: dict[str, int] | None) -> set[str]:
    return {issue.code for issue in long_plan_horizon_issues(payload, scope)}


def test_horizon_issues_accept_contiguous_multi_volume_coverage() -> None:
    payload = _long_plan([(1, 20), (21, 45), (46, 80), (81, 120)], splits=[2, 1, 1])

    assert long_plan_horizon_issues(payload, {"start": 1, "end": 120}) == []
    # Không có scope: chỉ kiểm phần cấu trúc, không bịa coverage.
    assert long_plan_horizon_issues(payload, None) == []


@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        ([(1, 20), (22, 40)], "gap_in_scope"),
        ([(2, 20), (21, 40)], "uncovered_scope_start"),
        ([(1, 20), (21, 39)], "uncovered_scope_end"),
        ([(1, 20), (20, 40)], "overlap"),
        ([(1, 20), (21, 41)], "out_of_scope_arc"),
    ],
)
def test_horizon_issues_reject_broken_coverage(
    ranges: list[tuple[int, int]], expected: str
) -> None:
    payload = _long_plan(ranges)

    codes = _codes(payload, {"start": 1, "end": 40})

    assert expected in codes


def test_horizon_issues_require_nonempty_volume_and_arcs() -> None:
    empty = LongPlanPayload.model_validate({"volumes": [], "global_threads": []})
    assert _codes(empty, {"start": 1, "end": 10}) == {"empty_long_plan"}

    no_arcs = LongPlanPayload.model_validate(
        {
            "volumes": [{"volume_id": "vol_0001", "title": "t", "theme": "t", "goal": "g", "arcs": []}],
            "global_threads": [],
        }
    )
    assert _codes(no_arcs, {"start": 1, "end": 10}) == {"empty_volume"}


def test_horizon_issues_accept_single_arc_covering_horizon() -> None:
    """Một arc phủ đúng horizon hợp lệ về cấu trúc: không có quota số arc (D017)."""
    payload = _long_plan([(1, 120)])

    assert long_plan_horizon_issues(payload, {"start": 1, "end": 120}) == []


def test_artifact_validation_reports_empty_long_plan_without_scope() -> None:
    """Validator chung (không có scope) vẫn bắt payload rỗng/volume rỗng."""
    payload = LongPlanPayload.model_validate({"volumes": [], "global_threads": []})

    result = validate_artifact_payload(
        "long_plan", payload, context=ValidationContext(index=ReferenceIndex())
    )

    assert result.state.value == "invalid"
    assert "empty_long_plan" in {issue.code for issue in result.errors}
