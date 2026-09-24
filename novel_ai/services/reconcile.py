"""Finalize, Reconciliation transaction và recovery (T17).

Module này hiện thực `docs/design/workflow.md` mục 3.2, 4.4 và
`docs/design/storage.md` mục 6–8, 11 cho phần "chapter đóng canon":

- `finalize_chapter` đóng băng prose revision đã review thành `final_candidate`
  (file `chapters/<ch>/final/final_rNNNN.md`) và chuyển chapter sang
  `finalizing`. Action này **không** unlock chương sau, **không** sửa timeline hay
  relationship.
- `generate_reconciliation` gọi prompt `reconcile.v1`, lưu raw output trước khi
  parse, validate proposal rồi lưu candidate artifact `reconciliation_<ch>`.
  Lỗi schema/timeout chỉ để lại raw + error record; accepted state và chapter
  status không đổi.
- `accept_reconciliation` commit **một transaction nhiều file**: final
  manuscript, `chapter.json`, reconciliation artifact accepted,
  `current_timeline.json`, `relationships.json` và snapshot cho chương sau. Chỉ
  khi manifest committed thì chapter mới `final_reconciled`; retry cùng
  `operation_id` trả kết quả cũ và không nhân đôi timeline/relationship.

Quyết định triển khai (chi tiết ở bàn giao T17):

- **Thứ tự commit trong write_set**: `final/<...>.md` (prose) → `chapter.json`
  (status `final_reconciled` + `final_revision`) → `reconciliation` artifact
  accepted → `current_timeline.json` → `relationships.json` → snapshot chương
  sau. Reader xác định "đã xong" bằng `chapter.json` + manifest committed, không
  bằng sự tồn tại của file, nên prose và trạng thái chapter đi trước state.
- **Timeline entry** được replace theo `chapter_id` (không append trùng khi
  rebuild downstream); `timeline_id` do backend cấp qua `next_stable_id`.
- **Relationship pair** do `character_ids` quyết định; `relationship_id` mới được
  cấp qua `next_stable_id`. Update trỏ tới `relationship_id` sai cặp bị từ chối.
- **Freshness** so proposal với manuscript revision của `final_candidate` và với
  revision hiện tại của mọi dependency pin thật (artifact accepted, chapter
  prose, timeline/relationship data version).
- **Retry an toàn**: `begin_operation` với `operation_id` đã committed trả
  `replayed=True` và service trả kết quả cũ từ manifest, không stage lại.

Module không phụ thuộc Streamlit và không tự chạy bước tiếp.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.context import ContextBundle, build_reconcile_context
from novel_ai.core.llm import (
    LLMClient,
    LLMError,
    LLMRequest,
    generate_structured,
)
from novel_ai.core.models import (
    ArtifactEnvelope,
    ArtifactStatus,
    ChapterContextSnapshot,
    ChapterMetadata,
    ChapterStatus,
    CurrentTimelineDocument,
    DependencyPin,
    ExcludedDueToEffectiveChapter,
    FinalCandidate,
    FinalRevision,
    HumanReviewRecord,
    PayloadSource,
    ReconciliationPayload,
    ReconciliationStatus,
    RelationshipHistoryEntry,
    RelationshipState,
    RelationshipStateDocument,
    RelationshipUpdate,
    SourceFinalRevision,
    SourceType,
    StructuredOutputError,
    TimelineEntry,
    ValidationResult,
    ValidationState,
    generate_error_id,
    generate_operation_id,
    generate_snapshot_id,
    next_stable_id,
    now_iso,
)
from novel_ai.core.prompts import PromptRegistry, render_prompt
from novel_ai.core.generation import EventSink, GenerationEmitter
from novel_ai.core.project import Project
from novel_ai.core.storage import load_co_create, read_json
from novel_ai.services.co_create import transport_complete_seam
from novel_ai.services import (
    ActionResult,
    GuardError,
    LLMUnavailableError,
    ServiceError,
    StaleDependencyError,
    ValidationFailure,
)

__all__ = [
    "RECONCILE_PROMPT_ID",
    "accept_reconciliation",
    "cancel_finalizing",
    "edit_reconciliation_candidate",
    "finalize_chapter",
    "generate_reconciliation",
    "pending_reconciliation",
    "recover",
    "reject_reconciliation",
    "retry_reconcile",
]

#: Prompt v1 dùng để trích xuất continuity từ final candidate.
RECONCILE_PROMPT_ID = "reconcile.v1"

#: Tên bước dùng cho `OperationManifest.operation_type`.
OPERATION_FINALIZE = "finalize_chapter"
OPERATION_RECONCILE_COMMIT = "finalize_reconcile"
OPERATION_RECONCILE_ERROR = "reconcile_error"

#: Artifact ID của reconciliation proposal cho một chapter.
RECONCILE_ARTIFACT_ID_TEMPLATE = "reconciliation_{chapter_id}"


# ---------------------------------------------------------------------------
# Helper chung (đọc-only)
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Repository root thật, dùng cho prompt loader (không phụ thuộc cwd)."""
    return Path(storage.__file__).resolve().parents[2]


def _abort_quietly(handle: storage.OperationHandle) -> None:
    """Abort rồi giữ nguyên exception gốc của action.

    `OperationHandle.abort()` bị từ chối bằng `RecoveryRequired` khi đã replace một
    phần target (đúng thiết kế: state nửa cũ nửa mới phải `recover_pending`). Lỗi
    đó không được che exception gốc mà caller cần thấy.
    """
    try:
        handle.abort()
    except Exception:  # RecoveryRequired / StorageError khi đang commit dở
        return


def _chapter_or_fail(project: Project, chapter_id: str) -> ChapterMetadata:
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        raise GuardError(
            f"Chưa có chapter metadata cho `{chapter_id}`.",
            code="chapter_missing",
            details={"chapter_id": chapter_id},
        )
    return chapter


def _retcon_marker(project: Project, chapter_id: str) -> dict[str, Any] | None:
    """Marker retcon đang mở của chapter (chỉ đọc)."""
    target = project.root / f"chapters/{chapter_id}/retcon/state.json"
    if not target.is_file():
        return None
    try:
        marker = read_json(target)
        return marker if isinstance(marker, dict) and marker.get("status") == "open" else None
    except Exception:  # pragma: no cover - marker hỏng không được chặn finalize
        return None


def _artifact_id_for_chapter(chapter_id: str) -> str:
    return RECONCILE_ARTIFACT_ID_TEMPLATE.format(chapter_id=chapter_id)


def _project_relpath(project: Project, path: Path) -> str:
    return str(path.relative_to(project.root)).replace("\\", "/")


def _timeline_data_version(timeline: CurrentTimelineDocument) -> int:
    """Revision của dữ liệu timeline (không phải metadata consistency)."""
    return int(timeline.latest_final_chapter)


def _relationship_data_version(relationships: RelationshipStateDocument) -> int:
    """Revision của dữ liệu relationship: chương cập nhật mới nhất."""
    return max(
        (item.last_updated_chapter for item in relationships.relationships),
        default=0,
    )


def _artifact_revision_map(project: Project, artifact_id: str) -> dict[str, int]:
    """Map ``artifact_id`` (đã accepted) -> revision, dùng cho freshness check."""
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or envelope.accepted_revision is None:
        return {}
    return {artifact_id: envelope.accepted_revision.revision}


