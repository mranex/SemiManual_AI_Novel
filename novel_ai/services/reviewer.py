"""Human Review, AI Review, Rewrite Section và ready-to-finalize (T16).

Phạm vi (workflow.md mục 3.1, 4.4; schemas.md mục 5.2, 5.3; decision D002):

- `ready_to_finalize`: hàm Python thuần đọc state, phân biệt draft partial,
  Human Review chưa xác nhận/đã hết hiệu lực, Skeleton không fresh và previous
  chapter chưa `final_reconciled`. Không gọi LLM, không mutate.
- `run_ai_review`: sinh report từ `review.v1`. Report **không** sửa prose, không
  đổi chapter status, không finalize; report là artifact hỗ trợ (accepted theo
  bookkeeping) và được gắn vào `chapter.ai_review_reports` theo `prose_revision`.
  Quote trong evidence phải là **nguyên văn** của đúng prose revision đó.
- `mark_reviewed`: ghi `HumanReviewRecord` gắn đúng `current_draft_revision`.
- `rewrite_section`: trả candidate replacement trong `ActionResult.data`, **không**
  tạo prose revision và không auto-apply (D015).
- `apply_rewrite`: chỉ apply khi `target_text` khớp **đúng** prose revision yêu
  cầu; tạo prose revision mới và làm Human Review cũ mất hiệu lực.

Quyết định triển khai (ghi ở bàn giao T16):

- `apply_rewrite` không tìm-thay mơ hồ: target phải xuất hiện trong revision
  `expected_revision`; revision lệch ⇒ `stale_replacement`, không tìm thấy ⇒
  `target_not_found`, replacement rỗng/giống hệt target ⇒ từ chối.
- `rewrite_section` resolve target từ `selected_text` trên **đúng** revision
  hiện tại. Writer output không có marker section nên nếu không có
  `selected_text`, service gửi toàn draft làm target kèm cảnh báo (không tự
  đoán ranh giới section).
- Reviewer không import writer: đọc prose revision bằng helper riêng để hai
  service không phụ thuộc nhau.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from novel_ai import config as app_config
from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.context import (
    ContextBundle,
    ContextError,
    build_review_context,
    build_rewrite_context,
)
from novel_ai.core.llm import (
    LLMClient,
    LLMError,
    LLMRequest,
    StructuredOutputParseError,
    parse_structured_text,
)
from novel_ai.core.models import (
    ChapterMetadata,
    ChapterStatus,
    HumanReviewRecord,
    PayloadSource,
    ProseRevision,
    ReviewReportPayload,
    ReviewReportRef,
    RewriteSectionPayload,
    RewriteSectionRequest,
    SourceType,
    generate_operation_id,
    now_iso,
)
from novel_ai.core.generation import EventSink, GenerationEmitter
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
from novel_ai.services.co_create import generation_stage, structured_transport

__all__ = [
    "REVIEW_PROMPT_ID",
    "REWRITE_PROMPT_ID",
    "ReadyCheck",
    "apply_rewrite",
    "mark_reviewed",
    "ready_to_finalize",
    "rewrite_section",
    "run_ai_review",
]

REVIEW_PROMPT_ID = "review.v1"
REWRITE_PROMPT_ID = "rewrite_section.v1"

#: Số ký tự ngữ cảnh hai bên target gửi kèm prompt rewrite (chỉ để đọc).
SURROUNDING_CHARS = 500


# ---------------------------------------------------------------------------
# Ready to finalize
# ---------------------------------------------------------------------------


@dataclass
class ReadyCheck:
    """Kết quả kiểm tra điều kiện finalize, đủ để UI hiển thị lý do cụ thể."""

    ready: bool
    reasons: list[str] = field(default_factory=list)
    has_draft: bool = False
    is_complete: bool = False
    human_review_valid: bool = False
    skeleton_fresh: bool = False
    previous_chapter_ok: bool = False
    pending_edits: bool = False


def _previous_chapter_ok(project: Project, chapter: ChapterMetadata) -> tuple[bool, str]:
    if chapter.chapter_number <= 1:
        return True, ""
    previous = (
        storage.load_chapter(project, chapter.previous_chapter_id)
        if chapter.previous_chapter_id
        else None
    )
    if previous is None:
        for chapter_id in storage.list_chapter_ids(project):
            candidate = storage.load_chapter(project, chapter_id)
            if candidate is not None and candidate.chapter_number == chapter.chapter_number - 1:
                previous = candidate
                break
    if previous is None:
        return False, (
            f"Không tìm thấy chapter {chapter.chapter_number - 1} để xác nhận guard chương trước."
        )
    if previous.status is not ChapterStatus.final_reconciled:
        return False, (
            f"Chương {previous.chapter_number} đang `{previous.status.value}`; "
            "chương sau chỉ unlock khi chương trước `final_reconciled`."
        )
    return True, ""


def ready_to_finalize(project: Project, *, chapter_id: str) -> ReadyCheck:
    """Kiểm tra điều kiện finalize của chapter (chỉ đọc, không mutate)."""
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        return ReadyCheck(
            ready=False,
            reasons=[f"Chưa có chapter metadata cho `{chapter_id}`."],
        )

    draft = chapter.current_draft
    has_draft = draft is not None
    is_complete = bool(draft is not None and draft.is_complete)
    review = chapter.human_review
    human_valid = bool(
        review is not None
        and chapter.current_draft_revision is not None
        and review.prose_revision == chapter.current_draft_revision
        and review.valid_for_current_revision
    )
    skeleton_result = lifecycle.guard_skeleton_for_chapter(project, chapter_id)
    skeleton_fresh = skeleton_result.allowed
    previous_ok, previous_reason = _previous_chapter_ok(project, chapter)
    pending_edits = (not is_complete) or (review is not None and not human_valid)

    reasons: list[str] = []
    if not has_draft:
        reasons.append(f"`{chapter_id}` chưa có prose revision nào.")
    elif not is_complete:
        reasons.append(
            f"Draft r{draft.revision} chưa complete (partial/stream đứt không được coi là review_required)."
        )
    if has_draft and review is None:
        reasons.append("Chưa có Human Review cho draft hiện tại; Human Review là gate cứng.")
    if review is not None and not human_valid:
        reasons.append(
            f"Human Review gắn r{review.prose_revision} nhưng draft hiện tại là "
            f"r{chapter.current_draft_revision}; cần review lại revision hiện tại."
        )
    if not skeleton_fresh:
        reasons.extend(skeleton_result.reasons)
    if not previous_ok:
        reasons.append(previous_reason)

    guard = lifecycle.guard_finalize(project, chapter_id)
    if not guard.allowed:
        for reason in guard.reasons:
            if reason not in reasons:
                reasons.append(reason)

    ready = bool(
        has_draft
        and is_complete
        and human_valid
        and skeleton_fresh
        and previous_ok
        and guard.allowed
    )
    return ReadyCheck(
        ready=ready,
        reasons=reasons,
        has_draft=has_draft,
        is_complete=is_complete,
        human_review_valid=human_valid,
        skeleton_fresh=skeleton_fresh,
        previous_chapter_ok=previous_ok,
        pending_edits=pending_edits,
    )


# ---------------------------------------------------------------------------
# Hạ tầng dùng chung trong module
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _registry(repo_root: str) -> PromptRegistry:
    return PromptRegistry.load(repo_root=Path(repo_root))


def _render(project: Project, prompt_id: str, bundle: ContextBundle):
    root = Path(app_config.REPO_ROOT)
    try:
        return render_prompt(
            _registry(str(root)),
            prompt_id,
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


def _require_chapter(project: Project, chapter_id: str) -> ChapterMetadata:
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        raise GuardError(
            f"Chưa có chapter metadata cho `{chapter_id}`.", code="chapter_missing"
        )
    return chapter


def _require_draft(project: Project, chapter_id: str) -> tuple[ChapterMetadata, ProseRevision]:
    """Chapter + draft hiện tại; từ chối khi chưa có hoặc chưa complete (guard_review)."""
    chapter = _require_chapter(project, chapter_id)
    guard = lifecycle.guard_review(project, chapter_id)
    if not guard.allowed:
        raise GuardError(
            "; ".join(guard.reasons) or f"Review guard chặn `{chapter_id}`.",
            code=guard.code,
            details={"reasons": list(guard.reasons)},
        )
    draft = chapter.current_draft
    if draft is None:  # pragma: no cover - guard_review đã chặn
        raise GuardError(f"`{chapter_id}` chưa có prose revision nào.", code="draft_missing")
    return chapter, draft


def _draft_text(project: Project, chapter: ChapterMetadata, revision: int) -> str:
    """Đọc markdown của một prose revision theo `markdown_ref` (không mutate)."""
    draft = chapter.draft_revision(int(revision))
    if draft is None:
        raise GuardError(
            f"`{chapter.chapter_id}` không có prose revision r{revision}.", code="draft_not_found"
        )
    candidates = (
        storage.ensure_within_project(project.root, project.root / draft.markdown_ref),
        project.paths.chapter_dir(chapter.chapter_id) / draft.markdown_ref,
        project.paths.drafts_dir(chapter.chapter_id) / draft.markdown_ref,
    )
    for path in candidates:
        if path.is_file():
            return storage.read_text(path)
    raise GuardError(
        f"Không thấy file markdown `{draft.markdown_ref}` của `{chapter.chapter_id}` r{revision}.",
        code="draft_markdown_missing",
    )


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _call_complete(client: LLMClient, request: LLMRequest, *, label: str) -> str:
    """Gọi LLM non-stream và trả raw text (lỗi provider được map rõ ràng)."""
    try:
        response = client.complete(request)
    except LLMError as exc:
        raise LLMUnavailableError(
            f"LLM lỗi khi {label}: {exc}", code=exc.code, details=exc.details
        ) from exc
    return response.text if isinstance(response.text, str) else str(response.text)


def _parse_payload(raw_text: str, model_cls: Any, *, raw_ref: str, label: str) -> Any:
    try:
        return parse_structured_text(raw_text, model_cls)
    except StructuredOutputParseError as exc:
        raise ValidationFailure(
            f"{label} không parse/không đúng schema: {exc}. Raw đã lưu tại {raw_ref}.",
            result=validation.result_from_issues(exc.errors),
            code="structured_output_parse",
            details={"raw_output_ref": raw_ref},
        ) from exc


def _next_revision(chapter: ChapterMetadata) -> int:
    return max((draft.revision for draft in chapter.drafts), default=0) + 1


def _draft_relpath(chapter_id: str, revision: int) -> str:
    return f"chapters/{chapter_id}/drafts/draft_r{revision:04d}.md"


def _invalidate_review(chapter: ChapterMetadata, new_revision: int) -> bool:
    review = chapter.human_review
    if review is not None and review.prose_revision != new_revision:
        chapter.human_review = review.model_copy(
            update={"valid_for_current_revision": False}
        )
        return True
    return False


# ---------------------------------------------------------------------------
# AI Review
# ---------------------------------------------------------------------------


def _validate_review_quotes(
    payload: ReviewReportPayload, prose_markdown: str
) -> validation.ValidationResult:
    """Quote trong evidence phải là nguyên văn của đúng prose revision."""
    collector = validation.IssueCollector()
    prose = _normalized(prose_markdown)
    for position, issue in enumerate(payload.issues):
        quote = issue.evidence.quote
        if quote is None or not quote.strip():
            continue
        if _normalized(quote) not in prose:
            collector.add(
                validation.json_pointer("payload", "issues", position, "evidence", "quote"),
                "quote_not_verbatim",
                "Quote phải là nguyên văn của prose revision được review; "
                "không được chế quote hoặc lấy từ revision khác.",
            )
    return collector.result()


def run_ai_review(
    project: Project,
    *,
    client: LLMClient,
    chapter_id: str,
    review_focus: str = "",
    operation_id: str | None = None,
    now: str | None = None,
    on_event: EventSink | None = None,
    stream: bool = False,
    attempt: int = 1,
) -> ActionResult:
    """Chạy AI Review trên draft hiện tại và lưu report.

    Yêu cầu draft `is_complete`. Report được lưu thành artifact
    `review_report_<chapter_id>` và thêm `ReviewReportRef` vào chapter; report
    **không** sửa prose, không đổi chapter status, không finalize. Stream hoàn tất
    cũng **không** biến report thành canon — report chỉ là dữ liệu hỗ trợ.
    `on_event` nhận `GenerationEvent` (T33/T35, D019).
    """
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter, draft = _require_draft(project, chapter_id)
    prose_markdown = _draft_text(project, chapter, draft.revision)
    emitter = (
        GenerationEmitter(
            operation_id=op_id,
            action="review.ai_review",
            sink=on_event,
            attempt=attempt,
            prompt_id=REVIEW_PROMPT_ID,
            artifact_id=f"review_report_{chapter_id}",
            chapter_id=chapter_id,
        )
        if on_event is not None
        else None
    )

    try:
        bundle = build_review_context(
            project,
            chapter_id=chapter_id,
            prose_revision=draft.revision,
            prose_markdown=prose_markdown,
            review_focus=review_focus,
        )
    except ContextError as exc:
        raise _map_context_error(exc) from exc

    rendered = _render(project, REVIEW_PROMPT_ID, bundle)
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
        label=f"review_{chapter_id}",
        emitter=emitter,
        stream=stream,
        now=stamp,
        prompt_id=REVIEW_PROMPT_ID,
    )
    raw_text = transport.text
    raw_ref = transport.raw_ref
    with generation_stage(emitter, detail="Đang parse/validate AI Review report."):
        payload = _parse_payload(
            raw_text, ReviewReportPayload, raw_ref=raw_ref, label="AI Review output"
        )

        if payload.chapter_id != chapter_id:
            raise ValidationFailure(
                f"Report khai `{payload.chapter_id}` nhưng action là `{chapter_id}`.",
                code="review_chapter_mismatch",
                details={"raw_output_ref": raw_ref},
            )
        if payload.prose_revision != draft.revision:
            raise ValidationFailure(
                f"Report gắn prose r{payload.prose_revision} nhưng draft hiện tại là r{draft.revision}; "
                "không nhận report của revision khác.",
                code="review_revision_mismatch",
                details={"raw_output_ref": raw_ref},
            )

        result = validation.validate_artifact_payload(
            "review_report",
            payload,
            context=validation.ValidationContext(chapter_number=chapter.chapter_number),
        )
        quote_result = _validate_review_quotes(payload, prose_markdown)
        if not quote_result.is_valid or not result.is_valid:
            combined = validation.result_from_issues(
                list(result.errors) + list(quote_result.errors)
            )
            raise ValidationFailure(
                "AI Review report không qua validation: " + validation.summarize_errors(combined),
                result=combined,
                code="validation_failed",
                details={"raw_output_ref": raw_ref},
            )

        artifact_id = lifecycle.artifact_id_for("review_report", chapter_id=chapter_id)
        envelope = storage.load_artifact(project, artifact_id) or lifecycle.new_artifact(
            "review_report", artifact_id
        )
        source = PayloadSource(
            source_type=SourceType.llm,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.prompt_version,
            prompt_hash=rendered.template_hash,
            operation_id=op_id,
            raw_output_ref=raw_ref,
        )
    pinned = lifecycle.set_candidate(
        envelope,
        payload,
        source=source,
        dependency_pins=bundle.dependency_pins,
        validation=result,
        now=stamp,
    )
    accepted = lifecycle.accept_candidate(pinned, accepted_by="system", validation=result, now=stamp)
    storage.save_artifact(project, accepted, operation_id=op_id)
    report_revision = (
        accepted.accepted_revision.revision if accepted.accepted_revision is not None else 1
    )

    chapter_updated = chapter.model_copy(deep=True)
    ref = ReviewReportRef(
        artifact_id=artifact_id, revision=report_revision, prose_revision=draft.revision
    )
    if ref not in chapter_updated.ai_review_reports:
        chapter_updated.ai_review_reports = [*chapter_updated.ai_review_reports, ref]
    storage.save_chapter(project, chapter_updated, operation_id=op_id)

    severities: dict[str, int] = {}
    for issue in payload.issues:
        severities[issue.severity.value] = severities.get(issue.severity.value, 0) + 1

    if emitter is not None:
        emitter.saved(
            detail=(
                f"AI Review report r{report_revision} đã lưu (report hỗ trợ, không thay "
                "Human Review và không sửa prose)."
            ),
            raw_ref=raw_ref,
        )
    return ActionResult(
        operation_id=op_id,
        artifact_id=artifact_id,
        chapter_id=chapter_id,
        message=(
            f"AI Review report r{report_revision} cho prose r{draft.revision}: "
            f"{len(payload.issues)} issue."
        ),
        warnings=[
            "Report là hỗ trợ: không thay Human Review, không sửa prose, không đổi chapter status."
        ],
        validation=result,
        data={
            "prose_revision": draft.revision,
            "report_revision": report_revision,
            "issues": [issue.model_dump(mode="json") for issue in payload.issues],
            "summary": payload.summary,
            "severity_counts": severities,
            "raw_output_ref": raw_ref,
        },
    )


# ---------------------------------------------------------------------------
# Human Review
# ---------------------------------------------------------------------------


def mark_reviewed(
    project: Project,
    *,
    chapter_id: str,
    notes: str = "",
    reviewed_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Ghi Human Review gắn đúng `current_draft_revision` của chapter.

    Yêu cầu draft `is_complete`. Sửa prose sau đó làm record này mất hiệu lực
    (D002); record cũ vẫn được giữ để audit.
    """
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter, draft = _require_draft(project, chapter_id)
    if chapter.status is ChapterStatus.final_reconciled:
        raise GuardError(
            f"`{chapter_id}` đã `final_reconciled`; review lại bản final phải đi qua action retcon.",
            code="chapter_already_final",
        )

    record = HumanReviewRecord(
        prose_revision=draft.revision,
        reviewed_at=stamp,
        reviewed_by=reviewed_by,
        notes=notes,
        valid_for_current_revision=True,
    )
    chapter_updated = chapter.model_copy(deep=True)
    chapter_updated.human_review = record
    storage.save_chapter(project, chapter_updated, operation_id=op_id)

    return ActionResult(
        operation_id=op_id,
        chapter_id=chapter_id,
        message=(
            f"Đã ghi Human Review cho `{chapter_id}` prose r{draft.revision} "
            f"({reviewed_by})."
        ),
        data={
            "prose_revision": draft.revision,
            "reviewed_by": reviewed_by,
            "reviewed_at": stamp,
            "valid_for_current_revision": True,
        },
    )


