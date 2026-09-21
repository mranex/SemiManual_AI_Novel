"""Short Plan và Rolling Plan service (T14).

Hiện thực action theo `docs/design/workflow.md` mục 4.2 và
`docs/design/schemas.md` mục 3.2, 6.5:

- `generate`: Short Plan candidate chỉ trong arc được chọn; chapter ID do backend
  cấp qua `assign_chapter_ids`; guard Long Plan accepted/fresh trước khi gọi LLM.
  `assigned_chapters=None` được suy theo quy tắc trong `resolve_assigned_chapters`:
  - `edit`/`regenerate` khi đã có Short Plan: dùng lại đúng các chapter đang có
    trong plan (trong range của arc) để LLM viết lại;
  - còn lại: lấy mọi chapter trong `arc.chapter_range` chưa có trong Short Plan
    accepted/candidate và chưa `final_reconciled`.
- `accept`: ghép candidate vào Short Plan accepted theo `chapter_id` (giữ chapter
  ngoài scope — prompt Short Plan yêu cầu điều này), validate toàn payload + FK/
  effective + scope arc, rồi tạo/cập nhật `ChapterMetadata` (`status=planned`,
  `short_plan_pin`, `previous_chapter_id`) trong **cùng transaction** với file
  Short Plan. Chapter bị thay plan làm Skeleton/draft/review liên quan stale
  (chỉ đánh dấu); chapter đã final/finalizing không bị sửa.
- `generate_rolling`: build context Rolling, lưu candidate `rolling_patch_<arc>`
  (`status=draft`, **không** accept). Proposal ngoài eligible scope hoặc vi phạm
  config `allow_relationship_replan` bị từ chối ngay ở backend.
- `accept_rolling`: ghép patch vào candidate Short Plan (giữ field/chương ngoài
  patch), validate lại toàn candidate + scope + config + pin mới nhất rồi accept;
  từ chối patch đụng chapter đã final/finalizing, vượt Long Plan hoặc ngoài
  eligible. Không apply một phần. Rolling proposal được đánh dấu accepted như dấu
  vết đã apply.
- `eligible_chapters_for_rolling`: chapter chưa final, không finalizing, nằm
  trong arc/Short Plan hiện hành và có số chương > `latest_consistent_chapter`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from novel_ai.core import context as context_core
from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.models import (
    ARTIFACT_PAYLOAD_MODELS,
    ArcPlan,
    ArtifactEnvelope,
    ArtifactStatus,
    ChapterMetadata,
    ChapterPlan,
    ChapterStatus,
    ContextMode,
    DependencyPin,
    LongPlanPayload,
    PayloadSource,
    PreparationContext,
    RollingPatchPayload,
    ShortPlanPayload,
    SourceType,
    consistent_chapter_number,
    generate_operation_id,
    reconciled_chapter_numbers,
    reserve_id_pool,
)
from novel_ai.core.project import Project
from novel_ai.services import ActionResult, GuardError
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

__all__ = [
    "accept",
    "accept_rolling",
    "assign_chapter_ids",
    "eligible_chapters_for_rolling",
    "generate",
    "generate_rolling",
    "reject",
    "reject_rolling",
    "resolve_assigned_chapters",
]

SHORT_PLAN_ARTIFACT_ID = "short_plan"
SHORT_PLAN_PROMPT_ID = "short_plan.v1"
ROLLING_PROMPT_ID = "rolling_plan.v1"

#: Số chương đưa vào reviewed range mặc định của Rolling review.
ROLLING_REVIEW_WINDOW = 3

#: Field ChapterPlan có thể mang ý định quan hệ; dùng cho guard config false.
_RELATIONSHIP_NARRATIVE_FIELDS: frozenset[str] = frozenset(
    {"summary", "outline", "threads", "chapter_goal", "planned_ending", "hook"}
)

_SHORT_PLAN_ACTIONS: frozenset[str] = frozenset({"generate", "regenerate", "edit"})

#: Chapter không được backend sửa metadata (đã khóa ở tầng chapter lifecycle).
_LOCKED_CHAPTER_STATUSES = (ChapterStatus.finalizing, ChapterStatus.final_reconciled)


# ---------------------------------------------------------------------------
# Helper đọc state
# ---------------------------------------------------------------------------


def _short_plan_payloads(project: Project) -> list[ShortPlanPayload]:
    envelope = storage.load_artifact(project, SHORT_PLAN_ARTIFACT_ID)
    if envelope is None:
        return []
    return [
        revision.payload
        for revision in (envelope.accepted_revision, envelope.candidate_revision)
        if revision is not None
    ]


def _chapter_metadata_by_number(project: Project) -> dict[int, ChapterMetadata]:
    result: dict[int, ChapterMetadata] = {}
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is not None:
            result[chapter.chapter_number] = chapter
    return result


def _planned_numbers(project: Project) -> set[int]:
    return {
        chapter.chapter_number
        for payload in _short_plan_payloads(project)
        for chapter in payload.chapters
    }


def _latest_final_chapter_number(project: Project) -> int:
    numbers = [
        chapter.chapter_number
        for chapter in _chapter_metadata_by_number(project).values()
        if chapter.status is ChapterStatus.final_reconciled
    ]
    numbers.append(storage.load_timeline(project).latest_final_chapter)
    return max(numbers, default=0)


def _latest_consistent_chapter(project: Project) -> int:
    """Chương cao nhất có state chain hợp lệ (khớp context builder + guard Writer)."""
    chapters = [
        chapter
        for chapter in _chapter_metadata_by_number(project).values()
    ]
    return consistent_chapter_number(
        timeline=storage.load_timeline(project),
        relationships=storage.load_relationships(project),
        reconciled_numbers=reconciled_chapter_numbers(chapters),
    )


def _locked_chapter_ids(project: Project) -> set[str]:
    """Chapter đã `finalizing`/`final_reconciled`: plan của chúng là canon."""
    return {
        chapter.chapter_id
        for chapter in _chapter_metadata_by_number(project).values()
        if chapter.status in (ChapterStatus.finalizing, ChapterStatus.final_reconciled)
    }


def _find_arc(long_plan: LongPlanPayload, arc_id: str) -> ArcPlan | None:
    for volume in long_plan.volumes:
        for arc in volume.arcs:
            if arc.arc_id == arc_id:
                return arc
    return None


def _accepted_long_plan(project: Project) -> LongPlanPayload:
    envelope = require_accepted_artifact(project, "long_plan")
    payload = envelope.accepted_revision.payload if envelope.accepted_revision else None
    if not isinstance(payload, LongPlanPayload):
        payload = LongPlanPayload.model_validate(payload.model_dump(mode="json"))
    return payload


def _envelope_or_new(
    project: Project, artifact_id: str, artifact_type: str, *, now: str | None
) -> ArtifactEnvelope[Any]:
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is not None:
        return envelope
    return lifecycle.new_artifact(artifact_type, artifact_id, now=now)


def _in_arc(arc: ArcPlan, number: int) -> bool:
    return arc.chapter_range.start <= number <= arc.chapter_range.end


# ---------------------------------------------------------------------------
# Chapter ID
# ---------------------------------------------------------------------------


def assign_chapter_ids(project: Project, count: int) -> list[dict[str, Any]]:
    """Cấp `count` chapter ID mới kèm `chapter_number` kế tiếp.

    `chapter_number` lấy tiếp từ số lớn nhất đang có trong chapter metadata và
    Short Plan accepted/candidate; `chapter_id` reserve mới, tránh trùng cả hai
    nguồn.
    """
    if count < 0:
        raise GuardError("`count` phải >= 0.", code="invalid_count", details={"count": count})
    if count == 0:
        return []
    used_ids = set(storage.list_chapter_ids(project))
    used_ids |= {
        chapter.chapter_id
        for payload in _short_plan_payloads(project)
        for chapter in payload.chapters
    }
    numbers = set(_chapter_metadata_by_number(project))
    numbers |= _planned_numbers(project)
    base = max(numbers, default=0)
    ids = reserve_id_pool("ch", count, used_ids)
    return [
        {"chapter_id": chapter_id, "chapter_number": base + offset}
        for offset, chapter_id in enumerate(ids, start=1)
    ]


def resolve_assigned_chapters(project: Project, arc: ArcPlan) -> list[dict[str, Any]]:
    """Suy `assigned_chapters` khi caller không truyền.

    Quy tắc: mọi `chapter_number` trong `arc.chapter_range` chưa có trong Short
    Plan accepted/candidate và chưa `final_reconciled`; chapter đã có metadata
    giữ nguyên ID của nó, chapter mới được reserve `ch_` mới. Không còn chapter
    nào để lập thì raise `GuardError`.
    """
    planned = _planned_numbers(project)
    by_number = _chapter_metadata_by_number(project)
    final_numbers = {
        number
        for number, chapter in by_number.items()
        if chapter.status is ChapterStatus.final_reconciled
    }
    wanted = [
        number
        for number in range(arc.chapter_range.start, arc.chapter_range.end + 1)
        if number not in planned and number not in final_numbers
    ]
    if not wanted:
        raise GuardError(
            f"Arc `{arc.arc_id}` đã có đủ chapter trong Short Plan hiện hành; "
            "không còn chapter nào để lập.",
            code="no_chapters_to_plan",
            details={"arc_id": arc.arc_id},
        )
    return _chapters_for_numbers(project, wanted)


def _chapters_for_numbers(project: Project, numbers: Sequence[int]) -> list[dict[str, Any]]:
    by_number = _chapter_metadata_by_number(project)
    planned_by_number = {
        chapter.chapter_number: chapter.chapter_id
        for payload in _short_plan_payloads(project)
        for chapter in payload.chapters
    }
    used_ids = set(storage.list_chapter_ids(project))
    used_ids |= set(planned_by_number.values())
    result: list[dict[str, Any]] = []
    for number in numbers:
        metadata = by_number.get(number)
        if metadata is not None:
            result.append({"chapter_id": metadata.chapter_id, "chapter_number": number})
            continue
        planned_id = planned_by_number.get(number)
        if planned_id is not None:
            result.append({"chapter_id": planned_id, "chapter_number": number})
            continue
        chapter_id = reserve_id_pool("ch", 1, used_ids)[0]
        used_ids.add(chapter_id)
        result.append({"chapter_id": chapter_id, "chapter_number": number})
    return result


def _current_plan_chapters(project: Project, arc: ArcPlan) -> list[dict[str, Any]]:
    """Chapter đang có trong Short Plan accepted/candidate và thuộc arc hiện hành."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in _short_plan_payloads(project):
        if payload.arc_id != arc.arc_id:
            continue
        for chapter in payload.chapters:
            if chapter.chapter_id in seen or not _in_arc(arc, chapter.chapter_number):
                continue
            seen.add(chapter.chapter_id)
            result.append(
                {"chapter_id": chapter.chapter_id, "chapter_number": chapter.chapter_number}
            )
    result.sort(key=lambda item: item["chapter_number"])
    return result


