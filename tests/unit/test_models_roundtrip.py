"""Round-trip document contract T02 qua models T08.

Mục tiêu: ví dụ contract trong `docs/design/examples/` phải parse được **và**
giữ nguyên dữ liệu cần thiết khi ghi lại. Nếu model bỏ field, đổi tên hoặc
thêm default che mất dữ liệu, test này fail.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from novel_ai.core.models import (
    ARTIFACT_PAYLOAD_MODELS,
    ChapterMetadata,
    CoCreateDocument,
    CurrentTimelineDocument,
    PlanningScope,
    ProjectConfig,
    RelationshipStateDocument,
    RewriteSectionPayload,
    RewriteSectionRequest,
    StructuredOutputError,
)
from novel_ai.core.validation import (
    parse_artifact_document,
    parse_chapter_metadata,
    parse_co_create_document,
    parse_model,
    parse_project_config,
    parse_relationship_document,
    parse_timeline_document,
)


def test_every_artifact_type_in_contract_examples_has_a_payload_model(
    t02_valid_document: dict[str, Any],
) -> None:
    artifacts = t02_valid_document["artifacts"]
    artifact_types = {str(item["artifact_type"]) for item in artifacts.values()}
    missing = sorted(artifact_types - set(ARTIFACT_PAYLOAD_MODELS))
    assert missing == [], f"Thiếu model payload cho artifact_type: {missing}"


def _assert_preserves_contract_fields(
    actual: Any, expected: Any, path: str = ""
) -> None:
    """Mọi field của ví dụ contract phải được giữ nguyên giá trị.

    Model được phép có thêm field app-owned (ví dụ `PayloadSource.id_map`,
    `DependencyPin.chapter_id = null`, `preparation_context`); điều không được
    phép là mất field hoặc đổi giá trị. Danh sách phải cùng độ dài để không
    giấu mất entry.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: kiểu không khớp"
        for key, value in expected.items():
            assert key in actual, f"Model làm mất field contract `{path}/{key}`"
            _assert_preserves_contract_fields(actual[key], value, f"{path}/{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: kiểu không khớp"
        assert len(actual) == len(expected), f"{path}: số phần tử lệch"
        for position, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            _assert_preserves_contract_fields(actual_item, expected_item, f"{path}/{position}")
    else:
        assert actual == expected, f"{path}: giá trị bị đổi"


@pytest.mark.parametrize("artifact_key", sorted([
    "premise",
    "characters",
    "world_rules",
    "foreshadow",
    "long_plan",
    "short_plan",
    "skeleton_ch_0001",
    "review_report_ch_0001",
    "reconciliation_ch_0001",
    "rolling_patch_arc_0001",
    "impact_report_ch_0001_retcon",
]))
def test_artifact_envelope_round_trips_without_losing_data(
    t02_valid_document: dict[str, Any], artifact_key: str
) -> None:
    raw = t02_valid_document["artifacts"][artifact_key]

    envelope = parse_artifact_document(raw)

    assert envelope.artifact_type == raw["artifact_type"]
    assert envelope.status.value == raw["status"]
    for side in ("accepted_revision", "candidate_revision"):
        raw_revision = raw[side]
        parsed_revision = getattr(envelope, side)
        if raw_revision is None:
            assert parsed_revision is None
            continue
        assert parsed_revision is not None
        # Payload round-trip: ghi lại phải bằng đúng dữ liệu contract cung cấp.
        assert parsed_revision.payload.model_dump(mode="json") == raw_revision["payload"]
        _assert_preserves_contract_fields(
            parsed_revision.payload_source.model_dump(mode="json"), raw_revision["payload_source"]
        )
        _assert_preserves_contract_fields(
            [pin.model_dump(mode="json") for pin in parsed_revision.dependency_pins],
            raw_revision["dependency_pins"],
        )
        _assert_preserves_contract_fields(
            parsed_revision.validation.model_dump(mode="json"), raw_revision["validation"]
        )
        assert parsed_revision.revision == raw_revision["revision"]
        assert parsed_revision.created_at == raw_revision["created_at"]
        assert parsed_revision.accepted_at == raw_revision["accepted_at"]
        assert parsed_revision.accepted_by == raw_revision["accepted_by"]
    envelope_dump = envelope.model_dump(mode="json")
    assert envelope_dump["stale_reasons"] == raw["stale_reasons"]
    assert envelope_dump["history_refs"] == raw["history_refs"]


def test_planning_scope_round_trips_and_legacy_files_stay_readable(
    t02_valid_document: dict[str, Any],
) -> None:
    """T30: `planning_scope` là metadata app-owned, optional, round-trip được.

    File cũ (không có field) phải parse ra `None` = legacy, không raise: nếu thêm
    field mà làm project cũ không mở được thì migration D017 vô nghĩa.
    """
    raw = t02_valid_document["artifacts"]["long_plan"]
    legacy = parse_artifact_document(raw)
    legacy_revision = legacy.accepted_revision or legacy.candidate_revision
    assert legacy_revision is not None
    assert legacy_revision.planning_scope is None

    with_scope = copy.deepcopy(raw)
    with_scope["accepted_revision"]["planning_scope"] = {"start": 1, "end": 120}
    parsed = parse_artifact_document(with_scope)
    revision = parsed.accepted_revision or parsed.candidate_revision
    assert revision is not None
    assert revision.planning_scope.start == 1
    assert revision.planning_scope.end == 120
    # Round-trip: dump lại rồi parse lại vẫn giữ nguyên horizon.
    dumped = parsed.model_dump(mode="json")
    assert dumped["accepted_revision"]["planning_scope"] == {"start": 1, "end": 120}
    reparsed = parse_artifact_document(dumped)
    reparsed_revision = reparsed.accepted_revision or reparsed.candidate_revision
    assert reparsed_revision is not None
    assert reparsed_revision.planning_scope == revision.planning_scope


@pytest.mark.parametrize("scope", [{"start": 5, "end": 2}, {"start": 0, "end": 3}])
def test_planning_scope_rejects_invalid_range(scope: dict[str, int]) -> None:
    """`1 <= start <= end` là invariant của D017, không chỉ của service."""
    with pytest.raises(ValidationError):
        PlanningScope.model_validate(scope)


def test_envelope_metadata_is_preserved_verbatim(t02_valid_document: dict[str, Any]) -> None:
    raw = t02_valid_document["artifacts"]["premise"]
    envelope = parse_artifact_document(raw)

    dumped = envelope.model_dump(mode="json")
    assert dumped["schema_version"] == 1
    assert dumped["artifact_id"] == "premise"
    assert dumped["stale_reasons"] == []
    assert dumped["history_refs"] == []
    assert dumped["accepted_revision"]["revision"] == 1
    assert dumped["accepted_revision"]["created_at"] == raw["accepted_revision"]["created_at"]
    assert dumped["accepted_revision"]["validation"] == {"state": "valid", "errors": []}


def test_project_config_round_trips(t02_valid_document: dict[str, Any]) -> None:
    raw = t02_valid_document["project"]
    config = parse_project_config(raw)

    assert isinstance(config, ProjectConfig)
    assert config.model_dump(mode="json") == raw
    assert config.auto_accept_structured is False
    assert config.rolling_plan_every == 3


def test_co_create_document_round_trips(t02_valid_document: dict[str, Any]) -> None:
    raw = t02_valid_document["co_create"]
    document = parse_co_create_document(raw)

    assert isinstance(document, CoCreateDocument)
    _assert_preserves_contract_fields(document.model_dump(mode="json"), raw)
    assert document.status.value == "finalized"
    assert document.base_idea is not None
    assert document.base_idea.status.value == "accepted"


def test_chapter_metadata_round_trips(t02_valid_document: dict[str, Any]) -> None:
    for raw in t02_valid_document["chapters"].values():
        chapter = parse_chapter_metadata(raw)
        assert isinstance(chapter, ChapterMetadata)
        _assert_preserves_contract_fields(chapter.model_dump(mode="json"), raw)


def test_chapter_helpers_expose_current_draft_without_inferring(
    t02_valid_document: dict[str, Any],
) -> None:
    chapter = parse_chapter_metadata(t02_valid_document["chapters"]["ch_0001"])

    assert chapter.current_draft_revision == 2
    assert chapter.current_draft is not None
    assert chapter.current_draft.markdown_ref == "chapter_0001_final.md"
    assert chapter.draft_revision(99) is None


def test_state_documents_round_trip(t02_valid_document: dict[str, Any]) -> None:
    state = t02_valid_document["state"]
    timeline = parse_timeline_document(state["current_timeline"])
    relationships = parse_relationship_document(state["relationships"])

    assert isinstance(timeline, CurrentTimelineDocument)
    assert isinstance(relationships, RelationshipStateDocument)
    _assert_preserves_contract_fields(timeline.model_dump(mode="json"), state["current_timeline"])
    _assert_preserves_contract_fields(
        relationships.model_dump(mode="json"), state["relationships"]
    )


def test_snapshot_fixture_parses_with_effective_chapter_exclusions(
    t02_valid_document: dict[str, Any],
) -> None:
    from novel_ai.core.models import ChapterContextSnapshot

    raw = t02_valid_document["state"]["snapshots"][0]
    snapshot = parse_model(ChapterContextSnapshot, raw)

    assert snapshot.for_chapter_number == 1
    kinds = {item.item_id: item.effective_from_chapter for item in snapshot.excluded_due_to_effective_chapter}
    assert kinds == {"char_0002": 2, "rule_0100": 100}


def test_rewrite_request_and_payload_round_trip(t02_valid_document: dict[str, Any]) -> None:
    raw = t02_valid_document["rewrite_section_example"]

    request = parse_model(RewriteSectionRequest, raw["request"])
    payload = parse_model(RewriteSectionPayload, raw["payload"])

    _assert_preserves_contract_fields(request.model_dump(mode="json"), raw["request"])
    _assert_preserves_contract_fields(payload.model_dump(mode="json"), raw["payload"])
    assert payload.changed_intent is False


def test_structured_output_error_round_trips(t02_valid_document: dict[str, Any]) -> None:
    raw = t02_valid_document["structured_output_error_example"]
    error = parse_model(StructuredOutputError, raw)

    assert error.model_dump(mode="json") == raw
    assert error.errors[0].path == "/payload/chapter_number"


def test_stable_id_helpers_do_not_reuse_existing_numbers() -> None:
    from novel_ai.core.models import (
        format_stable_id,
        is_temporary_id,
        next_stable_id,
        reserve_id_pool,
    )

    assert format_stable_id("char", 7) == "char_0007"
    assert next_stable_id("char", ["char_0001", "char_0010"]) == "char_0011"
    assert next_stable_id("rule", []) == "rule_0001"
    assert reserve_id_pool("section", 3, ["section_0002"]) == [
        "section_0003",
        "section_0004",
        "section_0005",
    ]
    assert is_temporary_id("tmp_section_1") is True
    assert is_temporary_id("tmp_character_1") is False
    assert is_temporary_id("section_0001") is False
