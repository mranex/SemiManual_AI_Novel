"""Upstream revision, stale propagation và retcon (T18).

Module này hiện thực `docs/design/workflow.md` mục 4.1, 4.5, 5.2 và
`docs/design/storage.md` mục 8, 10 cho phần "sửa upstream / retcon":

- `revise_base_idea` / `revise_premise` tạo accepted revision mới rồi đánh dấu
  stale downstream theo bảng propagation. Chúng **không** rewrite Final
  Manuscript, không đụng raw/history và không tự regenerate gì.
- `start_retcon` tạo draft retcon từ Final Manuscript; final cũ **vẫn là canon**
  cho tới khi commit retcon hoàn tất (D004). Chapter không tự về `draft` như một
  draft thường: trạng thái "đang retcon" được biểu diễn bằng marker
  `chapters/<ch>/retcon/state.json` (xem bàn giao T18).
- `generate_impact_report` chỉ lưu report `draft`; backend deterministic mới là
  nơi đánh dấu stale, report không tự apply.
- `reset_consistency_after_retcon` set `latest_consistent_chapter` của cả
  timeline lẫn relationship về chương retcon và đánh dấu stale entry/state sau
  đó. Nó **tuyệt đối không** ghi lùi `current_timeline`/relationship `current`
  về state của chương retcon như thể các chương sau biến mất.
- `reconcile_downstream` rebuild lại state của một chương downstream bằng state
  as-of chương trước qua đúng transaction của `services.reconcile`.

Module không phụ thuộc Streamlit và không tự chạy bước tiếp.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.context import build_retcon_impact_context
from novel_ai.core.llm import (
    LLMClient,
    LLMError,
    LLMRequest,
    generate_structured,
)
from novel_ai.core.models import (
    ArtifactEnvelope,
    ArtifactStatus,
    ChapterMetadata,
    ChapterStatus,
    DependencyPin,
    ImpactReportPayload,
    PayloadSource,
    PremisePayload,
    ProseRevision,
    SourceChange,
    SourceType,
    StructuredOutputError,
    ValidationState,
    generate_error_id,
    generate_operation_id,
    now_iso,
)
from novel_ai.core.prompts import PromptRegistry, render_prompt
from novel_ai.core.generation import EventSink, GenerationEmitter
from novel_ai.core.project import Project
from novel_ai.services import (
    ActionResult,
    GuardError,
    LLMUnavailableError,
    ServiceError,
    StaleDependencyError,
    ValidationFailure,
)
from novel_ai.services.co_create import transport_complete_seam
from novel_ai.services import reconcile as reconcile_service

__all__ = [
    "IMPACT_PROMPT_ID",
    "RETCON_MARKER_NAME",
    "downstream_blockers",
    "downstream_proposal_operation_id",
    "generate_impact_report",
    "mark_downstream_stale",
    "reaccept_stale",
    "reconcile_downstream",
    "reset_consistency_after_retcon",
    "retcon_state",
    "revise_base_idea",
    "revise_premise",
    "start_retcon",
]

#: Prompt v1 cho impact report.
IMPACT_PROMPT_ID = "retcon_impact.v1"

#: Tên file marker cho biết chapter đang trong quá trình retcon.
RETCON_MARKER_NAME = "state.json"

_OPERATION_REVISE_BASE_IDEA = "revise_base_idea"
_OPERATION_REVISE_PREMISE = "revise_premise"
_OPERATION_START_RETCON = "start_retcon"
_OPERATION_IMPACT_REPORT = "generate_impact_report"
_OPERATION_REACCEPT = "reaccept_stale"
_OPERATION_RESET_CONSISTENCY = "reset_consistency_after_retcon"
_OPERATION_RECONCILE_DOWNSTREAM = "reconcile_downstream"


# ---------------------------------------------------------------------------
# Helper chung
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(storage.__file__).resolve().parents[2]


def _abort_quietly(handle: storage.OperationHandle) -> None:
    """Abort rồi giữ nguyên exception gốc (abort giữa commit bị từ chối)."""
    try:
        handle.abort()
    except Exception:  # RecoveryRequired khi đã replace một phần target
        return


def _suffix(operation_id: str, suffix: str) -> str:
    return f"{operation_id}.{suffix}"


def downstream_proposal_operation_id(operation_id: str) -> str:
    """`operation_id` của lượt generate proposal bên trong `reconcile_downstream`.

    Export để UI dựng generation surface/recorder cùng một operation id với
    `GenerationEmitter` (T40/UI-02) thay vì tự đoán quy ước hậu tố.
    """
    return _suffix(operation_id, "proposal")


def _chapter_or_fail(project: Project, chapter_id: str) -> ChapterMetadata:
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        raise GuardError(
            f"Chưa có chapter metadata cho `{chapter_id}`.",
            code="chapter_missing",
            details={"chapter_id": chapter_id},
        )
    return chapter


def _artifact_id_for_chapter(artifact_type: str, chapter_id: str) -> str:
    return lifecycle.artifact_id_for(artifact_type, chapter_id=chapter_id)


def _prompt_inputs(bundle: Any, prompt_id: str, registry: PromptRegistry) -> dict[str, Any]:
    spec = registry.get(prompt_id)
    return {name: bundle.payload[name] for name in spec.template_variables if name in bundle.payload}


def _validate_payload(
    artifact_type: str,
    payload: Any,
    *,
    project: Project,
    context: validation.ValidationContext | None = None,
) -> validation.ValidationResult:
    return validation.validate_artifact_payload(artifact_type, payload, context=context)


def _reference_index(project: Project) -> validation.ReferenceIndex:
    from novel_ai.core.context import _State  # nội bộ T12: index accepted đã lọc effective

    return _State.load(project).reference_index()


def _raw_output_for(project: Project, *, operation_id: str, text: str, label: str) -> str:
    return storage.save_raw_output(
        project, operation_id=operation_id, text=text, label=label
    )


def _save_error_record(
    project: Project, *, relpath: str, error: StructuredOutputError, operation_id: str
) -> str:
    handle = storage.begin_operation(
        project,
        operation_type="revision_error",
        operation_id=_suffix(operation_id, "error"),
    )
    try:
        handle.add_json(relpath, error.model_dump(mode="json"))
        handle.commit()
    except Exception:  # pragma: no cover - ghi error record không được làm hỏng action
        _abort_quietly(handle)
    return relpath


# ---------------------------------------------------------------------------
# Revise Base Idea / Premise
# ---------------------------------------------------------------------------


def revise_base_idea(
    project: Project,
    *,
    markdown: str,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Revision mới của Base Idea + đánh dấu stale downstream (storage.md mục 10).

    Chỉ sửa `idea/base_idea.md` và `idea/base_idea.meta.json`; Premise/foundation/
    plan/skeleton/draft/review chưa final bị `stale` (không xóa dữ liệu). Final
    Manuscript, raw và history không đổi.
    """
    text = str(markdown)
    if not text.strip():
        raise ValidationFailure(
            "Base Idea revision không được rỗng; hủy thao tác để giữ bản accepted cũ.",
            code="empty_revision",
        )
    document = storage.load_co_create(project)
    if document is None or document.base_idea is None:
        raise GuardError(
            "Chưa có Base Idea accepted để revise.",
            code="missing_dependency",
        )
    current = document.base_idea
    op_id = operation_id or generate_operation_id()
    replay = _replay_result(project, op_id)
    if replay is not None:
        return replay
    new_revision = current.revision + 1
    stamp = now or now_iso()
    meta = current.model_copy(
        update={
            "status": ArtifactStatus.accepted,
            "revision": new_revision,
            "accepted_at": stamp,
            "accepted_by": accepted_by,
        }
    )
    updated = document.model_copy(update={"base_idea": meta})

    handle = storage.begin_operation(
        project, operation_type=_OPERATION_REVISE_BASE_IDEA, operation_id=op_id
    )
    if handle.replayed:
        return _replay_result(project, op_id) or _stale_replay(handle.manifest)
    try:
        handle.add_text(
            _relative(project, project.paths.base_idea_md), text
        )
        handle.add_json(
            _relative(project, project.paths.base_idea_meta_json),
            meta.model_dump(mode="json"),
        )
        manifest = handle.commit()
    except Exception:
        _abort_quietly(handle)
        raise

    marked = mark_downstream_stale(
        project,
        source_artifact_id="base_idea",
        source_revision=new_revision,
        change=lifecycle.STALE_CHANGE_BASE_IDEA_REVISE,
        now=stamp,
    )
    storage.save_co_create(
        project, updated, operation_id=_suffix(op_id, "co_create")
    )
    return ActionResult(
        operation_id=op_id,
        artifact_id="base_idea",
        message=(
            f"Base Idea đã lên r{new_revision}; {len(marked)} artifact downstream bị đánh dấu "
            "stale (không rewrite final manuscript)."
        ),
        data={
            "revision": new_revision,
            "stale_artifact_ids": marked,
            "applied_paths": list(manifest.applied_paths),
        },
    )


