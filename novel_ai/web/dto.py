"""Version 1 HTTP input catalog. Each command has a closed params schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from novel_ai.application.commands import _CATALOG, _SPECIAL
from novel_ai.core.models import RewriteSectionRequest, SourceChange


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ErrorBody(ClosedModel):
    code: str
    message: str
    operation_id: str | None = None
    issues: list[dict[str, Any]] = Field(default_factory=list)
    retryable: bool = False
    recovery_required: bool = False
    current_revision: int | None = None


class QueryBody(ClosedModel):
    kind: str
    project_id: str | None
    data: dict[str, Any]
    needs_recovery: bool
    read_only: bool
    write_blocked: bool


class ResultBody(ClosedModel):
    action: str
    operation_id: str
    project_id: str | None
    artifact_id: str | None = None
    chapter_id: str | None = None
    revision: int | None = None
    status: str | None = None
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    validation_issues: tuple[dict[str, Any], ...] = ()
    recovery_required: bool = False


class EventBody(ClosedModel):
    status: str
    operation_id: str
    action: str
    attempt: int
    transport: str
    prompt_id: str | None = None
    artifact_id: str | None = None
    chapter_id: str | None = None
    text_delta: str = ""
    detail: str = ""
    raw_ref: str | None = None


# Field declarations are explicit: no Python dependency, path, clock, client,
# callback or arbitrary params can cross the HTTP boundary.
S = str
I = int
B = bool
D = dict[str, Any]
LS = list[str]
LD = list[dict[str, Any]]
FIELDS: dict[str, dict[str, tuple[Any, Any]]] = {
    "create_project": {"title": (S, ...), "default_language": (S, "vi"), "genre_prompt_id": (S, ""), "writing_style_id": (S, "")},
    "probe_llm": {"prompt": (S, "ping")},
    "save_writing_defaults": {"default_pov": (S, ...), "default_length_guidance": (S, ...)},
    "recover_project": {},
    "co_create_turn": {"user_message": (S, ...)},
    "save_idea_state": {"idea_state": (D, ...)},
    "finalize_base_idea": {"markdown": (S | None, None), "accepted_by": (S, "user")},
    "reserve_foundation_ids": {"artifact_type": (S, ...), "count": (I, ...)},
    "generate_foundation": {"artifact_type": (S, ...), "action": (S, "generate"), "chapter_number": (I, 1), "assigned_ids": (LS | None, None), "user_instruction": (S, "")},
    "edit_foundation_candidate": {"artifact_type": (S, ...), "payload": (D, ...)},
    "accept_foundation": {"artifact_type": (S, ...), "accepted_by": (S, "user")},
    "reject_foundation": {"artifact_type": (S, ...)},
    "append_foundation_entries": {"artifact_type": (S, ...), "payload": (D, ...), "effective_from_chapter": (I, ...)},
    "generate_long_plan": {"planning_scope": (D | None, None), "action": (S, "generate"), "user_instruction": (S, "")},
    "reserve_plan_ids": {"volume_count": (I, 12), "arc_count": (I, 36)},
    "edit_long_plan_candidate": {"payload": (D, ...), "planning_scope": (D | None, None), "assigned_volume_ids": (LS, []), "assigned_arc_ids": (LS, [])},
    "accept_long_plan": {"accepted_by": (S, "user")},
    "reject_long_plan": {},
    "confirm_planning_scope": {"start": (I, ...), "end": (I, ...)},
    "reserve_chapter_ids": {"count": (I, ...)},
    "generate_short_plan": {"arc_id": (S, ...), "assigned_chapters": (LD | None, None), "chapter_constraints": (LD, []), "action": (S, "generate"), "user_instruction": (S, "")},
    "edit_short_plan_candidate": {"arc_id": (S, ...), "payload": (D, ...)},
    "accept_short_plan": {"accepted_by": (S, "user")},
    "reject_short_plan": {},
    "generate_rolling": {"arc_id": (S | None, None), "reviewed_chapter_range": (D | None, None), "user_instruction": (S, "")},
    "accept_rolling": {"accepted_by": (S, "user")},
    "reject_rolling": {},
    "reserve_section_ids": {"chapter_id": (S, ...), "count": (I, ...)},
    "generate_skeleton": {"chapter_id": (S, ...), "action": (S, "generate"), "user_instruction": (S, "")},
    "edit_skeleton_candidate": {"chapter_id": (S, ...), "payload": (D, ...)},
    "accept_skeleton": {"chapter_id": (S, ...), "accepted_by": (S, "user")},
    "reject_skeleton": {"chapter_id": (S, ...)},
    "write_draft": {"chapter_id": (S, ...), "mode": (Literal["generate", "regenerate", "continue"], "generate")},
    "save_draft": {"chapter_id": (S, ...), "text": (S, ...), "source_type": (Literal["user", "llm", "import", "service", "recovery"], "user"), "is_complete": (B | None, None)},
    "discard_draft": {"chapter_id": (S, ...), "revision": (I | None, None)},
    "run_ai_review": {"chapter_id": (S, ...), "review_focus": (S, "")},
    "rewrite_section": {"request": (RewriteSectionRequest, ...)},
    "apply_rewrite": {"chapter_id": (S, ...), "replacement_markdown": (S, ...), "target_text": (S, ...)},
    "mark_reviewed": {"chapter_id": (S, ...), "notes": (S, ""), "reviewed_by": (S, "user")},
    "finalize_chapter": {"chapter_id": (S, ...), "confirm_review": (B, False), "accepted_by": (S, "user")},
    "generate_reconciliation": {"chapter_id": (S, ...)},
    "retry_reconcile": {"chapter_id": (S, ...)},
    "edit_reconciliation_candidate": {"chapter_id": (S, ...), "payload": (D, ...)},
    "accept_reconciliation": {"chapter_id": (S, ...), "accepted_by": (S, "user"), "expect_chapter_number": (I | None, None), "refresh_state": (B, False)},
    "reject_reconciliation": {"chapter_id": (S, ...)},
    "cancel_finalizing": {"chapter_id": (S, ...)},
    "reaccept_stale": {"artifact_id": (S, ...), "accepted_by": (S, "user")},
    "start_retcon": {"chapter_id": (S, ...)},
    "edit_retcon_draft": {"chapter_id": (S, ...), "text": (S, ...)},
    "reset_consistency": {"chapter_number": (I, ...)},
    "revise_base_idea": {"markdown": (S, ...), "accepted_by": (S, "user")},
    "revise_premise": {"payload": (D, ...), "accepted_by": (S, "user")},
    "generate_impact_report": {"source_change": (SourceChange, ...), "before_content": (S, ""), "after_content": (S, ""), "analysis_scope": (S, "")},
    "reconcile_downstream": {"chapter_id": (S, ...)},
}

assert set(FIELDS) == set(_CATALOG) | _SPECIAL
GENERATION_NAMES = {name for name, spec in _CATALOG.items() if spec.generation} | {"write_draft"}


class Envelope(ClosedModel):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    project_id: str | None = None
    base_ref: str | None = None
    expected_revision: int | None = None
    expected_fingerprint: str | None = None
    audience: Literal["author", "writer"] = "author"
    attempt: int = Field(default=1, ge=1)
    stream: bool = False
    version: Literal[1] = 1


PARAM_MODELS: dict[str, type[ClosedModel]] = {
    name: create_model(f"{''.join(part.title() for part in name.split('_'))}Params", __base__=ClosedModel, **fields)
    for name, fields in FIELDS.items()
}
REQUEST_MODELS: dict[str, type[Envelope]] = {
    name: create_model(f"{''.join(part.title() for part in name.split('_'))}Request", __base__=Envelope,
                       params=(PARAM_MODELS[name], ...))
    for name in FIELDS
}