def _current_revisions_for_pins(
    project: Project,
    pins: Iterable[DependencyPin],
    *,
    timeline: CurrentTimelineDocument | None = None,
    relationships: RelationshipStateDocument | None = None,
) -> dict[str, int]:
    """Revision hiện tại của đúng các artifact mà ``pins`` tham chiếu.

    Chỉ tính artifact accepted thật, chapter prose hiện tại và data version của
    state. Nhờ vậy pin cho entry foundation không bị coi là "lệch" chỉ vì
    artifact đó không nằm trong revision map chung.
    """
    current: dict[str, int] = {}
    for pin in pins:
        artifact_id = pin.artifact_id
        if artifact_id in current:
            continue
        if artifact_id == "current_timeline":
            state = timeline if timeline is not None else storage.load_timeline(project)
            current[artifact_id] = _timeline_data_version(state)
            continue
        if artifact_id == "relationships":
            state = (
                relationships
                if relationships is not None
                else storage.load_relationships(project)
            )
            current[artifact_id] = _relationship_data_version(state)
            continue
        if artifact_id == "base_idea":
            document = load_co_create(project)
            if (
                document is not None
                and document.base_idea is not None
                and document.base_idea.status is ArtifactStatus.accepted
            ):
                current[artifact_id] = document.base_idea.revision
            continue
        if artifact_id.startswith("chapter_"):
            chapter = storage.load_chapter(project, artifact_id[len("chapter_"):])
            if chapter is not None:
                drafts = max((draft.revision for draft in chapter.drafts), default=0)
                revision = chapter.current_draft_revision or drafts
                if revision:
                    current[artifact_id] = revision
            continue
        current.update(_artifact_revision_map(project, artifact_id))
    return current


def _dependency_pins(project: Project, *, chapter_id: str | None = None) -> list[DependencyPin]:
    """Pin thật của mọi input accepted tại thời điểm build proposal."""
    pins: list[DependencyPin] = []
    document = load_co_create(project)
    if (
        document is not None
        and document.base_idea is not None
        and document.base_idea.status is ArtifactStatus.accepted
    ):
        pins.append(
            DependencyPin(
                artifact_id="base_idea",
                revision=document.base_idea.revision,
                scope="base_idea",
            )
        )
    for artifact_id, scope in (
        ("premise", "premise"),
        ("characters", "characters"),
        ("world_rules", "world_rules"),
        ("foreshadow", "foreshadow"),
        ("long_plan", "long_plan"),
        ("short_plan", "short_plan"),
    ):
        pins.extend(_artifact_pins(project, artifact_id, scope))
    if chapter_id is not None:
        pins.extend(
            _artifact_pins(
                project,
                lifecycle.artifact_id_for("skeleton", chapter_id=chapter_id),
                "skeleton",
                chapter_id=chapter_id,
            )
        )
    timeline = storage.load_timeline(project)
    if timeline.entries or timeline.latest_final_chapter:
        pins.append(
            DependencyPin(
                artifact_id="current_timeline",
                revision=max(_timeline_data_version(timeline), 1),
                scope="timeline_as_of",
            )
        )
    relationships = storage.load_relationships(project)
    if relationships.relationships:
        pins.append(
            DependencyPin(
                artifact_id="relationships",
                revision=max(_relationship_data_version(relationships), 1),
                scope="relationship_as_of",
            )
        )
    return pins


def _artifact_pins(
    project: Project, artifact_id: str, scope: str, *, chapter_id: str | None = None
) -> list[DependencyPin]:
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or envelope.accepted_revision is None:
        return []
    return [
        DependencyPin(
            artifact_id=artifact_id,
            revision=envelope.accepted_revision.revision,
            scope=scope,
            chapter_id=chapter_id,
        )
    ]


def _freshness_violations(
    project: Project, pins: Sequence[DependencyPin]
) -> list[str]:
    """Pin lệch revision **hoặc** dependency không còn `accepted` (T09 contract)."""
    if not pins:
        return []
    current = _current_revisions_for_pins(project, pins)
    return lifecycle.stale_dependencies(project, list(pins), current=current)


def _raise_if_stale(message: str, mismatches: Sequence[str], **details: Any) -> None:
    if mismatches:
        raise StaleDependencyError(
            f"{message} ({'; '.join(mismatches)}).",
            code="stale_dependency",
            details={**details, "mismatches": list(mismatches)},
        )


def _prompt_inputs(bundle: ContextBundle, prompt_id: str, registry: PromptRegistry) -> dict[str, Any]:
    """Chỉ gửi field mà manifest khai báo; extra/thiếu do manifest quyết định."""
    spec = registry.get(prompt_id)
    return {name: bundle.payload[name] for name in spec.template_variables if name in bundle.payload}


def _validation_failure(
    message: str, result: ValidationResult, **details: Any
) -> ValidationFailure:
    return ValidationFailure(
        f"{message}: {validation.summarize_errors(result)}",
        result=result,
        details=details,
    )


def _save_error_record(
    project: Project,
    *,
    chapter_id: str,
    error: StructuredOutputError,
    raw_ref: str | None,
) -> str:
    """Lưu `StructuredOutputError` bền vững trong project (không đổi accepted state)."""
    relpath = f"chapters/{chapter_id}/reconcile/errors/{error.error_id}.json"
    if raw_ref:
        error = error.model_copy(update={"raw_output_ref": raw_ref})
    handle = storage.begin_operation(
        project,
        operation_type=OPERATION_RECONCILE_ERROR,
        operation_id=f"op_reconcile_error_{error.error_id}",
    )
    try:
        handle.add_json(relpath, error.model_dump(mode="json"))
        handle.commit()
    except Exception:  # pragma: no cover - ghi error record không được làm hỏng action
        _abort_quietly(handle)
        return relpath
    return relpath


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------


def _final_markdown_relpath(chapter_id: str, prose_revision: int) -> str:
    return f"chapters/{chapter_id}/final/final_r{prose_revision:04d}.md"


def _confirm_human_review(
    chapter: ChapterMetadata, *, accepted_by: str, now: str | None
) -> HumanReviewRecord:
    """Ghi HumanReviewRecord cho revision hiện tại từ xác nhận trong action Finalize."""
    return HumanReviewRecord(
        prose_revision=int(chapter.current_draft_revision or 0),
        reviewed_at=now or now_iso(),
        reviewed_by=accepted_by,
        notes="Xác nhận Human Review trong action Finalize Chapter.",
        valid_for_current_revision=True,
    )