# ---------------------------------------------------------------------------
# Validation scope
# ---------------------------------------------------------------------------


def _candidate_scope_issues(
    payload: ShortPlanPayload, *, arc: ArcPlan, assigned: Sequence[Mapping[str, Any]]
) -> list[validation.ValidationIssue]:
    """Candidate phải phủ đúng assigned chapter, trong arc, và không mutate state."""
    collector = validation.IssueCollector()
    assigned_map = {str(item["chapter_id"]): int(item["chapter_number"]) for item in assigned}
    seen: set[str] = set()
    for position, chapter in enumerate(payload.chapters):
        base = validation.json_pointer("payload", "chapters", position)
        planned_number = assigned_map.get(chapter.chapter_id)
        if planned_number is None:
            collector.add(
                f"{base}/chapter_id",
                "unassigned_chapter_id",
                f"`{chapter.chapter_id}` không nằm trong assigned_chapters backend cấp.",
            )
        elif planned_number != chapter.chapter_number:
            collector.add(
                f"{base}/chapter_number",
                "chapter_number_mismatch",
                f"`{chapter.chapter_id}` phải có chapter_number {planned_number}.",
            )
        seen.add(chapter.chapter_id)
        if not _in_arc(arc, chapter.chapter_number):
            collector.add(
                f"{base}/chapter_number",
                "out_of_arc_scope",
                f"Chương {chapter.chapter_number} nằm ngoài chapter_range "
                f"{arc.chapter_range.start}-{arc.chapter_range.end} của arc `{arc.arc_id}`.",
            )
    missing = sorted(set(assigned_map) - seen)
    if missing:
        collector.add(
            "/payload/chapters",
            "incomplete_assigned_chapters",
            f"Candidate thiếu ChapterPlan cho assigned chapter {missing}.",
        )
    collector.extend(
        validation.validate_plan_does_not_mutate_state(payload.model_dump(mode="json")).errors
    )
    return collector.issues


def _accept_scope_issues(
    project: Project,
    merged: ShortPlanPayload,
    *,
    arc: ArcPlan,
    touched_ids: set[str],
) -> list[validation.ValidationIssue]:
    """Scope arc của payload đã ghép + chapter bị khóa không được sửa."""
    collector = validation.IssueCollector()
    for position, chapter in enumerate(merged.chapters):
        if not _in_arc(arc, chapter.chapter_number):
            collector.add(
                validation.json_pointer("payload", "chapters", position, "chapter_number"),
                "out_of_arc_scope",
                f"Chương {chapter.chapter_number} nằm ngoài chapter_range "
                f"{arc.chapter_range.start}-{arc.chapter_range.end} của arc `{arc.arc_id}`.",
            )
        if chapter.chapter_id not in touched_ids:
            continue
        metadata = storage.load_chapter(project, chapter.chapter_id)
        if metadata is not None and metadata.status in _LOCKED_CHAPTER_STATUSES:
            collector.add(
                validation.json_pointer("payload", "chapters", position, "chapter_id"),
                "chapter_locked",
                f"`{chapter.chapter_id}` đang `{metadata.status.value}`; "
                "không được thay plan của chapter đã final/finalizing.",
            )
    collector.extend(
        validation.validate_plan_does_not_mutate_state(merged.model_dump(mode="json")).errors
    )
    return collector.issues


