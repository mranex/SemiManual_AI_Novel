"""Explicit Python command boundary; services/core vẫn sở hữu luật nghiệp vụ."""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Mapping

from novel_ai.application.bootstrap import load_prompt_registry
from novel_ai.application.queries import ProjectQueries, QueryError
from novel_ai.config import AppConfig
from novel_ai.core import storage
from novel_ai.core.generation import GenerationEvent
from novel_ai.core.llm import ChatMessage, LLMClient, LLMError, LLMRequest
from novel_ai.core.models import generate_operation_id
from novel_ai.core.project import Project, ProjectError
from novel_ai.core.prompts import PromptRegistry
from novel_ai.services import (
    ActionResult,
    ServiceError,
    architect,
    co_create,
    long_planner,
    reconcile,
    reviewer,
    revision,
    short_planner,
    skeleton,
    writer,
)

Audience = Literal["author", "writer"]
EventSink = Callable[[GenerationEvent], None]


@dataclass(frozen=True)
class Command:
    """Một intent user; ``params`` chỉ chứa input của action, không chứa dependency."""

    name: str
    project_id: str | None = None
    operation_id: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    base_ref: str | None = None  # project | artifact:<id> | chapter:<id>
    expected_revision: int | None = None
    expected_fingerprint: str | None = None
    audience: Audience = "author"
    stream: bool = False
    attempt: int = 1
    version: int = 1


@dataclass(frozen=True)
class CommandResult:
    action: str
    operation_id: str
    project_id: str | None
    artifact_id: str | None = None
    chapter_id: str | None = None
    revision: int | None = None
    status: str | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    validation_issues: tuple[dict[str, Any], ...] = ()
    recovery_required: bool = False


class ApplicationError(ServiceError):
    """Error DTO ổn định, không đưa exception/path/secret thô cho adapter."""

    def __init__(
        self, code: str, message: str, *, operation_id: str | None = None,
        issues: tuple[dict[str, Any], ...] = (), retryable: bool = False,
        recovery_required: bool = False, current_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id
        self.issues = issues
        self.retryable = retryable
        self.recovery_required = recovery_required
        self.current_revision = current_revision

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "message": str(self), "operation_id": self.operation_id,
            "issues": list(self.issues), "retryable": self.retryable,
            "recovery_required": self.recovery_required,
            "current_revision": self.current_revision,
        }


@dataclass(frozen=True)
class _Spec:
    fn: Callable[..., Any]
    llm: bool = False
    generation: bool = False
    base_required: bool = False


_CATALOG: dict[str, _Spec] = {
    "co_create_turn": _Spec(co_create.run_turn, True, True),
    "save_idea_state": _Spec(co_create.set_working_state),
    "finalize_base_idea": _Spec(co_create.finalize_base_idea),
    "reserve_foundation_ids": _Spec(architect.assign_ids),
    "generate_foundation": _Spec(architect.generate, True, True),
    "edit_foundation_candidate": _Spec(architect.edit_candidate, base_required=True),
    "accept_foundation": _Spec(architect.accept),
    "reject_foundation": _Spec(architect.reject),
    "append_foundation_entries": _Spec(architect.append_entries),
    "generate_long_plan": _Spec(long_planner.generate, True, True),
    "reserve_plan_ids": _Spec(long_planner.assign_ids),
    "edit_long_plan_candidate": _Spec(long_planner.edit_candidate, base_required=True),
    "accept_long_plan": _Spec(long_planner.accept),
    "reject_long_plan": _Spec(long_planner.reject),
    "confirm_planning_scope": _Spec(long_planner.confirm_planning_scope),
    "reserve_chapter_ids": _Spec(short_planner.assign_chapter_ids),
    "generate_short_plan": _Spec(short_planner.generate, True, True),
    "edit_short_plan_candidate": _Spec(short_planner.edit_candidate, base_required=True),
    "accept_short_plan": _Spec(short_planner.accept),
    "reject_short_plan": _Spec(short_planner.reject),
    "generate_rolling": _Spec(short_planner.generate_rolling, True, True),
    "accept_rolling": _Spec(short_planner.accept_rolling),
    "reject_rolling": _Spec(short_planner.reject_rolling),
    "reserve_section_ids": _Spec(skeleton.assign_section_ids),
    "generate_skeleton": _Spec(skeleton.generate, True, True),
    "edit_skeleton_candidate": _Spec(skeleton.edit_candidate, base_required=True),
    "accept_skeleton": _Spec(skeleton.accept),
    "reject_skeleton": _Spec(skeleton.reject),
    "save_draft": _Spec(writer.save_draft, base_required=True),
    "discard_draft": _Spec(writer.discard_draft),
    "run_ai_review": _Spec(reviewer.run_ai_review, True, True),
    "rewrite_section": _Spec(reviewer.rewrite_section, True, True),
    "apply_rewrite": _Spec(reviewer.apply_rewrite, base_required=True),
    "mark_reviewed": _Spec(reviewer.mark_reviewed),
    "finalize_chapter": _Spec(reconcile.finalize_chapter),
    "generate_reconciliation": _Spec(reconcile.generate_reconciliation, True, True),
    "retry_reconcile": _Spec(reconcile.retry_reconcile, True, True),
    "edit_reconciliation_candidate": _Spec(reconcile.edit_reconciliation_candidate, base_required=True),
    "accept_reconciliation": _Spec(reconcile.accept_reconciliation),
    "reject_reconciliation": _Spec(reconcile.reject_reconciliation),
    "cancel_finalizing": _Spec(reconcile.cancel_finalizing),
    "reaccept_stale": _Spec(revision.reaccept_stale),
    "start_retcon": _Spec(revision.start_retcon),
    "edit_retcon_draft": _Spec(revision.edit_retcon_draft, base_required=True),
    "reset_consistency": _Spec(revision.reset_consistency_after_retcon),
    "revise_base_idea": _Spec(revision.revise_base_idea),
    "revise_premise": _Spec(revision.revise_premise),
    "generate_impact_report": _Spec(revision.generate_impact_report, True, True),
    "reconcile_downstream": _Spec(revision.reconcile_downstream, True, True),
}