def revise_premise(
    project: Project,
    *,
    payload: Mapping[str, Any] | PremisePayload,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Revision mới của Premise + stale Long/Short Plan, Skeleton, draft/review.

    Base Idea và Final Manuscript không đổi.
    """
    envelope = storage.load_artifact(project, "premise")
    if envelope is None or envelope.accepted_revision is None:
        raise GuardError(
            "Chưa có Premise accepted để revise.", code="missing_dependency"
        )
    try:
        parsed = (
            payload
            if isinstance(payload, PremisePayload)
            else PremisePayload.model_validate(payload)
        )
    except Exception as exc:  # pydantic ValidationError
        raise ValidationFailure(
            f"Premise revision không khớp schema: {exc}", code="invalid_payload"
        ) from exc

    result = _validate_payload("premise", parsed, project=project)
    if result.state is ValidationState.invalid:
        raise ValidationFailure(
            "Premise revision không hợp lệ: " + validation.summarize_errors(result),
            result=result,
        )

    op_id = operation_id or generate_operation_id()
    replay = _replay_result(project, op_id)
    if replay is not None:
        return replay
    accepted = envelope.accepted_revision
    new_revision = accepted.revision + 1
    stamp = now or now_iso()
    updated_envelope = envelope.model_copy(deep=True)
    updated_envelope.accepted_revision = accepted.model_copy(
        update={
            "revision": new_revision,
            "payload": parsed,
            "validation": result,
            "accepted_at": stamp,
            "accepted_by": accepted_by,
        }
    )
    updated_envelope.candidate_revision = None
    updated_envelope.status = ArtifactStatus.accepted
    updated_envelope.stale_reasons = []
    storage.save_artifact(project, updated_envelope, operation_id=_suffix(op_id, "premise"))

    marked = mark_downstream_stale(
        project,
        source_artifact_id="premise",
        source_revision=new_revision,
        change=lifecycle.STALE_CHANGE_PREMISE_REVISE,
        now=stamp,
    )
    return ActionResult(
        operation_id=op_id,
        artifact_id="premise",
        message=(
            f"Premise đã lên r{new_revision}; {len(marked)} artifact downstream bị đánh dấu "
            "stale (Base Idea và Final Manuscript không đổi)."
        ),
        validation=result,
        data={"revision": new_revision, "stale_artifact_ids": marked},
    )


def _relative(project: Project, path: Path) -> str:
    return str(path.relative_to(project.root)).replace("\\", "/")


def _replay_result(project: Project, operation_id: str) -> ActionResult | None:
    """Kết quả cũ nếu `operation_id` đã committed (retry/rerun UI)."""
    done_path = project.paths.ops_done_dir / f"{operation_id}.json"
    if not done_path.is_file():
        return None
    manifest = storage._parse_operation_manifest(storage.read_json(done_path), done_path)
    return _stale_replay(manifest, operation_id=operation_id)


def _stale_replay(manifest: Any, *, operation_id: str | None = None) -> ActionResult:
    return ActionResult(
        operation_id=operation_id or manifest.operation_id,
        message=(
            f"Operation `{manifest.operation_id}` đã commit trước đó; trả kết quả cũ, "
            "không ghi lại."
        ),
        data={
            "replayed": True,
            "operation_type": manifest.operation_type,
            "applied_paths": list(manifest.applied_paths),
            "result": dict(manifest.result or {}),
        },
    )


# ---------------------------------------------------------------------------
# Retcon
# ---------------------------------------------------------------------------


def _retcon_marker_path(project: Project, chapter_id: str) -> str:
    return f"chapters/{chapter_id}/retcon/{RETCON_MARKER_NAME}"


def retcon_state(project: Project, *, chapter_id: str) -> dict[str, Any] | None:
    """Marker "đang retcon" của chapter, ``None`` nếu không trong retcon."""
    target = project.root / _retcon_marker_path(project, chapter_id)
    if not target.is_file():
        return None
    return storage.read_json(target)


def start_retcon(
    project: Project,
    *,
    chapter_id: str,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Tạo draft retcon từ Final Manuscript; final cũ vẫn là canon (D004).

    Precondition: chapter đã `final_reconciled`. Draft retcon là `ProseRevision`
    mới (source `user`) **không** làm chapter rời `final_reconciled`: final cũ vẫn
    là canon và chương sau vẫn dùng state cũ cho tới khi retcon commit. Marker
    `chapters/<ch>/retcon/state.json` (field `retcon_draft_revision`) cho UI biết
    chapter đang có retcon mở; `chapter.final_revision` **không** đổi cho tới khi
    `finalize_chapter` + `accept_reconciliation` cho bản retcon hoàn tất.
    """
    chapter = _chapter_or_fail(project, chapter_id)
    if chapter.status is not ChapterStatus.final_reconciled or chapter.final_revision is None:
        raise GuardError(
            f"{chapter_id} chưa `final_reconciled`; retcon chỉ bắt đầu từ final manuscript.",
            code="chapter_not_final_reconciled",
            details={"chapter_id": chapter_id, "status": chapter.status.value},
        )
    if retcon_state(project, chapter_id=chapter_id) is not None:
        raise GuardError(
            f"{chapter_id} đang trong quá trình retcon; hoàn tất hoặc hủy trước.",
            code="retcon_already_open",
            details={"chapter_id": chapter_id},
        )
    op_id = operation_id or generate_operation_id()
    replay = _replay_result(project, op_id)
    if replay is not None:
        return replay

    stamp = now or now_iso()
    final = chapter.final_revision
    retcon_dir = project.paths.retcon_dir(chapter_id)
    new_revision = max(
        (draft.revision for draft in chapter.drafts), default=0
    ) + 1
    draft_relpath = _relative(
        project, retcon_dir / f"draft_r{new_revision:04d}.md"
    )
    markdown = storage.read_text(project.root / final.markdown_ref)

    marker = {
        "chapter_id": chapter_id,
        "chapter_number": chapter.chapter_number,
        "status": "open",
        "replaces_final_revision": final.revision,
        "replaces_final_markdown_ref": final.markdown_ref,
        "retcon_draft_revision": new_revision,
        "retcon_draft_markdown_ref": draft_relpath,
        "started_at": stamp,
        "final_still_canon": True,
    }
    updated = chapter.model_copy(
        update={
            # Chapter vẫn `final_reconciled`: final cũ là canon tới khi commit retcon.
            "status": ChapterStatus.final_reconciled,
            "drafts": [
                *chapter.drafts,
                ProseRevision(
                    revision=new_revision,
                    markdown_ref=draft_relpath,
                    source_type=SourceType.user,
                    is_complete=True,
                    created_at=stamp,
                    dependency_pins=[
                        DependencyPin(
                            artifact_id=f"chapter_{chapter_id}",
                            revision=final.source_prose_revision,
                            scope="prose_revision",
                            chapter_id=chapter_id,
                        )
                    ],
                ),
            ],
            "current_draft_revision": new_revision,
            "human_review": None,
        }
    )
    handle = storage.begin_operation(
        project, operation_type=_OPERATION_START_RETCON, operation_id=op_id
    )
    if handle.replayed:
        return _stale_replay(handle.manifest)
    try:
        handle.add_text(draft_relpath, markdown)
        handle.add_json(_retcon_marker_path(project, chapter_id), marker)
        handle.add_json(
            _relative(project, project.paths.chapter_json(chapter_id)), updated
        )
        manifest = handle.commit()
    except Exception:
        _abort_quietly(handle)
        raise

    return ActionResult(
        operation_id=op_id,
        chapter_id=chapter_id,
        message=(
            f"{chapter_id}: đã tạo draft retcon r{new_revision} từ final r{final.revision}. "
            "Final cũ **vẫn là canon** tới khi commit retcon hoàn tất."
        ),
        data={
            "status": ChapterStatus.final_reconciled.value,
            "retcon_open": True,
            "retcon_marker_ref": _retcon_marker_path(project, chapter_id),
            "retcon_draft_revision": new_revision,
            "retcon_draft_markdown_ref": draft_relpath,
            "replaces_final_revision": final.revision,
            "final_still_canon": True,
            "applied_paths": list(manifest.applied_paths),
        },
    )


# ---------------------------------------------------------------------------
# Impact report
# ---------------------------------------------------------------------------


def collect_downstream_items(
    project: Project, *, source_change: SourceChange
) -> list[dict[str, Any]]:
    """Chọn deterministic các artifact downstream để đưa vào impact context.

    Chỉ dùng artifact đang có (accepted/candidate) và dependency pins/range; không
    RAG, không để LLM tự tìm dữ liệu.
    """
    chapter_id = (
        source_change.item_id if source_change.item_kind.startswith("chapter") else None
    )
    chapter_number = None
    if chapter_id is not None:
        chapter = storage.load_chapter(project, chapter_id)
        chapter_number = chapter.chapter_number if chapter is not None else None
    items: list[dict[str, Any]] = []
    for artifact_id in storage.list_artifact_ids(project):
        if artifact_id == source_change.item_id:
            continue
        envelope = storage.load_artifact(project, artifact_id)
        if envelope is None:
            continue
        revision = envelope.accepted_revision or envelope.candidate_revision
        if revision is None:
            continue
        artifact_chapter = lifecycle._chapter_number_of_artifact(project, artifact_id)
        if (
            chapter_number is not None
            and artifact_chapter is not None
            and artifact_chapter < chapter_number
        ):
            continue
        summary = _content_summary(artifact_id, envelope.artifact_type, revision.payload)
        items.append(
            {
                "item_kind": envelope.artifact_type,
                "item_id": artifact_id,
                "revision": revision.revision,
                "dependency_pins": [
                    pin.model_dump(mode="json") for pin in revision.dependency_pins
                ],
                "content_summary": summary,
            }
        )
    return items


def _content_summary(artifact_id: str, artifact_type: str, payload: Any) -> str:
    """Tóm tắt KHÔNG chứa author-only/secret để không mở rộng phạm vi report."""
    if artifact_type == "skeleton":
        sections = getattr(payload, "sections", None) or []
        return (
            f"{artifact_id}: {len(sections)} section; "
            + " | ".join(getattr(item, "instruction", "") for item in sections[:4])
        )
    if artifact_type == "short_plan":
        chapters = getattr(payload, "chapters", None) or []
        return f"{artifact_id}: {len(chapters)} chapter trong arc {getattr(payload, 'arc_id', '')}"
    if artifact_type == "review_report":
        return (
            f"{artifact_id}: {getattr(payload, 'summary', '')} "
            f"(prose r{getattr(payload, 'prose_revision', '')})"
        )
    if artifact_type == "reconciliation":
        return f"{artifact_id}: proposal reconciliation cho {getattr(payload, 'chapter_id', '')}"
    if artifact_type == "premise":
        return f"{artifact_id}: logline `{getattr(payload, 'logline', '')}`"
    return f"{artifact_id}: revision của {artifact_type}"


def _impact_artifact_id(source_change: SourceChange) -> str:
    return f"impact_report_{source_change.item_id}"


def generate_impact_report(
    project: Project,
    *,
    client: LLMClient,
    source_change: SourceChange | Mapping[str, Any],
    before_content: str = "",
    after_content: str = "",
    analysis_scope: str = "",
    operation_id: str | None = None,
    now: str | None = None,
    on_event: EventSink | None = None,
    stream: bool = False,
    attempt: int = 1,
) -> ActionResult:
    """Sinh `retcon_impact.v1` và lưu candidate `impact_report_<item_id>` (`draft`).

    Report **không** tự apply và **không** tự đánh dấu stale; stale marking là
    backend deterministic (`mark_downstream_stale`) chạy độc lập.
    """
    try:
        change = (
            source_change
            if isinstance(source_change, SourceChange)
            else SourceChange.model_validate(source_change)
        )
    except Exception as exc:
        raise ValidationFailure(
            f"source_change không hợp lệ: {exc}", code="invalid_source_change"
        ) from exc
    if not change.item_id:
        raise ValidationFailure(
            "source_change thiếu item_id; không gọi LLM.", code="invalid_source_change"
        )

    op_id = operation_id or generate_operation_id()
    replay = _replay_result(project, op_id)
    if replay is not None:
        return replay

    downstream_items = collect_downstream_items(project, source_change=change)
    bundle = build_retcon_impact_context(
        project,
        source_change=change,
        before_content=str(before_content),
        after_content=str(after_content),
        downstream_items=downstream_items,
        analysis_scope=analysis_scope,
    )
    registry = PromptRegistry.load(repo_root=_repo_root())
    rendered = render_prompt(
        registry,
        IMPACT_PROMPT_ID,
        inputs=_prompt_inputs(bundle, IMPACT_PROMPT_ID, registry),
        repo_root=_repo_root(),
        config=project.config,
    )
    request = LLMRequest(
        messages=rendered.messages,
        json_output=True,
        prompt_id=rendered.prompt_id,
        prompt_version=rendered.prompt_version,
        prompt_hash=rendered.template_hash,
    )
    artifact_id = _impact_artifact_id(change)
    emitter = (
        GenerationEmitter(
            operation_id=op_id,
            action="revision.impact_report",
            sink=on_event,
            attempt=attempt,
            prompt_id=IMPACT_PROMPT_ID,
            artifact_id=artifact_id,
        )
        if on_event is not None
        else None
    )
    complete, holder = transport_complete_seam(
        project,
        client=client,
        operation_id=op_id,
        label=f"impact_{change.item_id}",
        emitter=emitter,
        stream=stream,
        now=now,
        prompt_id=IMPACT_PROMPT_ID,
    )
    # `generate_structured` phát transport (qua seam) rồi parse; bước `validating`
    # chỉ bắt đầu sau khi transport xong.
    parsed, raw_text, error = generate_structured(
        client, request, ImpactReportPayload, complete=complete
    )
    if emitter is not None:
        emitter.validating(detail="Đang parse/validate impact report.")

    raw_ref = holder.get("raw_ref") or _raw_output_for(
        project, operation_id=op_id, text=raw_text, label=f"impact_{change.item_id}"
    )
    if parsed is None or error is not None:
        record = (error or StructuredOutputError(
            error_id=generate_error_id(), created_at=now or now_iso()
        )).model_copy(
            update={
                "artifact_id": artifact_id,
                "raw_output_ref": raw_ref,
                "retryable": True,
            }
        )
        error_path = _save_error_record(
            project,
            relpath=f"chapters/{change.item_id}/impact/errors/{record.error_id}.json"
            if change.item_kind.startswith("chapter")
            else f"raw/impact_errors/{record.error_id}.json",
            error=record,
            operation_id=op_id,
        )
        if emitter is not None:
            emitter.invalid(
                detail=(
                    "Impact report không hợp lệ: raw được giữ, không mutation nào "
                    "được thực hiện."
                ),
                raw_ref=raw_ref,
            )
        return ActionResult(
            operation_id=op_id,
            artifact_id=artifact_id,
            message=(
                "Impact report không hợp lệ; raw được giữ để người dùng xử lý. "
                "Không mutation nào được thực hiện."
            ),
            warnings=[
                "; ".join(f"{item.path}: {item.message}" for item in record.errors)
                or "output không khớp schema ImpactReportPayload"
            ],
            data={
                "raw_output_ref": raw_ref,
                "error_record_ref": error_path,
                "error_id": record.error_id,
                "errors": [item.model_dump(mode="json") for item in record.errors],
            },
        )

    result = validation.validate_artifact_payload("impact_report", parsed)
    if result.state is ValidationState.invalid:
        record = StructuredOutputError(
            error_id=generate_error_id(),
            artifact_id=artifact_id,
            raw_output_ref=raw_ref,
            errors=list(result.errors),
            created_at=now or now_iso(),
            retryable=True,
        )
        error_path = _save_error_record(
            project,
            relpath=f"chapters/{change.item_id}/impact/errors/{record.error_id}.json"
            if change.item_kind.startswith("chapter")
            else f"raw/impact_errors/{record.error_id}.json",
            error=record,
            operation_id=op_id,
        )
        if emitter is not None:
            emitter.invalid(
                detail="Impact report sai contract: raw được giữ, không mutation nào được thực hiện.",
                raw_ref=raw_ref,
            )
        return ActionResult(
            operation_id=op_id,
            artifact_id=artifact_id,
            message=(
                "Impact report sai contract; raw được giữ. Không mutation nào được thực hiện."
            ),
            warnings=[validation.summarize_errors(result)],
            validation=result,
            data={"raw_output_ref": raw_ref, "error_record_ref": error_path},
        )

    envelope = storage.load_artifact(project, artifact_id) or lifecycle.new_artifact(
        "impact_report", artifact_id
    )
    candidate = lifecycle.set_candidate(
        envelope,
        parsed,
        source=PayloadSource(
            source_type=SourceType.llm,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.prompt_version,
            prompt_hash=rendered.template_hash,
            operation_id=op_id,
            raw_output_ref=raw_ref,
        ),
        dependency_pins=reconcile_service._dependency_pins(project),
        validation=result,
        now=now,
    )
    storage.save_artifact(project, candidate, operation_id=_suffix(op_id, "impact"))
    if emitter is not None:
        emitter.saved(
            detail=(
                "Impact report đã validate và lưu candidate `draft`; report không tự apply "
                "và không tự đánh dấu stale."
            ),
            raw_ref=raw_ref,
        )
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_id,
        message=(
            "Impact report đã lưu thành candidate `draft`; report không tự apply và backend "
            "mới là nơi đánh dấu stale."
        ),
        validation=result,
        data={
            "raw_output_ref": raw_ref,
            "affected_items": len(parsed.affected_items),
            "candidate_revision": candidate.candidate_revision.revision
            if candidate.candidate_revision
            else None,
        },
    )


