"""Long Plan service (T14, sửa theo horizon contract T30).

Hiện thực action Long Plan theo `docs/design/workflow.md` mục 4.2 và
`docs/design/schemas.md` mục 3.1/3.3/3.4:

- `generate`: Volume/Arc candidate từ LLM; guard Base Idea + Premise accepted và
  foundation không stale **trước** khi gọi LLM;
- `accept`: validate lại toàn payload + FK + coverage theo `planning_scope` đã lưu
  trên candidate, kiểm tra freshness pin, rồi đánh dấu stale Short Plan/Skeleton/
  draft/review liên quan (chỉ đánh dấu, không rewrite). Không đụng
  timeline/relationship;
- `reject`: bỏ candidate, giữ accepted cũ;
- `confirm_planning_scope`: action tường minh để gán horizon cho accepted revision
  legacy (D017) — không tự chạy khi mở project.

Contract horizon (D017, T29):

- `planning_scope = {start, end}` là **toàn horizon cần kiến trúc**, không phải
  kích thước arc và không phải edit window. Nó là metadata app-owned trên
  `ArtifactRevision.planning_scope`, không nằm trong payload LLM trả.
- Horizon **không** được suy từ `current_chapter`, Short Plan hay chapter metadata.
  `generate` cần input tường minh; `regenerate`/`edit` dùng scope đã lưu.
- Candidate phải phủ đúng horizon (không gap/overlap/out-of-scope, đủ hai đầu).
  Cùng một hàm validate chạy cho generate/regenerate/edit/accept/auto accept.
- Số volume/arc không có quota: một arc phủ đúng horizon vẫn hợp lệ về cấu trúc;
  service chỉ trả **warning** non-blocking khi cả plan có một arc.

Pool `vol_`/`arc_` được reserve dư (mặc định 2 volume, 6 arc); LLM có thể dùng
`tmp_volume_<n>`/`tmp_arc_<n>` cho entry mới, backend map sang stable ID trước
validation/accept và lưu mapping trong `PayloadSource.id_map` (D014).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from novel_ai.core import context as context_core
from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.generation import EventSink, GenerationEmitter
from novel_ai.core.models import (
    ARTIFACT_PAYLOAD_MODELS,
    ArtifactEnvelope,
    LongPlanPayload,
    PayloadSource,
    PlanningScope,
    SourceType,
    generate_operation_id,
    is_temporary_id,
    next_stable_id,
    reserve_id_pool,
    temporary_id_kind,
)
from novel_ai.core.project import Project
from novel_ai.services import (
    ActionResult,
    GuardError,
    ValidationFailure,
    StaleDependencyError,
)
from novel_ai.services.co_create import (
    artifact_write_committed,
    current_dependency_pins,
    check_pin_freshness,
    complete_json,
    foundation_reference_index,
    generation_stage,
    parse_structured_or_fail,
    payload_source,
    require_accepted_artifact,
    require_base_idea,
    validate_or_fail,
)

__all__ = [
    "accept",
    "assign_ids",
    "edit_candidate",
    "confirm_planning_scope",
    "generate",
    "reject",
    "resolve_planning_scope",
    "single_arc_warning",
    "stored_planning_scope",
]

LONG_PLAN_ARTIFACT_ID = "long_plan"
LONG_PLAN_PROMPT_ID = "long_plan.v1"

#: Pool ID reserve khi generate. Đây là **nguồn ID**, không phải chỉ tiêu phải dùng
#: hết và không phải quota số volume/arc: pool đủ rộng để horizon lớn (nhiều
#: volume/arc) vẫn có stable ID backend cấp. Nếu vẫn thiếu, model dùng ID cục bộ
#: `tmp_volume_<n>`/`tmp_arc_<n>` và backend map sang stable ID mới (D014, T30).
DEFAULT_VOLUME_ID_POOL = 12
DEFAULT_ARC_ID_POOL = 36



# ---------------------------------------------------------------------------
# Helper nội bộ
# ---------------------------------------------------------------------------


def _envelope_or_new(project: Project, *, now: str | None) -> ArtifactEnvelope[Any]:
    envelope = storage.load_artifact(project, LONG_PLAN_ARTIFACT_ID)
    if envelope is not None:
        return envelope
    return lifecycle.new_artifact(LONG_PLAN_ARTIFACT_ID, LONG_PLAN_ARTIFACT_ID, now=now)


def _payloads(project: Project) -> list[Any]:
    envelope = storage.load_artifact(project, LONG_PLAN_ARTIFACT_ID)
    if envelope is None:
        return []
    return [
        revision.payload
        for revision in (envelope.accepted_revision, envelope.candidate_revision)
        if revision is not None
    ]


def _existing_volume_ids(project: Project) -> set[str]:
    return {
        volume.volume_id
        for payload in _payloads(project)
        for volume in getattr(payload, "volumes", []) or []
    }


def _existing_arc_ids(project: Project) -> set[str]:
    return {
        arc.arc_id
        for payload in _payloads(project)
        for volume in getattr(payload, "volumes", []) or []
        for arc in volume.arcs
    }


def stored_planning_scope(project: Project) -> dict[str, int] | None:
    """Horizon đã lưu trên candidate hoặc accepted revision; `None` = legacy.

    Dùng cho UI prefill và cho `regenerate`/`edit`. Hàm chỉ đọc: nó **không** suy
    horizon từ progress, Short Plan hay chapter metadata (D017).
    """
    envelope = storage.load_artifact(project, LONG_PLAN_ARTIFACT_ID)
    if envelope is None:
        return None
    for revision in (envelope.candidate_revision, envelope.accepted_revision):
        if revision is not None and revision.planning_scope is not None:
            return {
                "start": int(revision.planning_scope.start),
                "end": int(revision.planning_scope.end),
            }
    return None


def _coerce_scope(planning_scope: Mapping[str, Any]) -> PlanningScope:
    start = int(planning_scope["start"])
    end = int(planning_scope["end"])
    if start < 1 or end < start:
        raise GuardError(
            f"`planning_scope` không hợp lệ: {{'start': {start}, 'end': {end}}}; "
            "cần 1 <= start <= end.",
            code="invalid_planning_scope",
            details={"start": start, "end": end},
        )
    return PlanningScope(start=start, end=end)


def resolve_planning_scope(
    project: Project,
    planning_scope: Mapping[str, Any] | None = None,
    *,
    action: str = "generate",
) -> PlanningScope:
    """Chốt `planning_scope` cho lần generate/regenerate/edit này (D017).

    - Caller truyền scope tường minh ⇒ dùng scope đó (kiểm `1 <= start <= end`);
    - Không truyền và action là `regenerate`/`edit` ⇒ dùng horizon đã lưu;
    - Không truyền ở `generate` (hoặc revision legacy không có scope) ⇒
      `GuardError` `missing_planning_scope`.

    Horizon **không** bao giờ được suy từ `current_chapter`, Short Plan hay chapter
    metadata: đó chính là lỗi BUG-004.
    """
    if planning_scope is not None:
        return _coerce_scope(planning_scope)

    stored = stored_planning_scope(project)
    if stored is not None:
        return _coerce_scope(stored)
    raise GuardError(
        "Thiếu `planning_scope`: cần chọn rõ horizon cần kiến trúc (start..end) "
        "trước khi lập Long Plan. App không suy horizon từ tiến độ viết.",
        code="missing_planning_scope",
        details={"action": action},
    )


def single_arc_warning(payload: LongPlanPayload) -> str | None:
    """Warning non-blocking khi cả plan chỉ có một arc (D017, `schemas.md` 11.3).

    Đây **không** phải validator và không phải quota: một arc phủ đúng horizon vẫn
    hợp lệ về cấu trúc. Warning chỉ nhắc người dùng rằng plan chưa thể hiện phân rã
    nhiều phase, và không đổi Auto Accept.
    """
    arcs = [arc for volume in payload.volumes for arc in volume.arcs]
    if len(arcs) > 1:
        return None
    return (
        "Long Plan chỉ có một arc cho toàn horizon: kiểm tra lại phân rã theo chuyển biến "
        "truyện nếu horizon bao gồm nhiều phase. Đây là cảnh báo, không phải lỗi cấu trúc."
    )


def _allocate_stable_id(prefix: str, used: set[str], pool: Iterator[str]) -> str:
    for value in pool:
        if value not in used:
            used.add(value)
            return value
    value = next_stable_id(prefix, used)
    used.add(value)
    return value


def map_temporary_ids(
    payload: LongPlanPayload,
    *,
    volume_pool: Sequence[str],
    arc_pool: Sequence[str],
    used_volume_ids: set[str] | None = None,
    used_arc_ids: set[str] | None = None,
) -> tuple[LongPlanPayload, dict[str, str], list[validation.ValidationIssue]]:
    """Map `tmp_volume_<n>`/`tmp_arc_<n>` sang stable ID backend reserve.

    Chỉ map ở vị trí định danh của entry; ID thật được giữ nguyên. Trả kèm danh
    sách issue nếu ID tạm sai kind hoặc bị lặp.
    """
    used_volumes = set(used_volume_ids or ())
    used_arcs = set(used_arc_ids or ())
    volume_iter = iter(volume_pool)
    arc_iter = iter(arc_pool)
    id_map: dict[str, str] = {}
    issues: list[validation.ValidationIssue] = []
    volumes = []
    for volume_position, volume in enumerate(payload.volumes):
        volume_id = volume.volume_id
        if is_temporary_id(volume_id):
            if temporary_id_kind(volume_id) != "volume":
                issues.append(
                    validation.ValidationIssue(
                        path=validation.json_pointer("payload", "volumes", volume_position, "volume_id"),
                        code="invalid_temporary_id",
                        message=f"`{volume_id}` không phải ID tạm của volume.",
                    )
                )
            elif volume_id in id_map:
                issues.append(
                    validation.ValidationIssue(
                        path=validation.json_pointer("payload", "volumes", volume_position, "volume_id"),
                        code="duplicate_temporary_id",
                        message=f"ID tạm `{volume_id}` bị dùng hai lần.",
                    )
                )
            else:
                new_id = _allocate_stable_id("vol", used_volumes, volume_iter)
                id_map[volume_id] = new_id
                volume_id = new_id
        else:
            used_volumes.add(volume_id)
        arcs = []
        for arc_position, arc in enumerate(volume.arcs):
            arc_id = arc.arc_id
            if is_temporary_id(arc_id):
                if temporary_id_kind(arc_id) != "arc":
                    issues.append(
                        validation.ValidationIssue(
                            path=validation.json_pointer(
                                "payload", "volumes", volume_position, "arcs", arc_position, "arc_id"
                            ),
                            code="invalid_temporary_id",
                            message=f"`{arc_id}` không phải ID tạm của arc.",
                        )
                    )
                elif arc_id in id_map:
                    issues.append(
                        validation.ValidationIssue(
                            path=validation.json_pointer(
                                "payload", "volumes", volume_position, "arcs", arc_position, "arc_id"
                            ),
                            code="duplicate_temporary_id",
                            message=f"ID tạm `{arc_id}` bị dùng hai lần.",
                        )
                    )
                else:
                    new_id = _allocate_stable_id("arc", used_arcs, arc_iter)
                    id_map[arc_id] = new_id
                    arc_id = new_id
            else:
                used_arcs.add(arc_id)
            arcs.append(arc.model_copy(update={"arc_id": arc_id}))
        volumes.append(volume.model_copy(update={"volume_id": volume_id, "arcs": arcs}))
    return payload.model_copy(update={"volumes": volumes}), id_map, issues


def horizon_issues(
    payload: LongPlanPayload, *, scope: Mapping[str, int] | None
) -> list[validation.ValidationIssue]:
    """Coverage/structural issues của payload theo horizon (D017).

    Bọc `validation.long_plan_horizon_issues` để service và validator dùng đúng một
    luật. `scope=None` chỉ kiểm phần không phụ thuộc horizon (payload/volume rỗng,
    range sai) — dùng khi chỉ đọc payload mà chưa có horizon.
    """
    return validation.long_plan_horizon_issues(payload, scope)


def _id_scope_issues(
    payload: LongPlanPayload,
    *,
    volume_pool: Sequence[str],
    arc_pool: Sequence[str],
    existing_volume_ids: set[str],
    existing_arc_ids: set[str],
    mapped_volume_ids: set[str] = frozenset(),
    mapped_arc_ids: set[str] = frozenset(),
) -> list[validation.ValidationIssue]:
    """Volume/arc ID phải thuộc pool backend cấp, ID cũ của plan, hoặc ID vừa được
    backend map từ ID cục bộ `tmp_*` (pool cạn vẫn phải dùng được — T30)."""
    allowed_volumes = {*volume_pool, *existing_volume_ids, *mapped_volume_ids}
    allowed_arcs = {*arc_pool, *existing_arc_ids, *mapped_arc_ids}
    collector = validation.IssueCollector()
    for volume_position, volume in enumerate(payload.volumes):
        if volume.volume_id not in allowed_volumes:
            collector.add(
                validation.json_pointer("payload", "volumes", volume_position, "volume_id"),
                "unassigned_stable_id",
                f"`{volume.volume_id}` không nằm trong assigned_volume_ids "
                f"{sorted(volume_pool)} và không phải ID cũ của plan.",
            )
        for arc_position, arc in enumerate(volume.arcs):
            if arc.arc_id not in allowed_arcs:
                collector.add(
                    validation.json_pointer(
                        "payload", "volumes", volume_position, "arcs", arc_position, "arc_id"
                    ),
                    "unassigned_stable_id",
                    f"`{arc.arc_id}` không nằm trong assigned_arc_ids "
                    f"{sorted(arc_pool)} và không phải ID cũ của plan.",
                )
    return collector.issues


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(
    project: Project,
    *,
    client: Any,
    planning_scope: Mapping[str, Any] | None = None,
    action: str = "generate",
    user_instruction: str = "",
    operation_id: str | None = None,
    now: str | None = None,
    on_event: EventSink | None = None,
    stream: bool = False,
    attempt: int = 1,
) -> ActionResult:
    """Generate/regenerate Long Plan candidate.

    Guard: Base Idea accepted + Premise accepted và không stale; `planning_scope`
    rõ ràng (tường minh hoặc đã lưu) và pool ID/context được chốt **trước** khi
    gọi LLM. Output luôn là candidate (hoặc accepted nếu Auto Accept bật và payload
    hợp lệ theo đúng horizon). `on_event` nhận `GenerationEvent` (T33/D019).
    """
    if action not in {"generate", "regenerate", "edit"}:
        raise GuardError(
            "Action Long Plan chỉ nhận generate/regenerate/edit.", code="invalid_action"
        )
    op_id = operation_id or generate_operation_id()
    require_base_idea(project)
    require_accepted_artifact(project, "premise")
    scope = resolve_planning_scope(project, planning_scope, action=action)
    scope_dict = {"start": scope.start, "end": scope.end}
    emitter = (
        GenerationEmitter(
            operation_id=op_id,
            action=f"long_plan.{action}",
            sink=on_event,
            attempt=attempt,
            prompt_id=LONG_PLAN_PROMPT_ID,
            artifact_id=LONG_PLAN_ARTIFACT_ID,
        )
        if on_event is not None
        else None
    )

    volume_pool = reserve_id_pool("vol", DEFAULT_VOLUME_ID_POOL, _existing_volume_ids(project))
    arc_pool = reserve_id_pool("arc", DEFAULT_ARC_ID_POOL, _existing_arc_ids(project))

    try:
        bundle = context_core.build_long_plan_context(
            project,
            planning_scope=scope_dict,
            assigned_volume_ids=volume_pool,
            assigned_arc_ids=arc_pool,
            action=action,
            user_instruction=user_instruction,
        )
    except context_core.ContextError as exc:
        raise GuardError(str(exc), code=exc.code, details=dict(exc.details)) from exc

    call = complete_json(
        project,
        client=client,
        prompt_id=LONG_PLAN_PROMPT_ID,
        bundle=bundle,
        operation_id=op_id,
        now=now,
        label="long_plan",
        emitter=emitter,
        stream=stream,
    )
    with generation_stage(emitter, detail="Đang parse/validate Long Plan."):
        parsed = parse_structured_or_fail(
            project,
            call=call,
            model_cls=ARTIFACT_PAYLOAD_MODELS["long_plan"],
            operation_id=op_id,
            now=now,
            label="long_plan",
            artifact_id=LONG_PLAN_ARTIFACT_ID,
        )
        parsed, id_map, mapping_issues = map_temporary_ids(
            parsed,
            volume_pool=volume_pool,
            arc_pool=arc_pool,
            used_volume_ids=_existing_volume_ids(project),
            used_arc_ids=_existing_arc_ids(project),
        )
        extra_issues = [
            *mapping_issues,
            *horizon_issues(parsed, scope=scope_dict),
            *_id_scope_issues(
                parsed,
                volume_pool=volume_pool,
                arc_pool=arc_pool,
                existing_volume_ids=_existing_volume_ids(project),
                existing_arc_ids=_existing_arc_ids(project),
                mapped_volume_ids={
                    new_id for old_id, new_id in id_map.items()
                    if temporary_id_kind(old_id) == "volume"
                },
                mapped_arc_ids={
                    new_id for old_id, new_id in id_map.items()
                    if temporary_id_kind(old_id) == "arc"
                },
            ),
        ]
        result = validate_or_fail(
            project,
            artifact_type="long_plan",
            payload=parsed,
            context=validation.ValidationContext(index=foundation_reference_index(project)),
            operation_id=op_id,
            now=now,
            label="long_plan",
            raw_output_ref=call.raw_ref,
            artifact_id=LONG_PLAN_ARTIFACT_ID,
            extra_issues=extra_issues,
        )

    envelope = _envelope_or_new(project, now=now)
    envelope = lifecycle.set_candidate(
        envelope,
        parsed,
        source=payload_source(call, operation_id=op_id, id_map=id_map),
        dependency_pins=bundle.dependency_pins,
        validation=result,
        planning_scope=scope,
        now=now,
    )
    warnings = [message for message in (single_arc_warning(parsed),) if message]
    auto_accepted = False
    if project.config.auto_accept_structured:
        # Auto Accept theo config sau **full structural validation** ở trên; warning
        # one-arc không đổi hành vi này (D017 điểm 6–7).
        scope_result = validation.validate_auto_accept_scope(
            auto_accept_structured=True, output_kind="long_plan"
        )
        if scope_result.is_valid:
            envelope = lifecycle.accept_candidate(
                envelope, accepted_by="auto_accept", validation=result, now=now
            )
            auto_accepted = True
    storage.save_artifact(project, envelope, operation_id=op_id)
    if emitter is not None:
        emitter.saved(
            detail=(
                "Auto Accept đã accept Long Plan sau full validation."
                if auto_accepted
                else "Long Plan candidate đã validate và lưu (chưa accept)."
            ),
            raw_ref=call.raw_ref,
        )

    return ActionResult(
        operation_id=op_id,
        artifact_id=LONG_PLAN_ARTIFACT_ID,
        message=(
            "Auto Accept đã accept Long Plan."
            if auto_accepted
            else "Đã tạo Long Plan candidate (chưa accept)."
        ),
        data={
            "status": envelope.status.value,
            "planning_scope": scope_dict,
            "assigned_volume_ids": volume_pool,
            "assigned_arc_ids": arc_pool,
            "id_map": id_map,
            "auto_accepted": auto_accepted,
            "warnings": warnings,
            "raw_output_ref": call.raw_ref,
            "context_id": bundle.context_id,
        },
        validation=result,
    )


def assign_ids(
    project: Project, *, volume_count: int = DEFAULT_VOLUME_ID_POOL, arc_count: int = DEFAULT_ARC_ID_POOL
) -> dict[str, list[str]]:
    """Cấp pool ID mới cho entry thêm tay (T36) — app sở hữu stable ID.

    Trả `{"volume_ids": [...], "arc_ids": [...]}`; ID đã tồn tại trong plan hiện có
    bị loại khỏi pool.
    """
    return {
        "volume_ids": reserve_id_pool(
            "vol", max(1, int(volume_count)), _existing_volume_ids(project)
        ),
        "arc_ids": reserve_id_pool("arc", max(1, int(arc_count)), _existing_arc_ids(project)),
    }


def _temporary_ids_in(payload: LongPlanPayload) -> list[str]:
    """ID tạm (`tmp_volume_*`/`tmp_arc_*`) còn sót trong payload người dùng sửa."""
    leftovers: list[str] = []
    for volume in payload.volumes:
        if is_temporary_id(volume.volume_id):
            leftovers.append(volume.volume_id)
        for arc in volume.arcs:
            if is_temporary_id(arc.arc_id):
                leftovers.append(arc.arc_id)
    return sorted(set(leftovers))


def edit_candidate(
    project: Project,
    *,
    payload: Mapping[str, Any] | LongPlanPayload,
    planning_scope: Mapping[str, Any] | None = None,
    expected_revision: int | None = None,
    assigned_volume_ids: Sequence[str] = (),
    assigned_arc_ids: Sequence[str] = (),
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Lưu bản Long Plan do **người dùng sửa** thành candidate mới (T36).

    Không gọi LLM và **không** auto accept dù `auto_accept_structured` bật. Metadata
    (`status`, `revision`, pins, `planning_scope`) do app sở hữu: payload người dùng
    chỉ chứa nội dung; ID phải là stable ID đã có hoặc do `assign_ids` cấp.

    Guard đầy đủ trước khi ghi: schema, không còn ID tạm, `planning_scope` hợp lệ và
    coverage đúng horizon (D017), ID/FK resolve, và working copy không cũ hơn
    revision hiện tại (`expected_revision`). Mọi lỗi giữ nguyên candidate/accepted cũ.
    """
    op_id = operation_id or generate_operation_id()
    envelope = storage.load_artifact(project, LONG_PLAN_ARTIFACT_ID)
    if envelope is None or (
        envelope.accepted_revision is None and envelope.candidate_revision is None
    ):
        raise GuardError(
            "Chưa có Long Plan accepted/candidate để sửa; hãy generate trước.",
            code="missing_artifact",
            details={"artifact_id": LONG_PLAN_ARTIFACT_ID},
        )
    base_revision_obj = envelope.candidate_revision or envelope.accepted_revision
    assert base_revision_obj is not None
    if expected_revision is not None and int(expected_revision) != int(base_revision_obj.revision):
        raise GuardError(
            f"Working copy đang dựa trên revision {expected_revision} nhưng Long Plan hiện tại "
            f"là r{base_revision_obj.revision}; nạp lại rồi sửa tiếp (không ghi đè revision mới).",
            code="stale_working_copy",
            details={
                "expected_revision": int(expected_revision),
                "current_revision": int(base_revision_obj.revision),
            },
        )

    data = payload.model_dump(mode="json") if isinstance(payload, LongPlanPayload) else dict(payload)
    try:
        parsed = LongPlanPayload.model_validate(data)
    except ValidationError as exc:
        issues = validation.issues_from_pydantic_error(exc, base_path="/payload")
        raise ValidationFailure(
            "Long Plan do bạn sửa không đúng contract: "
            f"{validation.summarize_errors(validation.result_from_issues(issues))}",
            result=validation.result_from_issues(issues),
            code="invalid_payload",
            details={"artifact_id": LONG_PLAN_ARTIFACT_ID},
        ) from exc

    leftovers = _temporary_ids_in(parsed)
    if leftovers:
        raise ValidationFailure(
            "Bản sửa tay còn ID tạm ("
            + ", ".join(leftovers)
            + "); dùng `long_planner.assign_ids` để backend cấp stable ID.",
            code="temporary_id_not_allowed",
            details={"temporary_ids": leftovers},
        )

    stored_scope = base_revision_obj.planning_scope
    if planning_scope is not None:
        scope = _coerce_scope(planning_scope)
    elif stored_scope is not None:
        scope = PlanningScope(start=int(stored_scope.start), end=int(stored_scope.end))
    else:
        raise GuardError(
            "Long Plan này là legacy (không có `planning_scope`): cần xác nhận horizon trước "
            "khi sửa tay.",
            code="missing_planning_scope",
            details={"artifact_id": LONG_PLAN_ARTIFACT_ID},
        )
    scope_dict = {"start": scope.start, "end": scope.end}

    result = validation.validate_artifact_payload(
        "long_plan",
        parsed,
        context=validation.ValidationContext(index=foundation_reference_index(project)),
    )
    existing_volumes = _existing_volume_ids(project)
    existing_arcs = _existing_arc_ids(project)
    extra_issues = [
        *horizon_issues(parsed, scope=scope_dict),
        *_id_scope_issues(
            parsed,
            volume_pool=[str(item) for item in assigned_volume_ids],
            arc_pool=[str(item) for item in assigned_arc_ids],
            existing_volume_ids=existing_volumes,
            existing_arc_ids=existing_arcs,
        ),
    ]
    combined = validation.result_from_issues([*result.errors, *extra_issues])
    if not combined.is_valid:
        raise ValidationFailure(
            "Long Plan sửa tay không qua validation: " + validation.summarize_errors(combined),
            result=combined,
            code="validation_failed",
            details={"artifact_id": LONG_PLAN_ARTIFACT_ID},
        )

    updated = lifecycle.set_candidate(
        envelope,
        parsed,
        source=PayloadSource(source_type=SourceType.user, operation_id=op_id),
        dependency_pins=current_dependency_pins(project),
        validation=combined,
        planning_scope=scope,
        now=now,
    )
    storage.save_artifact(project, updated, operation_id=op_id)
    warnings = [message for message in (single_arc_warning(parsed),) if message]
    return ActionResult(
        operation_id=op_id,
        artifact_id=LONG_PLAN_ARTIFACT_ID,
        message="Đã lưu Long Plan candidate do bạn sửa (chưa accept; không gọi LLM).",
        warnings=warnings,
        data={
            "status": updated.status.value,
            "revision": updated.candidate_revision.revision
            if updated.candidate_revision
            else None,
            "planning_scope": scope_dict,
        },
        validation=combined,
    )