def _rolling_issues(
    project: Project,
    patch: RollingPatchPayload,
    *,
    plan_payload: ShortPlanPayload,
    eligible_ids: set[str],
    latest_final: int,
) -> list[validation.ValidationIssue]:
    """Guard ngoài validator: config quan hệ gián tiếp, cặp ID, actual range."""
    collector = validation.IssueCollector()
    chapters = {chapter.chapter_id: chapter for chapter in plan_payload.chapters}
    if latest_final and patch.reviewed_chapter_range.end > latest_final:
        collector.add(
            "/payload/reviewed_chapter_range",
            "reviewed_range_beyond_actual",
            f"reviewed_chapter_range kết ở chương {patch.reviewed_chapter_range.end} "
            f"nhưng actual mới có tới chương {latest_final}.",
        )
    if not project.config.allow_relationship_replan:
        for position, change in enumerate(patch.short_plan_changes):
            target = chapters.get(change.chapter_id)
            if target is None or not target.relationship_changes:
                continue
            touched = sorted(set(change.changes) & _RELATIONSHIP_NARRATIVE_FIELDS)
            if touched:
                collector.add(
                    validation.json_pointer("payload", "short_plan_changes", position, "changes"),
                    "relationship_replan_disabled_indirect",
                    "allow_relationship_replan = false nên không được đổi ý định quan hệ "
                    f"gián tiếp qua {touched} của chapter `{change.chapter_id}`.",
                )
    for position, change in enumerate(patch.short_plan_changes):
        if change.chapter_id not in chapters:
            collector.add(
                validation.json_pointer("payload", "short_plan_changes", position, "chapter_id"),
                "unknown_reference",
                f"`{change.chapter_id}` không có trong Short Plan accepted.",
            )
        elif change.chapter_id not in eligible_ids:
            collector.add(
                validation.json_pointer("payload", "short_plan_changes", position, "chapter_id"),
                "out_of_scope",
                f"`{change.chapter_id}` không nằm trong eligible scope.",
            )
    for position, change in enumerate(patch.relationship_plan_changes):
        target = chapters.get(change.chapter_id)
        if target is None:
            collector.add(
                validation.json_pointer(
                    "payload", "relationship_plan_changes", position, "chapter_id"
                ),
                "unknown_reference",
                f"`{change.chapter_id}` không có trong Short Plan accepted.",
            )
            continue
        if change.chapter_id not in eligible_ids:
            collector.add(
                validation.json_pointer(
                    "payload", "relationship_plan_changes", position, "chapter_id"
                ),
                "out_of_scope",
                f"`{change.chapter_id}` không nằm trong eligible scope.",
            )
        for direction_position, direction in enumerate(change.relationship_changes):
            unknown = [
                character_id
                for character_id in direction.character_ids
                if character_id not in target.character_ids
            ]
            if unknown:
                collector.add(
                    validation.json_pointer(
                        "payload",
                        "relationship_plan_changes",
                        position,
                        "relationship_changes",
                        direction_position,
                        "character_ids",
                    ),
                    "relationship_pair_not_in_chapter",
                    f"{unknown} không nằm trong selector chapter `{change.chapter_id}`.",
                )
    return collector.issues


# ---------------------------------------------------------------------------
# Chapter metadata và transaction
# ---------------------------------------------------------------------------


def _previous_chapter_id(
    number: int,
    *,
    by_number: Mapping[int, ChapterMetadata],
    plan_by_number: Mapping[int, ChapterPlan],
) -> str | None:
    if number <= 1:
        return None
    previous_metadata = by_number.get(number - 1)
    if previous_metadata is not None:
        return previous_metadata.chapter_id
    previous_plan = plan_by_number.get(number - 1)
    return previous_plan.chapter_id if previous_plan is not None else None


def _chapter_metadata_updates(
    project: Project,
    payload: ShortPlanPayload,
    *,
    revision: int,
    touched_ids: set[str],
) -> list[ChapterMetadata]:
    """Metadata mới cho chapter bị plan thay; chapter final/finalizing giữ nguyên."""
    by_number = _chapter_metadata_by_number(project)
    plan_by_number = {chapter.chapter_number: chapter for chapter in payload.chapters}
    updates: list[ChapterMetadata] = []
    for chapter in payload.chapters:
        existing = by_number.get(chapter.chapter_number)
        if existing is not None and existing.chapter_id != chapter.chapter_id:
            raise GuardError(
                f"Chapter số {chapter.chapter_number} đang là `{existing.chapter_id}` "
                f"nhưng Short Plan khai `{chapter.chapter_id}`; từ chối ghi đè metadata.",
                code="chapter_id_mismatch",
            )
        if existing is not None and existing.status in _LOCKED_CHAPTER_STATUSES:
            if chapter.chapter_id in touched_ids:
                raise GuardError(
                    f"`{chapter.chapter_id}` đang `{existing.status.value}`; không thay plan "
                    "của chapter đã final/finalizing.",
                    code="chapter_locked",
                )
            continue
        pin = DependencyPin(
            artifact_id=SHORT_PLAN_ARTIFACT_ID,
            revision=revision,
            scope="short_plan",
            chapter_id=chapter.chapter_id,
        )
        previous_id = _previous_chapter_id(
            chapter.chapter_number, by_number=by_number, plan_by_number=plan_by_number
        )
        if existing is None:
            updates.append(
                ChapterMetadata(
                    chapter_id=chapter.chapter_id,
                    chapter_number=chapter.chapter_number,
                    title=chapter.title,
                    status=ChapterStatus.planned,
                    previous_chapter_id=previous_id,
                    short_plan_pin=pin,
                )
            )
        else:
            updates.append(
                existing.model_copy(
                    update={
                        "title": chapter.title,
                        # KHÔNG hạ status về `planned`: chapter đã có prose (draft /
                        # review_required / skeleton_ready) vẫn giữ nguyên lifecycle.
                        # Ghi đè về `planned` sẽ làm metadata nói ngược với dữ liệu
                        # thật trên disk (workflow.md mục 3).
                        "previous_chapter_id": previous_id,
                        "short_plan_pin": pin,
                    }
                )
            )
    return updates


