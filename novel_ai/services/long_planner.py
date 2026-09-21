"""Long Plan service (T14).

Hiện thực action Long Plan theo `docs/design/workflow.md` mục 4.2 và
`docs/design/schemas.md` mục 3.1:

- `generate`: Volume/Arc candidate từ LLM; guard Base Idea + Premise accepted và
  foundation không stale **trước** khi gọi LLM;
- `accept`: validate lại toàn payload + FK + range, kiểm tra freshness pin, rồi
  đánh dấu stale Short Plan/Skeleton/draft/review liên quan (chỉ đánh dấu, không
  rewrite). Không đụng timeline/relationship;
- `reject`: bỏ candidate, giữ accepted cũ.

Quyết định phát sinh (đã ghi ở bàn giao T14):

- `planning_scope` mặc định được suy theo `resolve_planning_scope`: `start = 1`,
  `end = max(3, current_chapter, arc range end lớn nhất của Long Plan accepted,
  chapter_number lớn nhất của Short Plan accepted/chapter metadata)`. Caller có
  thể truyền scope tường minh; scope luôn được kiểm tra `1 <= start <= end`.
- Pool `vol_`/`arc_` được reserve dư (mặc định 2 volume, 6 arc); LLM có thể dùng
  `tmp_volume_<n>`/`tmp_arc_<n>` cho entry mới, backend map sang stable ID trước
  validation/accept và lưu mapping trong `PayloadSource.id_map` (D014).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from novel_ai.core import context as context_core
from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.models import (
    ARTIFACT_PAYLOAD_MODELS,
    ArtifactEnvelope,
    LongPlanPayload,
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
    StaleDependencyError,
)
from novel_ai.services.co_create import (
    artifact_write_committed,
    check_pin_freshness,
    complete_json,
    foundation_reference_index,
    parse_structured_or_fail,
    payload_source,
    require_accepted_artifact,
    require_base_idea,
    validate_or_fail,
)

__all__ = ["accept", "generate", "reject", "resolve_planning_scope"]

LONG_PLAN_ARTIFACT_ID = "long_plan"
LONG_PLAN_PROMPT_ID = "long_plan.v1"

#: Pool ID reserve mặc định khi generate (pool để dư, không bắt dùng hết).
DEFAULT_VOLUME_ID_POOL = 2
DEFAULT_ARC_ID_POOL = 6

#: Scope tối thiểu cho project mới (chưa có plan/chapter nào).
MIN_PLAN_SCOPE_END = 3


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


def resolve_planning_scope(
    project: Project, planning_scope: Mapping[str, Any] | None = None
) -> dict[str, int]:
    """Chốt `planning_scope` thật sự dùng cho lần generate này.

    Caller truyền scope tường minh thì scope đó được dùng (sau khi kiểm tra
    `1 <= start <= end`). Không truyền thì suy từ Long Plan/Short Plan/chapter
    hiện có như mô tả ở docstring module.
    """
    if planning_scope is not None:
        start = int(planning_scope.get("start", 1))
        end = int(planning_scope.get("end", start))
        if start < 1 or end < start:
            raise GuardError(
                f"`planning_scope` không hợp lệ: {dict(planning_scope)}; cần 1 <= start <= end.",
                code="invalid_planning_scope",
            )
        return {"start": start, "end": end}

    ends: list[int] = []
    for payload in _payloads(project):
        ends.extend(
            arc.chapter_range.end
            for volume in getattr(payload, "volumes", []) or []
            for arc in volume.arcs
        )
    short_plan = storage.load_artifact(project, "short_plan")
    if short_plan is not None and short_plan.accepted_revision is not None:
        ends.extend(
            chapter.chapter_number
            for chapter in getattr(short_plan.accepted_revision.payload, "chapters", []) or []
        )
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is not None:
            ends.append(chapter.chapter_number)
    end = max([MIN_PLAN_SCOPE_END, int(project.config.current_chapter), *ends])
    return {"start": 1, "end": end}


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


def _range_issues(
    payload: LongPlanPayload, *, scope: Mapping[str, int] | None
) -> list[validation.ValidationIssue]:
    """`chapter_range` phải nằm trong scope và không chồng lấn giữa các arc."""
    collector = validation.IssueCollector()
    ranges: list[tuple[int, int, str]] = []
    for volume_position, volume in enumerate(payload.volumes):
        for arc_position, arc in enumerate(volume.arcs):
            low = arc.chapter_range.start
            high = arc.chapter_range.end
            path = validation.json_pointer(
                "payload", "volumes", volume_position, "arcs", arc_position, "chapter_range"
            )
            if scope is not None and (low < int(scope["start"]) or high > int(scope["end"])):
                collector.add(
                    path,
                    "out_of_scope",
                    f"chapter_range {low}-{high} nằm ngoài planning_scope "
                    f"{scope['start']}-{scope['end']}.",
                )
            ranges.append((low, high, arc.arc_id))
    ordered = sorted(ranges)
    for (low, high, arc_id), (next_low, _next_high, next_arc_id) in zip(ordered, ordered[1:]):
        if next_low <= high:
            collector.add(
                "/payload",
                "overlapping_chapter_range",
                f"Arc `{arc_id}` ({low}-{high}) chồng lấn arc `{next_arc_id}` "
                f"(bắt đầu {next_low}).",
            )
    return collector.issues


def _id_scope_issues(
    payload: LongPlanPayload,
    *,
    volume_pool: Sequence[str],
    arc_pool: Sequence[str],
    existing_volume_ids: set[str],
    existing_arc_ids: set[str],
) -> list[validation.ValidationIssue]:
    """Volume/arc ID phải thuộc pool backend cấp hoặc ID cũ của plan hiện có."""
    allowed_volumes = {*volume_pool, *existing_volume_ids}
    allowed_arcs = {*arc_pool, *existing_arc_ids}
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
) -> ActionResult:
    """Generate/regenerate Long Plan candidate.

    Guard: Base Idea accepted + Premise accepted và không stale. Scope, pool ID
    và context được chốt trước khi gọi LLM; output luôn là candidate (hoặc
    accepted nếu Auto Accept bật và payload hợp lệ).
    """
    if action not in {"generate", "regenerate", "edit"}:
        raise GuardError(
            "Action Long Plan chỉ nhận generate/regenerate/edit.", code="invalid_action"
        )
    op_id = operation_id or generate_operation_id()
    require_base_idea(project)
    require_accepted_artifact(project, "premise")
    scope = resolve_planning_scope(project, planning_scope)

    volume_pool = reserve_id_pool("vol", DEFAULT_VOLUME_ID_POOL, _existing_volume_ids(project))
    arc_pool = reserve_id_pool("arc", DEFAULT_ARC_ID_POOL, _existing_arc_ids(project))

    try:
        bundle = context_core.build_long_plan_context(
            project,
            planning_scope=scope,
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
    )
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
        *_range_issues(parsed, scope=scope),
        *_id_scope_issues(
            parsed,
            volume_pool=volume_pool,
            arc_pool=arc_pool,
            existing_volume_ids=_existing_volume_ids(project),
            existing_arc_ids=_existing_arc_ids(project),
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
        now=now,
    )
    auto_accepted = False
    if project.config.auto_accept_structured:
        scope_result = validation.validate_auto_accept_scope(
            auto_accept_structured=True, output_kind="long_plan"
        )
        if scope_result.is_valid:
            envelope = lifecycle.accept_candidate(
                envelope, accepted_by="auto_accept", validation=result, now=now
            )
            auto_accepted = True
    storage.save_artifact(project, envelope, operation_id=op_id)

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
            "planning_scope": scope,
            "assigned_volume_ids": volume_pool,
            "assigned_arc_ids": arc_pool,
            "id_map": id_map,
            "auto_accepted": auto_accepted,
            "raw_output_ref": call.raw_ref,
            "context_id": bundle.context_id,
        },
        validation=result,
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
        extra_issues=_range_issues(candidate.payload, scope=None),
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
    return ActionResult(
        operation_id=op_id,
        artifact_id=LONG_PLAN_ARTIFACT_ID,
        message=f"Đã accept Long Plan r{revision}.",
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
