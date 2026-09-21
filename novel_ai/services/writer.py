"""Writer service: sinh prose draft từ Skeleton accepted (T15).

Module này hiện thực contract `docs/design/workflow.md` mục 4.3, 5.1, 5.5 và
`docs/design/context.md` mục 6 trong phạm vi **một action một hàm**:

- `generate_draft` / `regenerate_draft`: sinh một prose revision mới từ
  `writer.v1`. Guard chạy **trước** khi gọi LLM (`context.build_writer_context`
  là guard backend: Skeleton accepted/fresh, đúng pin chapter, previous chapter
  `final_reconciled`, state actual, không leak secret). Guard fail thì client
  không được gọi lần nào.
- `continue_draft`: nối prose vào **tail của draft hiện tại** trong cùng
  Skeleton/context; không tạo plan mới, không đổi upstream. Lỗi giữ partial.
- `save_draft`: user edit toàn draft, sinh prose revision mới và làm Human
  Review cũ mất hiệu lực.
- `discard_draft`: bỏ một revision khỏi metadata (giữ nguyên final/accepted và
  giữ file markdown để audit).
- `draft_markdown`: đọc prose của một revision.
- `latest_operation_for`: metadata operation gần nhất của chapter để UI
  rerun/retry mà **không** tự chạy lại action.

Quyết định triển khai (ghi ở bàn giao T15):

- **Partial**: stream đứt hoặc output rỗng/chỉ là thông báo lỗi đều là
  `is_complete=False`; chapter về `draft`, **không** `review_required`.
  Output không có prose (rỗng hoặc chỉ thông báo lỗi) **không** tạo prose
  revision: không nhét thông báo lỗi vào manuscript (D015), chỉ ghi operation
  record `reason="no_prose_output"`.
- **Operation record**: mỗi run ghi `chapters/<ch>/operations/<operation_id>.json`
  qua `storage.write_json_atomic`; retry cùng `operation_id` trả lại record cũ
  và **không** gọi LLM / không tạo revision thứ hai (idempotency).
- **Draft markdown**: `chapters/<ch>/drafts/draft_r<NNNN>.md`, ghi qua
  `storage.write_markdown`; `ProseRevision.markdown_ref` là relpath trong project.
- Writer **không** mutate plan/foundation/timeline/relationship: chỉ ghi
  markdown prose, `chapter.json`, operation record và raw output.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

from novel_ai import config as app_config
from novel_ai.core import storage, validation
from novel_ai.core.context import ContextBundle, ContextError, build_writer_context
from novel_ai.core.llm import (
    LLMClient,
    LLMError,
    LLMRequest,
    StreamChunk,
    stream_text,
)
from novel_ai.core.models import (
    ChapterMetadata,
    ChapterStatus,
    DependencyPin,
    ProseRevision,
    SourceType,
    generate_operation_id,
    now_iso,
)
from novel_ai.core.project import Project
from novel_ai.core.prompts import PromptError, PromptRegistry, render_prompt
from novel_ai.services import (
    ActionResult,
    GuardError,
    LLMUnavailableError,
    ServiceError,
    StaleDependencyError,
)

__all__ = [
    "DRAFT_PROMPT_ID",
    "continue_draft",
    "discard_draft",
    "draft_markdown",
    "generate_draft",
    "latest_operation_for",
    "regenerate_draft",
    "save_draft",
]

#: Prompt v1 dùng cho mọi action Writer (output Markdown, không JSON).
DRAFT_PROMPT_ID = "writer.v1"

#: Operation type ghi trong operation record.
OPERATION_GENERATE = "writer_generate_draft"
OPERATION_REGENERATE = "writer_regenerate_draft"
OPERATION_CONTINUE = "writer_continue_draft"
OPERATION_SAVE = "writer_save_draft"
OPERATION_DISCARD = "writer_discard_draft"

#: Số ký tự tail của draft gửi kèm khi Continue (đủ để nối mạch, không quá dài).
CONTINUE_TAIL_CHARS = 1_500

#: Ngưỡng độ dài để coi một output ngắn là "thông báo lỗi" thay vì prose.
ERROR_NOTICE_MAX_CHARS = 400

#: Dấu hiệu output chỉ là thông báo từ chối/không đủ dữ liệu (catalog mục 8.2).
_REFUSAL_MARKERS: tuple[str, ...] = (
    "xin lỗi",
    "tôi không thể",
    "tôi không đủ",
    "không thể viết",
    "không đủ dữ liệu",
    "thiếu dữ liệu",
    "không có đủ thông tin",
    "i cannot",
    "i can't",
    "i'm unable",
    "unable to",
    "error:",
    "lỗi:",
)

#: Chỉ thị Continue ghép vào `user_instruction` (đã là field writer-safe).
_CONTINUE_INSTRUCTION = (
    "Tiếp tục đúng mạch prose dưới đây, viết tiếp phần còn thiếu của chương theo "
    "Skeleton hiện hành. Chỉ trả phần prose nối tiếp, không lặp lại phần đã có, "
    "không thêm tóm tắt hay lời dẫn.\n\n"
    "--- phần prose đã có (tail) ---\n{tail}\n--- hết tail ---"
)


# ---------------------------------------------------------------------------
# Hạ tầng dùng chung trong module
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _registry(repo_root: str) -> PromptRegistry:
    """Prompt registry v1 của repo (load một lần cho mỗi repo root)."""
    return PromptRegistry.load(repo_root=Path(repo_root))


def _render(project: Project, prompt_id: str, bundle: ContextBundle):
    """Render prompt v1 từ payload context; lỗi prompt thành `ServiceError`."""
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
    """Map `ContextError` (guard backend) sang lỗi service có code ổn định."""
    if exc.code in {"stale_dependency", "provisional_not_writer_ready"}:
        return StaleDependencyError(str(exc), code=exc.code, details=exc.details)
    return GuardError(str(exc), code=exc.code, details=exc.details)


def _auto_accept_warnings(project: Project, output_kind: str) -> list[str]:
    """Cảnh báo khi project bật Auto Accept: prose không bao giờ được auto accept.

    Auto Accept chỉ áp cho structured output (D005); prose/report/human review
    luôn cần action rõ ràng của người dùng. Hàm này chỉ *báo* giới hạn đó.
    """
    result = validation.validate_auto_accept_scope(
        auto_accept_structured=bool(project.config.auto_accept_structured),
        output_kind=output_kind,
    )
    if result.is_valid:
        return []
    return [
        f"Auto Accept đang bật nhưng không áp dụng cho `{output_kind}`; "
        "output vẫn là draft cần người dùng xác nhận."
    ]


def _day_of(stamp: str) -> str | None:
    """Ngày `YYYY-MM-DD` của timestamp ISO, dùng cho thư mục raw."""
    head = str(stamp)[:10]
    return head if len(head) == 10 and head[4] == "-" and head[7] == "-" else None


def _draft_relpath(chapter_id: str, revision: int) -> str:
    return f"chapters/{chapter_id}/drafts/draft_r{revision:04d}.md"


def _operations_relpath(chapter_id: str, operation_id: str) -> str:
    return f"chapters/{chapter_id}/operations/{operation_id}.json"


def _operation_record_path(project: Project, chapter_id: str, operation_id: str) -> Path:
    relpath = _operations_relpath(chapter_id, operation_id)
    return storage.ensure_within_project(project.root, project.root / relpath)


def _load_operation_record(project: Project, chapter_id: str, operation_id: str) -> dict[str, Any] | None:
    """Record của đúng `operation_id`; `None` nếu chưa chạy."""
    path = _operation_record_path(project, chapter_id, operation_id)
    if not path.is_file():
        return None
    data = storage.read_json(path)
    return dict(data) if isinstance(data, dict) else None


def _save_operation_record(
    project: Project,
    chapter_id: str,
    record: dict[str, Any],
    *,
    operation_id: str,
) -> None:
    """Ghi operation record (metadata rerun/retry, không phải canon)."""
    path = _operation_record_path(project, chapter_id, str(record["operation_id"]))
    storage.write_json_atomic(path, record, operation_id=f"{operation_id}.meta")


def latest_operation_for(project: Project, chapter_id: str) -> dict[str, Any] | None:
    """Operation record gần nhất của chapter, hoặc `None` nếu chưa có run nào.

    Chỉ **đọc** metadata để UI hiển thị rerun/retry; hàm không tự chạy lại action.
    """
    directory = project.paths.chapter_dir(chapter_id) / "operations"
    if not directory.is_dir():
        return None
    records: list[dict[str, Any]] = []
    for item in sorted(directory.glob("*.json")):
        try:
            data = storage.read_json(item)
        except storage.StorageError:  # pragma: no cover - record hỏng bị bỏ qua
            continue
        if isinstance(data, dict):
            records.append(dict(data))
    if not records:
        return None
    records.sort(
        key=lambda item: (
            int(item.get("revision") or 0),
            str(item.get("created_at") or ""),
            str(item.get("operation_id") or ""),
        )
    )
    return records[-1]


def _replay_result(record: dict[str, Any], *, action: str) -> ActionResult:
    return ActionResult(
        operation_id=record.get("operation_id"),
        chapter_id=record.get("chapter_id"),
        message=(
            f"`{action}` với operation_id `{record.get('operation_id')}` đã được xử lý; "
            "không gọi LLM và không tạo revision mới."
        ),
        warnings=["Idempotent replay theo operation_id."],
        data=dict(record),
    )


def _require_editable_chapter(project: Project, chapter_id: str) -> ChapterMetadata:
    """Chapter phải tồn tại và chưa ở trạng thái final/finalizing."""
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        raise GuardError(
            f"Chưa có chapter metadata cho `{chapter_id}`; chạy Short Plan/accept chapter trước.",
            code="chapter_missing",
        )
    if chapter.status is ChapterStatus.final_reconciled:
        raise GuardError(
            f"`{chapter_id}` đã `final_reconciled`; sửa prose phải đi qua action retcon.",
            code="chapter_already_final",
        )
    if chapter.status is ChapterStatus.finalizing:
        raise GuardError(
            f"`{chapter_id}` đang `finalizing`; cần hoàn tất hoặc hủy finalize trước khi sinh draft mới.",
            code="chapter_finalizing",
        )
    return chapter


def _writer_bundle(project: Project, chapter_id: str, user_instruction: str) -> ContextBundle:
    """Guard + context Writer; `ContextError` được map sang lỗi service."""
    try:
        return build_writer_context(
            project, chapter_id=chapter_id, user_instruction=user_instruction
        )
    except ContextError as exc:
        raise _map_context_error(exc) from exc


def _dependency_pins(
    project: Project, bundle: ContextBundle, chapter_id: str
) -> list[DependencyPin]:
    """Pin của prose revision: Skeleton accepted đã dùng + pins context khác."""
    pins: list[DependencyPin] = []
    revision = _skeleton_revision(project, chapter_id)
    if revision is not None:
        pins.append(
            DependencyPin(
                artifact_id=f"skeleton_{chapter_id}",
                revision=revision,
                scope="skeleton",
                chapter_id=chapter_id,
            )
        )
    return pins + [pin for pin in bundle.dependency_pins if pin.scope != "skeleton"]


def _skeleton_revision(project: Project, chapter_id: str) -> int | None:
    envelope = storage.load_artifact(project, f"skeleton_{chapter_id}")
    if envelope is None or envelope.accepted_revision is None:
        return None
    return envelope.accepted_revision.revision


def _collect_stream(
    client: LLMClient,
    request: LLMRequest,
    on_chunk: Callable[[StreamChunk], None] | None,
) -> tuple[str, str]:
    """Cộng dồn prose từ stream; trả `(text, terminal_status)`.

    Lỗi transport sau khi đã có delta → giữ phần đã nhận với status `partial`
    (stream đứt không bao giờ thành `completed`). Lỗi trước delta nào → raise
    `LLMUnavailableError` vì chưa có gì để lưu.
    """
    parts: list[str] = []
    status = "partial"
    try:
        for chunk in stream_text(client, request):
            if chunk.status == "delta":
                parts.append(chunk.text)
                if on_chunk is not None:
                    on_chunk(chunk)
                continue
            status = chunk.status
            if on_chunk is not None:
                on_chunk(chunk)
            break
    except LLMError as exc:
        if not parts:
            raise LLMUnavailableError(
                f"LLM lỗi trước khi có prose: {exc}",
                code=exc.code,
                details=exc.details,
            ) from exc
        status = "partial"
    return "".join(parts), status


def _looks_like_error_notice(text: str) -> bool:
    """True nếu output ngắn và chỉ là thông báo từ chối/không đủ dữ liệu.

    Đây là heuristic có chủ ý (không phải phân loại ngữ nghĩa): output ngắn, một
    đoạn, chứa dấu hiệu từ chối. Prose thật nhiều đoạn không bị ảnh hưởng.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) > ERROR_NOTICE_MAX_CHARS:
        return False
    if "\n\n" in stripped:
        return False
    lowered = stripped.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def _has_prose(text: str) -> bool:
    return bool(text.strip()) and not _looks_like_error_notice(text)