# ---------------------------------------------------------------------------
# Stale: mark, reaccept, reset consistency, reconcile downstream
# ---------------------------------------------------------------------------


def mark_downstream_stale(
    project: Project,
    *,
    source_artifact_id: str,
    source_revision: int,
    change: str,
    now: str | None = None,
) -> list[str]:
    """Wrapper deterministic quanh `lifecycle.mark_downstream_stale`.

    Bổ sung đúng luật `storage.md` mục 10 mà helper lifecycle chưa lọc: Skeleton
    của chapter **đã `final_reconciled`** (và report của nó) không bị đánh dấu
    stale khi Premise/Base Idea đổi — Final Manuscript và prose đã final không bị
    rewrite. Artifact bị đánh dấu nhầm được trả về trạng thái `accepted` ngay
    trong cùng operation, không mất dữ liệu.
    """
    if change not in lifecycle.STALE_CHANGES:
        raise ServiceError(
            f"change `{change}` không nằm trong bảng stale propagation.",
            code="unknown_stale_change",
            details={"change": change},
        )
    marked = lifecycle.mark_downstream_stale(
        project,
        source_artifact_id=source_artifact_id,
        source_revision=source_revision,
        change=change,
        now=now,
    )
    if change not in _FINAL_CHAPTER_PROTECTED_CHANGES:
        return marked
    keep: list[str] = []
    restored: list[str] = []
    for artifact_id in marked:
        family = lifecycle._family_of(artifact_id)
        if family not in {"skeleton", "review_report"}:
            keep.append(artifact_id)
            continue
        chapter_id = lifecycle._chapter_id_from_artifact_id(artifact_id)
        chapter = storage.load_chapter(project, chapter_id) if chapter_id else None
        if chapter is None or chapter.status is not ChapterStatus.final_reconciled:
            keep.append(artifact_id)
            continue
        envelope = storage.load_artifact(project, artifact_id)
        if envelope is None or envelope.accepted_revision is None:
            keep.append(artifact_id)
            continue
        restored_envelope = envelope.model_copy(
            update={
                "status": ArtifactStatus.stale
                if envelope.stale_reasons and _has_other_stale_source(
                    envelope, source_artifact_id, source_revision
                )
                else ArtifactStatus.accepted,
                "stale_reasons": [
                    item
                    for item in envelope.stale_reasons
                    if not (
                        item.source_artifact_id == source_artifact_id
                        and item.source_revision == source_revision
                    )
                ],
            }
        )
        storage.save_artifact(
            project,
            restored_envelope,
            operation_id=f"keep_final_{artifact_id}_r{source_revision}",
        )
        restored.append(artifact_id)
    return keep