_SPECIAL = {"create_project", "probe_llm", "save_writing_defaults", "write_draft", "recover_project"}
_PRIVILEGED_PARAMS = {"project", "client", "on_event", "on_chunk", "now", "registry", "operation_id"}
_OP_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_SENSITIVE_KEYS = ("api_key", "authorization", "headers", "author_truth", "future_plot", "full_prompt", "context_bundle")
_WRITER_DATA_KEYS = {"chapter_id", "artifact_id", "revision", "prose_revision", "status", "is_complete", "draft_complete", "replayed", "already_accepted"}


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _safe_data(value: Any, *, audience: Audience, project: Project | None, config: AppConfig) -> Any:
    if isinstance(value, Mapping):
        items = {str(k): v for k, v in value.items()}
        if audience == "writer":
            items = {k: v for k, v in items.items() if k in _WRITER_DATA_KEYS}
        return {
            key: _safe_data(item, audience=audience, project=project, config=config)
            for key, item in items.items()
            if not any(part in key.lower() for part in _SENSITIVE_KEYS)
            and key.lower() not in {"path", "project_root", "absolute_path"}
        }
    if isinstance(value, list):
        return [_safe_data(item, audience=audience, project=project, config=config) for item in value]
    if isinstance(value, str):
        result = value
        for secret in (config.api_key, str(project.root) if project else None):
            if secret:
                result = result.replace(secret, "[redacted]")
        return result
    return value


def _issues(error: ServiceError) -> tuple[dict[str, Any], ...]:
    raw: Any = error.details.get("issues")
    if raw is None and getattr(error, "result", None) is not None:
        raw = error.result.errors
    if not isinstance(raw, (list, tuple)):
        return ()
    issues = []
    for item in raw:
        data = _plain(item)
        if isinstance(data, Mapping):
            issues.append({key: data[key] for key in ("path", "field", "chapter_id", "code", "message") if key in data})
    return tuple(issues)


def _base_path(project: Project, ref: str) -> tuple[Any, int | None]:
    if ref == "project":
        return project.paths.project_json, None
    kind, separator, item_id = ref.partition(":")
    if not separator or not item_id:
        raise ApplicationError("invalid_base_ref", "base_ref phải là project/artifact:ID/chapter:ID.")
    if kind == "chapter":
        chapter = storage.load_chapter(project, item_id)
        return project.paths.chapter_json(item_id), chapter.current_draft_revision if chapter else None
    if kind == "artifact":
        envelope = storage.load_artifact(project, item_id)
        artifact_type = envelope.artifact_type if envelope else storage.artifact_type_from_id(item_id)
        current = envelope.candidate_revision.revision if envelope and envelope.candidate_revision else None
        return project.paths.artifact_path(item_id, artifact_type), current
    raise ApplicationError("invalid_base_ref", "base_ref không hợp lệ.")