def _next_revision(chapter: ChapterMetadata) -> int:
    return max((draft.revision for draft in chapter.drafts), default=0) + 1


def _invalidate_review(chapter: ChapterMetadata, new_revision: int) -> None:
    """Prose revision mới làm Human Review cũ mất hiệu lực (D002)."""
    review = chapter.human_review
    if review is not None and review.prose_revision != new_revision:
        chapter.human_review = review.model_copy(
            update={"valid_for_current_revision": False}
        )


def _apply_new_draft(
    chapter: ChapterMetadata,
    draft: ProseRevision,
    *,
    is_complete: bool,
) -> ChapterMetadata:
    """Bản chapter đã thêm prose revision mới và cập nhật status/review."""
    updated = chapter.model_copy(deep=True)
    updated.drafts = [*chapter.drafts, draft]
    updated.current_draft_revision = draft.revision
    _invalidate_review(updated, draft.revision)
    updated.status = ChapterStatus.review_required if is_complete else ChapterStatus.draft
    return updated


def _join_continuation(previous: str, continuation: str) -> str:
    head = previous.rstrip()
    tail = continuation.strip()
    if not head:
        return tail
    if not tail:
        return head
    return f"{head}\n\n{tail}"


def _draft_tail(text: str, limit: int = CONTINUE_TAIL_CHARS) -> str:
    stripped = text.rstrip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]