def finalize_chapter(
    project: Project,
    *,
    chapter_id: str,
    confirm_review: bool = False,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Đóng băng prose revision đã review thành `final_candidate` (D003).

    Guard chạy **trước** mọi bước ghi: draft hiện tại phải complete, Skeleton phải
    accepted/fresh, và Human Review phải hợp lệ cho `current_draft_revision`
    **hoặc** user xác nhận rõ bằng `confirm_review=True` (khi đó backend ghi
    `HumanReviewRecord` cho revision hiện tại trong chính action này).

    Kết quả: file `chapters/<ch>/final/final_rNNNN.md` (staged write),
    `chapter.final_candidate` với `reconciliation_status="missing"`, và
    `chapter.status = finalizing`. Không unlock chương sau, không đụng
    timeline/relationship.
    """
    chapter = _chapter_or_fail(project, chapter_id)
    retcon_open = _retcon_marker(project, chapter_id) is not None
    if chapter.status is ChapterStatus.final_reconciled and not retcon_open:
        raise GuardError(
            f"{chapter_id} đã `final_reconciled`; sửa bản final phải đi qua action retcon "
            "(`revision.start_retcon`).",
            code="chapter_already_final",
            details={"chapter_id": chapter_id},
        )
    if chapter.status is ChapterStatus.finalizing and chapter.final_candidate is not None:
        raise GuardError(
            f"{chapter_id} đang `finalizing`; cancel hoặc hoàn tất reconciliation trước.",
            code="chapter_already_finalizing",
            details={"chapter_id": chapter_id},
        )

    # `lifecycle.GuardBlockedError` không phải `services.ServiceError`; UI chỉ bắt
    # `ServiceError` nên phải map tại ranh giới service (T23 phát hiện khi test
    # cross-module bắt `GuardError` mà nhận `GuardBlockedError`).
    for guard in (
        lifecycle.guard_review(project, chapter_id),
        lifecycle.guard_skeleton_for_chapter(project, chapter_id),
    ):
        if not guard.allowed:
            raise GuardError(
                "; ".join(guard.reasons) or "Finalize bị chặn bởi lifecycle guard.",
                code=guard.code or "guard_failed",
                details={"chapter_id": chapter_id},
            )

    human_ready = (
        chapter.human_review is not None
        and chapter.current_draft_revision is not None
        and chapter.human_review.prose_revision == chapter.current_draft_revision
        and chapter.human_review.valid_for_current_revision
    )
    if not human_ready and not confirm_review:
        raise GuardError(
            f"{chapter_id} chưa có Human Review hợp lệ cho draft hiện tại "
            f"r{chapter.current_draft_revision}; Human Review là gate cứng của Finalize "
            "(hoặc truyền confirm_review=True để xác nhận rõ trong action này).",
            code="human_review_missing",
            details={"chapter_id": chapter_id, "prose_revision": chapter.current_draft_revision},
        )

    draft = chapter.current_draft
    assert draft is not None  # guard_review đã kiểm tra
    prose_revision = int(chapter.current_draft_revision or draft.revision)
    prose_markdown = storage.read_text(project.root / draft.markdown_ref)

    op_id = operation_id or generate_operation_id()
    relpath = _final_markdown_relpath(chapter_id, prose_revision)
    stamp = now or now_iso()

    if human_ready:
        human_record = chapter.human_review
        review_confirmed = False
    else:
        human_record = _confirm_human_review(chapter, accepted_by=accepted_by, now=stamp)
        review_confirmed = True

    updated = chapter.model_copy(
        update={
            "status": ChapterStatus.finalizing,
            "human_review": human_record,
            "final_candidate": FinalCandidate(
                prose_revision=prose_revision,
                markdown_ref=relpath,
                created_at=stamp,
                reconciliation_status=ReconciliationStatus.missing,
            ),
        }
    )

    handle = storage.begin_operation(
        project, operation_type=OPERATION_FINALIZE, operation_id=op_id
    )
    if handle.replayed:
        return _finalize_replay_result(handle.manifest, chapter_id, relpath)
    try:
        # Finalize: prose trước, `chapter.json` (`finalizing`) sau cùng.
        handle.add_bytes(relpath, prose_markdown.encode("utf-8"))
        handle.add_json(_project_relpath(project, project.paths.chapter_json(chapter_id)), updated)
        manifest = handle.commit()
    except Exception:
        _abort_quietly(handle)
        raise

    return ActionResult(
        operation_id=op_id,
        chapter_id=chapter_id,
        message=(
            f"{chapter_id}: đóng băng prose r{prose_revision} thành final candidate; "
            "chapter chuyển `finalizing` (chương sau vẫn khóa)."
        ),
        data={
            "status": ChapterStatus.finalizing.value,
            "prose_revision": prose_revision,
            "final_markdown_ref": relpath,
            "reconciliation_status": ReconciliationStatus.missing.value,
            "human_review_confirmed_in_action": review_confirmed,
            "applied_paths": list(manifest.applied_paths),
        },
    )


def _finalize_replay_result(manifest: Any, chapter_id: str, relpath: str) -> ActionResult:
    """Kết quả cũ khi retry cùng `operation_id` đã committed (không ghi lại)."""
    return ActionResult(
        operation_id=manifest.operation_id,
        chapter_id=chapter_id,
        message=(
            f"{chapter_id}: operation finalize đã commit trước đó; trả kết quả cũ, "
            "không ghi lại."
        ),
        data={
            "replayed": True,
            "final_markdown_ref": relpath,
            "applied_paths": list(manifest.applied_paths),
            "result": dict(manifest.result or {}),
        },
    )


# ---------------------------------------------------------------------------
# Generate reconciliation
# ---------------------------------------------------------------------------


def _test_replay_result(project: Project, operation_id: str) -> ActionResult | None:
    """Kết quả cũ nếu `operation_id` đã committed (retry/rerun UI)."""
    done_path = project.paths.ops_done_dir / f"{operation_id}.json"
    if not done_path.is_file():
        return None
    manifest = storage._parse_operation_manifest(storage.read_json(done_path), done_path)
    result = dict(manifest.result or {})
    return ActionResult(
        operation_id=operation_id,
        chapter_id=result.get("chapter_id"),
        artifact_id=result.get("artifact_id"),
        message=(
            f"Operation `{operation_id}` đã commit trước đó; trả kết quả cũ, "
            "không gọi LLM và không ghi lại."
        ),
        data={
            "replayed": True,
            "operation_type": manifest.operation_type,
            "applied_paths": list(manifest.applied_paths),
            "result": result,
        },
    )


def _chapter_and_candidate(
    project: Project, chapter_id: str, *, refresh: bool = False
) -> tuple[ChapterMetadata, FinalCandidate]:
    """Chapter phải đang `finalizing` (hoặc đã final nếu `refresh` = rebuild downstream)."""
    chapter = _chapter_or_fail(project, chapter_id)
    if refresh:
        if chapter.final_revision is None:
            raise GuardError(
                f"{chapter_id} chưa có final manuscript để rebuild state.",
                code="final_candidate_missing",
                details={"chapter_id": chapter_id},
            )
        if chapter.final_candidate is not None:
            return chapter, chapter.final_candidate
        return chapter, _candidate_from_final(chapter)
    if chapter.status is not ChapterStatus.finalizing:
        raise GuardError(
            f"{chapter_id} đang `{chapter.status.value}`; reconciliation chỉ chạy khi "
            "chapter `finalizing`.",
            code="chapter_not_finalizing",
            details={"chapter_id": chapter_id, "status": chapter.status.value},
        )
    if chapter.final_candidate is None:
        raise GuardError(
            f"{chapter_id} chưa có `final_candidate`; chạy Finalize Chapter trước.",
            code="final_candidate_missing",
            details={"chapter_id": chapter_id},
        )
    return chapter, chapter.final_candidate


def _candidate_from_final(chapter: ChapterMetadata) -> FinalCandidate:
    """Dựng lại final candidate (không ghi disk) khi rebuild state sau retcon.

    Sau commit, `final_candidate` về null còn `final_revision` là canon; rebuild
    downstream dùng chính final revision đó, **không** hạ chapter về `finalizing`
    và không tạo final candidate mới.
    """
    assert chapter.final_revision is not None
    final = chapter.final_revision
    return FinalCandidate(
        prose_revision=final.source_prose_revision,
        markdown_ref=final.markdown_ref,
        created_at=final.reconciled_at,
        reconciliation_status=ReconciliationStatus.missing,
    )


def _proposal_source(
    *,
    prompt_id: str,
    prompt_version: str | None,
    prompt_hash: str | None,
    operation_id: str,
    raw_ref: str | None,
    source_type: SourceType = SourceType.llm,
) -> PayloadSource:
    return PayloadSource(
        source_type=source_type,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        operation_id=operation_id,
        raw_output_ref=raw_ref,
    )


def _validate_proposal(
    project: Project,
    chapter: ChapterMetadata,
    payload: ReconciliationPayload,
    *,
    check_source: bool = True,
) -> ValidationResult:
    """Validate proposal: chapter number, ID resolve, hiệu lực, cặp relationship."""
    from novel_ai.core.context import _State  # nội bộ T12: index accepted đã lọc effective

    state = _State.load(project)
    result = validation.validate_artifact_payload(
        "reconciliation",
        payload,
        context=validation.ValidationContext(
            index=state.reference_index(),
            chapter_number=chapter.chapter_number,
        ),
    )
    issues = list(result.errors)
    if check_source and chapter.final_candidate is not None:
        expected = chapter.final_candidate.prose_revision
        actual = payload.source_final_candidate.prose_revision
        if actual != expected:
            issues.append(
                validation.ValidationIssue(
                    path="/payload/source_final_candidate/prose_revision",
                    code="stale_candidate",
                    message=(
                        f"Proposal trích từ prose r{actual} nhưng final candidate hiện tại là "
                        f"r{expected}; regenerate reconciliation."
                    ),
                )
            )
    if payload.chapter_id != chapter.chapter_id:
        issues.append(
            validation.ValidationIssue(
                path="/payload/chapter_id",
                code="chapter_mismatch",
                message=(
                    f"Proposal khai chapter_id `{payload.chapter_id}` nhưng chapter đang "
                    f"finalize là `{chapter.chapter_id}`."
                ),
            )
        )
    return validation.result_from_issues(issues)


def generate_reconciliation(
    project: Project,
    *,
    client: LLMClient,
    chapter_id: str,
    operation_id: str | None = None,
    now: str | None = None,
    _refresh: bool = False,
    on_event: EventSink | None = None,
    stream: bool = False,
    attempt: int = 1,
) -> ActionResult:
    """Sinh proposal reconciliation cho chapter đang `finalizing`.

    Guard chạy trước khi gọi LLM. Raw output được lưu trước khi parse. Proposal
    hợp lệ được lưu thành candidate `reconciliation_<chapter_id>` với
    `status=draft` và `final_candidate.reconciliation_status="draft"`; khi
    `auto_accept_structured=True` (và validation pass) service commit luôn qua
    `accept_reconciliation`.

    Schema sai/ID không resolve/timeout: lưu raw + `StructuredOutputError`, giữ
    chapter `finalizing`, accepted state không đổi. Stream lỗi sau khi đã có delta
    cũng đi đúng đường này (raw partial được lưu, không commit, chapter vẫn
    `finalizing`) nên retry không nhân đôi timeline/relationship.

    ``_refresh=True`` là đường nội bộ cho `revision.reconcile_downstream`: chapter
    đã `final_reconciled` và state N−1 vừa được rebuild, nên proposal không cần
    khớp prose revision của final candidate cũ.
    """
    chapter, candidate = _chapter_and_candidate(project, chapter_id, refresh=_refresh)
    op_id = operation_id or generate_operation_id()
    replayed = _test_replay_result(project, op_id)
    if replayed is not None:
        return replayed

    final_markdown = storage.read_text(project.root / candidate.markdown_ref)
    bundle = build_reconcile_context(
        project, chapter_id=chapter_id, final_candidate_markdown=final_markdown
    )
    registry = PromptRegistry.load(repo_root=_repo_root())
    rendered = render_prompt(
        registry,
        RECONCILE_PROMPT_ID,
        inputs=_prompt_inputs(bundle, RECONCILE_PROMPT_ID, registry),
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
    emitter = (
        GenerationEmitter(
            operation_id=op_id,
            action="reconcile.proposal",
            sink=on_event,
            attempt=attempt,
            prompt_id=RECONCILE_PROMPT_ID,
            artifact_id=_artifact_id_for_chapter(chapter_id),
            chapter_id=chapter_id,
        )
        if on_event is not None
        else None
    )
    complete, holder = transport_complete_seam(
        project,
        client=client,
        operation_id=op_id,
        label=f"reconcile_{chapter_id}",
        emitter=emitter,
        stream=stream,
        now=now,
        prompt_id=RECONCILE_PROMPT_ID,
    )
    # `generate_structured` phát phần transport (qua seam ở trên) rồi parse; chỉ sau
    # khi transport xong mới vào bước `validating`.
    parsed, raw_text, error = generate_structured(
        client, request, ReconciliationPayload, complete=complete
    )
    if emitter is not None:
        emitter.validating(detail="Đang parse/validate Reconciliation proposal.")

    raw_ref = holder.get("raw_ref") or storage.save_raw_output(
        project,
        operation_id=op_id,
        text=raw_text,
        label=f"reconcile_{chapter_id}",
    )

    if parsed is None or error is not None:
        record = error or StructuredOutputError(
            error_id=generate_error_id(), created_at=now or now_iso()
        )
        record = record.model_copy(
            update={
                "artifact_id": _artifact_id_for_chapter(chapter_id),
                "raw_output_ref": raw_ref,
                "retryable": True,
            }
        )
        error_path = _save_error_record(
            project, chapter_id=chapter_id, error=record, raw_ref=raw_ref
        )
        _set_reconciliation_status(
            project,
            chapter=chapter,
            status=ReconciliationStatus.failed,
            operation_id=f"op_reconcile_status_{op_id}",
            now=now,
            refresh=_refresh,
        )
        detail = "; ".join(
            f"{item.path}: {item.message}" for item in record.errors
        ) or "output không khớp schema"
        if emitter is not None:
            emitter.invalid(
                detail=(
                    "Reconciliation proposal không hợp lệ: chapter vẫn `finalizing`, "
                    "chương sau vẫn khóa, không commit."
                ),
                raw_ref=raw_ref,
            )
        return ActionResult(
            operation_id=op_id,
            artifact_id=_artifact_id_for_chapter(chapter_id),
            chapter_id=chapter_id,
            message=(
                "Reconciliation proposal không hợp lệ; chapter vẫn `finalizing`, chương sau "
                f"vẫn khóa. Raw: {raw_ref}. Lỗi: {detail}"
            ),
            warnings=["Proposal lỗi được giữ để người dùng sửa tay hoặc retry."],
            data={
                "status": ChapterStatus.finalizing.value,
                "reconciliation_status": ReconciliationStatus.failed.value,
                "raw_output_ref": raw_ref,
                "error_record_ref": error_path,
                "error_id": record.error_id,
                "errors": [item.model_dump(mode="json") for item in record.errors],
            },
        )

    result = _validate_proposal(project, chapter, parsed, check_source=not _refresh)
    if result.state is ValidationState.invalid:
        record = StructuredOutputError(
            error_id=generate_error_id(),
            artifact_id=_artifact_id_for_chapter(chapter_id),
            raw_output_ref=raw_ref,
            errors=list(result.errors),
            created_at=now or now_iso(),
            retryable=True,
        )
        error_path = _save_error_record(
            project, chapter_id=chapter_id, error=record, raw_ref=raw_ref
        )
        _set_reconciliation_status(
            project,
            chapter=chapter,
            status=ReconciliationStatus.failed,
            operation_id=f"op_reconcile_status_{op_id}",
            now=now,
            refresh=_refresh,
        )
        if emitter is not None:
            emitter.invalid(
                detail=(
                    "Reconciliation proposal sai contract/ID: chapter vẫn `finalizing`, "
                    "không commit."
                ),
                raw_ref=raw_ref,
            )
        return ActionResult(
            operation_id=op_id,
            artifact_id=_artifact_id_for_chapter(chapter_id),
            chapter_id=chapter_id,
            message=(
                "Reconciliation proposal sai contract/ID; chapter vẫn `finalizing`, "
                f"chương sau vẫn khóa. Raw: {raw_ref}."
            ),
            warnings=[
                "; ".join(f"{item.path}: {item.message}" for item in result.errors)
            ],
            validation=result,
            data={
                "status": ChapterStatus.finalizing.value,
                "reconciliation_status": ReconciliationStatus.failed.value,
                "raw_output_ref": raw_ref,
                "error_record_ref": error_path,
                "error_id": record.error_id,
                "errors": [item.model_dump(mode="json") for item in result.errors],
            },
        )

    envelope = _store_proposal(
        project,
        chapter=chapter,
        payload=parsed,
        validation_result=result,
        prompt_id=rendered.prompt_id,
        prompt_version=rendered.prompt_version,
        prompt_hash=rendered.template_hash,
        raw_ref=raw_ref,
        operation_id=op_id,
        now=now,
    )

    auto_accepted_revision: int | None = None
    if project.config.auto_accept_structured and not _refresh:
        accepted = accept_reconciliation(
            project,
            chapter_id=chapter_id,
            accepted_by="auto_accept",
            operation_id=f"{op_id}.accept",
            now=now,
        )
        auto_accepted_revision = accepted.data.get("committed_revision")
        if emitter is not None:
            emitter.saved(
                detail=(
                    "Reconciliation proposal đã validate và auto-accept theo config; "
                    "chapter `final_reconciled`."
                ),
                raw_ref=raw_ref,
            )
        return ActionResult(
            operation_id=op_id,
            artifact_id=envelope.artifact_id,
            chapter_id=chapter_id,
            message=(
                "Reconciliation proposal hợp lệ và đã auto-accept (structured output); "
                "chapter `final_reconciled`."
            ),
            validation=result,
            data={
                "status": ChapterStatus.final_reconciled.value,
                "auto_accepted": True,
                "auto_accepted_revision": auto_accepted_revision,
                "raw_output_ref": raw_ref,
                "notes": list(parsed.notes),
            },
        )

    if emitter is not None:
        emitter.saved(
            detail=(
                "Reconciliation proposal đã validate và lưu candidate `draft` (chưa accept, "
                "chapter vẫn `finalizing`)."
            ),
            raw_ref=raw_ref,
        )
    return ActionResult(
        operation_id=op_id,
        artifact_id=envelope.artifact_id,
        chapter_id=chapter_id,
        message=(
            "Reconciliation proposal đã lưu thành candidate `draft`; cần user accept "
            "trước khi chapter `final_reconciled`."
        ),
        validation=result,
        data={
            "status": ChapterStatus.finalizing.value,
            "reconciliation_status": ReconciliationStatus.draft.value,
            "raw_output_ref": raw_ref,
            "candidate_revision": envelope.candidate_revision.revision
            if envelope.candidate_revision
            else None,
            "notes": list(parsed.notes),
        },
    )


def _store_proposal(
    project: Project,
    *,
    chapter: ChapterMetadata,
    payload: ReconciliationPayload,
    validation_result: ValidationResult,
    prompt_id: str,
    prompt_version: str | None,
    prompt_hash: str | None,
    raw_ref: str | None,
    operation_id: str,
    now: str | None,
    source_type: SourceType = SourceType.llm,
) -> ArtifactEnvelope[Any]:
    """Lưu proposal thành candidate `draft` và set `final_candidate` status."""
    artifact_id = _artifact_id_for_chapter(chapter.chapter_id)
    envelope = storage.load_artifact(project, artifact_id) or lifecycle.new_artifact(
        "reconciliation", artifact_id
    )
    pins = _dependency_pins(project, chapter_id=chapter.chapter_id)
    refreshed = chapter.status is ChapterStatus.final_reconciled
    candidate = lifecycle.set_candidate(
        envelope,
        payload,
        source=_proposal_source(
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            operation_id=operation_id,
            raw_ref=raw_ref,
            source_type=source_type,
        ),
        dependency_pins=pins,
        validation=validation_result,
        now=now,
    )
    if refreshed and envelope.accepted_revision is not None:
        # Rebuild downstream: accepted reconciliation cũ vẫn là pin hợp lệ cho tới
        # khi transaction commit, nên status giữ `accepted`/`stale` thay vì `draft`.
        candidate = candidate.model_copy(
            update={
                "status": ArtifactStatus.stale
                if envelope.status is ArtifactStatus.stale
                else ArtifactStatus.accepted
            }
        )
    storage.save_artifact(project, candidate, operation_id=f"{operation_id}.proposal")
    stored = storage.load_artifact(project, artifact_id)
    assert stored is not None  # vừa ghi xong
    _set_reconciliation_status(
        project,
        chapter=storage.load_chapter(project, chapter.chapter_id) or chapter,
        status=ReconciliationStatus.draft,
        operation_id=f"{operation_id}.status",
        now=now,
        refresh=refreshed,
    )
    return stored


def _set_reconciliation_status(
    project: Project,
    *,
    chapter: ChapterMetadata,
    status: ReconciliationStatus,
    operation_id: str,
    now: str | None,
    refresh: bool = False,
) -> None:
    """Cập nhật `final_candidate.reconciliation_status` mà không đổi accepted state.

    ``refresh=True`` (rebuild downstream) không bao giờ hạ `reconciliation` accepted
    về `draft/failed`: pin cũ vẫn là canon cho tới khi transaction commit.
    """
    if refresh:
        return
    if chapter.final_candidate is None:
        return
    if chapter.final_candidate.reconciliation_status is status:
        return
    updated = chapter.model_copy(
        update={
            "final_candidate": chapter.final_candidate.model_copy(
                update={"reconciliation_status": status}
            )
        }
    )
    storage.save_chapter(project, updated, operation_id=operation_id)


def edit_reconciliation_candidate(
    project: Project,
    *,
    chapter_id: str,
    payload: Mapping[str, Any] | ReconciliationPayload,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """User sửa JSON proposal thủ công rồi backend validate lại.

    Accepted state không đổi; proposal vẫn là candidate `draft`. Nếu JSON/schema
    sai thì raise `ValidationFailure` và candidate cũ giữ nguyên.
    """
    chapter, _candidate = _chapter_and_candidate(project, chapter_id)
    artifact_id = _artifact_id_for_chapter(chapter_id)
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or (
        envelope.candidate_revision is None and envelope.accepted_revision is None
    ):
        raise GuardError(
            f"Chưa có reconciliation proposal cho {chapter_id}; chạy generate trước.",
            code="missing_candidate",
            details={"chapter_id": chapter_id, "artifact_id": artifact_id},
        )
    try:
        parsed = (
            payload
            if isinstance(payload, ReconciliationPayload)
            else ReconciliationPayload.model_validate(payload)
        )
    except Exception as exc:  # pydantic ValidationError
        raise ValidationFailure(
            f"JSON reconciliation sửa tay không khớp schema: {exc}",
            details={"chapter_id": chapter_id},
        ) from exc

    result = _validate_proposal(project, chapter, parsed)
    if result.state is ValidationState.invalid:
        raise _validation_failure(
            "Reconciliation sửa tay không hợp lệ", result, chapter_id=chapter_id
        )

    op_id = operation_id or generate_operation_id()
    stored = _store_proposal(
        project,
        chapter=chapter,
        payload=parsed,
        validation_result=result,
        prompt_id=envelope.candidate_revision.payload_source.prompt_id
        if envelope.candidate_revision
        else None,
        prompt_version=envelope.candidate_revision.payload_source.prompt_version
        if envelope.candidate_revision
        else None,
        prompt_hash=envelope.candidate_revision.payload_source.prompt_hash
        if envelope.candidate_revision
        else None,
        raw_ref=envelope.candidate_revision.payload_source.raw_output_ref
        if envelope.candidate_revision
        else None,
        operation_id=op_id,
        now=now,
        source_type=SourceType.user,
    )
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_id,
        chapter_id=chapter_id,
        message="Đã cập nhật reconciliation candidate từ JSON sửa tay; vẫn cần user accept.",
        validation=result,
        data={
            "status": chapter.status.value,
            "candidate_revision": stored.candidate_revision.revision
            if stored.candidate_revision
            else None,
        },
    )


# ---------------------------------------------------------------------------
# Accept reconciliation (transaction nhiều file)
# ---------------------------------------------------------------------------


def _proposal_envelope(project: Project, chapter_id: str) -> ArtifactEnvelope[Any]:
    artifact_id = _artifact_id_for_chapter(chapter_id)
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None:
        raise GuardError(
            f"Chưa có reconciliation proposal cho {chapter_id}.",
            code="missing_candidate",
            details={"chapter_id": chapter_id, "artifact_id": artifact_id},
        )
    return envelope


def _build_timeline_entry(
    *,
    chapter: ChapterMetadata,
    proposal: ReconciliationPayload,
    final_revision: int,
    timeline: CurrentTimelineDocument,
    now: str | None,
) -> TimelineEntry:
    """Entry timeline cho chapter, replace theo `chapter_id` để không nhân đôi."""
    existing = next(
        (entry for entry in timeline.entries if entry.chapter_id == chapter.chapter_id),
        None,
    )
    timeline_id = (
        existing.timeline_id
        if existing is not None
        else next_stable_id("timeline", [entry.timeline_id for entry in timeline.entries])
    )
    return TimelineEntry(
        timeline_id=timeline_id,
        chapter_id=chapter.chapter_id,
        chapter_number=chapter.chapter_number,
        time=proposal.timeline.time,
        location=proposal.timeline.location,
        status=proposal.timeline.status,
        source_final_revision=SourceFinalRevision(
            chapter_id=chapter.chapter_id, revision=final_revision
        ),
        accepted_at=now or now_iso(),
        stale=False,
    )


def _apply_relationship_updates(
    *,
    proposal: ReconciliationPayload,
    chapter: ChapterMetadata,
    final_revision: int,
    relationships: RelationshipStateDocument,
) -> tuple[RelationshipStateDocument, list[str]]:
    """Merge relationship update: cấp ID cho cặp mới, append history, giữ canon mới hơn."""
    document = relationships.model_copy(deep=True)
    applied: list[str] = []
    for update in proposal.relationship_updates:
        pair = list(update.character_ids)
        resolved = _resolve_relationship(document, update)
        entry = RelationshipHistoryEntry(
            chapter_id=chapter.chapter_id,
            chapter_number=chapter.chapter_number,
            current=update.current,
            source_final_revision=final_revision,
        )
        if resolved is None:
            relationship_id = next_stable_id(
                "rel", [item.relationship_id for item in document.relationships]
            )
            document.relationships.append(
                RelationshipState(
                    relationship_id=relationship_id,
                    character_ids=pair,
                    current=update.current,
                    last_updated_chapter=chapter.chapter_number,
                    history=[entry],
                    stale=False,
                )
            )
            applied.append(relationship_id)
            continue
        index, state = resolved
        new_state = state.model_copy(deep=True)
        new_state.history = [
            item for item in new_state.history if item.chapter_id != chapter.chapter_id
        ]
        new_state.history.append(entry)
        # Không ghi lùi canon: state đến từ chương xa hơn giữ nguyên `current`.
        if chapter.chapter_number >= new_state.last_updated_chapter:
            new_state.current = update.current
            new_state.last_updated_chapter = chapter.chapter_number
        new_state.stale = False
        document.relationships[index] = new_state
        applied.append(new_state.relationship_id)
    return document, applied


def _resolve_relationship(
    document: RelationshipStateDocument, update: RelationshipUpdate
) -> tuple[int, RelationshipState] | None:
    """Tìm relationship state theo ID (nếu có) hoặc theo cặp character IDs."""
    if update.relationship_id is not None:
        for index, state in enumerate(document.relationships):
            if state.relationship_id == update.relationship_id:
                return index, state
        raise StaleDependencyError(
            f"Reconciliation trỏ tới relationship `{update.relationship_id}` không tồn tại "
            "trong state accepted.",
            code="stale_dependency",
            details={"relationship_id": update.relationship_id},
        )
    wanted = tuple(sorted(update.character_ids))
    for index, state in enumerate(document.relationships):
        if tuple(sorted(state.character_ids)) == wanted:
            return index, state
    return None


def _accepted_character_ids(project: Project) -> list[str]:
    envelope = storage.load_artifact(project, "characters")
    if envelope is None or envelope.accepted_revision is None:
        return []
    payload = envelope.accepted_revision.payload
    return [item.character_id for item in getattr(payload, "characters", []) or []]


def _next_chapter_snapshot(
    project: Project,
    *,
    chapter: ChapterMetadata,
    pins: Sequence[DependencyPin],
    now: str | None,
) -> ChapterContextSnapshot | None:
    """Snapshot chuẩn bị cho chương kế nếu chương đó đã có metadata."""
    next_chapter = None
    for candidate in storage.list_chapter_ids(project):
        other = storage.load_chapter(project, candidate)
        if other is not None and other.chapter_number == chapter.chapter_number + 1:
            next_chapter = other
            break
    if next_chapter is None:
        return None
    characters = _accepted_character_ids(project)
    effective = [
        item for item in characters if _character_effective(project, item) <= next_chapter.chapter_number
    ]
    excluded = [
        ExcludedDueToEffectiveChapter(
            item_kind="character",
            item_id=item,
            effective_from_chapter=_character_effective(project, item),
        )
        for item in characters
        if _character_effective(project, item) > next_chapter.chapter_number
    ]
    timeline = storage.load_timeline(project)
    relationships = storage.load_relationships(project)
    return ChapterContextSnapshot(
        snapshot_id=generate_snapshot_id(),
        for_chapter_id=next_chapter.chapter_id,
        for_chapter_number=next_chapter.chapter_number,
        created_from_action="accept_reconciliation",
        dependency_pins=list(pins),
        timeline_entry_ids=[
            entry.timeline_id
            for entry in timeline.entries
            if entry.chapter_number <= chapter.chapter_number and not entry.stale
        ],
        relationship_versions=[
            {"relationship_id": item.relationship_id, "last_updated_chapter": item.last_updated_chapter}
            for item in relationships.relationships
            if item.last_updated_chapter <= chapter.chapter_number and not item.stale
        ],
        effective_character_ids=effective,
        effective_world_rule_ids=[],
        effective_foreshadow_ids=[],
        excluded_due_to_effective_chapter=excluded,
        writer_projection_hash=None,
        preparation_context=None,
        created_at=now or now_iso(),
    )


def _character_effective(project: Project, character_id: str) -> int:
    envelope = storage.load_artifact(project, "characters")
    if envelope is None or envelope.accepted_revision is None:
        return 1
    payload = envelope.accepted_revision.payload
    for item in getattr(payload, "characters", []) or []:
        if item.character_id == character_id:
            return int(item.effective_from_chapter)
    return 1


def _commit_reconciliation(
    project: Project,
    *,
    chapter: ChapterMetadata,
    proposal: ReconciliationPayload,
    envelope: ArtifactEnvelope[Any],
    accepted_by: str,
    operation_id: str | None,
    now: str | None,
    refresh: bool = False,
) -> ActionResult:
    """Commit transaction nhiều file cho một proposal đã validate.

    ``refresh=True`` là rebuild downstream sau retcon: final manuscript không đổi
    (giữ nguyên `final_revision` cũ), chỉ timeline/relationship/snapshot của
    chương đó được cập nhật lại theo state as-of chương trước.
    """
    candidate = chapter.final_candidate or _candidate_from_final(chapter)
    final_revision_number = (
        chapter.final_revision.revision
        if refresh and chapter.final_revision is not None
        else (chapter.final_revision.revision + 1 if chapter.final_revision else 1)
    )
    final_revision = FinalRevision(
        revision=final_revision_number,
        source_prose_revision=candidate.prose_revision,
        markdown_ref=candidate.markdown_ref,
        reconciled_at=now or now_iso(),
    )
    final_markdown = storage.read_text(project.root / candidate.markdown_ref)

    timeline = storage.load_timeline(project)
    entry = _build_timeline_entry(
        chapter=chapter,
        proposal=proposal,
        final_revision=final_revision_number,
        timeline=timeline,
        now=now,
    )
    updated_timeline = timeline.model_copy(deep=True)
    replaced = False
    new_entries: list[TimelineEntry] = []
    for existing in updated_timeline.entries:
        if existing.chapter_id == chapter.chapter_id:
            new_entries.append(entry)
            replaced = True
        else:
            new_entries.append(existing)
    if not replaced:
        new_entries.append(entry)
    updated_timeline.entries = new_entries
    updated_timeline.latest_final_chapter = max(
        updated_timeline.latest_final_chapter, chapter.chapter_number
    )
    if updated_timeline.latest_consistent_chapter < chapter.chapter_number:
        # Chỉ tiến lên, không bao giờ ghi lùi latest consistent state.
        inconsistent_later = any(
            item.chapter_number > chapter.chapter_number
            and (item.stale or item.chapter_number > updated_timeline.latest_final_chapter)
            for item in updated_timeline.entries
        )
        if not inconsistent_later:
            updated_timeline.latest_consistent_chapter = max(
                updated_timeline.latest_consistent_chapter, chapter.chapter_number
            )

    relationships = storage.load_relationships(project)
    updated_relationships, applied = _apply_relationship_updates(
        proposal=proposal,
        chapter=chapter,
        final_revision=final_revision_number,
        relationships=relationships,
    )
    if updated_relationships.latest_consistent_chapter < chapter.chapter_number:
        later_stale = any(
            item.last_updated_chapter > chapter.chapter_number or item.stale
            for item in updated_relationships.relationships
        )
        if not later_stale:
            updated_relationships.latest_consistent_chapter = max(
                updated_relationships.latest_consistent_chapter, chapter.chapter_number
            )

    accepted_envelope = lifecycle.accept_candidate(
        envelope,
        accepted_by=accepted_by,
        validation=envelope.candidate_revision.validation
        if envelope.candidate_revision is not None
        else None,
        now=now,
    )
    updated_chapter = chapter.model_copy(
        update={
            "status": ChapterStatus.final_reconciled,
            "final_candidate": None,
            "final_revision": final_revision,
            "reconciliation_pin": DependencyPin(
                artifact_id=envelope.artifact_id,
                revision=accepted_envelope.accepted_revision.revision,
                scope="reconciliation",
                chapter_id=chapter.chapter_id,
            ),
        }
    )
    op_id = operation_id or generate_operation_id()
    handle = storage.begin_operation(
        project, operation_type=OPERATION_RECONCILE_COMMIT, operation_id=op_id
    )
    if handle.replayed:
        return _reconcile_replay_result(handle.manifest, chapter.chapter_id)
    snapshot = _next_chapter_snapshot(
        project,
        chapter=chapter,
        pins=_dependency_pins(project, chapter_id=chapter.chapter_id),
        now=now,
    )
    retcon_marker = _retcon_marker(project, chapter.chapter_id) if not refresh else None
    try:
        # Thứ tự commit (bất biến "chapter chỉ `final_reconciled` khi commit xong"):
        # 1. final manuscript prose
        # 2. reconciliation artifact accepted
        # 3. current_timeline.json
        # 4. relationships.json
        # 5. snapshot chương sau (nếu có)
        # 6. chapter.json (status `final_reconciled`) — LUÔN cuối cùng
        if not refresh:
            # Rebuild downstream không bao giờ rewrite final prose (D004).
            handle.add_text(candidate.markdown_ref, final_markdown)
        handle.add_json(
            _project_relpath(
                project,
                project.paths.artifact_path(envelope.artifact_id, "reconciliation"),
            ),
            accepted_envelope,
        )
        handle.add_json(
            _project_relpath(project, project.paths.timeline_json), updated_timeline
        )
        handle.add_json(
            _project_relpath(project, project.paths.relationships_json),
            updated_relationships,
        )
        if snapshot is not None:
            handle.add_json(
                _project_relpath(
                    project,
                    project.paths.snapshots_dir / f"snapshot_{chapter.chapter_id}_reconcile_r{final_revision_number:04d}.json",
                ),
                snapshot,
            )
        if retcon_marker is not None:
            handle.add_json(
                f"chapters/{chapter.chapter_id}/retcon/state.json",
                {**retcon_marker, "status": "committed", "final_revision": final_revision_number},
            )
        handle.add_json(
            _project_relpath(project, project.paths.chapter_json(chapter.chapter_id)),
            updated_chapter,
        )
        manifest = handle.commit()
    except Exception:
        _abort_quietly(handle)
        raise

    return ActionResult(
        operation_id=op_id,
        artifact_id=envelope.artifact_id,
        chapter_id=chapter.chapter_id,
        message=(
            f"{chapter.chapter_id} đã commit final manuscript + timeline + relationship; "
            "chapter `final_reconciled`."
        ),
        data={
            "status": ChapterStatus.final_reconciled.value,
            "final_revision": final_revision_number,
            "source_prose_revision": candidate.prose_revision,
            "final_markdown_ref": candidate.markdown_ref,
            "timeline_id": entry.timeline_id,
            "relationship_ids": applied,
            "snapshot_created": snapshot is not None,
            "committed_revision": accepted_envelope.accepted_revision.revision,
            "rebuilt_downstream": refresh,
            "applied_paths": list(manifest.applied_paths),
        },
    )


def _reconcile_replay_result(manifest: Any, chapter_id: str) -> ActionResult:
    return ActionResult(
        operation_id=manifest.operation_id,
        chapter_id=chapter_id,
        message=(
            f"{chapter_id}: operation reconcile đã commit trước đó; trả kết quả cũ, "
            "không nhân đôi timeline/relationship."
        ),
        data={
            "replayed": True,
            "result": dict(manifest.result or {}),
            "applied_paths": list(manifest.applied_paths),
        },
    )


def accept_reconciliation(
    project: Project,
    *,
    chapter_id: str,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
    expect_chapter_number: int | None = None,
    refresh_state: bool = False,
) -> ActionResult:
    """Commit transaction final manuscript + state cho proposal đã validate.

    Freshness: proposal phải khớp manuscript revision của `final_candidate` và
    input state as-of N−1 hiện tại. Lệch revision → `StaleDependencyError` và
    không target nào đổi; retry cùng `operation_id` trả kết quả cũ.

    ``refresh_state=True`` dùng cho `revision.reconcile_downstream` (rebuild sau
    retcon): chapter đã `final_reconciled` và state N−1 vừa được rebuild nên
    proposal **không** cần pin cũ.
    """
    chapter = _chapter_or_fail(project, chapter_id)
    op_id = operation_id or generate_operation_id()
    replayed = _test_replay_result(project, op_id)
    if replayed is not None:
        return replayed

    envelope = _proposal_envelope(project, chapter_id)
    accepted_artifact = (
        envelope.accepted_revision is not None
        and envelope.status in {ArtifactStatus.accepted, ArtifactStatus.stale}
    )

    if refresh_state:
        if not accepted_artifact:
            raise GuardError(
                f"{chapter_id} chưa có reconciliation accepted để rebuild.",
                code="missing_accepted",
                details={"chapter_id": chapter_id},
            )
        chapter, _candidate = _chapter_and_candidate(project, chapter_id, refresh=True)
        if expect_chapter_number is not None and chapter.chapter_number != expect_chapter_number:
            raise GuardError(
                f"Chapter `{chapter_id}` là chương {chapter.chapter_number}, không phải "
                f"{expect_chapter_number}.",
                code="chapter_mismatch",
                details={"chapter_id": chapter_id},
            )
    else:
        chapter, _candidate = _chapter_and_candidate(project, chapter_id)
        if expect_chapter_number is not None and chapter.chapter_number != expect_chapter_number:
            raise GuardError(
                f"Chapter `{chapter_id}` là chương {chapter.chapter_number}, không phải "
                f"{expect_chapter_number}.",
                code="chapter_mismatch",
                details={"chapter_id": chapter_id},
            )

    candidate_revision = envelope.candidate_revision
    if candidate_revision is None:
        raise GuardError(
            f"{chapter_id}: không có candidate reconciliation để accept.",
            code="missing_candidate",
            details={"chapter_id": chapter_id},
        )
    try:
        proposal = (
            candidate_revision.payload
            if isinstance(candidate_revision.payload, ReconciliationPayload)
            else ReconciliationPayload.model_validate(candidate_revision.payload)
        )
    except Exception as exc:
        raise ValidationFailure(
            f"Proposal reconciliation không parse được: {exc}",
            details={"chapter_id": chapter_id},
        ) from exc

    if chapter.final_candidate is None and not refresh_state:
        raise GuardError(
            f"{chapter_id} không còn `final_candidate` để reconcile.",
            code="final_candidate_missing",
            details={"chapter_id": chapter_id},
        )

    result = _validate_proposal(project, chapter, proposal, check_source=not refresh_state)
    if result.state is ValidationState.invalid:
        raise _validation_failure(
            "Reconciliation proposal không hợp lệ, không commit", result, chapter_id=chapter_id
        )

    if not refresh_state:
        mismatches = _freshness_violations(project, candidate_revision.dependency_pins)
        _raise_if_stale(
            "Reconciliation proposal không còn fresh với input state as-of chương trước",
            mismatches,
            chapter_id=chapter_id,
        )

    return _commit_reconciliation(
        project,
        chapter=chapter,
        proposal=proposal,
        envelope=envelope,
        accepted_by=accepted_by,
        operation_id=op_id,
        now=now,
        refresh=refresh_state,
    )


def reject_reconciliation(
    project: Project,
    *,
    chapter_id: str,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Reject proposal: chapter vẫn `finalizing`, accepted state không đổi."""
    chapter, _candidate = _chapter_and_candidate(project, chapter_id)
    op_id = operation_id or generate_operation_id()
    envelope = _proposal_envelope(project, chapter_id)
    rejected = lifecycle.reject_candidate(envelope)
    if rejected.status is ArtifactStatus.accepted:  # pragma: no cover - phòng hờ
        raise ServiceError(
            f"{chapter_id}: không thể reject vì proposal đang accepted.",
            code="invalid_transition",
        )
    storage.save_artifact(project, rejected, operation_id=f"{op_id}.reject")
    _set_reconciliation_status(
        project,
        chapter=chapter,
        status=ReconciliationStatus.missing,
        operation_id=f"{op_id}.status",
        now=now,
    )
    return ActionResult(
        operation_id=op_id,
        artifact_id=envelope.artifact_id,
        chapter_id=chapter_id,
        message=(
            "Đã reject proposal; chapter vẫn `finalizing`, timeline/relationship không đổi."
        ),
        data={
            "status": ChapterStatus.finalizing.value,
            "reconciliation_status": ReconciliationStatus.missing.value,
        },
    )


def cancel_finalizing(
    project: Project,
    *,
    chapter_id: str,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Hủy `finalizing`: bỏ final candidate, về `review_required`, accepted state giữ nguyên."""
    chapter = _chapter_or_fail(project, chapter_id)
    if chapter.status is not ChapterStatus.finalizing:
        raise GuardError(
            f"{chapter_id} đang `{chapter.status.value}`, không phải `finalizing`.",
            code="chapter_not_finalizing",
            details={"chapter_id": chapter_id},
        )
    op_id = operation_id or generate_operation_id()
    updated = chapter.model_copy(
        update={
            "status": ChapterStatus.review_required,
            "final_candidate": None,
        }
    )
    storage.save_chapter(project, updated, operation_id=f"{op_id}.cancel")
    return ActionResult(
        operation_id=op_id,
        chapter_id=chapter_id,
        message=(
            "Đã hủy `finalizing`: final candidate bị bỏ, chapter về `review_required`, "
            "accepted state không đổi."
        ),
        data={
            "status": ChapterStatus.review_required.value,
            "final_candidate": None,
        },
    )


def retry_reconcile(
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
    """Retry reconciliation: idempotent, **không** generate lại prose.

    Nếu proposal đã accepted (chapter `final_reconciled`) thì trả kết quả cũ và
    không gọi LLM; ngược lại chạy lại `generate_reconciliation`.
    """
    chapter = _chapter_or_fail(project, chapter_id)
    op_id = operation_id or generate_operation_id()
    replayed = _test_replay_result(project, op_id)
    if replayed is not None:
        return replayed
    envelope = storage.load_artifact(project, _artifact_id_for_chapter(chapter_id))
    if (
        chapter.status is ChapterStatus.final_reconciled
        and envelope is not None
        and envelope.status is ArtifactStatus.accepted
    ):
        return ActionResult(
            operation_id=op_id,
            artifact_id=envelope.artifact_id,
            chapter_id=chapter_id,
            message=(
                f"{chapter_id} đã `final_reconciled` với reconciliation accepted; "
                "retry không tạo proposal mới và không gọi LLM."
            ),
            data={
                "status": ChapterStatus.final_reconciled.value,
                "reconciliation_status": ReconciliationStatus.accepted.value,
                "already_accepted": True,
            },
        )
    result = generate_reconciliation(
        project, client=client, chapter_id=chapter_id, operation_id=op_id, now=now,
        on_event=on_event, stream=stream, attempt=attempt,
    )
    result.message = "Retry reconcile (không generate lại prose): " + result.message
    return result


def recover(project: Project) -> storage.RecoveryReport:
    """Delegate `storage.recover_pending` cho UI/Arbiter sau crash."""
    return storage.recover_pending(project)


def pending_reconciliation(project: Project, *, chapter_id: str) -> dict[str, Any] | None:
    """Helper chỉ-đọc cho UI/Arbiter: trạng thái finalizing/reconciliation của chapter."""
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        return None
    artifact_id = _artifact_id_for_chapter(chapter_id)
    envelope = storage.load_artifact(project, artifact_id)
    return {
        "chapter_id": chapter_id,
        "chapter_number": chapter.chapter_number,
        "status": chapter.status.value,
        "final_candidate": chapter.final_candidate.model_dump(mode="json")
        if chapter.final_candidate
        else None,
        "final_revision": chapter.final_revision.model_dump(mode="json")
        if chapter.final_revision
        else None,
        "reconciliation_artifact_id": artifact_id,
        "reconciliation_status": (
            envelope.status.value if envelope is not None else ArtifactStatus.missing.value
        ),
        "candidate_revision": (
            envelope.candidate_revision.revision
            if envelope is not None and envelope.candidate_revision
            else None
        ),
        "accepted_revision": (
            envelope.accepted_revision.revision
            if envelope is not None and envelope.accepted_revision
            else None
        ),
        "needs_recovery": storage.needs_recovery(project),
        "requires_manual_recovery": storage.requires_manual_recovery(project),
        "pending_operation_ids": storage.pending_operation_ids(project),
    }