# ---------------------------------------------------------------------------
# Rewrite Section
# ---------------------------------------------------------------------------


def _resolve_target(
    request: RewriteSectionRequest, prose_markdown: str
) -> tuple[str, str, str, list[str]]:
    """`(target, before, after, warnings)` cho prompt rewrite."""
    warnings: list[str] = []
    selected = request.selected_text or ""
    if selected:
        if selected in prose_markdown:
            target = selected
        elif selected.strip() and selected.strip() in prose_markdown:
            target = selected.strip()
        else:
            raise GuardError(
                f"`selected_text` không có trong prose r{request.prose_revision}; "
                "không tìm-thay mơ hồ trên bản khác revision.",
                code="target_not_found",
            )
    else:
        target = prose_markdown
        warnings.append(
            "Không có `selected_text`: gửi toàn bộ draft làm target. Writer output không có "
            "marker section nên backend không tự đoán ranh giới section."
        )
    start = prose_markdown.find(target)
    before = prose_markdown[max(0, start - SURROUNDING_CHARS) : start] if start >= 0 else ""
    end = start + len(target) if start >= 0 else 0
    after = prose_markdown[end : end + SURROUNDING_CHARS] if start >= 0 else ""
    return target, before, after, warnings


def rewrite_section(
    project: Project,
    *,
    client: LLMClient,
    request: RewriteSectionRequest,
    operation_id: str | None = None,
    now: str | None = None,
    on_event: EventSink | None = None,
    stream: bool = False,
    attempt: int = 1,
) -> ActionResult:
    """Sinh candidate replacement cho một đoạn prose; **không** tạo revision.

    Candidate nằm trong `ActionResult.data["replacement_markdown"]`; user phải
    gọi `apply_rewrite` để thay vào draft (JSON response không auto-apply prose).
    Stream hoàn tất cũng **không** apply: apply vẫn là action riêng của user.
    """
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = _require_chapter(project, request.chapter_id)
    emitter = (
        GenerationEmitter(
            operation_id=op_id,
            action="review.rewrite_section",
            sink=on_event,
            attempt=attempt,
            prompt_id=REWRITE_PROMPT_ID,
            artifact_id=f"rewrite_{request.chapter_id}",
            chapter_id=request.chapter_id,
        )
        if on_event is not None
        else None
    )
    if chapter.current_draft_revision != request.prose_revision:
        raise GuardError(
            f"`{request.chapter_id}` đang ở prose r{chapter.current_draft_revision} nhưng request "
            f"gắn r{request.prose_revision}; cần rewrite trên revision hiện tại.",
            code="stale_replacement",
        )
    prose_markdown = _draft_text(project, chapter, request.prose_revision)
    target, before, after, warnings = _resolve_target(request, prose_markdown)

    try:
        bundle = build_rewrite_context(
            project,
            request=request,
            target_markdown=target,
            surrounding_before=before,
            surrounding_after=after,
        )
    except ContextError as exc:
        raise _map_context_error(exc) from exc

    rendered = _render(project, REWRITE_PROMPT_ID, bundle)
    llm_request = LLMRequest(
        messages=rendered.messages,
        json_output=True,
        prompt_id=rendered.prompt_id,
        prompt_version=rendered.prompt_version,
        prompt_hash=rendered.template_hash,
    )
    transport = structured_transport(
        project,
        client=client,
        request=llm_request,
        operation_id=op_id,
        label=f"rewrite_{request.chapter_id}",
        emitter=emitter,
        stream=stream,
        now=stamp,
        prompt_id=REWRITE_PROMPT_ID,
    )
    raw_text = transport.text
    raw_ref = transport.raw_ref
    with generation_stage(emitter, detail="Đang parse/validate Rewrite candidate."):
        payload = _parse_payload(
            raw_text, RewriteSectionPayload, raw_ref=raw_ref, label="Rewrite output"
        )

        result = validation.validate_artifact_payload("rewrite_section", payload)
        if not result.is_valid:
            raise ValidationFailure(
                "Rewrite candidate không qua validation: " + validation.summarize_errors(result),
                result=result,
                code="validation_failed",
                details={"raw_output_ref": raw_ref},
            )
    if payload.replacement_markdown == target:
        warnings.append(
            "Replacement giống hệt target (no-op): không có gì để apply."
        )
    if payload.changed_intent:
        warnings.append(
            "`changed_intent=True`: rewrite có thể vượt quyền Writer; cần người dùng review upstream "
            "trước khi apply."
        )

    if emitter is not None:
        emitter.saved(
            detail=(
                "Rewrite candidate đã validate và trả về; **chưa** apply vào prose "
                "(apply là action riêng của user)."
            ),
            raw_ref=raw_ref,
        )
    return ActionResult(
        operation_id=op_id,
        chapter_id=request.chapter_id,
        message=(
            f"Rewrite candidate cho prose r{request.prose_revision} của `{request.chapter_id}`; "
            "chưa tạo revision — cần `apply_rewrite` do người dùng gọi."
        ),
        warnings=warnings,
        validation=result,
        data={
            "chapter_id": request.chapter_id,
            "prose_revision": request.prose_revision,
            "section_id": request.section_id,
            "target_text": target,
            "replacement_markdown": payload.replacement_markdown,
            "notes": list(payload.notes),
            "changed_intent": payload.changed_intent,
            "raw_output_ref": raw_ref,
            "prompt_id": rendered.prompt_id,
            "prompt_version": rendered.prompt_version,
        },
    )