# ---------------------------------------------------------------------------
# Generate / regenerate / continue
# ---------------------------------------------------------------------------


def _run_generation(
    project: Project,
    *,
    client: LLMClient,
    chapter_id: str,
    operation_type: str,
    user_instruction: str = "",
    stream: bool = False,
    on_chunk: Callable[[StreamChunk], None] | None = None,
    operation_id: str | None = None,
    now: str | None = None,
    continuation_of: str | None = None,
) -> ActionResult:
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = _require_editable_chapter(project, chapter_id)

    existing = _load_operation_record(project, chapter_id, op_id)
    if existing is not None:
        return _replay_result(existing, action=operation_type)

    # Guard backend chạy trước khi client được gọi (D011).
    bundle = _writer_bundle(project, chapter_id, user_instruction)
    rendered = _render(project, DRAFT_PROMPT_ID, bundle)
    request = LLMRequest(
        messages=rendered.messages,
        json_output=False,
        prompt_id=rendered.prompt_id,
        prompt_version=rendered.prompt_version,
        prompt_hash=rendered.template_hash,
    )

    if stream:
        text, stream_status = _collect_stream(client, request, on_chunk)
    else:
        try:
            response = client.complete(request)
        except LLMError as exc:
            raise LLMUnavailableError(
                f"LLM lỗi khi sinh draft: {exc}", code=exc.code, details=exc.details
            ) from exc
        text = response.text if isinstance(response.text, str) else str(response.text)
        stream_status = "completed"

    raw_ref = storage.save_raw_output(
        project,
        operation_id=op_id,
        text=text,
        label=f"writer_{chapter_id}",
        day=_day_of(stamp),
    )

    warnings = _auto_accept_warnings(project, "prose")

    if not _has_prose(text):
        record = {
            "operation_id": op_id,
            "operation_type": operation_type,
            "chapter_id": chapter_id,
            "revision": None,
            "is_complete": False,
            "status": chapter.status.value,
            "stream_status": stream_status,
            "reason": "no_prose_output",
            "raw_output_ref": raw_ref,
            "prompt_id": rendered.prompt_id,
            "prompt_version": rendered.prompt_version,
            "created_at": stamp,
        }
        _save_operation_record(project, chapter_id, record, operation_id=op_id)
        return ActionResult(
            operation_id=op_id,
            chapter_id=chapter_id,
            message=(
                "Writer không trả prose (output rỗng hoặc chỉ là thông báo lỗi); "
                "không mở Human Review/Finalize và không tạo prose revision."
            ),
            warnings=warnings
            + [
                "Run được ghi `is_complete=False`; raw output đã lưu để người dùng xử lý."
            ],
            data=dict(record),
        )

    if continuation_of is not None and not _has_prose(text):
        # Không thể xảy ra (đã kiểm tra ở trên) nhưng giữ nhánh tường minh.
        raise GuardError("Không có prose nối tiếp để ghi.", code="no_prose_output")

    is_complete = stream_status == "completed"
    if continuation_of is None:
        prose_text = text
    else:
        prose_text = _join_continuation(continuation_of, text)

    revision = _next_revision(chapter)
    markdown_ref = storage.write_markdown(
        project,
        _draft_relpath(chapter_id, revision),
        prose_text,
        operation_id=op_id,
    )
    draft = ProseRevision(
        revision=revision,
        markdown_ref=markdown_ref,
        source_type=SourceType.llm,
        is_complete=is_complete,
        created_at=stamp,
        dependency_pins=_dependency_pins(project, bundle, chapter_id),
    )
    updated = _apply_new_draft(chapter, draft, is_complete=is_complete)
    storage.save_chapter(project, updated, operation_id=op_id)

    record = {
        "operation_id": op_id,
        "operation_type": operation_type,
        "chapter_id": chapter_id,
        "revision": revision,
        "is_complete": is_complete,
        "status": updated.status.value,
        "stream_status": stream_status,
        "markdown_ref": markdown_ref,
        "raw_output_ref": raw_ref,
        "prompt_id": rendered.prompt_id,
        "prompt_version": rendered.prompt_version,
        "created_at": stamp,
    }
    _save_operation_record(project, chapter_id, record, operation_id=op_id)

    if is_complete:
        message = (
            f"Writer draft r{revision} complete cho `{chapter_id}`; chapter chuyển "
            "`review_required`."
        )
    else:
        message = (
            f"Writer draft r{revision} là partial (`{stream_status}`) cho `{chapter_id}`; "
            "chapter ở `draft` và chưa mở review/finalize."
        )
        warnings = warnings + [
            "Stream đứt/chưa hoàn tất: draft được giữ partial, có thể Continue sau."
        ]

    return ActionResult(
        operation_id=op_id,
        chapter_id=chapter_id,
        message=message,
        warnings=warnings,
        data=dict(record),
    )


