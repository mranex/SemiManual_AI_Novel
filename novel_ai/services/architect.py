"""Architect / foundation service (T13).

Hiện thực các action foundation theo `docs/design/workflow.md` mục 4.1 và
`docs/design/schemas.md` mục 2:

- `assign_ids`: backend reserve stable ID (`char_`/`rule_`/`fs_`) tránh trùng cả
  accepted **và** candidate;
- `generate`: Premise / Characters / World Rules / Foreshadow candidate từ LLM,
  guard trước khi gọi, lưu raw output, validate schema + scope ID;
- `edit_candidate`: user sửa tay payload thành candidate mới;
- `accept` / `reject`: lifecycle candidate, kiểm tra freshness pin trước accept;
- `append_entries`: thêm entry mới với `effective_from_chapter`; entry hiệu lực
  tương lai không làm stale artifact/chapter có as-of nhỏ hơn;
- `auto_accept_enabled`: Auto Accept chỉ cho structured output hợp lệ (D005).

Service không tự chạy bước tiếp theo: accept Premise không generate Characters,
append không regenerate plan. Stale chỉ được **đánh dấu**, không rewrite.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from novel_ai.core import context as context_core
from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.generation import EventSink, GenerationEmitter
from novel_ai.core.models import (
    ARTIFACT_PAYLOAD_MODELS,
    ArtifactEnvelope,
    PayloadSource,
    SourceType,
    generate_operation_id,
    reserve_id_pool,
)
from novel_ai.core.project import Project
from novel_ai.services import (
    ActionResult,
    GuardError,
    StaleDependencyError,
    ValidationFailure,
)
from novel_ai.services.co_create import (
    StructuredCall,
    artifact_write_committed,
    check_pin_freshness,
    complete_json,
    current_dependency_pins,
    foundation_reference_index,
    generation_stage,
    parse_structured_or_fail,
    payload_source,
    require_base_idea,
    require_accepted_artifact,
    validate_or_fail,
)

__all__ = [
    "FOUNDATION_TYPES",
    "append_entries",
    "accept",
    "assign_ids",
    "auto_accept_enabled",
    "edit_candidate",
    "generate",
    "reject",
]

#: Foundation artifact mà service này quản lý.
FOUNDATION_TYPES: tuple[str, ...] = ("premise", "characters", "world_rules", "foreshadow")

#: Action hợp lệ cho `generate`.
_FOUNDATION_ACTIONS: frozenset[str] = frozenset({"generate", "regenerate", "edit"})

#: Prompt v1 theo artifact_type.
_FOUNDATION_PROMPT_IDS: dict[str, str] = {
    "premise": "architect.premise.v1",
    "characters": "architect.characters.v1",
    "world_rules": "architect.world_rules.v1",
    "foreshadow": "architect.foreshadow.v1",
}

#: `artifact_type` -> `(field collection, field ID, prefix ID)`.
_ID_FIELDS: dict[str, tuple[str, str, str]] = {
    "characters": ("characters", "character_id", "char"),
    "world_rules": ("world_rules", "world_rule_id", "rule"),
    "foreshadow": ("foreshadows", "foreshadow_id", "fs"),
}

#: Số ID reserve mặc định khi caller không truyền `assigned_ids`.
DEFAULT_ASSIGNED_ID_POOL = 3

#: Loại change dùng cho stale propagation khi append entry.
_APPEND_CHANGE: dict[str, str] = {
    "characters": lifecycle.STALE_CHANGE_CHARACTERS_APPEND,
    "world_rules": lifecycle.STALE_CHANGE_WORLD_RULES_APPEND,
    "foreshadow": lifecycle.STALE_CHANGE_FORESHADOW_APPEND,
}


# ---------------------------------------------------------------------------
# Guard và helper nội bộ
# ---------------------------------------------------------------------------


def _require_foundation_type(artifact_type: str) -> None:
    if artifact_type not in FOUNDATION_TYPES:
        raise GuardError(
            f"`{artifact_type}` không phải foundation type; chỉ nhận {list(FOUNDATION_TYPES)}.",
            code="unknown_artifact_type",
            details={"artifact_type": artifact_type},
        )


def _collection_field(artifact_type: str) -> tuple[str, str, str]:
    _require_foundation_type(artifact_type)
    if artifact_type not in _ID_FIELDS:
        raise GuardError(
            f"`{artifact_type}` không dùng stable ID entry nên không có ID pool.",
            code="unknown_artifact_type",
            details={"artifact_type": artifact_type},
        )
    return _ID_FIELDS[artifact_type]


def _entity_ids(payload: Any, artifact_type: str) -> set[str]:
    collection, id_field, _prefix = _ID_FIELDS[artifact_type]
    return {
        str(getattr(item, id_field))
        for item in (getattr(payload, collection, None) or [])
    }


def foundation_entity_ids(project: Project, artifact_type: str) -> set[str]:
    """ID đã dùng trong accepted **và** candidate của một foundation artifact."""
    _collection_field(artifact_type)
    envelope = storage.load_artifact(project, artifact_type)
    ids: set[str] = set()
    if envelope is not None:
        for revision in (envelope.accepted_revision, envelope.candidate_revision):
            if revision is not None:
                ids |= _entity_ids(revision.payload, artifact_type)
    return ids


def _load_or_new(project: Project, artifact_type: str, *, now: str | None) -> ArtifactEnvelope[Any]:
    envelope = storage.load_artifact(project, artifact_type)
    if envelope is not None:
        return envelope
    return lifecycle.new_artifact(artifact_type, artifact_type, now=now)


def _build_foundation_bundle(
    project: Project,
    artifact_type: str,
    *,
    action: str,
    chapter_number: int,
    assigned_ids: Sequence[str],
    user_instruction: str,
) -> context_core.ContextBundle:
    try:
        return context_core.build_foundation_context(
            project,
            artifact_type,
            action=action,
            chapter_number=chapter_number,
            assigned_ids=assigned_ids,
            user_instruction=user_instruction,
        )
    except context_core.ContextError as exc:
        raise GuardError(str(exc), code=exc.code, details=dict(exc.details)) from exc


def _scope_issues(
    artifact_type: str, payload: Any, assigned_ids: Sequence[str]
) -> list[validation.ValidationIssue]:
    """Entry phải dùng ID backend cấp và payload không được rỗng (no-op)."""
    collector = validation.IssueCollector()
    collection, id_field, _prefix = _collection_field(artifact_type)
    items = list(getattr(payload, collection, None) or [])
    if not items:
        collector.add(
            "/payload",
            "empty_payload",
            "Output rỗng là no-op nên không được coi là đã thực hiện yêu cầu; "
            "cần generate lại với yêu cầu rõ hơn.",
        )
        return collector.issues
    allowed = {str(item) for item in assigned_ids}
    if allowed:
        for position, item in enumerate(items):
            value = str(getattr(item, id_field))
            if value not in allowed:
                collector.add(
                    validation.json_pointer("payload", collection, position, id_field),
                    "unassigned_stable_id",
                    f"ID `{value}` không nằm trong pool backend cấp {sorted(allowed)}.",
                )
    return collector.issues


def _accept_if_enabled(
    project: Project,
    envelope: ArtifactEnvelope[Any],
    *,
    artifact_type: str,
    result: validation.ValidationResult,
    now: str | None,
) -> tuple[ArtifactEnvelope[Any], bool]:
    if not auto_accept_enabled(project):
        return envelope, False
    scope = validation.validate_auto_accept_scope(
        auto_accept_structured=True, output_kind=artifact_type
    )
    if not scope.is_valid:
        return envelope, False
    accepted = lifecycle.accept_candidate(
        envelope, accepted_by="auto_accept", validation=result, now=now
    )
    return accepted, True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def auto_accept_enabled(project: Project) -> bool:
    """Auto Accept structured có bật cho project này không (D005)."""
    return bool(project.config.auto_accept_structured)


def assign_ids(project: Project, artifact_type: str, count: int) -> list[str]:
    """Reserve `count` stable ID mới cho Characters/World Rules/Foreshadow.

    Pool tránh trùng cả accepted **và** candidate hiện có; pool để dư, không bắt
    dùng hết.
    """
    collection, _id_field, prefix = _collection_field(artifact_type)
    del collection
    if count < 0:
        raise GuardError("`count` phải >= 0.", code="invalid_count", details={"count": count})
    if count == 0:
        return []
    return reserve_id_pool(prefix, count, foundation_entity_ids(project, artifact_type))


def generate(
    project: Project,
    *,
    client: Any,
    artifact_type: str,
    action: str = "generate",
    chapter_number: int = 1,
    assigned_ids: Sequence[str] | None = None,
    user_instruction: str = "",
    operation_id: str | None = None,
    now: str | None = None,
    on_event: EventSink | None = None,
    stream: bool = False,
    attempt: int = 1,
) -> ActionResult:
    """Generate/regenerate/edit một foundation artifact qua LLM.

    Guard chạy trước khi gọi LLM: Base Idea phải accepted; Characters/World
    Rules/Foreshadow cần thêm Premise accepted và không stale. Output luôn là
    candidate (hoặc accepted ngay nếu Auto Accept bật và output hợp lệ).
    Accepted revision cũ không bị ghi đè khi LLM lỗi hoặc output sai schema.
    `on_event` nhận `GenerationEvent` theo contract T33 (D019).
    """
    _require_foundation_type(artifact_type)
    if action not in _FOUNDATION_ACTIONS:
        raise GuardError(
            f"Action `{action}` không hợp lệ; chỉ nhận {sorted(_FOUNDATION_ACTIONS)}.",
            code="invalid_action",
        )
    if chapter_number < 1:
        raise GuardError("`chapter_number` phải >= 1.", code="invalid_chapter_number")
    op_id = operation_id or generate_operation_id()
    warnings: list[str] = []
    emitter = (
        GenerationEmitter(
            operation_id=op_id,
            action=f"architect.{artifact_type}.{action}",
            sink=on_event,
            attempt=attempt,
            prompt_id=_FOUNDATION_PROMPT_IDS[artifact_type],
            artifact_id=artifact_type,
        )
        if on_event is not None
        else None
    )

    require_base_idea(project)
    if artifact_type != "premise":
        require_accepted_artifact(project, "premise")

    envelope = _load_or_new(project, artifact_type, now=now)
    if action == "edit" and envelope.accepted_revision is None:
        raise GuardError(
            f"Action `edit` cần accepted `{artifact_type}` làm bản gốc.",
            code="missing_dependency",
            details={"artifact_id": artifact_type},
        )

    ids: list[str] = []
    if artifact_type != "premise":
        if assigned_ids is None:
            ids = assign_ids(project, artifact_type, DEFAULT_ASSIGNED_ID_POOL)
            warnings.append(
                "Không có `assigned_ids`: backend reserve pool mặc định "
                f"{ids}; entry ngoài pool sẽ bị từ chối."
            )
        else:
            ids = [str(item) for item in assigned_ids]

    bundle = _build_foundation_bundle(
        project,
        artifact_type,
        action=action,
        chapter_number=chapter_number,
        assigned_ids=ids,
        user_instruction=user_instruction,
    )
    call: StructuredCall = complete_json(
        project,
        client=client,
        prompt_id=_FOUNDATION_PROMPT_IDS[artifact_type],
        bundle=bundle,
        operation_id=op_id,
        now=now,
        label=artifact_type,
        emitter=emitter,
        stream=stream,
    )
    with generation_stage(emitter, detail=f"Đang parse/validate `{artifact_type}`."):
        parsed = parse_structured_or_fail(
            project,
            call=call,
            model_cls=ARTIFACT_PAYLOAD_MODELS[artifact_type],
            operation_id=op_id,
            now=now,
            label=artifact_type,
            artifact_id=artifact_type,
        )
        result = validate_or_fail(
            project,
            artifact_type=artifact_type,
            payload=parsed,
            context=validation.ValidationContext(index=foundation_reference_index(project)),
            operation_id=op_id,
            now=now,
            label=artifact_type,
            raw_output_ref=call.raw_ref,
            artifact_id=artifact_type,
            extra_issues=_scope_issues(artifact_type, parsed, ids)
            if artifact_type != "premise"
            else (),
        )

    envelope = lifecycle.set_candidate(
        envelope,
        parsed,
        source=payload_source(call, operation_id=op_id),
        dependency_pins=bundle.dependency_pins,
        validation=result,
        now=now,
    )
    envelope, auto_accepted = _accept_if_enabled(
        project, envelope, artifact_type=artifact_type, result=result, now=now
    )
    storage.save_artifact(project, envelope, operation_id=op_id)
    if emitter is not None:
        emitter.saved(
            detail=(
                f"Đã auto accept `{artifact_type}` (validate xong theo config)."
                if auto_accepted
                else f"Candidate `{artifact_type}` đã lưu (chưa accept)."
            ),
            raw_ref=call.raw_ref,
        )

    revision = (
        envelope.accepted_revision.revision
        if envelope.accepted_revision is not None
        else (envelope.candidate_revision.revision if envelope.candidate_revision else None)
    )
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_type,
        message=(
            f"Đã tạo candidate `{artifact_type}` r{revision}."
            if not auto_accepted
            else f"Auto Accept đã accept `{artifact_type}` r{revision}."
        ),
        warnings=warnings,
        data={
            "status": envelope.status.value,
            "revision": revision,
            "auto_accepted": auto_accepted,
            "assigned_ids": ids,
            "action": action,
            "raw_output_ref": call.raw_ref,
            "context_id": bundle.context_id,
        },
        validation=result,
    )


def edit_candidate(
    project: Project,
    *,
    artifact_type: str,
    payload: Mapping[str, Any] | BaseModel,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """User sửa tay payload thành candidate mới (không tự accept).

    Payload được parse + validate cross-field; lỗi raise `ValidationFailure` và
    không ghi candidate. Pin được refresh theo accepted revision hiện tại vì đây
    là nội dung do user tạo tại thời điểm này.
    """
    _require_foundation_type(artifact_type)
    op_id = operation_id or generate_operation_id()
    model_cls = ARTIFACT_PAYLOAD_MODELS[artifact_type]
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    try:
        parsed = model_cls.model_validate(data)
    except ValidationError as exc:
        issues = validation.issues_from_pydantic_error(exc, base_path="/payload")
        raise ValidationFailure(
            f"Payload `{artifact_type}` do user sửa không đúng contract: "
            f"{validation.summarize_errors(validation.result_from_issues(issues))}",
            result=validation.result_from_issues(issues),
            details={"artifact_id": artifact_type},
        ) from exc

    result = validate_or_fail(
        project,
        artifact_type=artifact_type,
        payload=parsed,
        context=validation.ValidationContext(index=foundation_reference_index(project)),
        operation_id=op_id,
        now=now,
        label=f"{artifact_type}.edit",
        artifact_id=artifact_type,
    )
    envelope = _load_or_new(project, artifact_type, now=now)
    envelope = lifecycle.set_candidate(
        envelope,
        parsed,
        source=PayloadSource(source_type=SourceType.user, operation_id=op_id),
        dependency_pins=current_dependency_pins(project),
        validation=result,
        now=now,
    )
    storage.save_artifact(project, envelope, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_type,
        message=f"Đã lưu candidate `{artifact_type}` do user sửa (chưa accept).",
        data={
            "status": envelope.status.value,
            "revision": envelope.candidate_revision.revision if envelope.candidate_revision else None,
        },
        validation=result,
    )


def accept(
    project: Project,
    *,
    artifact_type: str,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Accept candidate foundation sau khi validate lại schema/ID/scope và pin.

    Candidate tạo từ dependency revision cũ bị từ chối bằng
    `StaleDependencyError` (không merge). Gọi lại với **cùng** `operation_id`
    sau khi đã accept là no-op idempotent, không sinh revision thứ hai.
    """
    _require_foundation_type(artifact_type)
    op_id = operation_id or generate_operation_id()
    envelope = storage.load_artifact(project, artifact_type)
    if envelope is None:
        raise GuardError(
            f"Chưa có artifact `{artifact_type}` trong project.",
            code="missing_artifact",
            details={"artifact_id": artifact_type},
        )

    had_previous = envelope.accepted_revision is not None
    candidate = envelope.candidate_revision
    if candidate is None:
        same_source = (
            bool(operation_id)
            and envelope.accepted_revision is not None
            and envelope.accepted_revision.payload_source.operation_id == operation_id
        )
        if envelope.accepted_revision is not None and (
            same_source
            or artifact_write_committed(project, operation_id, artifact_type, artifact_type)
        ):
            return ActionResult(
                operation_id=op_id,
                artifact_id=artifact_type,
                message=(
                    f"Candidate `{artifact_type}` đã được accept trước đó cho cùng "
                    "`operation_id`; không tạo revision mới."
                ),
                data={
                    "status": envelope.status.value,
                    "revision": envelope.accepted_revision.revision,
                    "idempotent": True,
                },
                validation=envelope.accepted_revision.validation,
            )
        raise GuardError(
            f"`{artifact_type}` không có candidate để accept.",
            code="missing_candidate",
            details={"artifact_id": artifact_type},
        )

    check_pin_freshness(project, candidate.dependency_pins, artifact_id=artifact_type)
    result = validate_or_fail(
        project,
        artifact_type=artifact_type,
        payload=candidate.payload,
        context=validation.ValidationContext(index=foundation_reference_index(project)),
        operation_id=op_id,
        now=now,
        label=f"{artifact_type}.accept",
        raw_output_ref=candidate.payload_source.raw_output_ref,
        artifact_id=artifact_type,
    )

    accepted = lifecycle.accept_candidate(
        envelope, accepted_by=accepted_by, validation=result, now=now
    )
    try:
        storage.save_artifact(project, accepted, operation_id=op_id)
    except storage.StaleCandidateError as exc:
        raise StaleDependencyError(
            f"`{artifact_type}` bị từ chối khi ghi accepted mới: {exc}",
            code="stale_dependency",
            details={"artifact_id": artifact_type},
        ) from exc

    marked: list[str] = []
    if artifact_type == "premise" and had_previous and accepted.accepted_revision is not None:
        marked = lifecycle.mark_downstream_stale(
            project,
            source_artifact_id="premise",
            source_revision=accepted.accepted_revision.revision,
            change=lifecycle.STALE_CHANGE_PREMISE_REVISE,
            now=now,
        )

    revision = accepted.accepted_revision.revision if accepted.accepted_revision else None
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_type,
        message=f"Đã accept `{artifact_type}` r{revision}.",
        data={
            "status": accepted.status.value,
            "revision": revision,
            "accepted_by": accepted_by,
            "stale_marked": marked,
        },
        validation=result,
    )


