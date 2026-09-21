"""Co-create, Base Idea và helper runtime dùng chung cho service T13–T14.

Module này hiện thực workspace Co-create theo `docs/design/workflow.md` mục 4.1
và `docs/design/schemas.md` mục 1.5:

- `run_turn`: một lượt hội thoại co-create; kết quả chỉ là **working state**
  trong `co_create.json`, không phải canon;
- `set_working_state`: user sửa tay working state. Đây cũng là action reopen
  tường minh: sau khi Base Idea finalized, chỉ action này mở lại working state
  cho `run_turn` (guard của `run_turn` chặn khi status còn `finalized`);
- `finalize_base_idea`: biến working state (hoặc markdown user cấp) thành Base
  Idea accepted: ghi `idea/base_idea.md` + `idea/base_idea.meta.json` và đặt
  `co_create.status = finalized`.

Ngoài ba action trên, module giữ các helper runtime mà bốn service T13/T14 dùng
chung (`architect`, `long_planner`, `short_planner` import lại): render prompt
v1, gọi LLM rồi **lưu raw output trước khi parse**, parse structured output, lưu
`StructuredOutputError` khi parse/validate lỗi, validate cross-field, dựng
reference index từ accepted state và kiểm tra dependency pin. Phạm vi task
T13/T14 không cho tạo module dùng chung mới nên các helper nằm ở đây; chúng
không tự chạy workflow thay service nào.

Luật đã giữ trong module:

- guard chạy **trước** khi gọi LLM;
- raw output luôn lưu trước khi parse; lỗi parse/validate lưu error record và
  **không** đổi accepted revision;
- Base Idea là canon; co-create turn không tự finalize;
- mọi ghi file đi qua `novel_ai.core.storage` (atomic); không `Path.write_text`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from novel_ai.core import context as context_core
from novel_ai.core import lifecycle, storage, validation
from novel_ai.core.llm import (
    LLMError,
    LLMRequest,
    StructuredOutputParseError,
    parse_structured_text,
)
from novel_ai.core.models import (
    ArtifactEnvelope,
    ArtifactStatus,
    BaseIdeaMetadata,
    CoCreateDocument,
    CoCreateMessage,
    CoCreateStatus,
    DependencyPin,
    IdeaState,
    IdeaStateResponse,
    StructuredOutputError,
    generate_error_id,
    generate_operation_id,
    now_iso,
)
from novel_ai.core.project import Project
from novel_ai.core.prompts import PromptError, PromptRegistry, RenderedPrompt, render_prompt
from novel_ai.services import (
    ActionResult,
    GuardError,
    LLMUnavailableError,
    ServiceError,
    StaleDependencyError,
    ValidationFailure,
)

__all__ = [
    "BASE_IDEA_MARKDOWN_RELPATH",
    "CO_CREATE_PROMPT_ID",
    "finalize_base_idea",
    "render_base_idea_markdown",
    "run_turn",
    "set_working_state",
]

#: Prompt co-create đã đăng ký trong manifest v1.
CO_CREATE_PROMPT_ID = "co_create.v1"
#: Đường dẫn markdown Base Idea trong project.
BASE_IDEA_MARKDOWN_RELPATH = "idea/base_idea.md"

#: Artifact mà dependency pin được dựng lại từ accepted revision hiện tại.
_PINNED_ARTIFACTS: tuple[str, ...] = (
    "base_idea",
    "premise",
    "characters",
    "world_rules",
    "foreshadow",
    "long_plan",
    "short_plan",
)

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Helper thời gian và đường dẫn
# ---------------------------------------------------------------------------


def stamp(now: str | None = None) -> str:
    """Timestamp ISO 8601 dùng cho metadata app; `now` cho phép test cố định."""
    return now or now_iso()


def raw_day(now: str | None = None) -> str:
    """Ngày `YYYY-MM-DD` cho thư mục `raw/`, suy từ `now` khi có."""
    if now and _DAY_RE.match(str(now)[:10]):
        return str(now)[:10]
    return datetime.now().astimezone().date().isoformat()


def safe_label(value: Any) -> str:
    """Chuẩn hóa nhãn thành phần tên file an toàn (giống quy ước raw output)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("_")
    return cleaned or "output"