class ApplicationCommands:
    """Một command = một action user; không chạy workflow kế tiếp."""

    def __init__(
        self, config: AppConfig, *, client: LLMClient | None = None,
        registry: PromptRegistry | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.registry = registry
        self.queries = ProjectQueries(config)
        self._completed: dict[tuple[str | None, str], tuple[str, CommandResult, GenerationEvent | None]] = {}

    def execute(self, command: Command, *, on_event: EventSink | None = None) -> CommandResult:
        if command.version != 1 or command.name not in _CATALOG and command.name not in _SPECIAL:
            raise ApplicationError("unknown_command", "Command/version không được hỗ trợ.")
        if command.audience not in ("author", "writer"):
            raise ApplicationError("invalid_audience", "Audience không hợp lệ.")
        if command.attempt < 1:
            raise ApplicationError("invalid_attempt", "attempt phải >= 1.")
        params = dict(command.params)
        if _PRIVILEGED_PARAMS.intersection(params):
            raise ApplicationError("invalid_input", "Input chứa field do application quản lý.")
        op_id = command.operation_id or generate_operation_id()
        if not _OP_ID.fullmatch(op_id):
            raise ApplicationError("invalid_operation_id", "operation_id không hợp lệ.")
        intent = json.dumps(
            _plain({"name": command.name, "project_id": command.project_id, "params": params,
                    "base_ref": command.base_ref, "expected_revision": command.expected_revision,
                    "expected_fingerprint": command.expected_fingerprint,
                    "stream": command.stream, "attempt": command.attempt}),
            sort_keys=True, ensure_ascii=False, default=str,
        )
        cache_key = (command.project_id, op_id)
        cached = self._completed.get(cache_key)
        if cached is not None:
            if cached[0] != intent:
                raise ApplicationError("operation_conflict", "operation_id đã dùng cho intent khác.", operation_id=op_id)
            if on_event is not None and cached[2] is not None:
                on_event(cached[2])
            return cached[1]

        project: Project | None = None
        terminal: GenerationEvent | None = None
        try:
            if command.name == "create_project":
                result = self._create_project(params, op_id)
            elif command.name == "probe_llm":
                result = self._probe(params, op_id)
            else:
                if command.project_id is None:
                    raise ApplicationError("invalid_project_id", "Command cần project_id.", operation_id=op_id)
                project = self.queries.resolve(command.project_id)
                if command.name != "recover_project" and storage.needs_recovery(project):
                    raise ApplicationError("recovery_required", "Project cần recovery trước khi ghi.", operation_id=op_id, recovery_required=True)
                self._check_base(project, command, op_id)
                if command.name == "save_writing_defaults":
                    result = self._save_defaults(project, params, op_id)
                elif command.name == "recover_project":
                    report = reconcile.recover(project)
                    result = CommandResult("recover_project", op_id, project.config.project_id,
                                           data={"status": report.status,
                                                 "operation_ids": list(report.operation_ids),
                                                 "applied_paths": _safe_data(list(report.applied_paths), audience=command.audience,
                                                                             project=project, config=self.config),
                                                 "manual_required": _safe_data(list(report.manual_required), audience=command.audience,
                                                                              project=project, config=self.config),
                                                 "messages": _safe_data(list(report.messages), audience=command.audience,
                                                                        project=project, config=self.config)},
                                           recovery_required=storage.needs_recovery(project))
                else:
                    result, terminal = self._service_action(project, command, params, op_id, on_event)
        except ApplicationError:
            raise
        except QueryError as exc:
            raise ApplicationError(exc.code, str(exc), operation_id=op_id) from exc
        except ServiceError as exc:
            message = _safe_data(str(exc), audience=command.audience, project=project, config=self.config)
            if command.audience == "writer":
                message = "Action bị chặn hoặc thất bại; xem code/issues."
            issues = tuple(_safe_data(item, audience="author", project=project, config=self.config) for item in _issues(exc))
            if command.audience == "writer":
                issues = tuple({key: item[key] for key in ("path", "field", "chapter_id", "code") if key in item} for item in issues)
            raise ApplicationError(exc.code, message, operation_id=op_id, issues=issues,
                                   recovery_required=exc.code == "recovery_required") from exc
        except (storage.StorageError, ProjectError, LLMError, ValueError, TypeError) as exc:
            code = getattr(exc, "code", None) or "invalid_input"
            if isinstance(exc, LLMError):
                code = "llm_unavailable"
            raise ApplicationError(code, "Action thất bại; xem mã lỗi và thử lại sau khi kiểm tra state.",
                                   operation_id=op_id, recovery_required=code == "recovery_required") from exc
        except Exception as exc:
            raise ApplicationError("internal_error", "Action thất bại ngoài dự kiến; state cần được kiểm tra.",
                                   operation_id=op_id) from exc
        self._completed[cache_key] = (intent, result, terminal)
        return result

    def _check_base(self, project: Project, command: Command, op_id: str) -> None:
        spec = _CATALOG.get(command.name)
        required = (spec.base_required if spec else command.name == "save_writing_defaults")
        if required and (command.base_ref is None or command.expected_fingerprint is None):
            raise ApplicationError("missing_base_revision", "Save cần base_ref và expected_fingerprint từ query.", operation_id=op_id)
        if command.base_ref is None:
            return
        path, revision = _base_path(project, command.base_ref)
        if command.expected_revision is not None and revision != command.expected_revision:
            raise ApplicationError("stale_candidate", "Revision đã đổi; tải lại trước khi Save.",
                                   operation_id=op_id, current_revision=revision)
        if command.expected_fingerprint is not None and storage.file_fingerprint(path) != command.expected_fingerprint:
            raise ApplicationError("stale_candidate", "File đã đổi; tải lại trước khi Save.",
                                   operation_id=op_id, current_revision=revision)

    def _service_action(
        self, project: Project, command: Command, params: dict[str, Any],
        op_id: str, on_event: EventSink | None,
    ) -> tuple[CommandResult, GenerationEvent | None]:
        if command.name == "write_draft":
            mode = params.pop("mode", "generate")
            fn = {"generate": writer.generate_draft, "regenerate": writer.regenerate_draft,
                  "continue": writer.continue_draft}.get(mode)
            if fn is None:
                raise ApplicationError("invalid_input", "mode Writer không hợp lệ.", operation_id=op_id)
            spec = _Spec(fn, True, True)
        else:
            spec = _CATALOG[command.name]
        if spec.llm:
            if self.client is None:
                raise ApplicationError("llm_unavailable", "Chưa cấu hình LLM client.", operation_id=op_id)
            if self.registry is None:
                self.registry, error = load_prompt_registry(self.config)
                if self.registry is None:
                    raise ApplicationError("configuration_error", error or "Prompt manifest không dùng được.", operation_id=op_id)
        if spec.generation and _generation_raw_exists(project, op_id):
            # Raw đã có nghĩa là request cũ có thể đã gửi provider. Không gửi
            # lại âm thầm sau restart, kể cả khi parse/validate đã thất bại.
            raise ApplicationError("retry_result_unavailable", "Raw output cũ cần xử lý thủ công; dùng attempt mới nếu muốn gọi lại.", operation_id=op_id)
        signature = inspect.signature(spec.fn)
        accepted = set(signature.parameters)
        has_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        unknown = set(params) - accepted if not has_kwargs else set()
        if unknown:
            raise ApplicationError("invalid_input", "Command có field không thuộc action: " + ", ".join(sorted(unknown)), operation_id=op_id)
        kwargs = dict(params)
        emitted: list[GenerationEvent] = []
        if spec.llm:
            kwargs["client"] = self.client
        if "operation_id" in accepted or has_kwargs:
            kwargs["operation_id"] = op_id
        if "expected_revision" in accepted and "expected_revision" not in kwargs and command.expected_revision is not None:
            kwargs["expected_revision"] = command.expected_revision
        if spec.generation:
            if "stream" in accepted or has_kwargs:
                kwargs["stream"] = command.stream
            if "attempt" in accepted or has_kwargs:
                kwargs["attempt"] = command.attempt
            if "on_event" in accepted or has_kwargs:
                def forward(event: GenerationEvent) -> None:
                    projected = _safe_event(event, project, self.config, command.audience, op_id)
                    emitted.append(projected)
                    if on_event is not None:
                        on_event(projected)

                kwargs["on_event"] = forward
        try:
            signature.bind(project, **kwargs)
        except TypeError as exc:
            raise ApplicationError("invalid_input", "Thiếu hoặc sai field của command.", operation_id=op_id) from exc
        service_result = spec.fn(project, **kwargs)
        result = _result_from_service(command.name, op_id, project, service_result, command.audience, self.config)
        if spec.generation and not emitted and (
            result.data.get("replayed") or result.data.get("already_accepted")
        ):
            replay_event = GenerationEvent(
                status="saved", operation_id=op_id, action=command.name,
                attempt=command.attempt, artifact_id=result.artifact_id,
                chapter_id=result.chapter_id, detail="Kết quả đã lưu từ trước.",
            )
            emitted.append(replay_event)
            if on_event is not None:
                on_event(replay_event)
        if spec.generation and (not emitted or not emitted[-1].is_terminal):
            raise ApplicationError("missing_terminal_event", "Generation chưa có kết quả terminal; cần kiểm tra state.", operation_id=op_id)
        if emitted and emitted[-1].is_terminal:
            result = replace(result, status=emitted[-1].status)
        return result, emitted[-1] if emitted and emitted[-1].is_terminal else None

    def _create_project(self, params: dict[str, Any], op_id: str) -> CommandResult:
        allowed = {"title", "default_language", "genre_prompt_id", "writing_style_id"}
        if set(params) - allowed or not str(params.get("title") or "").strip():
            raise ApplicationError("invalid_input", "Cần title hợp lệ; field project create không hợp lệ.", operation_id=op_id)
        project = Project.create(self.config.projects_root, **params)
        return CommandResult("create_project", op_id, project.config.project_id,
                             data={"project_id": project.config.project_id, "title": project.config.title, "slug": project.slug})

    def _probe(self, params: dict[str, Any], op_id: str) -> CommandResult:
        if set(params) - {"prompt"}:
            raise ApplicationError("invalid_input", "Probe chỉ nhận prompt.", operation_id=op_id)
        if self.client is None:
            raise ApplicationError("llm_unavailable", "Chưa cấu hình LLM client.", operation_id=op_id)
        self.client.complete(LLMRequest(messages=(ChatMessage(role="user", content=str(params.get("prompt") or "ping")),)))
        return CommandResult("probe_llm", op_id, None, data={"connected": True})

    def _save_defaults(self, project: Project, params: dict[str, Any], op_id: str) -> CommandResult:
        if set(params) != {"default_pov", "default_length_guidance"}:
            raise ApplicationError("invalid_input", "Save default cần POV và length guidance.", operation_id=op_id)
        config = project.update_config(
            default_pov=str(params["default_pov"]).strip(),
            default_length_guidance=str(params["default_length_guidance"]).strip(),
        )
        return CommandResult("save_writing_defaults", op_id, config.project_id,
                             data={"default_pov": config.default_pov, "default_length_guidance": config.default_length_guidance})


def _generation_raw_exists(project: Project, op_id: str) -> bool:
    return any(project.paths.raw_dir.glob(f"*/{op_id}_*.txt"))


def _safe_event(
    event: GenerationEvent, project: Project, config: AppConfig,
    audience: Audience, operation_id: str,
) -> GenerationEvent:
    detail = _safe_data(event.detail, audience=audience, project=project, config=config)[:512]
    text_delta = _safe_data(event.text_delta, audience=audience, project=project, config=config)[:4096]
    raw_ref = event.raw_ref
    if raw_ref and (raw_ref.startswith(("/", "\\")) or ":" in raw_ref):
        raw_ref = None
    # Một vài service dùng child operation ID cho proposal nội bộ (downstream).
    # Bên ngoài boundary luôn correlate event theo command operation ID.
    return replace(event, operation_id=operation_id, detail=detail, text_delta=text_delta, raw_ref=raw_ref)


def _result_from_service(
    action: str, op_id: str, project: Project, value: Any, audience: Audience, config: AppConfig,
) -> CommandResult:
    if isinstance(value, ActionResult):
        data = _safe_data(_plain(value.data), audience=audience, project=project, config=config)
        issues = tuple(_plain(issue) for issue in value.validation.errors) if value.validation else ()
        revision = data.get("revision") if isinstance(data, dict) else None
        status = data.get("status", data.get("stream_status")) if isinstance(data, dict) else None
        return CommandResult(
            action, op_id, project.config.project_id,
            artifact_id=value.artifact_id, chapter_id=value.chapter_id,
            revision=revision if isinstance(revision, int) else None,
            status=str(status) if status is not None else None,
            message=_safe_data(value.message, audience=audience, project=project, config=config),
            data=data if isinstance(data, dict) else {"value": data},
            warnings=() if audience == "writer" else tuple(_safe_data(item, audience=audience, project=project, config=config) for item in value.warnings),
            validation_issues=() if audience == "writer" else issues,
        )
    return CommandResult(action, op_id, project.config.project_id,
                         data={"value": _safe_data(_plain(value), audience=audience, project=project, config=config)})