def apply_rewrite(
    project: Project,
    *,
    chapter_id: str,
    replacement_markdown: str,
    target_text: str,
    expected_revision: int,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Thay **đúng** `target_text` trong prose r`expected_revision`.

    Revision lệch ⇒ `stale_replacement`; không tìm thấy target ⇒
    `target_not_found`; replacement rỗng/giống target ⇒ từ chối. Thành công tạo
    prose revision mới và làm Human Review cũ mất hiệu lực; không đổi gì ngoài
    scope này.
    """
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = _require_chapter(project, chapter_id)
    if chapter.status is ChapterStatus.final_reconciled:
        raise GuardError(
            f"`{chapter_id}` đã `final_reconciled`; sửa prose phải đi qua action retcon.",
            code="chapter_already_final",
        )
    if chapter.status is ChapterStatus.finalizing:
        raise GuardError(
            f"`{chapter_id}` đang `finalizing`; hoàn tất hoặc hủy finalize trước khi sửa prose.",
            code="chapter_finalizing",
        )
    if chapter.current_draft_revision != expected_revision:
        raise GuardError(
            f"`{chapter_id}` đang ở prose r{chapter.current_draft_revision} nhưng apply gắn "
            f"r{expected_revision}; replacement đã stale.",
            code="stale_replacement",
        )
    draft = chapter.draft_revision(int(expected_revision))
    if draft is None:
        raise GuardError(
            f"`{chapter_id}` không có prose revision r{expected_revision}.",
            code="draft_not_found",
        )
    if not target_text or not target_text.strip():
        raise GuardError("`target_text` rỗng; không có gì để thay.", code="target_not_found")
    if not replacement_markdown or not replacement_markdown.strip():
        raise GuardError(
            "`replacement_markdown` rỗng là no-op; backend không tạo revision mới từ replacement rỗng.",
            code="empty_replacement",
        )
    if replacement_markdown == target_text:
        raise GuardError(
            "`replacement_markdown` giống hệt `target_text`; apply sẽ không đổi gì.",
            code="no_op_replacement",
        )

    prose_markdown = _draft_text(project, chapter, expected_revision)
    occurrences = prose_markdown.count(target_text)
    if occurrences == 0:
        raise GuardError(
            f"Không tìm thấy `target_text` trong prose r{expected_revision} của `{chapter_id}`.",
            code="target_not_found",
        )
    warnings: list[str] = []
    if occurrences > 1:
        warnings.append(
            f"`target_text` xuất hiện {occurrences} lần trong prose r{expected_revision}; "
            "chỉ lần xuất hiện đầu tiên được thay."
        )

    new_prose = prose_markdown.replace(target_text, replacement_markdown, 1)
    revision = _next_revision(chapter)
    markdown_ref = storage.write_markdown(
        project,
        _draft_relpath(chapter_id, revision),
        new_prose,
        operation_id=op_id,
    )
    new_draft = ProseRevision(
        revision=revision,
        markdown_ref=markdown_ref,
        source_type=SourceType.user,
        is_complete=draft.is_complete,
        created_at=stamp,
        dependency_pins=list(draft.dependency_pins),
    )
    chapter_updated = chapter.model_copy(deep=True)
    chapter_updated.drafts = [*chapter.drafts, new_draft]
    chapter_updated.current_draft_revision = revision
    review_invalidated = _invalidate_review(chapter_updated, revision)
    chapter_updated.status = (
        ChapterStatus.review_required if new_draft.is_complete else ChapterStatus.draft
    )
    storage.save_chapter(project, chapter_updated, operation_id=op_id)

    if review_invalidated:
        warnings.append(
            "Human Review cũ không còn hiệu lực cho revision mới; cần review lại trước khi finalize."
        )
    return ActionResult(
        operation_id=op_id,
        chapter_id=chapter_id,
        message=(
            f"Đã apply rewrite vào `{chapter_id}`: prose r{draft.revision} → r{revision} "
            f"({len(target_text)} → {len(replacement_markdown)} ký tự)."
        ),
        warnings=warnings,
        data={
            "chapter_id": chapter_id,
            "previous_revision": draft.revision,
            "revision": revision,
            "markdown_ref": markdown_ref,
            "is_complete": new_draft.is_complete,
            "status": chapter_updated.status.value,
            "human_review_invalidated": review_invalidated,
        },
    )