def error_relpath(
    project: Project, *, operation_id: str, label: str, now: str | None = None
) -> str:
    """Đường dẫn tương đối của error record trong `raw/<ngày>/`."""
    day = raw_day(now)
    return f"raw/{day}/{safe_label(operation_id)}_{safe_label(label)}.error.json"


# ---------------------------------------------------------------------------
# Guard dùng chung
# ---------------------------------------------------------------------------


def require_base_idea(project: Project) -> BaseIdeaMetadata:
    """Base Idea phải đang `accepted`; nếu không raise `GuardError`."""
    document = storage.load_co_create(project)
    meta = document.base_idea if document is not None else None
    if meta is None or meta.status is not ArtifactStatus.accepted:
        raise GuardError(
            "Base Idea chưa accepted; action này cần user Finalize Base Idea trước.",
            code="base_idea_missing",
        )
    return meta


def require_accepted_artifact(project: Project, artifact_id: str) -> ArtifactEnvelope[Any]:
    """Artifact phải có accepted revision và không stale.

    Artifact stale không được dùng cho action phụ thuộc (`workflow.md` mục 5.2);
    user phải reaccept hoặc regenerate trước.
    """
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or envelope.accepted_revision is None:
        raise GuardError(
            f"Chưa có accepted revision cho `{artifact_id}`.",
            code="missing_dependency",
            details={"artifact_id": artifact_id},
        )
    if envelope.status is ArtifactStatus.stale:
        reasons = "; ".join(item.reason for item in envelope.stale_reasons)
        raise StaleDependencyError(
            f"`{artifact_id}` đang stale nên không dùng được cho action phụ thuộc: {reasons}",
            code="stale_dependency",
            details={"artifact_id": artifact_id},
        )
    if envelope.status is not ArtifactStatus.accepted:
        raise GuardError(
            f"`{artifact_id}` có status `{envelope.status.value}`, chưa accepted.",
            code="missing_dependency",
            details={"artifact_id": artifact_id, "status": envelope.status.value},
        )
    return envelope


def foundation_reference_index(project: Project) -> validation.ReferenceIndex:
    """Reference index dựng từ accepted artifact hiện tại (candidate không vào)."""
    def payload_of(artifact_id: str) -> Any:
        envelope = storage.load_artifact(project, artifact_id)
        if envelope is None or envelope.accepted_revision is None:
            return None
        if envelope.status is not ArtifactStatus.accepted:
            return None
        return envelope.accepted_revision.payload

    long_plan = payload_of("long_plan")
    short_plan = payload_of("short_plan")
    chapters = [
        chapter
        for chapter in (storage.load_chapter(project, item) for item in storage.list_chapter_ids(project))
        if chapter is not None
    ]
    return validation.build_reference_index(
        characters=getattr(payload_of("characters"), "characters", []) or [],
        world_rules=getattr(payload_of("world_rules"), "world_rules", []) or [],
        foreshadows=getattr(payload_of("foreshadow"), "foreshadows", []) or [],
        long_plan_arcs=[
            arc for volume in (long_plan.volumes if long_plan else []) for arc in volume.arcs
        ],
        long_plan_volumes=long_plan.volumes if long_plan else [],
        short_plan_chapters=short_plan.chapters if short_plan else [],
        chapters=chapters,
        relationships=storage.load_relationships(project),
    )


def current_dependency_pins(project: Project) -> list[DependencyPin]:
    """Pin mới nhất cho các artifact nền tảng (dùng cho action do user thực hiện)."""
    revisions = storage.current_artifact_revisions(project)
    pins: list[DependencyPin] = []
    for artifact_id in _PINNED_ARTIFACTS:
        revision = revisions.get(artifact_id)
        if revision:
            pins.append(
                DependencyPin(artifact_id=artifact_id, revision=revision, scope=artifact_id)
            )
    return pins