def _merge_with_accepted(
    project: Project, candidate: ShortPlanPayload
) -> ShortPlanPayload:
    """Ghép candidate vào Short Plan accepted cùng arc, giữ chapter ngoài scope."""
    envelope = storage.load_artifact(project, SHORT_PLAN_ARTIFACT_ID)
    if envelope is None or envelope.accepted_revision is None:
        return candidate
    previous = envelope.accepted_revision.payload
    if previous.arc_id != candidate.arc_id:
        return candidate
    candidate_ids = {chapter.chapter_id for chapter in candidate.chapters}
    kept = [chapter for chapter in previous.chapters if chapter.chapter_id not in candidate_ids]
    if not kept:
        return candidate
    merged = [*candidate.chapters, *kept]
    merged.sort(key=lambda chapter: chapter.chapter_number)
    return candidate.model_copy(update={"chapters": merged})


def _commit_plan_accept(
    project: Project,
    *,
    plan_envelope: ArtifactEnvelope[Any],
    merged_payload: ShortPlanPayload,
    touched_ids: set[str],
    result: validation.ValidationResult,
    accepted_by: str,
    operation_id: str,
    now: str | None,
    extra_artifacts: Sequence[tuple[str, str, ArtifactEnvelope[Any]]] = (),
) -> tuple[ArtifactEnvelope[Any], list[ChapterMetadata], list[str]]:
    """Accept Short Plan + ghi chapter metadata (+ artifact phụ) trong một transaction."""
    if plan_envelope.candidate_revision is None:
        raise GuardError(
            f"`{plan_envelope.artifact_id}` không có candidate để accept.",
            code="missing_candidate",
        )
    staged = plan_envelope.model_copy(
        update={
            "candidate_revision": plan_envelope.candidate_revision.model_copy(
                update={"payload": merged_payload, "validation": result}
            )
            if plan_envelope.candidate_revision is not None
            else None
        }
    )
    accepted_plan = lifecycle.accept_candidate(
        staged, accepted_by=accepted_by, validation=result, now=now
    )
    accepted_revision = accepted_plan.accepted_revision
    assert accepted_revision is not None
    revision = accepted_revision.revision
    chapters = _chapter_metadata_updates(
        project, merged_payload, revision=revision, touched_ids=touched_ids
    )

    handle = storage.begin_operation(
        project, operation_type="accept_short_plan", operation_id=operation_id
    )
    try:
        for artifact_id, artifact_type, envelope in extra_artifacts:
            handle.add_json(storage.artifact_relpath(artifact_id, artifact_type), envelope)
        handle.add_json(
            storage.artifact_relpath(SHORT_PLAN_ARTIFACT_ID, "short_plan"), accepted_plan
        )
        for chapter in chapters:
            relpath = str(
                project.paths.chapter_json(chapter.chapter_id).relative_to(project.root)
            ).replace("\\", "/")
            handle.add_json(relpath, chapter)
    except Exception:
        handle.abort()
        raise
    handle.commit()

    marked = lifecycle.mark_downstream_stale(
        project,
        source_artifact_id=SHORT_PLAN_ARTIFACT_ID,
        source_revision=revision,
        change=lifecycle.STALE_CHANGE_SHORT_PLAN_REPLACE,
        now=now,
    )
    return accepted_plan, chapters, marked


# ---------------------------------------------------------------------------
# Short Plan actions
# ---------------------------------------------------------------------------