def accept(
    project: Project,
    *,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Accept Long Plan candidate.

    Validate FK trong authority + range, kiểm tra pin. Sau accept, Short Plan/
    Skeleton/draft/review liên quan bị đánh dấu stale qua
    `lifecycle.mark_downstream_stale`; timeline/relationship không bị đụng.
    """
    op_id = operation_id or generate_operation_id()
    envelope = storage.load_artifact(project, LONG_PLAN_ARTIFACT_ID)
    if envelope is None:
        raise GuardError("Chưa có Long Plan trong project.", code="missing_artifact")
    candidate = envelope.candidate_revision
    if candidate is None:
        same_source = (
            bool(operation_id)
            and envelope.accepted_revision is not None
            and envelope.accepted_revision.payload_source.operation_id == operation_id
        )
        if envelope.accepted_revision is not None and (
            same_source
            or artifact_write_committed(
                project, operation_id, LONG_PLAN_ARTIFACT_ID, LONG_PLAN_ARTIFACT_ID
            )
        ):
            return ActionResult(
                operation_id=op_id,
                artifact_id=LONG_PLAN_ARTIFACT_ID,
                message="Long Plan đã được accept trước đó cho cùng operation_id.",
                data={
                    "status": envelope.status.value,
                    "revision": envelope.accepted_revision.revision,
                    "idempotent": True,
                },
                validation=envelope.accepted_revision.validation,
            )
        raise GuardError("Long Plan không có candidate để accept.", code="missing_candidate")

    check_pin_freshness(project, candidate.dependency_pins, artifact_id=LONG_PLAN_ARTIFACT_ID)
    if candidate.planning_scope is None:
        raise GuardError(
            "Candidate Long Plan không có `planning_scope` (legacy): regenerate với horizon "
            "tường minh rồi accept lại. Không revalidate bằng scope suy diễn.",
            code="missing_planning_scope",
            details={
                "candidate_revision": candidate.revision,
                "next_step": "regenerate hoặc confirm_planning_scope cho accepted revision legacy",
            },
        )
    scope_dict = {
        "start": int(candidate.planning_scope.start),
        "end": int(candidate.planning_scope.end),
    }
    result = validate_or_fail(
        project,
        artifact_type="long_plan",
        payload=candidate.payload,
        context=validation.ValidationContext(index=foundation_reference_index(project)),
        operation_id=op_id,
        now=now,
        label="long_plan.accept",
        raw_output_ref=candidate.payload_source.raw_output_ref,
        artifact_id=LONG_PLAN_ARTIFACT_ID,
        extra_issues=horizon_issues(candidate.payload, scope=scope_dict),
    )
    accepted = lifecycle.accept_candidate(
        envelope, accepted_by=accepted_by, validation=result, now=now
    )
    try:
        storage.save_artifact(project, accepted, operation_id=op_id)
    except storage.StaleCandidateError as exc:
        raise StaleDependencyError(
            f"Long Plan bị từ chối khi ghi accepted mới: {exc}",
            code="stale_dependency",
        ) from exc

    revision = accepted.accepted_revision.revision if accepted.accepted_revision else 1
    marked = lifecycle.mark_downstream_stale(
        project,
        source_artifact_id=LONG_PLAN_ARTIFACT_ID,
        source_revision=revision,
        change=lifecycle.STALE_CHANGE_LONG_PLAN_REPLACE,
        now=now,
    )
    warnings = [message for message in (single_arc_warning(candidate.payload),) if message]
    return ActionResult(
        operation_id=op_id,
        artifact_id=LONG_PLAN_ARTIFACT_ID,
        message=f"Đã accept Long Plan r{revision}.",
        data={
            "status": accepted.status.value,
            "revision": revision,
            "accepted_by": accepted_by,
            "planning_scope": scope_dict,
            "warnings": warnings,
            "stale_marked": marked,
        },
        validation=result,
    )


def confirm_planning_scope(
    project: Project,
    *,
    start: int,
    end: int,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Gán horizon cho accepted revision **legacy** (D017, `storage.md` mục 13).

    Chỉ chạy khi user bấm action tường minh; mở project không bao giờ tự gọi hàm
    này. Idempotent: accepted revision đã có scope ⇒ no-op, không snapshot mới,
    không tăng revision. Nếu horizon không khớp coverage hiện có của payload thì
    từ chối và accepted giữ nguyên.
    """
    scope = _coerce_scope({"start": start, "end": end})
    envelope = storage.load_artifact(project, LONG_PLAN_ARTIFACT_ID)
    if envelope is None or envelope.accepted_revision is None:
        raise GuardError(
            "Chưa có Long Plan accepted để xác nhận horizon.", code="missing_artifact"
        )
    accepted_revision = envelope.accepted_revision
    if accepted_revision.planning_scope is not None:
        return ActionResult(
            operation_id=operation_id or generate_operation_id(),
            artifact_id=LONG_PLAN_ARTIFACT_ID,
            message="Long Plan accepted đã có `planning_scope`; không cần migrate.",
            data={
                "status": envelope.status.value,
                "revision": accepted_revision.revision,
                "planning_scope": {
                    "start": int(accepted_revision.planning_scope.start),
                    "end": int(accepted_revision.planning_scope.end),
                },
                "migrated": False,
                "idempotent": True,
            },
            validation=accepted_revision.validation,
        )

    scope_dict = {"start": scope.start, "end": scope.end}
    issues = horizon_issues(accepted_revision.payload, scope=scope_dict)
    if issues:
        raise GuardError(
            "Horizon xác nhận không khớp coverage của Long Plan accepted; "
            "regenerate plan cho horizon đó thay vì migrate.",
            code="scope_does_not_cover_payload",
            details={
                "planning_scope": scope_dict,
                "errors": [issue.model_dump(mode="json") for issue in issues],
            },
        )
    op_id = operation_id or generate_operation_id()
    updated = envelope.model_copy(deep=True)
    updated.accepted_revision = accepted_revision.model_copy(
        update={"planning_scope": scope}
    )
    storage.save_artifact(project, updated, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=LONG_PLAN_ARTIFACT_ID,
        message=(
            f"Đã xác nhận horizon {scope.start}–{scope.end} cho Long Plan accepted "
            f"r{accepted_revision.revision} (payload không đổi)."
        ),
        data={
            "status": updated.status.value,
            "revision": accepted_revision.revision,
            "planning_scope": scope_dict,
            "migrated": True,
        },
        validation=accepted_revision.validation,
    )


def reject(
    project: Project,
    *,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Reject Long Plan candidate; accepted cũ giữ nguyên."""
    op_id = operation_id or generate_operation_id()
    envelope = storage.load_artifact(project, LONG_PLAN_ARTIFACT_ID)
    if envelope is None or envelope.candidate_revision is None:
        raise GuardError("Long Plan không có candidate để reject.", code="missing_candidate")
    rejected = lifecycle.reject_candidate(envelope)
    storage.save_artifact(project, rejected, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=LONG_PLAN_ARTIFACT_ID,
        message="Đã reject Long Plan candidate; accepted cũ giữ nguyên.",
        data={
            "status": rejected.status.value,
            "revision": rejected.accepted_revision.revision if rejected.accepted_revision else None,
        },
    )