def check_pin_freshness(project: Project, pins: Sequence[DependencyPin], *, artifact_id: str) -> None:
    """Từ chối merge/accept khi pin lệch accepted revision hiện tại."""
    mismatches = storage.pin_mismatch_messages(
        list(pins), storage.current_artifact_revisions(project)
    )
    if mismatches:
        raise StaleDependencyError(
            f"`{artifact_id}` được tạo từ dependency revision cũ "
            f"({'; '.join(mismatches)}); từ chối merge, cần regenerate hoặc review lại.",
            code="stale_dependency",
            details={"artifact_id": artifact_id, "mismatches": mismatches},
        )


# ---------------------------------------------------------------------------
# Idempotency theo operation manifest
# ---------------------------------------------------------------------------


def operation_committed(
    project: Project, operation_id: str | None, *, targets: Sequence[str] = ()
) -> bool:
    """True nếu `operation_id` đã commit (**chỉ đọc**), đủ để retry thành no-op.

    Đọc manifest đã đóng của storage: `.ops/done/<op>.json` (transaction nhiều
    file) và `history/<op>/manifest.json` (ghi một file). Khi có `targets`, chỉ
    tính là đã commit nếu write set/applied paths chứa một trong các target đó.
    """
    if not operation_id:
        return False
    wanted = {str(target) for target in targets}
    candidates = (
        project.paths.ops_done_dir / f"{operation_id}.json",
        project.paths.history_dir / operation_id / "manifest.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = storage.read_json(path)
        except Exception:  # pragma: no cover - manifest hỏng thì coi như chưa commit
            continue
        if not isinstance(data, Mapping) or data.get("status") != "committed":
            continue
        if not wanted:
            return True
        written = {
            str(entry.get("target"))
            for entry in data.get("write_set") or []
            if isinstance(entry, Mapping)
        }
        applied = {str(item) for item in data.get("applied_paths") or []}
        if (written | applied) & wanted:
            return True
    return False


def artifact_write_committed(
    project: Project, operation_id: str | None, artifact_id: str, artifact_type: str
) -> bool:
    """Retry cùng `operation_id` cho đúng artifact này đã commit hay chưa."""
    if not operation_id:
        return False
    try:
        relpath = storage.artifact_relpath(artifact_id, artifact_type)
    except storage.StorageError:  # pragma: no cover - artifact_type lạ
        return False
    return operation_committed(project, operation_id, targets=[relpath])


# ---------------------------------------------------------------------------
# Runtime prompt + LLM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructuredCall:
    """Kết quả một lần gọi LLM có JSON output: text, raw ref và metadata prompt."""

    text: str
    raw_ref: str
    prompt_id: str
    prompt_version: str
    prompt_hash: str


def load_prompt_registry() -> PromptRegistry:
    """Load manifest prompt v1; lỗi load là `ServiceError` có `code` ổn định."""
    try:
        return PromptRegistry.load()
    except PromptError as exc:
        raise ServiceError(
            f"Không load được prompt v1: {exc}",
            code=exc.code,
            details=dict(exc.details),
        ) from exc


def render_service_prompt(
    project: Project, prompt_id: str, inputs: Mapping[str, Any]
) -> RenderedPrompt:
    """Render prompt v1 từ payload context; render fail thì **không** gọi LLM."""
    registry = load_prompt_registry()
    try:
        return render_prompt(
            registry,
            prompt_id,
            inputs=inputs,
            repo_root=registry.repo_root,
            config=project.config,
        )
    except PromptError as exc:
        raise ServiceError(
            f"Không render được prompt `{prompt_id}`: {exc}",
            code=exc.code,
            details={"prompt_id": prompt_id, "variable": exc.variable, **dict(exc.details)},
        ) from exc


def save_llm_error(
    project: Project,
    *,
    operation_id: str,
    error: LLMError,
    now: str | None = None,
    label: str = "llm",
) -> str:
    """Lưu error record của lần gọi LLM lỗi (timeout/mạng/provider)."""
    record = {
        "error_id": generate_error_id(),
        "kind": "llm_error",
        "code": getattr(error, "code", "llm_error"),
        "retryable": bool(getattr(error, "retryable", False)),
        "message": str(error),
        "operation_id": operation_id,
        "created_at": stamp(now),
        "details": dict(getattr(error, "details", {}) or {}),
    }
    relpath = error_relpath(project, operation_id=operation_id, label=f"{label}.llm", now=now)
    storage.write_json_atomic(project.root / relpath, record, operation_id=f"{operation_id}.error")
    return relpath


def save_structured_error(
    project: Project,
    *,
    operation_id: str,
    error: StructuredOutputError,
    now: str | None = None,
    label: str = "structured",
) -> str:
    """Lưu `StructuredOutputError` record cạnh raw output để UI/user xử lý."""
    relpath = error_relpath(project, operation_id=operation_id, label=label, now=now)
    storage.write_json_atomic(project.root / relpath, error, operation_id=f"{operation_id}.error")
    return relpath


def complete_json(
    project: Project,
    *,
    client: Any,
    prompt_id: str,
    bundle: context_core.ContextBundle,
    operation_id: str,
    now: str | None = None,
    label: str = "output",
) -> StructuredCall:
    """Gọi LLM một lần và lưu raw output trước khi parse.

    Lỗi provider/timeout được đổi thành `LLMUnavailableError`; raw (nếu provider
    kịp trả) và error record được lưu lại, accepted state không đổi.
    """
    rendered = render_service_prompt(project, prompt_id, bundle.payload)
    request = LLMRequest(
        messages=rendered.messages,
        json_output=True,
        prompt_id=rendered.prompt_id,
        prompt_version=rendered.prompt_version,
        prompt_hash=rendered.template_hash,
    )
    try:
        response = client.complete(request)
    except LLMError as exc:
        partial = getattr(exc, "raw_text", "") or ""
        partial_ref = None
        if partial:
            partial_ref = storage.save_raw_output(
                project,
                operation_id=operation_id,
                text=partial,
                label=f"{label}.partial",
                day=raw_day(now),
            )
        error_ref = save_llm_error(
            project, operation_id=operation_id, error=exc, now=now, label=label
        )
        raise LLMUnavailableError(
            f"LLM không khả dụng khi gọi `{prompt_id}` (code `{exc.code}`): {exc}",
            code=exc.code,
            details={
                "prompt_id": prompt_id,
                "raw_output_ref": partial_ref,
                "error_ref": error_ref,
            },
        ) from exc

    text = response.text if isinstance(response.text, str) else str(response.text)
    raw_ref = storage.save_raw_output(
        project,
        operation_id=operation_id,
        text=text,
        label=label,
        day=raw_day(now),
    )
    return StructuredCall(
        text=text,
        raw_ref=raw_ref,
        prompt_id=rendered.prompt_id,
        prompt_version=rendered.prompt_version,
        prompt_hash=rendered.template_hash,
    )


def parse_structured_or_fail(
    project: Project,
    *,
    call: StructuredCall,
    model_cls: type[BaseModel],
    operation_id: str,
    now: str | None = None,
    label: str = "output",
    artifact_id: str | None = None,
) -> Any:
    """Parse JSON thành `model_cls`; lỗi parse/schema raise `ValidationFailure`."""
    try:
        return parse_structured_text(call.text, model_cls)
    except StructuredOutputParseError as exc:
        record = StructuredOutputError(
            error_id=generate_error_id(),
            artifact_id=artifact_id,
            raw_output_ref=call.raw_ref,
            errors=list(exc.errors),
            created_at=stamp(now),
            retryable=True,
        )
        error_ref = save_structured_error(
            project,
            operation_id=operation_id,
            error=record,
            now=now,
            label=label or call.prompt_id,
        )
        raise ValidationFailure(
            f"Output LLM cho `{label or call.prompt_id}` không parse/không khớp schema: {exc}. "
            "Raw output đã được lưu; accepted state giữ nguyên.",
            result=validation.result_from_issues(record.errors),
            details={
                "artifact_id": artifact_id,
                "error_ref": error_ref,
                "raw_output_ref": call.raw_ref,
            },
        ) from exc


def validate_or_fail(
    project: Project,
    *,
    artifact_type: str,
    payload: Any,
    context: validation.ValidationContext,
    operation_id: str,
    now: str | None = None,
    label: str = "output",
    raw_output_ref: str | None = None,
    artifact_id: str | None = None,
    extra_issues: Sequence[validation.ValidationIssue] = (),
) -> validation.ValidationResult:
    """Validate cross-field; lỗi raise `ValidationFailure` và lưu error record.

    Candidate không được lưu khi invalid: accepted revision và candidate cũ giữ
    nguyên, raw output + error record đủ để user sửa tay qua `edit_candidate`.
    """
    result = validation.validate_artifact_payload(artifact_type, payload, context=context)
    issues = [*result.errors, *extra_issues]
    final = validation.result_from_issues(issues)
    if final.is_valid:
        return final
    record = StructuredOutputError(
        error_id=generate_error_id(),
        artifact_id=artifact_id or artifact_type,
        raw_output_ref=raw_output_ref,
        errors=list(final.errors),
        created_at=stamp(now),
        retryable=True,
    )
    error_ref = save_structured_error(
        project, operation_id=operation_id, error=record, now=now, label=label
    )
    raise ValidationFailure(
        f"`{artifact_type}` không qua validation: {validation.summarize_errors(final)}. "
        "Accepted state giữ nguyên; raw output và error record đã được lưu.",
        result=final,
        details={
            "artifact_id": artifact_id or artifact_type,
            "error_ref": error_ref,
            "raw_output_ref": raw_output_ref,
        },
    )


def payload_source(call: StructuredCall, *, operation_id: str, id_map: Mapping[str, str] | None = None) -> Any:
    """`PayloadSource` từ kết quả gọi LLM (metadata debug, không chứa secret)."""
    from novel_ai.core.models import PayloadSource, SourceType

    return PayloadSource(
        source_type=SourceType.llm,
        prompt_id=call.prompt_id,
        prompt_version=call.prompt_version,
        prompt_hash=call.prompt_hash,
        operation_id=operation_id,
        raw_output_ref=call.raw_ref,
        id_map=dict(id_map or {}),
    )


# ---------------------------------------------------------------------------
# Co-create actions
# ---------------------------------------------------------------------------


def _document(project: Project) -> CoCreateDocument:
    document = storage.load_co_create(project)
    return document if document is not None else CoCreateDocument()


def _require_working(document: CoCreateDocument) -> None:
    if document.status is CoCreateStatus.finalized:
        raise GuardError(
            "Base Idea đã finalized nên co-create turn bị chặn. "
            "User phải gọi action reopen (`set_working_state`) trước khi trao đổi tiếp.",
            code="co_create_finalized",
            details={"status": document.status.value},
        )


def render_base_idea_markdown(idea_state: IdeaState) -> str:
    """Markdown Base Idea tất định từ `idea_state` (không gọi LLM)."""
    lines: list[str] = ["# Base Idea", ""]
    lines.append(f"- Thể loại: {idea_state.genre.strip()}")
    lines.append(f"- Hạt nhân truyện: {idea_state.core_concept.strip()}")
    if idea_state.tone.strip():
        lines.append(f"- Tone: {idea_state.tone.strip()}")
    if idea_state.protagonist.strip():
        lines.append(f"- Nhân vật trung tâm: {idea_state.protagonist.strip()}")
    if idea_state.setting.strip():
        lines.append(f"- Bối cảnh: {idea_state.setting.strip()}")
    if idea_state.conflict.strip():
        lines.append(f"- Xung đột chính: {idea_state.conflict.strip()}")
    lines.append("")
    lines.append("## Ràng buộc đã chốt")
    lines.append("")
    if idea_state.constraints:
        lines.extend(f"- {item}" for item in idea_state.constraints)
    else:
        lines.append("- (chưa có)")
    lines.append("")
    lines.append("## Điều còn mở")
    lines.append("")
    if idea_state.open_questions:
        lines.extend(f"- {item}" for item in idea_state.open_questions)
    else:
        lines.append("- (chưa có)")
    return "\n".join(lines) + "\n"


def run_turn(
    project: Project,
    *,
    client: Any,
    user_message: str,
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Chạy một lượt co-create và cập nhật working state.

    Guard: co-create còn `working` (hoặc user đã reopen bằng
    `set_working_state`). Kết quả **không** phải canon: `idea_state` và messages
    chỉ nằm trong `co_create.json` cho tới khi user Finalize Base Idea.

    Lỗi LLM: raw/error được lưu, working state không đổi, raise
    `LLMUnavailableError`. Output sai schema: raise `ValidationFailure`.
    """
    text_message = str(user_message or "").strip()
    if not text_message:
        raise GuardError("Co-create turn cần `user_message` không rỗng.", code="empty_message")
    op_id = operation_id or generate_operation_id()
    document = _document(project)
    _require_working(document)

    try:
        bundle = context_core.build_co_create_context(project, user_message=text_message)
    except context_core.ContextError as exc:
        raise GuardError(str(exc), code=exc.code, details=dict(exc.details)) from exc

    call = complete_json(
        project,
        client=client,
        prompt_id=CO_CREATE_PROMPT_ID,
        bundle=bundle,
        operation_id=op_id,
        now=now,
        label="co_create",
    )
    parsed: IdeaStateResponse = parse_structured_or_fail(
        project,
        call=call,
        model_cls=IdeaStateResponse,
        operation_id=op_id,
        now=now,
        label="co_create",
        artifact_id="co_create",
    )

    stamp_value = stamp(now)
    document.messages = [
        *document.messages,
        CoCreateMessage(role="user", content=text_message, created_at=stamp_value),
        CoCreateMessage(role="assistant", content=parsed.message, created_at=stamp_value),
    ]
    document.idea_state = parsed.idea_state
    document.status = CoCreateStatus.working
    storage.save_co_create(project, document, operation_id=f"{op_id}.co_create")

    return ActionResult(
        operation_id=op_id,
        artifact_id="co_create",
        message="Đã cập nhật working idea state (chưa phải canon).",
        data={
            "status": document.status.value,
            "assistant_message": parsed.message,
            "idea_state": parsed.idea_state.model_dump(mode="json"),
            "raw_output_ref": call.raw_ref,
            "turn_count": len(document.messages),
        },
    )


def set_working_state(
    project: Project,
    *,
    idea_state: IdeaState | Mapping[str, Any],
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """User sửa tay working state và (nếu cần) reopen co-create.

    Action này không sửa Base Idea accepted đang có: canon chỉ đổi khi user
    Finalize lại. Sau khi Base Idea finalized, đây là đường reopen tường minh.
    """
    op_id = operation_id or generate_operation_id()
    try:
        parsed = (
            idea_state
            if isinstance(idea_state, IdeaState)
            else IdeaState.model_validate(dict(idea_state))
        )
    except Exception as exc:  # pydantic ValidationError
        issues = validation.issues_from_pydantic_error(exc, base_path="/idea_state")
        raise ValidationFailure(
            f"Working idea state không hợp lệ: {validation.summarize_errors(validation.result_from_issues(issues))}",
            result=validation.result_from_issues(issues),
        ) from exc

    document = _document(project)
    document.idea_state = parsed
    document.status = CoCreateStatus.working
    storage.save_co_create(project, document, operation_id=f"{op_id}.co_create")
    return ActionResult(
        operation_id=op_id,
        artifact_id="co_create",
        message="Đã cập nhật working idea state do user nhập.",
        data={
            "status": document.status.value,
            "idea_state": parsed.model_dump(mode="json"),
            "base_idea_status": document.base_idea.status.value if document.base_idea else "missing",
        },
    )


def finalize_base_idea(
    project: Project,
    *,
    markdown: str | None = None,
    accepted_by: str = "user",
    operation_id: str | None = None,
    now: str | None = None,
) -> ActionResult:
    """Finalize Base Idea thành canon accepted.

    Yêu cầu tối thiểu: `markdown` do user cấp **hoặc** working `idea_state` có
    `genre` + `core_concept`. Thiếu dữ liệu → `GuardError`, working state giữ
    nguyên. Finalize lại với **cùng nội dung** là no-op idempotent (không bump
    revision). Finalize lại với nội dung khác bump revision và đánh dấu stale
    downstream liên quan (chỉ đánh dấu, không rewrite).
    """
    op_id = operation_id or generate_operation_id()
    document = _document(project)
    manual = (markdown or "").strip()

    if manual:
        text = manual + "\n"
    elif (
        document.idea_state is not None
        and document.idea_state.genre.strip()
        and document.idea_state.core_concept.strip()
    ):
        text = render_base_idea_markdown(document.idea_state)
    else:
        raise GuardError(
            "Finalize Base Idea cần `markdown` do user cấp hoặc working state có "
            "`genre` + `core_concept`; working state giữ nguyên.",
            code="base_idea_incomplete",
        )

    previous = (
        document.base_idea
        if document.base_idea is not None and document.base_idea.status is ArtifactStatus.accepted
        else None
    )
    existing_text = (
        storage.read_text(project.paths.base_idea_md)
        if project.paths.base_idea_md.is_file()
        else None
    )
    if previous is not None and existing_text == text:
        return ActionResult(
            operation_id=op_id,
            artifact_id="base_idea",
            message="Base Idea đã accepted với đúng nội dung này; không tạo revision mới.",
            data={
                "status": ArtifactStatus.accepted.value,
                "revision": previous.revision,
                "markdown_ref": previous.markdown_ref,
                "idempotent": True,
            },
            validation=validation.valid_result(),
        )

    revision = (previous.revision + 1) if previous is not None else 1
    markdown_relpath = storage.write_markdown(
        project,
        BASE_IDEA_MARKDOWN_RELPATH,
        text,
        operation_id=f"{op_id}.markdown",
    )
    meta = BaseIdeaMetadata(
        status=ArtifactStatus.accepted,
        revision=revision,
        markdown_ref=Path(markdown_relpath).name,
        dependency_pins=[],
        accepted_at=stamp(now),
        accepted_by=accepted_by,
    )
    storage.write_json_atomic(
        project.paths.base_idea_meta_json, meta, operation_id=f"{op_id}.meta"
    )

    document.status = CoCreateStatus.finalized
    document.base_idea = meta
    storage.save_co_create(project, document, operation_id=f"{op_id}.co_create")

    marked: list[str] = []
    if previous is not None:
        marked = lifecycle.mark_downstream_stale(
            project,
            source_artifact_id="base_idea",
            source_revision=revision,
            change=lifecycle.STALE_CHANGE_BASE_IDEA_REVISE,
            now=now,
        )

    return ActionResult(
        operation_id=op_id,
        artifact_id="base_idea",
        message="Base Idea đã accepted; Architect actions đã unlock.",
        data={
            "status": ArtifactStatus.accepted.value,
            "revision": revision,
            "markdown_ref": meta.markdown_ref,
            "stale_marked": marked,
        },
        validation=validation.valid_result(),
    )
