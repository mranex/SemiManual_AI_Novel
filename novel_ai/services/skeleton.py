"""Skeleton service: sinh, sửa, accept/reject Skeleton của một chapter (T15).

Hiện thực `docs/design/workflow.md` mục 4.3 và `docs/design/schemas.md` mục 4,
4.1:

- `generate` build context `skeleton.v1` (có thể là `actual` hoặc `provisional`
  theo quyền chuẩn bị trước của spec mục 4), lưu raw **trước** khi parse, parse
  `SkeletonPayload`, map ID tạm `tmp_section_<n>` sang stable ID đã reserve một
  lần cho candidate/run, validate cross-field rồi lưu **candidate**.
- Candidate provisional **không** được accept/auto-accept: `accept` từ chối khi
  chapter còn dấu `preparation_context` mode `provisional` (pins/context basis
  cũ) cho tới khi user regenerate/edit trên actual.
- `accept` set `chapter.skeleton_pin`, đưa chapter sang `skeleton_ready` khi
  guard chương trước cho phép (ngược lại giữ `planned`) và lưu snapshot.
- `reject` bỏ candidate, **giữ nguyên** accepted cũ.
- `edit_candidate` nhận payload do người dùng sửa (stable ID, không ID tạm).

Quyết định triển khai (ghi ở bàn giao T15):

- `preparation_context` (ContextBasis + pins của plan nguồn) nằm ở
  `ChapterMetadata.preparation_context` — field app-owned đã có trong T08 cho
  candidate Skeleton/Short Plan; `ArtifactRevision` không có field này nên
  không nhét thêm shape mới vào payload.
- `id_map` lưu trong `PayloadSource.id_map`; `lifecycle.set_candidate` dùng
  chính id_map đó để retry cùng run không sinh revision trùng.
- Mọi lần chạy `generate` ghi operation record để rerun/retry UI không lặp
  request (retry cùng `operation_id` là replay, không gọi LLM).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from novel_ai import config as app_config
from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.context import ContextBundle, ContextError, build_skeleton_context
from novel_ai.core.llm import LLMClient, LLMError, LLMRequest, StructuredOutputParseError, parse_structured_text
from novel_ai.core.models import (
    ArtifactStatus,
    ChapterMetadata,
    ChapterStatus,
    DependencyPin,
    PayloadSource,
    SkeletonPayload,
    SourceType,
    generate_operation_id,
    is_temporary_id,
    now_iso,
    reserve_id_pool,
)
from novel_ai.core.project import Project
from novel_ai.core.prompts import PromptError, PromptRegistry, render_prompt
from novel_ai.services import (
    ActionResult,
    GuardError,
    LLMUnavailableError,
    ServiceError,
    StaleDependencyError,
    ValidationFailure,
)
from novel_ai.core.generation import EventSink, GenerationEmitter
from novel_ai.services.co_create import generation_stage, structured_transport

__all__ = [
    "SKELETON_PROMPT_ID",
    "accept",
    "assign_section_ids",
    "edit_candidate",
    "generate",
    "load_accepted",
    "reject",
]

SKELETON_PROMPT_ID = "skeleton.v1"

OPERATION_GENERATE = "skeleton_generate"
OPERATION_EDIT = "skeleton_edit_candidate"
OPERATION_ACCEPT = "skeleton_accept"
OPERATION_REJECT = "skeleton_reject"

#: Số ID dự phòng thêm cho section mới ngoài số section đã có.
SECTION_POOL_SPARE = 6


# ---------------------------------------------------------------------------
# Hạ tầng dùng chung trong module
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _registry(repo_root: str) -> PromptRegistry:
    return PromptRegistry.load(repo_root=Path(repo_root))


def _render(project: Project, bundle: ContextBundle):
    root = Path(app_config.REPO_ROOT)
    try:
        return render_prompt(
            _registry(str(root)),
            SKELETON_PROMPT_ID,
            inputs=bundle.payload,
            repo_root=root,
            config=project.config,
        )
    except PromptError as exc:
        raise ServiceError(str(exc), code=exc.code) from exc


def _map_context_error(exc: ContextError) -> ServiceError:
    if exc.code in {"stale_dependency"}:
        return StaleDependencyError(str(exc), code=exc.code, details=exc.details)
    return GuardError(str(exc), code=exc.code, details=exc.details)


def _skeleton_artifact_id(chapter_id: str) -> str:
    return lifecycle.artifact_id_for("skeleton", chapter_id=chapter_id)


def _operation_record_path(project: Project, chapter_id: str, operation_id: str) -> Path:
    relpath = f"chapters/{chapter_id}/operations/{operation_id}.json"
    return storage.ensure_within_project(project.root, project.root / relpath)


def _load_operation_record(
    project: Project, chapter_id: str, operation_id: str
) -> dict[str, Any] | None:
    path = _operation_record_path(project, chapter_id, operation_id)
    if not path.is_file():
        return None
    data = storage.read_json(path)
    return dict(data) if isinstance(data, dict) else None


def _save_operation_record(
    project: Project, chapter_id: str, record: dict[str, Any], *, operation_id: str
) -> None:
    path = _operation_record_path(project, chapter_id, str(record["operation_id"]))
    storage.write_json_atomic(path, record, operation_id=f"{operation_id}.meta")


def _replay_result(record: dict[str, Any], *, action: str) -> ActionResult:
    return ActionResult(
        artifact_id=record.get("artifact_id"),
        chapter_id=record.get("chapter_id"),
        operation_id=record.get("operation_id"),
        message=(
            f"`{action}` với operation_id `{record.get('operation_id')}` đã được xử lý; "
            "không gọi LLM và không tạo candidate mới."
        ),
        warnings=["Idempotent replay theo operation_id."],
        data=dict(record),
    )


def _day_of(stamp: str) -> str | None:
    head = str(stamp)[:10]
    return head if len(head) == 10 and head[4] == "-" and head[7] == "-" else None


def _require_chapter(project: Project, chapter_id: str) -> ChapterMetadata:
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        raise GuardError(
            f"Chưa có chapter metadata cho `{chapter_id}`; Skeleton thuộc một chapter đã có trong Short Plan.",
            code="chapter_missing",
        )
    if chapter.status is ChapterStatus.final_reconciled:
        raise GuardError(
            f"`{chapter_id}` đã `final_reconciled`; đổi Skeleton phải đi qua action retcon.",
            code="chapter_already_final",
        )
    if chapter.status is ChapterStatus.finalizing:
        raise GuardError(
            f"`{chapter_id}` đang `finalizing`; hoàn tất hoặc hủy finalize trước khi đổi Skeleton.",
            code="chapter_finalizing",
        )
    return chapter


# ---------------------------------------------------------------------------
# Reference index và ID section
# ---------------------------------------------------------------------------


def _accepted_payload(project: Project, artifact_id: str) -> Any:
    """Payload accepted của một artifact; `None` nếu thiếu/không accepted/stale."""
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or envelope.accepted_revision is None:
        return None
    if envelope.status is not ArtifactStatus.accepted:
        return None
    return envelope.accepted_revision.payload


def _reference_index(project: Project):
    """Index FK từ các artifact **accepted** (candidate/stale không được tính)."""
    characters = _accepted_payload(project, "characters")
    world_rules = _accepted_payload(project, "world_rules")
    foreshadows = _accepted_payload(project, "foreshadow")
    long_plan = _accepted_payload(project, "long_plan")
    short_plan = _accepted_payload(project, "short_plan")
    chapters = [
        chapter
        for chapter in (
            storage.load_chapter(project, chapter_id)
            for chapter_id in storage.list_chapter_ids(project)
        )
        if chapter is not None
    ]
    return validation.build_reference_index(
        characters=getattr(characters, "characters", None) or [],
        world_rules=getattr(world_rules, "world_rules", None) or [],
        foreshadows=getattr(foreshadows, "foreshadows", None) or [],
        long_plan_arcs=[
            arc for volume in (getattr(long_plan, "volumes", None) or []) for arc in volume.arcs
        ],
        long_plan_volumes=getattr(long_plan, "volumes", None) or [],
        short_plan_chapters=getattr(short_plan, "chapters", None) or [],
        chapters=chapters,
        relationships=storage.load_relationships(project),
    )


def _existing_section_ids(project: Project, chapter_id: str) -> list[str]:
    """Section ID đã có trong accepted và candidate của chapter (không trùng lại)."""
    artifact_id = _skeleton_artifact_id(chapter_id)
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None:
        return []
    ids: list[str] = []
    for revision in (envelope.accepted_revision, envelope.candidate_revision):
        if revision is None:
            continue
        payload = revision.payload
        for section in getattr(payload, "sections", None) or []:
            section_id = getattr(section, "section_id", None)
            if isinstance(section_id, str) and section_id:
                ids.append(section_id)
    return ids


def assign_section_ids(project: Project, chapter_id: str, count: int) -> list[str]:
    """Pool `section_<n>` mới, không trùng accepted/candidate của chapter đó."""
    if count < 0:
        raise ServiceError("`count` của assign_section_ids phải >= 0.", code="invalid_argument")
    if count == 0:
        return []
    return reserve_id_pool("section", count, _existing_section_ids(project, chapter_id))


def _section_pool(project: Project, chapter_id: str) -> list[str]:
    """Pool dự phòng cho section mới: sau section ID đã có của chapter."""
    existing = _existing_section_ids(project, chapter_id)
    stable = [item for item in existing if not is_temporary_id(item)]
    count = max(4, len(stable)) + SECTION_POOL_SPARE
    return assign_section_ids(project, chapter_id, count)


def _find_temporary_ids(value: Any, *, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_find_temporary_ids(child, path=f"{path}/{key}"))
    elif isinstance(value, (list, tuple)):
        for position, child in enumerate(value):
            found.extend(_find_temporary_ids(child, path=f"{path}/{position}"))
    elif isinstance(value, str) and is_temporary_id(value):
        found.append(value)
    return found


def _replace_leaf_ids(value: Any, id_map: dict[str, str]) -> Any:
    """Thay ID tạm ở **vị trí định danh** (string khớp chính xác), không thay substring."""
    if isinstance(value, dict):
        return {key: _replace_leaf_ids(child, id_map) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_leaf_ids(child, id_map) for child in value]
    if isinstance(value, str) and value in id_map:
        return id_map[value]
    return value


def _map_temporary_section_ids(
    payload: SkeletonPayload,
    *,
    project: Project,
    chapter_id: str,
) -> tuple[SkeletonPayload, dict[str, str]]:
    """Map `tmp_section_<n>` sang stable ID đã reserve một lần cho candidate/run.

    Trả `(payload đã normalize, id_map)`. ID tạm còn lại sau mapping (dùng sai vị
    trí, không khai báo) bị từ chối thay vì đi vào accepted data.
    """
    data = payload.model_dump(mode="json")
    sections = data.get("sections") or []
    stable_used = {
        str(section.get("section_id"))
        for section in sections
        if not is_temporary_id(str(section.get("section_id") or ""))
    }
    available = [item for item in _section_pool(project, chapter_id) if item not in stable_used]
    id_map: dict[str, str] = {}
    cursor = 0
    for section in sections:
        section_id = str(section.get("section_id") or "")
        if not is_temporary_id(section_id) or section_id in id_map:
            continue
        if cursor >= len(available):
            # Output cần nhiều section hơn pool dự phòng: reserve thêm một batch,
            # vẫn chỉ map một lần cho candidate/run này.
            extra = reserve_id_pool(
                "section",
                SECTION_POOL_SPARE,
                list(stable_used) + list(id_map.values()) + available,
            )
            available.extend(extra)
        id_map[section_id] = available[cursor]
        cursor += 1

    normalized = _replace_leaf_ids(data, id_map)
    leftovers = sorted(set(_find_temporary_ids(normalized)))
    if leftovers:
        raise ValidationFailure(
            "Skeleton còn ID tạm không khai báo ở vị trí định danh hợp lệ: "
            + ", ".join(leftovers)
            + ". ID tạm chỉ dùng cho section_id của entry mới, không dùng làm FK.",
            code="temporary_id_not_allowed",
            details={"temporary_ids": leftovers},
        )
    return SkeletonPayload.model_validate(normalized), id_map


# ---------------------------------------------------------------------------
# Generate / edit
# ---------------------------------------------------------------------------


def _skeleton_bundle(
    project: Project,
    chapter_id: str,
    *,
    action: str,
    user_instruction: str,
) -> ContextBundle:
    try:
        return build_skeleton_context(
            project,
            chapter_id=chapter_id,
            assigned_section_ids=_section_pool(project, chapter_id),
            action=action,
            user_instruction=user_instruction,
        )
    except ContextError as exc:
        raise _map_context_error(exc) from exc


def _validate_skeleton(
    project: Project, chapter: ChapterMetadata, payload: SkeletonPayload
) -> validation.ValidationResult:
    context = validation.ValidationContext(
        index=_reference_index(project),
        chapter_number=chapter.chapter_number,
    )
    return validation.validate_artifact_payload("skeleton", payload, context=context)


def _save_snapshot(
    project: Project, bundle: ContextBundle, *, action: str, stamp: str, op_id: str
) -> str | None:
    """Lưu snapshot context; lỗi snapshot chỉ là cảnh báo, không làm hỏng candidate.

    Snapshot là bằng chứng tái hiện context, không phải canon. Candidate/accepted
    đã ghi xong nên lỗi snapshot không được biến action thành "thất bại" trong
    khi state đã đổi.
    """
    try:
        snapshot = bundle.to_snapshot(created_from_action=action, created_at=stamp)
        return storage.save_snapshot(project, snapshot, operation_id=op_id)
    except (storage.StorageError, ContextError):
        return None


def generate(
    project: Project,
    *,
    client: LLMClient,
    chapter_id: str,
    action: str = "generate",
    user_instruction: str = "",
    operation_id: str | None = None,
    now: str | None = None,
    on_event: EventSink | None = None,
    stream: bool = False,
    attempt: int = 1,
) -> ActionResult:
    """Sinh Skeleton candidate cho một chapter từ `skeleton.v1`.

    Candidate **không** bao giờ được auto accept, kể cả khi project bật Auto
    Accept và cả khi candidate là provisional. Raw output được lưu trước parse;
    schema sai chỉ tạo lỗi validation, accepted cũ giữ nguyên. `on_event` nhận
    `GenerationEvent` theo contract T33/D019.
    """
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = _require_chapter(project, chapter_id)
    artifact_id = _skeleton_artifact_id(chapter_id)
    emitter = (
        GenerationEmitter(
            operation_id=op_id,
            action=f"skeleton.{action}",
            sink=on_event,
            attempt=attempt,
            prompt_id=SKELETON_PROMPT_ID,
            artifact_id=artifact_id,
            chapter_id=chapter_id,
        )
        if on_event is not None
        else None
    )

    existing = _load_operation_record(project, chapter_id, op_id)
    if existing is not None:
        if emitter is not None:
            emitter.replay(detail="Operation Skeleton đã hoàn tất trước đó; replay theo operation_id.")
        return _replay_result(existing, action=OPERATION_GENERATE)

    bundle = _skeleton_bundle(
        project, chapter_id, action=action, user_instruction=user_instruction
    )
    rendered = _render(project, bundle)
    request = LLMRequest(
        messages=rendered.messages,
        json_output=True,
        prompt_id=rendered.prompt_id,
        prompt_version=rendered.prompt_version,
        prompt_hash=rendered.template_hash,
    )

    transport = structured_transport(
        project,
        client=client,
        request=request,
        operation_id=op_id,
        label=f"skeleton_{chapter_id}",
        emitter=emitter,
        stream=stream,
        now=stamp,
        prompt_id=SKELETON_PROMPT_ID,
    )
    raw_text = transport.text
    raw_ref = transport.raw_ref

    with generation_stage(emitter, detail="Đang parse/validate Skeleton."):
        try:
            parsed = parse_structured_text(raw_text, SkeletonPayload)
        except StructuredOutputParseError as exc:
            raise ValidationFailure(
                f"Skeleton output không parse/không đúng schema: {exc}. Raw đã lưu tại {raw_ref}.",
                result=validation.result_from_issues(exc.errors),
                code="structured_output_parse",
                details={"raw_output_ref": raw_ref},
            ) from exc

        payload, id_map = _map_temporary_section_ids(
            parsed, project=project, chapter_id=chapter_id
        )
        if payload.chapter_id != chapter_id or payload.chapter_number != chapter.chapter_number:
            raise ValidationFailure(
                f"Skeleton output khai `{payload.chapter_id}`/chương {payload.chapter_number} "
                f"nhưng action là `{chapter_id}`/chương {chapter.chapter_number}.",
                code="skeleton_chapter_mismatch",
                details={"raw_output_ref": raw_ref},
            )
        result = _validate_skeleton(project, chapter, payload)
        if not result.is_valid:
            raise ValidationFailure(
                "Skeleton candidate không qua validation: " + validation.summarize_errors(result),
                result=result,
                code="validation_failed",
                details={"raw_output_ref": raw_ref, "id_map": id_map},
            )

    # `preparation_context` là dấu app-owned cho biết candidate dựa trên basis nào.
    # Ghi dấu **trước** candidate để crash giữa chừng không thể tạo ra candidate
    # provisional mà chapter lại không còn dấu (khi đó accept sẽ bị chặn oan).
    chapter_updated = chapter.model_copy(deep=True)
    chapter_updated.preparation_context = bundle.preparation_context
    storage.save_chapter(project, chapter_updated, operation_id=op_id)

    envelope = storage.load_artifact(project, artifact_id) or lifecycle.new_artifact(
        "skeleton", artifact_id
    )
    source = PayloadSource(
        source_type=SourceType.llm,
        prompt_id=rendered.prompt_id,
        prompt_version=rendered.prompt_version,
        prompt_hash=rendered.template_hash,
        operation_id=op_id,
        raw_output_ref=raw_ref,
        id_map=id_map,
    )
    updated = lifecycle.set_candidate(
        envelope,
        payload,
        source=source,
        dependency_pins=bundle.dependency_pins,
        validation=result,
        now=stamp,
    )
    storage.save_artifact(project, updated, operation_id=op_id)
    snapshot_ref = _save_snapshot(
        project, bundle, action=f"skeleton_{action}", stamp=stamp, op_id=op_id
    )

    candidate_revision = updated.candidate_revision.revision if updated.candidate_revision else None
    warnings: list[str] = []
    if snapshot_ref is None:
        warnings.append("Không ghi được snapshot context; candidate vẫn hợp lệ nhưng thiếu bằng chứng tái hiện.")
    if bundle.preparation_context is not None:
        warnings.append(
            "Candidate provisional (chương trước chưa `final_reconciled`): chỉ để chuẩn bị trước, "
            "không dùng được cho Writer và không được accept cho tới khi review/regenerate trên actual."
        )
    warnings.append(
        "Skeleton luôn là candidate; không auto-accept kể cả khi Auto Accept đang bật."
    )
    record = {
        "operation_id": op_id,
        "operation_type": OPERATION_GENERATE,
        "chapter_id": chapter_id,
        "artifact_id": artifact_id,
        "revision": candidate_revision,
        "mode": bundle.mode.value,
        "sections": len(payload.sections),
        "id_map": dict(id_map),
        "raw_output_ref": raw_ref,
        "prompt_id": rendered.prompt_id,
        "prompt_version": rendered.prompt_version,
        "created_at": stamp,
    }
    _save_operation_record(project, chapter_id, record, operation_id=op_id)
    if emitter is not None:
        emitter.saved(
            detail=(
                f"Skeleton candidate r{candidate_revision} đã validate và lưu "
                "(luôn là candidate, không auto accept)."
            ),
            raw_ref=raw_ref,
        )
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_id,
        chapter_id=chapter_id,
        message=(
            f"Đã lưu Skeleton candidate r{candidate_revision} cho `{chapter_id}` "
            f"({len(payload.sections)} section, mode `{bundle.mode.value}`)."
        ),
        warnings=warnings,
        validation=result,
        data=dict(record),
    )


def edit_candidate(
    project: Project,
    *,
    chapter_id: str,
    payload: SkeletonPayload | dict[str, Any],
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Lưu bản Skeleton do người dùng sửa thành candidate mới.

    Payload người dùng phải dùng stable ID (ID tạm chỉ dành cho output LLM và
    được service map). Accepted cũ giữ nguyên cho tới khi `accept` thành công.
    """
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = _require_chapter(project, chapter_id)
    artifact_id = _skeleton_artifact_id(chapter_id)

    existing = _load_operation_record(project, chapter_id, op_id)
    if existing is not None:
        return _replay_result(existing, action=OPERATION_EDIT)

    try:
        parsed = (
            payload
            if isinstance(payload, SkeletonPayload)
            else SkeletonPayload.model_validate(payload)
        )
    except ValidationError as exc:
        issues = validation.issues_from_pydantic_error(exc, base_path="/payload")
        result = validation.result_from_issues(issues)
        raise ValidationFailure(
            f"Skeleton người dùng sửa không đúng schema: {validation.summarize_errors(result)}",
            result=result,
            code="invalid_payload",
        ) from exc

    leftovers = sorted(set(_find_temporary_ids(parsed.model_dump(mode="json"))))
    if leftovers:
        raise ValidationFailure(
            "Bản sửa tay còn ID tạm ("
            + ", ".join(leftovers)
            + "); hãy dùng stable ID đã map hoặc gọi `generate` để backend map.",
            code="temporary_id_not_allowed",
            details={"temporary_ids": leftovers},
        )
    if parsed.chapter_id != chapter_id or parsed.chapter_number != chapter.chapter_number:
        raise ValidationFailure(
            f"Payload khai `{parsed.chapter_id}`/chương {parsed.chapter_number} nhưng chapter là "
            f"`{chapter_id}`/chương {chapter.chapter_number}.",
            code="skeleton_chapter_mismatch",
        )

    result = _validate_skeleton(project, chapter, parsed)
    if not result.is_valid:
        raise ValidationFailure(
            "Skeleton sửa tay không qua validation: " + validation.summarize_errors(result),
            result=result,
            code="validation_failed",
        )

    # D014: edit là một lần user review trên basis hiện tại. Nếu actual đầu chương
    # đã đủ thì basis mới là actual và dấu provisional cũ được xóa cùng pins mới.
    current = _skeleton_bundle(project, chapter_id, action="edit", user_instruction="")

    envelope = storage.load_artifact(project, artifact_id) or lifecycle.new_artifact(
        "skeleton", artifact_id
    )
    source = PayloadSource(
        source_type=SourceType.user,
        operation_id=op_id,
        id_map={},
    )
    updated = lifecycle.set_candidate(
        envelope,
        parsed,
        source=source,
        dependency_pins=current.dependency_pins,
        validation=result,
        now=stamp,
    )
    storage.save_artifact(project, updated, operation_id=op_id)

    chapter_updated = chapter.model_copy(deep=True)
    chapter_updated.preparation_context = current.preparation_context
    storage.save_chapter(project, chapter_updated, operation_id=op_id)

    candidate_revision = updated.candidate_revision.revision if updated.candidate_revision else None
    record = {
        "operation_id": op_id,
        "operation_type": OPERATION_EDIT,
        "chapter_id": chapter_id,
        "artifact_id": artifact_id,
        "revision": candidate_revision,
        "mode": current.mode.value,
        "sections": len(parsed.sections),
        "created_at": stamp,
    }
    _save_operation_record(project, chapter_id, record, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_id,
        chapter_id=chapter_id,
        message=f"Đã lưu Skeleton candidate r{candidate_revision} cho `{chapter_id}` (bản sửa tay).",
        warnings=[
            "Candidate mới chưa được accept; accepted cũ vẫn nguyên cho tới khi accept thành công."
        ],
        validation=result,
        data=dict(record),
    )