def reject(
    project: Project,
    *,
    artifact_type: str,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Reject candidate; accepted revision cũ giữ nguyên (không xóa dữ liệu)."""
    _require_foundation_type(artifact_type)
    op_id = operation_id or generate_operation_id()
    envelope = storage.load_artifact(project, artifact_type)
    if envelope is None or envelope.candidate_revision is None:
        raise GuardError(
            f"`{artifact_type}` không có candidate để reject.",
            code="missing_candidate",
            details={"artifact_id": artifact_type},
        )
    rejected = lifecycle.reject_candidate(envelope)
    storage.save_artifact(project, rejected, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_type,
        message=f"Đã reject candidate `{artifact_type}`; accepted cũ giữ nguyên.",
        data={
            "status": rejected.status.value,
            "revision": (
                rejected.accepted_revision.revision if rejected.accepted_revision else None
            ),
        },
    )


def append_entries(
    project: Project,
    *,
    artifact_type: str,
    payload: Mapping[str, Any] | BaseModel,
    effective_from_chapter: int,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Append entry mới vào foundation accepted với `effective_from_chapter`.

    Entry mới nhận đúng `effective_from_chapter` của action (không suy từ payload)
    và giữ stable ID do caller cấp. Entry hiệu lực **tương lai** không làm stale
    artifact/chapter có as-of nhỏ hơn; nếu hiệu lực <= `current_chapter` thì
    downstream liên quan được đánh dấu stale (chỉ đánh dấu, không rewrite).
    """
    collection, id_field, _prefix = _collection_field(artifact_type)
    if effective_from_chapter < 1:
        raise GuardError(
            "`effective_from_chapter` phải >= 1.", code="invalid_effective_chapter"
        )
    op_id = operation_id or generate_operation_id()
    envelope = require_accepted_artifact(project, artifact_type)
    accepted_revision = envelope.accepted_revision
    assert accepted_revision is not None  # require_accepted_artifact đã kiểm tra
    if (
        operation_id
        and accepted_revision.payload_source.operation_id == operation_id
    ) or artifact_write_committed(project, operation_id, artifact_type, artifact_type):
        return ActionResult(
            operation_id=op_id,
            artifact_id=artifact_type,
            message=(
                f"Append cho `{artifact_type}` đã được ghi trước đó với cùng "
                "`operation_id`; không merge trùng."
            ),
            data={
                "status": envelope.status.value,
                "revision": accepted_revision.revision,
                "idempotent": True,
            },
            validation=accepted_revision.validation,
        )

    model_cls = ARTIFACT_PAYLOAD_MODELS[artifact_type]
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    try:
        parsed = model_cls.model_validate(data)
    except ValidationError as exc:
        issues = validation.issues_from_pydantic_error(exc, base_path="/payload")
        raise ValidationFailure(
            f"Payload append `{artifact_type}` không đúng contract: "
            f"{validation.summarize_errors(validation.result_from_issues(issues))}",
            result=validation.result_from_issues(issues),
            details={"artifact_id": artifact_type},
        ) from exc

    existing_items = list(getattr(accepted_revision.payload, collection) or [])
    existing_ids = {str(getattr(item, id_field)) for item in existing_items}
    new_items = list(getattr(parsed, collection) or [])
    extra: list[validation.ValidationIssue] = []
    if not new_items:
        extra.append(
            validation.ValidationIssue(
                path="/payload",
                code="empty_payload",
                message="Append không có entry mới nào nên là no-op.",
            )
        )
    for position, item in enumerate(new_items):
        value = str(getattr(item, id_field))
        if value in existing_ids:
            extra.append(
                validation.ValidationIssue(
                    path=validation.json_pointer("payload", collection, position, id_field),
                    code="duplicate_stable_id",
                    message=(
                        f"ID `{value}` đã có trong accepted `{artifact_type}`; "
                        "append chỉ nhận entry mới."
                    ),
                )
            )

    forced_items = [
        item.model_copy(update={"effective_from_chapter": effective_from_chapter})
        for item in new_items
    ]
    merged = accepted_revision.payload.model_copy(
        update={collection: [*existing_items, *forced_items]}
    )
    result = validate_or_fail(
        project,
        artifact_type=artifact_type,
        payload=merged,
        context=validation.ValidationContext(index=foundation_reference_index(project)),
        operation_id=op_id,
        now=now,
        label=f"{artifact_type}.append",
        artifact_id=artifact_type,
        extra_issues=extra,
    )

    staged = lifecycle.set_candidate(
        envelope,
        merged,
        source=PayloadSource(source_type=SourceType.user, operation_id=op_id),
        dependency_pins=current_dependency_pins(project),
        validation=result,
        now=now,
    )
    accepted = lifecycle.accept_candidate(staged, accepted_by="user", validation=result, now=now)
    storage.save_artifact(project, accepted, operation_id=op_id)

    marked: list[str] = []
    if effective_from_chapter <= project.config.current_chapter:
        marked = lifecycle.mark_downstream_stale(
            project,
            source_artifact_id=artifact_type,
            source_revision=accepted.accepted_revision.revision if accepted.accepted_revision else 1,
            change=_APPEND_CHANGE[artifact_type],
            now=now,
        )

    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_type,
        message=(
            f"Đã append {len(forced_items)} entry vào `{artifact_type}` "
            f"(effective_from_chapter={effective_from_chapter})."
        ),
        data={
            "status": accepted.status.value,
            "revision": accepted.accepted_revision.revision if accepted.accepted_revision else None,
            "appended_ids": [str(getattr(item, id_field)) for item in forced_items],
            "effective_from_chapter": effective_from_chapter,
            "stale_marked": marked,
        },
        validation=result,
    )