def generate(
    project: Project,
    *,
    client: Any,
    arc_id: str,
    assigned_chapters: Sequence[Mapping[str, Any]] | None = None,
    chapter_constraints: Sequence[Mapping[str, Any]] = (),
    action: str = "generate",
    user_instruction: str = "",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Generate/regenerate/edit Short Plan candidate cho **một** arc.

    Guard: Base Idea accepted, Long Plan accepted/fresh, arc tồn tại, và có
    chapter được giao. Candidate provisional (chưa đủ actual) **không** được
    auto-accept theo D014.
    """
    if action not in _SHORT_PLAN_ACTIONS:
        raise GuardError(
            f"Action Short Plan chỉ nhận {sorted(_SHORT_PLAN_ACTIONS)}.", code="invalid_action"
        )
    op_id = operation_id or generate_operation_id()
    require_base_idea(project)
    long_plan = _accepted_long_plan(project)
    arc = _find_arc(long_plan, arc_id)
    if arc is None:
        raise GuardError(
            f"Arc `{arc_id}` không có trong Long Plan accepted.",
            code="unknown_arc",
            details={"arc_id": arc_id},
        )
    if assigned_chapters is not None:
        assigned = [dict(item) for item in assigned_chapters]
    elif action in {"edit", "regenerate"} and _current_plan_chapters(project, arc):
        assigned = _current_plan_chapters(project, arc)
    else:
        assigned = resolve_assigned_chapters(project, arc)
    if not assigned:
        raise GuardError(
            "Short Plan cần ít nhất một assigned chapter.", code="missing_dependency"
        )
    # Chapter đã `finalizing`/`final_reconciled` không bao giờ được đưa vào candidate
    # mới: plan của chương đã final là canon (validation cũng từ chối). Lọc ở đây để
    # context basis tính trên đúng tập chapter còn lập được, thay vì để candidate
    # chứa chapter bị khóa rồi hỏng luôn ở bước accept.
    locked = _locked_chapter_ids(project)
    skipped_locked = [item for item in assigned if str(item["chapter_id"]) in locked]
    assigned = [item for item in assigned if str(item["chapter_id"]) not in locked]
    if not assigned:
        raise GuardError(
            "Mọi chapter được giao đều đã `finalizing`/`final_reconciled`; "
            "Short Plan của chúng là canon và không được thay.",
            code="chapter_already_final",
            details={"chapter_ids": sorted(locked)},
        )

    try:
        bundle = context_core.build_short_plan_context(
            project,
            arc_id=arc_id,
            assigned_chapters=assigned,
            chapter_constraints=chapter_constraints,
            action=action,
            user_instruction=user_instruction,
        )
    except context_core.ContextError as exc:
        raise GuardError(str(exc), code=exc.code, details=dict(exc.details)) from exc

    call = complete_json(
        project,
        client=client,
        prompt_id=SHORT_PLAN_PROMPT_ID,
        bundle=bundle,
        operation_id=op_id,
        now=now,
        label="short_plan",
    )
    parsed = parse_structured_or_fail(
        project,
        call=call,
        model_cls=ARTIFACT_PAYLOAD_MODELS["short_plan"],
        operation_id=op_id,
        now=now,
        label="short_plan",
        artifact_id=SHORT_PLAN_ARTIFACT_ID,
    )
    result = validate_or_fail(
        project,
        artifact_type="short_plan",
        payload=parsed,
        context=validation.ValidationContext(index=foundation_reference_index(project)),
        operation_id=op_id,
        now=now,
        label="short_plan",
        raw_output_ref=call.raw_ref,
        artifact_id=SHORT_PLAN_ARTIFACT_ID,
        extra_issues=_candidate_scope_issues(parsed, arc=arc, assigned=assigned),
    )

    envelope = _envelope_or_new(project, SHORT_PLAN_ARTIFACT_ID, "short_plan", now=now)
    provisional = bundle.mode is ContextMode.provisional
    envelope = lifecycle.set_candidate(
        envelope,
        parsed,
        source=payload_source(call, operation_id=op_id),
        dependency_pins=bundle.dependency_pins,
        validation=result,
        # Giữ dấu chuẩn bị trước trong chính revision (schemas.md mục 4.1) để
        # `accept` từ chối được candidate provisional ngay cả khi chapter metadata
        # chưa được service này ghi.
        preparation_context=(
            PreparationContext(
                context_basis=bundle.preparation_context.context_basis,
                dependency_pins=list(bundle.dependency_pins),
            )
            if provisional and bundle.preparation_context is not None
            else None
        ),
        now=now,
    )
    warnings: list[str] = []
    if provisional:
        warnings.append(
            "Candidate được chuẩn bị ở mode provisional (chưa đủ actual đầu range); "
            "phải review/regenerate với actual và pin mới trước khi accept."
        )
    auto_accepted = False
    if project.config.auto_accept_structured and not provisional:
        scope_result = validation.validate_auto_accept_scope(
            auto_accept_structured=True, output_kind="short_plan"
        )
        if scope_result.is_valid:
            merged = _merge_with_accepted(project, parsed)
            touched_ids = {chapter.chapter_id for chapter in parsed.chapters}
            merged_result = validate_or_fail(
                project,
                artifact_type="short_plan",
                payload=merged,
                context=validation.ValidationContext(
                    index=foundation_reference_index(project)
                ),
                operation_id=op_id,
                now=now,
                label="short_plan.auto_accept",
                raw_output_ref=call.raw_ref,
                artifact_id=SHORT_PLAN_ARTIFACT_ID,
                extra_issues=_accept_scope_issues(
                    project, merged, arc=arc, touched_ids=touched_ids
                ),
            )
            _, chapters, _marked = _commit_plan_accept(
                project,
                plan_envelope=envelope,
                merged_payload=merged,
                touched_ids=touched_ids,
                result=merged_result,
                accepted_by="auto_accept",
                operation_id=op_id,
                now=now,
            )
            auto_accepted = True
            envelope = storage.load_artifact(project, SHORT_PLAN_ARTIFACT_ID) or envelope
            warnings.append(
                f"Auto Accept đã accept Short Plan và tạo/cập nhật {len(chapters)} chapter metadata."
            )
    if not auto_accepted:
        storage.save_artifact(project, envelope, operation_id=op_id)

    revision = (
        envelope.accepted_revision.revision
        if envelope.accepted_revision is not None
        else (envelope.candidate_revision.revision if envelope.candidate_revision else None)
    )
    return ActionResult(
        operation_id=op_id,
        artifact_id=SHORT_PLAN_ARTIFACT_ID,
        message=(
            f"Auto Accept đã accept Short Plan r{revision}."
            if auto_accepted
            else f"Đã tạo Short Plan candidate r{revision} cho arc `{arc_id}`."
        ),
        warnings=warnings,
        data={
            "status": envelope.status.value,
            "revision": revision,
            "arc_id": arc_id,
            "assigned_chapters": assigned,
            "context_mode": bundle.mode.value,
            "auto_accepted": auto_accepted,
            "raw_output_ref": call.raw_ref,
            "context_id": bundle.context_id,
        },
        validation=result,
    )


def _guard_arc_replacement(
    project: Project,
    envelope: ArtifactEnvelope[Any],
    candidate: ShortPlanPayload,
) -> None:
    """Không cho accept Short Plan ghi đè plan của **arc khác** còn chapter đang dùng.

    Một `ShortPlanPayload` accepted chỉ thuộc một `arc_id`, nên khi candidate nhắm
    arc khác thì `_merge_with_accepted` trả về thẳng candidate và mọi chapter của
    arc cũ biến mất khỏi plan trong khi metadata chapter vẫn còn trên disk. Từ chối
    thay vì âm thầm bỏ dữ liệu: người dùng phải chủ động xử lý arc cũ trước.
    """
    accepted = envelope.accepted_revision
    if accepted is None or accepted.payload.arc_id == candidate.arc_id:
        return
    orphaned = [
        chapter.chapter_id
        for chapter in accepted.payload.chapters
        if chapter.chapter_id not in {item.chapter_id for item in candidate.chapters}
        and _chapter_uses_short_plan(project, chapter.chapter_id)
    ]
    if orphaned:
        raise GuardError(
            f"Short Plan accepted đang thuộc arc `{accepted.payload.arc_id}` và candidate "
            f"nhắm arc `{candidate.arc_id}`; accept sẽ bỏ chapter {orphaned} khỏi plan trong "
            "khi chapter metadata vẫn dùng plan đó. Hoàn tất/replace các chapter này trước.",
            code="arc_replacement_not_allowed",
            details={
                "accepted_arc_id": accepted.payload.arc_id,
                "candidate_arc_id": candidate.arc_id,
                "orphaned_chapter_ids": orphaned,
            },
        )


def _chapter_uses_short_plan(project: Project, chapter_id: str) -> bool:
    """True nếu chapter vẫn đang pin Short Plan accepted (chưa final/finalizing)."""
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        return False
    if chapter.status in _LOCKED_CHAPTER_STATUSES:
        return False
    return chapter.short_plan_pin is not None


def _guard_provisional_candidate(project: Project, candidate: Any) -> None:
    """Từ chối accept candidate Short Plan đã bị actual vượt qua (D014, F-B2).

    Candidate `provisional` là chuẩn bị trước: các chương trong range chưa có
    state thật nên plan dựa trên `planned_bridge`. Nó chỉ còn là **ý định cũ** khi
    một chương mà bridge đang mô tả đã có prose (người dùng đã viết lệch plan) —
    lúc đó phải regenerate/review trên actual.

    Ngược lại vẫn cho accept: lập plan trước khi viết (chương 1 chưa có gì, hoặc
    chương 2–3 lập khi chương 1 còn là ý định) là luồng bình thường của MVP, không
    phải lỗi. Trước đây Short Plan chỉ chặn ở đường auto-accept nên candidate dựa
    trên bridge lỗi thời vẫn vào canon qua accept thủ công.
    """
    preparation = getattr(candidate, "preparation_context", None)
    if preparation is None or preparation.context_basis.mode is not ContextMode.provisional:
        return
    bridge = preparation.context_basis.planned_bridge
    if not bridge:
        return
    prose_numbers = {
        chapter.chapter_number
        for chapter in _chapter_metadata_by_number(project).values()
        if chapter.current_draft_revision is not None
    }
    outdated = sorted(
        item.chapter_number for item in bridge if item.chapter_number in prose_numbers
    )
    if outdated:
        raise GuardError(
            "Candidate Short Plan dựa trên `planned_bridge` của chương đã có prose "
            f"({', '.join(f'chương {number}' for number in outdated)}). Phải "
            "regenerate/review trên context actual rồi mới accept.",
            code="provisional_candidate_not_writer_ready",
            details={
                "context_basis": preparation.context_basis.model_dump(mode="json"),
                "chapters_with_prose": outdated,
            },
        )


def accept(
    project: Project,
    *,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Accept Short Plan candidate và ghi ChapterMetadata trong cùng transaction.

    Candidate được ghép vào Short Plan accepted cùng arc theo `chapter_id`
    (chapter ngoài scope giữ nguyên), rồi validate lại toàn payload đã ghép.
    """
    op_id = operation_id or generate_operation_id()
    envelope = storage.load_artifact(project, SHORT_PLAN_ARTIFACT_ID)
    if envelope is None:
        raise GuardError("Chưa có Short Plan trong project.", code="missing_artifact")
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
                project, operation_id, SHORT_PLAN_ARTIFACT_ID, SHORT_PLAN_ARTIFACT_ID
            )
        ):
            return ActionResult(
                operation_id=op_id,
                artifact_id=SHORT_PLAN_ARTIFACT_ID,
                message="Short Plan đã được accept trước đó cho cùng operation_id.",
                data={
                    "status": envelope.status.value,
                    "revision": envelope.accepted_revision.revision,
                    "idempotent": True,
                },
                validation=envelope.accepted_revision.validation,
            )
        raise GuardError("Short Plan không có candidate để accept.", code="missing_candidate")

    check_pin_freshness(project, candidate.dependency_pins, artifact_id=SHORT_PLAN_ARTIFACT_ID)
    _guard_provisional_candidate(project, candidate)
    long_plan = _accepted_long_plan(project)
    arc = _find_arc(long_plan, candidate.payload.arc_id)
    if arc is None:
        raise GuardError(
            f"Arc `{candidate.payload.arc_id}` không còn trong Long Plan accepted; "
            "không accept Short Plan.",
            code="unknown_arc",
            details={"arc_id": candidate.payload.arc_id},
        )
    merged = _merge_with_accepted(project, candidate.payload)
    touched_ids = {chapter.chapter_id for chapter in candidate.payload.chapters}
    _guard_arc_replacement(project, envelope, candidate.payload)
    result = validate_or_fail(
        project,
        artifact_type="short_plan",
        payload=merged,
        context=validation.ValidationContext(index=foundation_reference_index(project)),
        operation_id=op_id,
        now=now,
        label="short_plan.accept",
        raw_output_ref=candidate.payload_source.raw_output_ref,
        artifact_id=SHORT_PLAN_ARTIFACT_ID,
        extra_issues=_accept_scope_issues(
            project, merged, arc=arc, touched_ids=touched_ids
        ),
    )
    accepted, chapters, marked = _commit_plan_accept(
        project,
        plan_envelope=envelope,
        merged_payload=merged,
        touched_ids=touched_ids,
        result=result,
        accepted_by=accepted_by,
        operation_id=op_id,
        now=now,
    )
    revision = accepted.accepted_revision.revision if accepted.accepted_revision else None
    return ActionResult(
        operation_id=op_id,
        artifact_id=SHORT_PLAN_ARTIFACT_ID,
        message=(
            f"Đã accept Short Plan r{revision}; tạo/cập nhật {len(chapters)} chapter metadata."
        ),
        data={
            "status": accepted.status.value,
            "revision": revision,
            "accepted_by": accepted_by,
            "chapters": [chapter.chapter_id for chapter in chapters],
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
    """Reject Short Plan candidate; accepted plan và chapter metadata giữ nguyên."""
    op_id = operation_id or generate_operation_id()
    envelope = storage.load_artifact(project, SHORT_PLAN_ARTIFACT_ID)
    if envelope is None or envelope.candidate_revision is None:
        raise GuardError("Short Plan không có candidate để reject.", code="missing_candidate")
    rejected = lifecycle.reject_candidate(envelope)
    storage.save_artifact(project, rejected, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=SHORT_PLAN_ARTIFACT_ID,
        message="Đã reject Short Plan candidate; accepted cũ giữ nguyên.",
        data={
            "status": rejected.status.value,
            "revision": rejected.accepted_revision.revision if rejected.accepted_revision else None,
        },
    )


# ---------------------------------------------------------------------------
# Rolling Plan actions
# ---------------------------------------------------------------------------


def eligible_chapters_for_rolling(project: Project) -> list[dict[str, Any]]:
    """Chapter đủ điều kiện cho Rolling review (suy từ lifecycle, không từ file).

    Điều kiện: Short Plan accepted; chapter nằm trong arc hiện hành; số chương >
    `latest_consistent_chapter`; chapter không `finalizing`/`final_reconciled`.
    """
    envelope = storage.load_artifact(project, SHORT_PLAN_ARTIFACT_ID)
    if (
        envelope is None
        or envelope.accepted_revision is None
        or envelope.status is not ArtifactStatus.accepted
    ):
        return []
    payload = envelope.accepted_revision.payload
    latest_consistent = _latest_consistent_chapter(project)
    arc: ArcPlan | None = None
    long_plan_envelope = storage.load_artifact(project, "long_plan")
    if (
        long_plan_envelope is not None
        and long_plan_envelope.accepted_revision is not None
        and long_plan_envelope.status is ArtifactStatus.accepted
    ):
        arc = _find_arc(long_plan_envelope.accepted_revision.payload, payload.arc_id)
    result: list[dict[str, Any]] = []
    for chapter in payload.chapters:
        if chapter.chapter_number <= latest_consistent:
            continue
        if arc is not None and not _in_arc(arc, chapter.chapter_number):
            continue
        metadata = storage.load_chapter(project, chapter.chapter_id)
        if metadata is not None and metadata.status in _LOCKED_CHAPTER_STATUSES:
            continue
        result.append(
            {"chapter_id": chapter.chapter_id, "chapter_number": chapter.chapter_number}
        )
    result.sort(key=lambda item: item["chapter_number"])
    return result


def _resolve_reviewed_range(
    project: Project, reviewed_chapter_range: Mapping[str, Any] | None
) -> dict[str, int] | None:
    """Chốt reviewed range: mặc định `ROLLING_REVIEW_WINDOW` chương final gần nhất."""
    latest_final = _latest_final_chapter_number(project)
    if reviewed_chapter_range is not None:
        start = int(reviewed_chapter_range.get("start", 1))
        end = int(reviewed_chapter_range.get("end", start))
        if start < 1 or end < start:
            raise GuardError(
                f"`reviewed_chapter_range` không hợp lệ: {dict(reviewed_chapter_range)}.",
                code="invalid_reviewed_range",
            )
        return {"start": start, "end": end}
    if latest_final < 1:
        return None
    return {
        "start": max(1, latest_final - ROLLING_REVIEW_WINDOW + 1),
        "end": latest_final,
    }


def _rolling_artifact_id(arc_id: str) -> str:
    return lifecycle.artifact_id_for("rolling_patch", arc_id=arc_id)


def generate_rolling(
    project: Project,
    *,
    client: Any,
    arc_id: str | None = None,
    reviewed_chapter_range: Mapping[str, Any] | None = None,
    user_instruction: str = "",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Sinh proposal Rolling cho Short Plan hiện hành và lưu candidate draft.

    Proposal **không** được accept ở đây. Target ngoài eligible scope, reviewed
    range vượt actual, vi phạm `allow_relationship_replan` hoặc field ngoài
    allowlist bị từ chối ngay bằng `ValidationFailure`.
    """
    op_id = operation_id or generate_operation_id()
    plan_envelope = require_accepted_artifact(project, SHORT_PLAN_ARTIFACT_ID)
    plan_payload = plan_envelope.accepted_revision.payload if plan_envelope.accepted_revision else None
    assert plan_payload is not None
    target_arc_id = arc_id or plan_payload.arc_id
    if target_arc_id != plan_payload.arc_id:
        raise GuardError(
            f"Rolling review chỉ áp cho arc hiện hành `{plan_payload.arc_id}`, "
            f"không nhận arc `{target_arc_id}`.",
            code="arc_mismatch",
            details={"arc_id": target_arc_id, "current_arc_id": plan_payload.arc_id},
        )
    arc = _find_arc(_accepted_long_plan(project), target_arc_id)
    if arc is None:
        raise GuardError(
            f"Arc `{target_arc_id}` không có trong Long Plan accepted.",
            code="unknown_arc",
            details={"arc_id": target_arc_id},
        )
    reviewed = _resolve_reviewed_range(project, reviewed_chapter_range)
    if reviewed is None:
        raise GuardError(
            "Rolling review cần ít nhất một chapter đã final_reconciled làm actual; "
            "chưa có actual nào để đối chiếu.",
            code="missing_dependency",
        )
    eligible = eligible_chapters_for_rolling(project)
    eligible_ids = {item["chapter_id"] for item in eligible}
    latest_final = _latest_final_chapter_number(project)

    try:
        bundle = context_core.build_rolling_context(
            project,
            arc_id=target_arc_id,
            eligible_chapters=eligible,
            reviewed_chapter_range=reviewed,
            allow_relationship_replan=project.config.allow_relationship_replan,
            user_instruction=user_instruction,
        )
    except context_core.ContextError as exc:
        raise GuardError(str(exc), code=exc.code, details=dict(exc.details)) from exc

    call = complete_json(
        project,
        client=client,
        prompt_id=ROLLING_PROMPT_ID,
        bundle=bundle,
        operation_id=op_id,
        now=now,
        label="rolling_patch",
    )
    artifact_id = _rolling_artifact_id(target_arc_id)
    parsed = parse_structured_or_fail(
        project,
        call=call,
        model_cls=ARTIFACT_PAYLOAD_MODELS["rolling_patch"],
        operation_id=op_id,
        now=now,
        label="rolling_patch",
        artifact_id=artifact_id,
    )
    result = validate_or_fail(
        project,
        artifact_type="rolling_patch",
        payload=parsed,
        context=validation.ValidationContext(
            index=foundation_reference_index(project),
            eligible_chapter_ids=frozenset(eligible_ids),
            allow_relationship_replan=project.config.allow_relationship_replan,
            reviewed_chapter_range=(reviewed["start"], reviewed["end"]),
        ),
        operation_id=op_id,
        now=now,
        label="rolling_patch",
        raw_output_ref=call.raw_ref,
        artifact_id=artifact_id,
        extra_issues=_rolling_issues(
            project,
            parsed,
            plan_payload=plan_payload,
            eligible_ids=eligible_ids,
            latest_final=latest_final,
        ),
    )

    envelope = _envelope_or_new(project, artifact_id, "rolling_patch", now=now)
    envelope = lifecycle.set_candidate(
        envelope,
        parsed,
        source=payload_source(call, operation_id=op_id),
        dependency_pins=bundle.dependency_pins,
        validation=result,
        now=now,
    )
    storage.save_artifact(project, envelope, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_id,
        message=(
            f"Đã lưu Rolling proposal `{artifact_id}` status=draft "
            "(chưa accept; Short Plan chưa đổi)."
        ),
        data={
            "status": envelope.status.value,
            "proposal_status": parsed.status.value,
            "arc_id": target_arc_id,
            "reviewed_chapter_range": reviewed,
            "eligible_chapters": eligible,
            "short_plan_changes": len(parsed.short_plan_changes),
            "relationship_plan_changes": len(parsed.relationship_plan_changes),
            "deviations": len(parsed.deviations),
            "blocked_by_authority": len(parsed.blocked_by_authority),
            "raw_output_ref": call.raw_ref,
            "context_id": bundle.context_id,
        },
        validation=result,
    )


def _merge_rolling_patch(
    base: ShortPlanPayload, patch: RollingPatchPayload
) -> tuple[ShortPlanPayload, list[validation.ValidationIssue]]:
    """Ghép patch vào Short Plan, giữ field/chương ngoài patch."""
    chapters = list(base.chapters)
    index = {chapter.chapter_id: position for position, chapter in enumerate(chapters)}
    issues: list[validation.ValidationIssue] = []
    for position, change in enumerate(patch.short_plan_changes):
        target_position = index.get(change.chapter_id)
        if target_position is None:
            issues.append(
                validation.ValidationIssue(
                    path=validation.json_pointer(
                        "payload", "short_plan_changes", position, "chapter_id"
                    ),
                    code="unknown_reference",
                    message=f"`{change.chapter_id}` không có trong Short Plan hiện hành.",
                )
            )
            continue
        data = chapters[target_position].model_dump(mode="json")
        data.update(change.changes)
        try:
            chapters[target_position] = ChapterPlan.model_validate(data)
        except ValidationError as exc:
            issues.extend(
                validation.issues_from_pydantic_error(
                    exc,
                    base_path=validation.json_pointer(
                        "payload", "short_plan_changes", position, "changes"
                    ),
                )
            )
    for position, change in enumerate(patch.relationship_plan_changes):
        target_position = index.get(change.chapter_id)
        if target_position is None:
            issues.append(
                validation.ValidationIssue(
                    path=validation.json_pointer(
                        "payload", "relationship_plan_changes", position, "chapter_id"
                    ),
                    code="unknown_reference",
                    message=f"`{change.chapter_id}` không có trong Short Plan hiện hành.",
                )
            )
            continue
        chapters[target_position] = chapters[target_position].model_copy(
            update={"relationship_changes": list(change.relationship_changes)}
        )
    return base.model_copy(update={"chapters": chapters}), issues


def _rolling_candidate(
    project: Project,
) -> tuple[ArtifactEnvelope[Any], ArtifactEnvelope[Any], ShortPlanPayload]:
    """Tìm rolling patch candidate của arc hiện hành và Short Plan accepted."""
    plan_envelope = require_accepted_artifact(project, SHORT_PLAN_ARTIFACT_ID)
    plan_payload = plan_envelope.accepted_revision.payload if plan_envelope.accepted_revision else None
    assert plan_payload is not None
    artifact_id = _rolling_artifact_id(plan_payload.arc_id)
    patch_envelope = storage.load_artifact(project, artifact_id)
    if patch_envelope is None:
        raise GuardError(
            f"Chưa có Rolling proposal `{artifact_id}` cho arc `{plan_payload.arc_id}`.",
            code="missing_artifact",
            details={"artifact_id": artifact_id},
        )
    return plan_envelope, patch_envelope, plan_payload


def accept_rolling(
    project: Project,
    *,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Ghép Rolling patch vào candidate Short Plan rồi accept trong một transaction.

    Từ chối (không apply một phần) nếu patch đụng chapter đã final/finalizing,
    ngoài eligible scope, vượt Long Plan, vi phạm config quan hệ hoặc pin đã lệch.
    Sau accept, Rolling proposal được đánh dấu accepted như dấu vết đã apply.
    """
    op_id = operation_id or generate_operation_id()
    plan_envelope, patch_envelope, plan_payload = _rolling_candidate(project)
    candidate = patch_envelope.candidate_revision
    if candidate is None:
        same_source = (
            bool(operation_id)
            and patch_envelope.accepted_revision is not None
            and patch_envelope.accepted_revision.payload_source.operation_id == operation_id
        )
        if patch_envelope.accepted_revision is not None and (
            same_source
            or artifact_write_committed(
                project, operation_id, patch_envelope.artifact_id, "rolling_patch"
            )
        ):
            return ActionResult(
                operation_id=op_id,
                artifact_id=patch_envelope.artifact_id,
                message="Rolling proposal đã được apply trước đó cho cùng operation_id.",
                data={"status": patch_envelope.status.value, "idempotent": True},
                validation=patch_envelope.accepted_revision.validation,
            )
        raise GuardError(
            f"`{patch_envelope.artifact_id}` không có candidate để accept.",
            code="missing_candidate",
        )
    patch: RollingPatchPayload = candidate.payload
    check_pin_freshness(project, candidate.dependency_pins, artifact_id=patch_envelope.artifact_id)

    arc = _find_arc(_accepted_long_plan(project), plan_payload.arc_id)
    if arc is None:
        raise GuardError(
            f"Arc `{plan_payload.arc_id}` không còn trong Long Plan accepted.",
            code="unknown_arc",
        )
    eligible_ids = {item["chapter_id"] for item in eligible_chapters_for_rolling(project)}
    latest_final = _latest_final_chapter_number(project)
    patch_issues = _rolling_issues(
        project,
        patch,
        plan_payload=plan_payload,
        eligible_ids=eligible_ids,
        latest_final=latest_final,
    )
    patch_result = validate_or_fail(
        project,
        artifact_type="rolling_patch",
        payload=patch,
        context=validation.ValidationContext(
            index=foundation_reference_index(project),
            eligible_chapter_ids=frozenset(eligible_ids),
            allow_relationship_replan=project.config.allow_relationship_replan,
            reviewed_chapter_range=(
                patch.reviewed_chapter_range.start,
                patch.reviewed_chapter_range.end,
            ),
        ),
        operation_id=op_id,
        now=now,
        label="rolling_patch.accept",
        raw_output_ref=candidate.payload_source.raw_output_ref,
        artifact_id=patch_envelope.artifact_id,
        extra_issues=patch_issues,
    )

    base_payload = (
        plan_envelope.candidate_revision.payload
        if plan_envelope.candidate_revision is not None
        else plan_payload
    )
    merged, merge_issues = _merge_rolling_patch(base_payload, patch)
    touched_ids = {
        change.chapter_id for change in patch.short_plan_changes
    } | {change.chapter_id for change in patch.relationship_plan_changes}
    merged_result = validate_or_fail(
        project,
        artifact_type="short_plan",
        payload=merged,
        context=validation.ValidationContext(index=foundation_reference_index(project)),
        operation_id=op_id,
        now=now,
        label="short_plan.rolling_merge",
        raw_output_ref=candidate.payload_source.raw_output_ref,
        artifact_id=SHORT_PLAN_ARTIFACT_ID,
        extra_issues=[
            *merge_issues,
            *_accept_scope_issues(project, merged, arc=arc, touched_ids=touched_ids),
        ],
    )

    staged_plan = lifecycle.set_candidate(
        plan_envelope,
        merged,
        source=PayloadSource(
            source_type=SourceType.service,
            operation_id=op_id,
            raw_output_ref=candidate.payload_source.raw_output_ref,
        ),
        dependency_pins=candidate.dependency_pins,
        validation=merged_result,
        now=now,
    )
    accepted_patch = lifecycle.accept_candidate(
        patch_envelope, accepted_by=accepted_by, validation=patch_result, now=now
    )
    accepted_plan, chapters, marked = _commit_plan_accept(
        project,
        plan_envelope=staged_plan,
        merged_payload=merged,
        touched_ids=touched_ids,
        result=merged_result,
        accepted_by=accepted_by,
        operation_id=op_id,
        now=now,
        extra_artifacts=[(patch_envelope.artifact_id, "rolling_patch", accepted_patch)],
    )
    revision = (
        accepted_plan.accepted_revision.revision if accepted_plan.accepted_revision else None
    )
    return ActionResult(
        operation_id=op_id,
        artifact_id=SHORT_PLAN_ARTIFACT_ID,
        message=(
            f"Đã ghép Rolling proposal vào Short Plan r{revision} và cập nhật "
            f"{len(chapters)} chapter metadata."
        ),
        data={
            "status": accepted_plan.status.value,
            "revision": revision,
            "rolling_patch_id": patch_envelope.artifact_id,
            "chapters": [chapter.chapter_id for chapter in chapters],
            "changed_chapters": sorted(touched_ids),
            "stale_marked": marked,
        },
        validation=merged_result,
    )


def reject_rolling(
    project: Project,
    *,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Reject Rolling proposal; Short Plan accepted không đổi."""
    op_id = operation_id or generate_operation_id()
    _plan_envelope, patch_envelope, _plan_payload = _rolling_candidate(project)
    if patch_envelope.candidate_revision is None:
        raise GuardError(
            f"`{patch_envelope.artifact_id}` không có candidate để reject.",
            code="missing_candidate",
        )
    rejected = lifecycle.reject_candidate(patch_envelope)
    storage.save_artifact(project, rejected, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=patch_envelope.artifact_id,
        message="Đã reject Rolling proposal; Short Plan accepted giữ nguyên.",
        data={
            "status": rejected.status.value,
            "revision": rejected.accepted_revision.revision if rejected.accepted_revision else None,
        },
    )