#: Change chỉ stale planning/draft, không stale Skeleton/report của chapter đã final.
_FINAL_CHAPTER_PROTECTED_CHANGES: frozenset[str] = frozenset(
    {
        lifecycle.STALE_CHANGE_BASE_IDEA_REVISE,
        lifecycle.STALE_CHANGE_PREMISE_REVISE,
        lifecycle.STALE_CHANGE_LONG_PLAN_REPLACE,
        lifecycle.STALE_CHANGE_SHORT_PLAN_REPLACE,
    }
)


def _has_other_stale_source(
    envelope: ArtifactEnvelope[Any], source_artifact_id: str, source_revision: int
) -> bool:
    return any(
        not (
            item.source_artifact_id == source_artifact_id
            and item.source_revision == source_revision
        )
        for item in envelope.stale_reasons
    )


def reaccept_stale(
    project: Project,
    *,
    artifact_id: str,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Review lại artifact `stale`: validate schema/ID/scope/freshness rồi cập nhật pins.

    Nếu validation/freshness không còn pass thì **giữ** `stale` và raise
    `ValidationFailure`; không artifact nào bị coi là accepted trước khi user
    thực sự review xong.
    """
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None:
        raise GuardError(
            f"Không thấy artifact `{artifact_id}`.", code="artifact_missing"
        )
    if envelope.accepted_revision is None:
        raise GuardError(
            f"`{artifact_id}` chưa có accepted revision để reaccept.",
            code="missing_accepted",
        )
    context = _reaccept_context(project, envelope)
    result = validation.validate_artifact_payload(
        envelope.artifact_type, envelope.accepted_revision.payload, context=context
    )
    if result.state is ValidationState.invalid:
        raise ValidationFailure(
            f"`{artifact_id}` không còn pass validation deterministic; giữ `stale` và cần "
            f"regenerate: {validation.summarize_errors(result)}",
            result=result,
            code="reaccept_validation_failed",
            details={"artifact_id": artifact_id, "status": envelope.status.value},
        )
    mismatches = _freshness_mismatches(project, envelope)
    if mismatches:
        raise StaleDependencyError(
            f"`{artifact_id}` còn dependency lệch revision hiện tại; giữ `stale` "
            f"({'; '.join(mismatches)}).",
            code="stale_dependency",
            details={"artifact_id": artifact_id, "mismatches": mismatches},
        )

    op_id = operation_id or generate_operation_id()
    pins = reconcile_service._dependency_pins(project)
    try:
        refreshed = lifecycle.reaccept(
            envelope, dependency_pins=pins, now=now or now_iso()
        )
    except lifecycle.LifecycleError as exc:
        raise ValidationFailure(
            f"`{artifact_id}` không reaccept được: {exc}",
            result=result,
            code=getattr(exc, "code", "reaccept_failed"),
        ) from exc
    refreshed = refreshed.model_copy(
        update={
            "accepted_revision": refreshed.accepted_revision.model_copy(
                update={"validation": result, "accepted_by": accepted_by}
            )
        }
    )
    storage.save_artifact(project, refreshed, operation_id=_suffix(op_id, "reaccept"))
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_id,
        message=(
            f"`{artifact_id}` đã được review/reaccept với pins hiện tại; nội dung không đổi."
        ),
        validation=result,
        data={
            "status": ArtifactStatus.accepted.value,
            "revision": refreshed.accepted_revision.revision,
            "dependency_pins": [pin.model_dump(mode="json") for pin in pins],
        },
    )


def _reaccept_context(
    project: Project, envelope: ArtifactEnvelope[Any]
) -> validation.ValidationContext:
    """Scope/temporal context cho validation lại theo từng loại artifact."""
    index = _reference_index(project)
    chapter_number = lifecycle._chapter_number_of_artifact(project, envelope.artifact_id)
    payload = envelope.accepted_revision.payload if envelope.accepted_revision else None
    if chapter_number is None and payload is not None:
        value = getattr(payload, "chapter_number", None)
        if isinstance(value, int):
            chapter_number = value
    return validation.ValidationContext(index=index, chapter_number=chapter_number)


def _stale_mismatches(
    project: Project, envelope: ArtifactEnvelope[Any]
) -> list[str]:
    """Stale reason chưa được giải quyết bằng pin mới hơn (chống reaccept nhầm).

    Nếu artifact còn stale reason có `source_artifact_id` mà accepted revision chưa
    pin tới revision mới nhất của nguồn đó thì nội dung cũ vẫn dựa trên upstream
    cũ; user phải regenerate chứ không được reaccept im lặng.
    """
    accepted = envelope.accepted_revision
    if accepted is None or not envelope.stale_reasons:
        return []
    pinned = {pin.artifact_id: pin.revision for pin in accepted.dependency_pins}
    unresolved: list[str] = []
    reported: set[str] = set()
    for reason in envelope.stale_reasons:
        source_id = reason.source_artifact_id
        if source_id in reported or source_id.startswith("chapter_"):
            continue
        latest = _latest_revision_of(project, source_id)
        if latest is None:
            continue
        current = pinned.get(source_id)
        if current is not None and current >= latest:
            # Accepted revision đã pin đúng bản mới nhất của nguồn: stale reason
            # đã được xử lý, không chặn reaccept.
            continue
        reported.add(source_id)
        unresolved.append(
            f"{source_id}: nội dung accepted vẫn dựa trên bản trước khi `{source_id}` đổi"
        )
    return unresolved


def _latest_revision_of(project: Project, artifact_id: str) -> int | None:
    if artifact_id == "base_idea":
        document = storage.load_co_create(project)
        if document is not None and document.base_idea is not None:
            return document.base_idea.revision
        return None
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or envelope.accepted_revision is None:
        return None
    return envelope.accepted_revision.revision


def _freshness_mismatches(
    project: Project, envelope: ArtifactEnvelope[Any]
) -> list[str]:
    """Pin lệch revision/không còn accepted, bỏ pin đã rõ là nguồn stale marking."""
    accepted = envelope.accepted_revision
    if accepted is None:
        return []
    mismatches = (
        lifecycle.stale_dependencies(
            project,
            accepted.dependency_pins,
            current=reconcile_service._current_revisions_for_pins(
                project, accepted.dependency_pins
            ),
        )
        if accepted.dependency_pins
        else []
    )
    # Pin tới chính artifact nguồn đã gây stale phải được phép cập nhật: reaccept
    # chính là action ghi lại pin đó, nên không coi là vi phạm freshness.
    source_ids = {item.source_artifact_id for item in envelope.stale_reasons}
    filtered = [
        message
        for message in mismatches
        if not any(message.startswith(f"{source_id}:") for source_id in source_ids)
    ]
    return [*filtered, *_stale_mismatches(project, envelope)]


def reset_consistency_after_retcon(
    project: Project,
    *,
    chapter_number: int,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Set `latest_consistent_chapter = chapter_number` và stale state sau đó.

    Entry timeline/relationship của các chương **sau** chương retcon bị đánh dấu
    `stale`; snapshot downstream (và reconciliation proposal của chương sau) bị
    `stale` để Writer không dùng state cũ. Hàm **không** ghi lùi `current_timeline`
    hay relationship `current` về state chương retcon.
    """
    op_id = operation_id or generate_operation_id()
    replay = _replay_result(project, op_id)
    if replay is not None:
        return replay

    timeline = storage.load_timeline(project)
    updated_timeline = timeline.model_copy(deep=True)
    stale_entry_ids: list[str] = []
    for entry in updated_timeline.entries:
        if entry.chapter_number > chapter_number and not entry.stale:
            entry.stale = True
            stale_entry_ids.append(entry.timeline_id)
    updated_timeline.latest_consistent_chapter = chapter_number

    relationships = storage.load_relationships(project)
    updated_relationships = relationships.model_copy(deep=True)
    stale_relationship_ids: list[str] = []
    for item in updated_relationships.relationships:
        if item.last_updated_chapter > chapter_number and not item.stale:
            item.stale = True
            stale_relationship_ids.append(item.relationship_id)
    updated_relationships.latest_consistent_chapter = chapter_number

    stale_artifact_ids = _mark_downstream_stale_for_retcon(
        project, chapter_number=chapter_number, now=now
    )

    handle = storage.begin_operation(
        project, operation_type=_OPERATION_RESET_CONSISTENCY, operation_id=op_id
    )
    if handle.replayed:
        return _stale_replay(handle.manifest)
    try:
        handle.add_json(
            _relative(project, project.paths.timeline_json), updated_timeline
        )
        handle.add_json(
            _relative(project, project.paths.relationships_json), updated_relationships
        )
        manifest = handle.commit()
    except Exception:
        _abort_quietly(handle)
        raise

    return ActionResult(
        operation_id=op_id,
        message=(
            f"`latest_consistent_chapter` = {chapter_number}; state sau chương đó bị đánh dấu "
            "stale. Current timeline/relationship **không** bị ghi lùi."
        ),
        data={
            "latest_consistent_chapter": chapter_number,
            "stale_timeline_entry_ids": stale_entry_ids,
            "stale_relationship_ids": stale_relationship_ids,
            "stale_artifact_ids": stale_artifact_ids,
            "applied_paths": list(manifest.applied_paths),
        },
    )


def _mark_downstream_stale_for_retcon(
    project: Project, *, chapter_number: int, now: str | None
) -> list[str]:
    """Đánh dấu stale reconciliation proposal + snapshot downstream của retcon."""
    stale_ids: list[str] = []
    source_artifact_id = f"chapter_{_chapter_id_for_number(project, chapter_number) or chapter_number}"
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is None or chapter.chapter_number <= chapter_number:
            continue
        artifact_id = _artifact_id_for_chapter("reconciliation", chapter_id)
        envelope = storage.load_artifact(project, artifact_id)
        if (
            envelope is None
            or (envelope.accepted_revision is None and envelope.candidate_revision is None)
            or envelope.status is ArtifactStatus.rejected
            or _already_marked(envelope, source_artifact_id, max(chapter_number, 1))
        ):
            continue
        marked = lifecycle.mark_stale(
            envelope,
            source_artifact_id=source_artifact_id,
            source_revision=max(chapter_number, 1),
            reason=(
                f"retcon: state của chương {chapter_number} đổi nên reconciliation của "
                f"{chapter_id} cần rebuild lại bằng action reconcile_downstream."
            ),
            affected_range={"start": chapter.chapter_number, "end": chapter.chapter_number},
            now=now,
        )
        storage.save_artifact(
            project,
            marked,
            operation_id=f"stale_retcon_{chapter_id}_c{chapter_number}",
        )
        stale_ids.append(artifact_id)
    return stale_ids


def _chapter_id_for_number(project: Project, chapter_number: int) -> str | None:
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is not None and chapter.chapter_number == chapter_number:
            return chapter_id
    return None


def _already_marked(
    envelope: ArtifactEnvelope[Any], source_artifact_id: str, source_revision: int
) -> bool:
    return any(
        item.source_artifact_id == source_artifact_id
        and item.source_revision == source_revision
        for item in envelope.stale_reasons
    )


def reconcile_downstream(
    project: Project,
    *,
    client: LLMClient,
    chapter_id: str,
    operation_id: str | None = None,
    now: str | None = None,
    on_event: EventSink | None = None,
    stream: bool = False,
    attempt: int = 1,
) -> ActionResult:
    """Rebuild/reconcile một chương downstream theo state as-of chương trước.

    Chapter phải đã `final_reconciled` (retcon rebuild, không viết lại prose).
    Hàm dùng đúng transaction của `services.reconcile.accept_reconciliation` với
    `refresh_state=True` nên proposal mới được commit thay entry timeline/
    relationship của chính chương đó (không nhân đôi) và `latest_consistent_chapter`
    tiến lên theo thứ tự.

    ``on_event``/``stream``/``attempt`` (T40) đi thẳng vào
    `reconcile.generate_reconciliation`: đây cũng là một action gọi LLM nên UI phải
    có cùng generation surface như workspace Reconcile (UI-02), không chặn cứng
    trang khi provider chậm.
    """
    chapter = _chapter_or_fail(project, chapter_id)
    if chapter.final_revision is None:
        raise GuardError(
            f"{chapter_id} chưa có final manuscript; reconcile_downstream chỉ dùng cho "
            "chương đã final.",
            code="chapter_not_final_reconciled",
            details={"chapter_id": chapter_id},
        )
    if chapter.status is not ChapterStatus.final_reconciled:
        raise GuardError(
            f"{chapter_id} đang `{chapter.status.value}`; reconcile_downstream cần chapter "
            "`final_reconciled`.",
            code="chapter_not_final_reconciled",
            details={"chapter_id": chapter_id, "status": chapter.status.value},
        )
    op_id = operation_id or generate_operation_id()
    replay = _replay_result(project, op_id)
    if replay is not None:
        return replay

    final = chapter.final_revision
    proposal_result = reconcile_service.generate_reconciliation(
        project,
        client=client,
        chapter_id=chapter_id,
        operation_id=_suffix(op_id, "proposal"),
        now=now,
        _refresh=True,
        on_event=on_event,
        stream=stream,
        attempt=attempt,
    )
    if proposal_result.data.get("reconciliation_status") == "failed":
        return ActionResult(
            operation_id=op_id,
            chapter_id=chapter_id,
            message=(
                f"{chapter_id}: rebuild downstream không tạo được proposal hợp lệ; state giữ "
                "nguyên và Writer vẫn bị khóa."
            ),
            warnings=proposal_result.warnings,
            data={"status": ChapterStatus.final_reconciled.value, "rebuild_failed": True},
        )

    accepted = reconcile_service.accept_reconciliation(
        project,
        chapter_id=chapter_id,
        accepted_by="user",
        operation_id=_suffix(op_id, "commit"),
        now=now,
        refresh_state=True,
    )
    accepted.message = (
        f"Rebuild downstream {chapter_id} (từ final r{final.revision}): " + accepted.message
    )
    accepted.operation_id = op_id
    accepted.data["rebuilt_from_final_revision"] = final.revision
    return accepted


def downstream_blockers(project: Project) -> list[dict[str, Any]]:
    """Artifact/chương đang stale chặn Writer, cho Arbiter/UI."""
    blockers: list[dict[str, Any]] = []
    for artifact_id in storage.list_artifact_ids(project):
        envelope = storage.load_artifact(project, artifact_id)
        if envelope is None or envelope.status is not ArtifactStatus.stale:
            continue
        blockers.append(
            {
                "kind": "artifact",
                "artifact_id": artifact_id,
                "artifact_type": envelope.artifact_type,
                "reason": "; ".join(item.reason for item in envelope.stale_reasons)
                or "upstream đổi, cần user review/reaccept hoặc regenerate.",
                "source_artifact_ids": sorted(
                    {item.source_artifact_id for item in envelope.stale_reasons}
                ),
                "suggested_action": "reaccept_stale",
            }
        )
    timeline = storage.load_timeline(project)
    for entry in timeline.entries:
        if entry.stale:
            blockers.append(
                {
                    "kind": "timeline_entry",
                    "artifact_id": entry.timeline_id,
                    "chapter_id": entry.chapter_id,
                    "chapter_number": entry.chapter_number,
                    "reason": "Timeline entry thuộc chain cũ sau retcon; cần rebuild.",
                    "suggested_action": "reconcile_downstream",
                }
            )
    relationships = storage.load_relationships(project)
    for item in relationships.relationships:
        if item.stale:
            blockers.append(
                {
                    "kind": "relationship",
                    "artifact_id": item.relationship_id,
                    "chapter_number": item.last_updated_chapter,
                    "reason": "Relationship state thuộc chain cũ sau retcon; cần rebuild.",
                    "suggested_action": "reconcile_downstream",
                }
            )
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is None:
            continue
        if chapter.status is ChapterStatus.finalizing:
            blockers.append(
                {
                    "kind": "chapter",
                    "chapter_id": chapter_id,
                    "chapter_number": chapter.chapter_number,
                    "reason": "Chapter đang `finalizing`: reconciliation chưa commit.",
                    "suggested_action": "generate_reconciliation",
                }
            )
            continue
        if (
            chapter.chapter_number > 1
            and chapter.status is not ChapterStatus.final_reconciled
        ):
            blockers.append(
                {
                    "kind": "chapter",
                    "chapter_id": chapter_id,
                    "chapter_number": chapter.chapter_number,
                    "reason": (
                        f"Chapter {chapter.chapter_number} chưa `final_reconciled`; Writer "
                        "chương sau bị khóa."
                    ),
                    "suggested_action": "finalize_chapter",
                }
            )
    return blockers