def generate_draft(
    project: Project,
    *,
    client: LLMClient,
    chapter_id: str,
    user_instruction: str = "",
    stream: bool = False,
    on_chunk: Callable[[StreamChunk], None] | None = None,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Sinh một prose draft mới từ `writer.v1` (action Generate Writer Draft).

    Guard (Skeleton accepted/fresh, previous chapter `final_reconciled`, context
    actual, không leak secret) chạy **trước** khi gọi LLM. `stream=True` phát
    từng delta qua `on_chunk`; stream đứt giữ partial draft.
    """
    return _run_generation(
        project,
        client=client,
        chapter_id=chapter_id,
        operation_type=OPERATION_GENERATE,
        user_instruction=user_instruction,
        stream=stream,
        on_chunk=on_chunk,
        operation_id=operation_id,
        now=now,
    )


def regenerate_draft(
    project: Project,
    *,
    client: LLMClient,
    chapter_id: str,
    **kwargs: Any,
) -> ActionResult:
    """Regenerate: prose revision mới, accepted/plan/final giữ nguyên.

    `kwargs` nhận `user_instruction`, `stream`, `on_chunk`, `operation_id`,
    `now`. Lỗi LLM hoặc guard fail **không** đổi draft cũ, final hay accepted.
    """
    allowed = {"user_instruction", "stream", "on_chunk", "operation_id", "now"}
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise ServiceError(
            f"`regenerate_draft` không nhận tham số {unknown}; chỉ có {sorted(allowed)}.",
            code="invalid_argument",
        )
    return _run_generation(
        project,
        client=client,
        chapter_id=chapter_id,
        operation_type=OPERATION_REGENERATE,
        user_instruction=str(kwargs.get("user_instruction") or ""),
        stream=bool(kwargs.get("stream")),
        on_chunk=kwargs.get("on_chunk"),
        operation_id=kwargs.get("operation_id"),
        now=kwargs.get("now"),
    )


def continue_draft(
    project: Project,
    *,
    client: LLMClient,
    chapter_id: str,
    stream: bool = False,
    on_chunk: Callable[[StreamChunk], None] | None = None,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Nối prose vào tail của draft hiện tại, cùng Skeleton/context.

    Không tạo plan mới và không đổi upstream. Lỗi giữ nguyên partial cũ.
    """
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = _require_editable_chapter(project, chapter_id)

    existing = _load_operation_record(project, chapter_id, op_id)
    if existing is not None:
        return _replay_result(existing, action=OPERATION_CONTINUE)

    current = chapter.current_draft
    if current is None:
        raise GuardError(
            f"`{chapter_id}` chưa có prose revision nào để Continue.",
            code="draft_missing",
        )
    previous_text = draft_markdown(project, chapter_id, current.revision)
    instruction = _CONTINUE_INSTRUCTION.format(tail=_draft_tail(previous_text))

    warnings: list[str] = []
    if current.is_complete:
        warnings.append(
            f"Draft hiện tại r{current.revision} đã complete; Continue sẽ tạo revision mới."
        )

    result = _run_generation(
        project,
        client=client,
        chapter_id=chapter_id,
        operation_type=OPERATION_CONTINUE,
        user_instruction=instruction,
        stream=stream,
        on_chunk=on_chunk,
        operation_id=op_id,
        now=stamp,
        continuation_of=previous_text,
    )
    if warnings:
        result.warnings = warnings + list(result.warnings)
    return result


# ---------------------------------------------------------------------------
# Save / discard
# ---------------------------------------------------------------------------


def save_draft(
    project: Project,
    *,
    chapter_id: str,
    text: str,
    source_type: SourceType = SourceType.user,
    is_complete: bool | None = None,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Lưu bản prose do người dùng sửa thành prose revision mới.

    `is_complete=None` thì suy theo nội dung (rỗng/chỉ thông báo lỗi ⇒ partial).
    Revision mới làm Human Review cũ mất hiệu lực.
    """
    if not isinstance(text, str):
        raise ServiceError("`text` của save_draft phải là chuỗi.", code="invalid_argument")
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = _require_editable_chapter(project, chapter_id)

    existing = _load_operation_record(project, chapter_id, op_id)
    if existing is not None:
        return _replay_result(existing, action=OPERATION_SAVE)

    complete = _has_prose(text) if is_complete is None else bool(is_complete)
    if complete and not text.strip():
        raise GuardError(
            "Không thể đánh dấu `is_complete=True` cho bản prose rỗng.",
            code="invalid_draft",
        )

    revision = _next_revision(chapter)
    markdown_ref = storage.write_markdown(
        project,
        _draft_relpath(chapter_id, revision),
        text,
        operation_id=op_id,
    )
    draft = ProseRevision(
        revision=revision,
        markdown_ref=markdown_ref,
        source_type=source_type,
        is_complete=complete,
        created_at=stamp,
        dependency_pins=list(chapter.current_draft.dependency_pins)
        if chapter.current_draft is not None
        else [],
    )
    updated = _apply_new_draft(chapter, draft, is_complete=complete)
    storage.save_chapter(project, updated, operation_id=op_id)

    record = {
        "operation_id": op_id,
        "operation_type": OPERATION_SAVE,
        "chapter_id": chapter_id,
        "revision": revision,
        "is_complete": complete,
        "status": updated.status.value,
        "source_type": source_type.value,
        "markdown_ref": markdown_ref,
        "created_at": stamp,
    }
    _save_operation_record(project, chapter_id, record, operation_id=op_id)

    warnings = _auto_accept_warnings(project, "prose")
    if chapter.human_review is not None and chapter.human_review.valid_for_current_revision:
        warnings.append(
            "Human Review của revision trước không còn hiệu lực; cần review lại revision mới."
        )
    return ActionResult(
        operation_id=op_id,
        chapter_id=chapter_id,
        message=(
            f"Đã lưu prose revision r{revision} cho `{chapter_id}` "
            f"({'complete' if complete else 'partial'})."
        ),
        warnings=warnings,
        data=dict(record),
    )


def discard_draft(
    project: Project,
    *,
    chapter_id: str,
    revision: int | None = None,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Bỏ một prose revision **cũ** khỏi metadata (không xóa final/accepted, giữ file).

    `storage.save_chapter` (T09) chặn mọi cập nhật làm lùi
    `current_draft_revision` để không đè prose mới hơn. Vì vậy:

    - `revision=None` bỏ revision mới nhất **không phải** revision đang hiển thị;
    - không được bỏ revision đang là `current_draft_revision`,
      `final_candidate` hay nguồn của `final_revision`.

    Muốn thay bản đang hiển thị thì tạo revision mới bằng Save/Regenerate, rồi
    bỏ bản cũ bằng action này.
    """
    stamp = now or now_iso()
    op_id = operation_id or generate_operation_id()
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        raise GuardError(
            f"Chưa có chapter metadata cho `{chapter_id}`.", code="chapter_missing"
        )

    existing = _load_operation_record(project, chapter_id, op_id)
    if existing is not None:
        return _replay_result(existing, action=OPERATION_DISCARD)

    if not chapter.drafts:
        raise GuardError(f"`{chapter_id}` chưa có prose revision nào.", code="draft_missing")
    if revision is None:
        target = max(
            (
                item.revision
                for item in chapter.drafts
                if item.revision != chapter.current_draft_revision
            ),
            default=None,
        )
        if target is None:
            raise GuardError(
                f"`{chapter_id}` chỉ có prose revision đang hiển thị "
                f"(r{chapter.current_draft_revision}); storage không cho lùi current draft. "
                "Hãy tạo revision mới bằng Save Draft/Regenerate rồi bỏ bản cũ.",
                code="draft_is_current",
            )
    else:
        target = int(revision)
        if chapter.draft_revision(target) is None:
            raise GuardError(
                f"`{chapter_id}` không có prose revision r{target}.", code="draft_not_found"
            )
        if target == chapter.current_draft_revision:
            raise GuardError(
                f"r{target} đang là prose revision hiển thị của `{chapter_id}`; "
                "storage không cho lùi `current_draft_revision`. Hãy tạo revision mới "
                "bằng Save Draft/Regenerate rồi bỏ bản cũ.",
                code="draft_is_current",
            )
    if chapter.final_revision is not None and chapter.final_revision.source_prose_revision == target:
        raise GuardError(
            f"r{target} là nguồn của final revision; không được discard.",
            code="draft_is_final",
        )
    if chapter.final_candidate is not None and chapter.final_candidate.prose_revision == target:
        raise GuardError(
            f"r{target} đang là final candidate; hủy finalize trước khi discard.",
            code="draft_is_final_candidate",
        )
    if chapter.status is ChapterStatus.final_reconciled:
        raise GuardError(
            f"`{chapter_id}` đã `final_reconciled`; sửa lịch sử prose phải đi qua action retcon.",
            code="chapter_already_final",
        )

    updated = chapter.model_copy(deep=True)
    updated.drafts = [item for item in chapter.drafts if item.revision != target]
    if updated.human_review is not None and updated.human_review.prose_revision == target:
        updated.human_review = updated.human_review.model_copy(
            update={"valid_for_current_revision": False}
        )
    current = updated.current_draft
    if updated.drafts:
        updated.status = (
            ChapterStatus.review_required
            if current is not None and current.is_complete
            else ChapterStatus.draft
        )
    else:
        updated.status = (
            ChapterStatus.skeleton_ready
            if updated.skeleton_pin is not None
            else ChapterStatus.planned
        )

    storage.save_chapter(project, updated, operation_id=op_id)
    record = {
        "operation_id": op_id,
        "operation_type": OPERATION_DISCARD,
        "chapter_id": chapter_id,
        "revision": target,
        "is_complete": False,
        "status": updated.status.value,
        "created_at": stamp,
    }
    _save_operation_record(project, chapter_id, record, operation_id=op_id)
    return ActionResult(
        operation_id=op_id,
        chapter_id=chapter_id,
        message=(
            f"Đã bỏ prose revision r{target} khỏi metadata `{chapter_id}`; "
            "file markdown được giữ để audit."
        ),
        data=dict(record),
    )


# ---------------------------------------------------------------------------
# Đọc prose
# ---------------------------------------------------------------------------


def draft_markdown(project: Project, chapter_id: str, revision: int) -> str:
    """Nội dung markdown của một prose revision; raise nếu không resolve được."""
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        raise GuardError(f"Chưa có chapter metadata cho `{chapter_id}`.", code="chapter_missing")
    draft = chapter.draft_revision(int(revision))
    if draft is None:
        raise GuardError(
            f"`{chapter_id}` không có prose revision r{revision}.", code="draft_not_found"
        )
    candidates: Iterable[Path] = (
        storage.ensure_within_project(project.root, project.root / draft.markdown_ref),
        project.paths.chapter_dir(chapter_id) / draft.markdown_ref,
        project.paths.drafts_dir(chapter_id) / draft.markdown_ref,
    )
    for path in candidates:
        if path.is_file():
            return storage.read_text(path)
    raise GuardError(
        f"Không thấy file markdown `{draft.markdown_ref}` của `{chapter_id}` r{revision}.",
        code="draft_markdown_missing",
    )