# ---------------------------------------------------------------------------
# Accept / reject
# ---------------------------------------------------------------------------


def _chapter_by_number(project: Project, number: int) -> ChapterMetadata | None:
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is not None and chapter.chapter_number == number:
            return chapter
    return None


def _previous_chapter_ready(project: Project, chapter: ChapterMetadata) -> bool:
    """Guard chương trước cho `skeleton_ready`: chương 1 luôn đủ, N>1 cần N-1 final."""
    if chapter.chapter_number <= 1:
        return True
    previous = (
        storage.load_chapter(project, chapter.previous_chapter_id)
        if chapter.previous_chapter_id
        else _chapter_by_number(project, chapter.chapter_number - 1)
    )
    return previous is not None and previous.status is ChapterStatus.final_reconciled


def accept(
    project: Project,
    *,
    chapter_id: str,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Accept Skeleton candidate: set `chapter.skeleton_pin` và mở Writer nếu đủ guard.

    Từ chối khi candidate còn dựa trên context `provisional` (chưa được review
    trên actual), khi pin lệch accepted hiện tại, khi payload còn ID tạm hoặc
    khi validation không pass. Accepted cũ không bị thay khi accept thất bại.
    """
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = _require_chapter(project, chapter_id)
    artifact_id = _skeleton_artifact_id(chapter_id)

    existing = _load_operation_record(project, chapter_id, op_id)
    if existing is not None:
        return _replay_result(existing, action=OPERATION_ACCEPT)

    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or envelope.candidate_revision is None:
        raise GuardError(
            f"`{artifact_id}` không có candidate để accept.", code="missing_candidate"
        )

    preparation = chapter.preparation_context
    if preparation is not None and preparation.context_basis.mode.value == "provisional":
        raise GuardError(
            "Candidate Skeleton được tạo với context `provisional` (chương trước chưa "
            "`final_reconciled`). Phải review/regenerate trên context actual rồi mới accept.",
            code="provisional_candidate_not_writer_ready",
            details={"context_basis": preparation.context_basis.model_dump(mode="json")},
        )

    candidate = envelope.candidate_revision
    payload = candidate.payload
    leftovers = sorted(
        set(_find_temporary_ids(payload.model_dump(mode="json")))
    )
    if leftovers:
        raise GuardError(
            "Candidate còn ID tạm chưa được map ("
            + ", ".join(leftovers)
            + "); không được accept vào data ổn định.",
            code="temporary_id_not_mapped",
            details={"temporary_ids": leftovers},
        )
    if payload.chapter_id != chapter_id or payload.chapter_number != chapter.chapter_number:
        raise GuardError(
            f"Candidate khai `{payload.chapter_id}`/chương {payload.chapter_number} nhưng chapter là "
            f"`{chapter_id}`/chương {chapter.chapter_number}.",
            code="skeleton_chapter_mismatch",
        )

    result = _validate_skeleton(project, chapter, payload)
    if not result.is_valid:
        raise ValidationFailure(
            "Skeleton candidate không qua validation: " + validation.summarize_errors(result),
            result=result,
            code="validation_failed",
        )

    mismatches = storage.pin_mismatch_messages(
        candidate.dependency_pins, storage.current_artifact_revisions(project)
    )
    if mismatches:
        raise StaleDependencyError(
            f"Candidate `{artifact_id}` r{candidate.revision} dựa trên pin cũ: "
            + "; ".join(mismatches)
            + ". Cần regenerate trước khi accept.",
            code="stale_candidate",
            details={"mismatches": mismatches},
        )

    updated = lifecycle.accept_candidate(
        envelope, accepted_by=accepted_by, validation=result, now=stamp
    )
    storage.save_artifact(project, updated, operation_id=op_id)

    accepted_revision = (
        updated.accepted_revision.revision if updated.accepted_revision is not None else None
    )
    chapter_updated = chapter.model_copy(deep=True)
    chapter_updated.skeleton_pin = DependencyPin(
        artifact_id=artifact_id,
        revision=accepted_revision or 1,
        scope="skeleton",
        chapter_id=chapter_id,
    )
    if chapter.status is not ChapterStatus.final_reconciled:
        chapter_updated.status = (
            ChapterStatus.skeleton_ready
            if _previous_chapter_ready(project, chapter)
            else ChapterStatus.planned
        )
    chapter_updated.preparation_context = None
    storage.save_chapter(project, chapter_updated, operation_id=op_id)

    try:
        bundle = _skeleton_bundle(
            project, chapter_id, action="accept", user_instruction=""
        )
    except (GuardError, StaleDependencyError):
        bundle = None
    snapshot_ref = (
        _save_snapshot(project, bundle, action="accept_skeleton", stamp=stamp, op_id=op_id)
        if bundle is not None
        else None
    )

    warnings: list[str] = []
    if snapshot_ref is None:
        warnings.append("Không ghi được snapshot context cho lần accept; accepted vẫn hợp lệ.")
    if chapter_updated.status is ChapterStatus.planned:
        warnings.append(
            f"Chapter `{chapter_id}` giữ `planned` vì chương trước chưa `final_reconciled`; "
            "Writer vẫn bị khóa."
        )
    record = {
        "operation_id": op_id,
        "operation_type": OPERATION_ACCEPT,
        "chapter_id": chapter_id,
        "artifact_id": artifact_id,
        "revision": accepted_revision,
        "status": chapter_updated.status.value,
        "accepted_by": accepted_by,
        "created_at": stamp,
    }
    _save_operation_record(project, chapter_id, record, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_id,
        chapter_id=chapter_id,
        message=(
            f"Đã accept Skeleton `{artifact_id}` r{accepted_revision}; "
            f"chapter `{chapter_id}` → `{chapter_updated.status.value}`."
        ),
        warnings=warnings,
        validation=result,
        data=dict(record),
    )


def reject(
    project: Project,
    *,
    chapter_id: str,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Reject Skeleton candidate; accepted cũ (nếu có) giữ nguyên."""
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = _require_chapter(project, chapter_id)
    artifact_id = _skeleton_artifact_id(chapter_id)

    existing = _load_operation_record(project, chapter_id, op_id)
    if existing is not None:
        return _replay_result(existing, action=OPERATION_REJECT)

    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or envelope.candidate_revision is None:
        raise GuardError(
            f"`{artifact_id}` không có candidate để reject.", code="missing_candidate"
        )

    rejected_revision = envelope.candidate_revision.revision
    updated = lifecycle.reject_candidate(envelope)
    storage.save_artifact(project, updated, operation_id=op_id)

    chapter_updated = chapter.model_copy(deep=True)
    chapter_updated.preparation_context = None
    storage.save_chapter(project, chapter_updated, operation_id=op_id)

    record = {
        "operation_id": op_id,
        "operation_type": OPERATION_REJECT,
        "chapter_id": chapter_id,
        "artifact_id": artifact_id,
        "revision": rejected_revision,
        "status": updated.status.value,
        "created_at": stamp,
    }
    _save_operation_record(project, chapter_id, record, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_id,
        chapter_id=chapter_id,
        message=(
            f"Đã reject Skeleton candidate r{rejected_revision} của `{chapter_id}`; "
            "accepted cũ (nếu có) không đổi."
        ),
        data=dict(record),
    )


# ---------------------------------------------------------------------------
# Đọc
# ---------------------------------------------------------------------------


def load_accepted(project: Project, chapter_id: str) -> SkeletonPayload | None:
    """Payload Skeleton **accepted** của chapter, hoặc `None` nếu chưa có/stale."""
    envelope = storage.load_artifact(project, _skeleton_artifact_id(chapter_id))
    if envelope is None or envelope.accepted_revision is None:
        return None
    if envelope.status is not ArtifactStatus.accepted:
        return None
    payload = envelope.accepted_revision.payload
    return payload if isinstance(payload, SkeletonPayload) else SkeletonPayload.model_validate(payload)
