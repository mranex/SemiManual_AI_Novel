"""Models dữ liệu cho Manual AI Novel (T08).

File này hiện thực contract trong `docs/design/schemas.md`. Nó **không** chứa
luật lifecycle, không đọc/ghi file và không gọi LLM; các lớp ở đây chỉ mô tả
shape dữ liệu và các ràng buộc cục bộ (kiểu, enum, range, field bắt buộc).
Kiểm tra cross-field (FK, hiệu lực chương, secret leak, scope patch) nằm ở
`novel_ai.core.validation`.

Quy ước quan trọng:

- Mọi model từ chối field lạ (`extra="forbid"`). Field bị đổi tên hoặc payload
  LLM trả thêm khóa lạ phải fail rõ ràng thay vì bị bỏ qua âm thầm.
- `IsoDateTime` là **string** đã kiểm tra parse được ISO 8601, không phải
  `datetime`. Nhờ vậy round-trip JSON giữ nguyên byte mà không đổi định dạng
  thời gian do người dùng hoặc fixture tạo.
- `StableId` chỉ bắt hình dạng "trông như ID" (ASCII, không khoảng trắng) để
  chặn việc dùng tên hiển thị làm khóa. Việc ID có thật hay không do
  `validation.build_reference_index` + FK check quyết định.
- Envelope structured là generic: `ArtifactEnvelope[PremisePayload]` cho type
  cụ thể, `ArtifactEnvelope[Any]` khi chưa biết `artifact_type`. Dùng
  `parse_artifact_document` (validation.py) để lấy đúng model theo
  `artifact_type`.
- App sở hữu metadata: `status`, `revision`, `accepted_at`, `dependency_pins`,
  stable ID cuối cùng và đường dẫn file. Payload LLM trả không chứa chúng.

ID ổn định do backend cấp; helper ở cuối file (`next_stable_id`,
`reserve_id_pool`, `generate_operation_id`, `is_temporary_id`) là nguồn duy
nhất cho format ID để storage/service không tự ghép chuỗi.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Generic, Iterable, Literal, TypeVar
from uuid import uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

#: Version envelope structured của contract T02.
ENVELOPE_SCHEMA_VERSION = 1
#: Version `project.json` theo spec v0.2.
PROJECT_SCHEMA_VERSION = 2
#: Version `chapter.json`.
CHAPTER_SCHEMA_VERSION = 1

SUPPORTED_ENVELOPE_SCHEMA_VERSIONS: frozenset[int] = frozenset({ENVELOPE_SCHEMA_VERSION})
SUPPORTED_PROJECT_SCHEMA_VERSIONS: frozenset[int] = frozenset({PROJECT_SCHEMA_VERSION})
SUPPORTED_CHAPTER_SCHEMA_VERSIONS: frozenset[int] = frozenset({CHAPTER_SCHEMA_VERSION})
SUPPORTED_STATE_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


def _iso8601(value: str) -> str:
    """Chấp nhận string ISO 8601, giữ nguyên giá trị gốc."""
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - message được test gián tiếp
        raise ValueError("phải là chuỗi ISO 8601 (ví dụ 2026-09-19T09:00:00+07:00)") from exc
    return value


#: Timestamp ISO 8601 dạng string (giữ nguyên định dạng gốc khi round-trip).
IsoDateTime = Annotated[str, AfterValidator(_iso8601)]

#: ID ổn định do app quản lý. Không chứa khoảng trắng/ký tự ngoài ASCII nên
#: tên hiển thị tiếng Việt không thể vô tình trở thành khóa liên kết.
StableId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]

#: Số chương >= 1. Không suy ra từ tên file.
ChapterNumber = Annotated[int, Field(ge=1)]
#: Số revision >= 1 trong một artifact/chapter.
RevisionNumber = Annotated[int, Field(ge=1)]

MarkdownRef = str
JsonPath = str


class StrictModel(BaseModel):
    """Base model của mọi document trong contract: từ chối field lạ."""

    model_config = ConfigDict(extra="forbid")


class ArtifactStatus(str, Enum):
    """Trạng thái lifecycle structured artifact (workflow.md mục 2)."""

    missing = "missing"
    draft = "draft"
    accepted = "accepted"
    stale = "stale"
    rejected = "rejected"


class ChapterStatus(str, Enum):
    """Trạng thái lifecycle của chapter (workflow.md mục 3)."""

    planned = "planned"
    skeleton_ready = "skeleton_ready"
    draft = "draft"
    review_required = "review_required"
    finalizing = "finalizing"
    final_reconciled = "final_reconciled"


class SourceType(str, Enum):
    user = "user"
    llm = "llm"
    #: Tên member phải là `import_` vì `import` là từ khóa Python; giá trị vẫn là "import".
    import_ = "import"
    service = "service"
    recovery = "recovery"


class Visibility(str, Enum):
    """Mức hiển thị của entry/field. `author_only` không bao giờ tới Writer."""

    writer_safe = "writer_safe"
    skeleton_only = "skeleton_only"
    planner_only = "planner_only"
    author_only = "author_only"


class ValidationState(str, Enum):
    valid = "valid"
    invalid = "invalid"
    not_checked = "not_checked"


class Severity(str, Enum):
    info = "info"
    minor = "minor"
    major = "major"
    blocking = "blocking"


class EntryStatus(str, Enum):
    accepted = "accepted"
    retired = "retired"
    stale = "stale"


class ForeshadowStatus(str, Enum):
    active = "active"
    planted = "planted"
    paid_off = "paid_off"
    retired = "retired"


class ReviewIssueStatus(str, Enum):
    open = "open"
    dismissed_by_user = "dismissed_by_user"
    resolved_in_revision = "resolved_in_revision"


class CharacterTier(str, Enum):
    core = "core"
    major = "major"
    supporting = "supporting"
    minor = "minor"


class ReconciliationStatus(str, Enum):
    missing = "missing"
    draft = "draft"
    accepted = "accepted"
    failed = "failed"


class RollingStatus(str, Enum):
    ok = "ok"
    adjust = "adjust"


class ContextMode(str, Enum):
    """`actual` dùng state thật trước target; `provisional` chỉ là chuẩn bị trước."""

    actual = "actual"
    provisional = "provisional"


class CoCreateStatus(str, Enum):
    working = "working"
    finalized = "finalized"


class OperationStatus(str, Enum):
    preparing = "preparing"
    staged = "staged"
    committing = "committing"
    committed = "committed"
    aborted = "aborted"
    needs_manual_recovery = "needs_manual_recovery"


#: Field không bao giờ được xuất hiện trong Writer projection (context.md mục 3.2).
FORBIDDEN_WRITER_FIELDS: frozenset[str] = frozenset(
    {
        "author_only",
        "author_only_notes",
        "truth_author_only",
        "future_direction",
        "planned_payoff",
        "planned_planting",
        "relationship_directions",
        "relationship_changes",
        "major_reveals",
        "end_state",
    }
)

#: Field `ChapterPlan` được phép thay bởi Rolling patch (schemas.md mục 6.5).
ROLLING_SHORT_PLAN_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "summary",
        "hook",
        "outline",
        "threads",
        "chapter_goal",
        "planned_ending",
    }
)


# ---------------------------------------------------------------------------
# Metadata dùng chung
# ---------------------------------------------------------------------------


class ChapterRange(StrictModel):
    start: ChapterNumber
    end: ChapterNumber

    @model_validator(mode="after")
    def _check_order(self) -> ChapterRange:
        if self.start > self.end:
            raise ValueError("chapter_range.start phải <= chapter_range.end")
        return self


class PlanningScope(StrictModel):
    """Horizon cấp truyện của một revision Long Plan (D017, `schemas.md` 1.3/3.1).

    App-owned metadata: service ghi khi tạo candidate, Accept giữ nguyên. Không
    phải field payload do LLM trả, không phải kích thước arc và không phải edit
    window. `end` là endpoint chương, không bao giờ là số chương của một arc.
    """

    start: ChapterNumber
    end: ChapterNumber

    @model_validator(mode="after")
    def _check_order(self) -> PlanningScope:
        if self.start > self.end:
            raise ValueError("planning_scope.start phải <= planning_scope.end")
        return self


class ValidationIssue(StrictModel):
    """Một lỗi validation có đường dẫn field. `path` dùng JSON Pointer."""

    path: JsonPath
    code: str
    message: str
    severity: Severity = Severity.blocking


class ValidationResult(StrictModel):
    state: ValidationState = ValidationState.not_checked
    errors: list[ValidationIssue] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.state == ValidationState.valid


class DependencyPin(StrictModel):
    """Revision input đã dùng để tạo/accept artifact."""

    artifact_id: str
    revision: RevisionNumber
    scope: str
    chapter_id: StableId | None = None


class PayloadSource(StrictModel):
    """Nguồn của payload. Chỉ chứa metadata debug, không chứa secret."""

    source_type: SourceType
    prompt_id: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    operation_id: str | None = None
    raw_output_ref: str | None = None
    id_map: dict[str, str] = Field(default_factory=dict)


class StaleReason(StrictModel):
    """Lý do một artifact bị đánh dấu stale (storage.md mục 10)."""

    source_artifact_id: str
    source_revision: RevisionNumber
    reason: str
    affected_range: ChapterRange | None = None
    created_at: IsoDateTime
    can_reaccept: bool = True


class PlannedBridgeEntry(StrictModel):
    """Một chương được bắc cầu bằng **ý định** từ plan accepted (không phải actual)."""

    chapter_id: StableId
    chapter_number: ChapterNumber
    summary: str


class ContextBasis(StrictModel):
    """Nền context của Short Plan/Skeleton: `actual` hoặc `provisional` (schemas.md mục 4.1)."""

    mode: ContextMode = ContextMode.actual
    actual_through_chapter: int = Field(default=0, ge=0)
    planned_bridge: list[PlannedBridgeEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_bridge(self) -> ContextBasis:
        if self.mode is ContextMode.actual and self.planned_bridge:
            raise ValueError("context_basis actual không được chứa planned_bridge")
        previous = self.actual_through_chapter
        for entry in self.planned_bridge:
            if entry.chapter_number <= previous:
                raise ValueError("planned_bridge phải tăng dần và nằm sau actual_through_chapter")
            previous = entry.chapter_number
        return self


class PreparationContext(StrictModel):
    """Metadata app-owned của candidate chuẩn bị trước.

    `schemas.md` mục 4.1 yêu cầu giữ nguyên `ContextBasis` và pins của plan nguồn
    trong field này của `ArtifactRevision` Short Plan/Skeleton. Candidate
    provisional **không** được auto-accept hay dùng làm Writer context.
    """

    context_basis: ContextBasis
    dependency_pins: list[DependencyPin] = Field(default_factory=list)


T = TypeVar("T")


class ArtifactRevision(StrictModel, Generic[T]):
    """Một revision của structured artifact: candidate hoặc accepted."""

    revision: RevisionNumber
    payload: T
    payload_source: PayloadSource
    dependency_pins: list[DependencyPin] = Field(default_factory=list)
    validation: ValidationResult = Field(default_factory=ValidationResult)
    created_at: IsoDateTime
    accepted_at: IsoDateTime | None = None
    accepted_by: str | None = None
    #: App-owned: chỉ set cho candidate Short Plan/Skeleton chuẩn bị trước
    #: (`schemas.md` mục 4.1). Không do LLM trả và không đổi top-level payload.
    preparation_context: PreparationContext | None = None
    #: App-owned: horizon của revision (D017, `schemas.md` mục 1.3). Chỉ Long Plan
    #: dùng; artifact khác để `None`. File cũ thiếu field đọc ra `None` = legacy.
    planning_scope: PlanningScope | None = None


class ArtifactEnvelope(StrictModel, Generic[T]):
    """Envelope chung cho mọi structured artifact (schemas.md mục 1.3)."""

    schema_version: int = ENVELOPE_SCHEMA_VERSION
    artifact_id: str
    artifact_type: str
    status: ArtifactStatus = ArtifactStatus.missing
    accepted_revision: ArtifactRevision[T] | None = None
    candidate_revision: ArtifactRevision[T] | None = None
    stale_reasons: list[StaleReason] = Field(default_factory=list)
    history_refs: list[str] = Field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """Chỉ `accepted` mới dùng được cho action phụ thuộc (workflow.md mục 2)."""
        return self.status is ArtifactStatus.accepted and self.accepted_revision is not None


# ---------------------------------------------------------------------------
# Project, Co-create và Base Idea
# ---------------------------------------------------------------------------


class ProjectConfig(StrictModel):
    """`project.json`. Không chứa API key hay endpoint."""

    schema_version: int = PROJECT_SCHEMA_VERSION
    project_id: str
    title: str
    default_language: str = "vi"
    #: D016: default viết theo project, do user nhập. Rỗng = "chưa thiết lập",
    #: không phải một default ngầm do app/model bịa. Override từng chương thắng
    #: giá trị này cho request mới; đổi default không sửa accepted plan.
    default_pov: str = ""
    default_length_guidance: str = ""
    genre_prompt_id: str = "custom"
    writing_style_id: str = "default"
    current_chapter: ChapterNumber = 1
    auto_accept_structured: bool = False
    allow_relationship_replan: bool = True
    rolling_plan_every: int = Field(default=3, ge=1)
    created_at: IsoDateTime
    updated_at: IsoDateTime


class IdeaState(StrictModel):
    genre: str
    tone: str = ""
    protagonist: str = ""
    core_concept: str
    setting: str = ""
    conflict: str = ""
    constraints: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class IdeaStateResponse(StrictModel):
    """Output của prompt `co_create.v1`."""

    message: str
    idea_state: IdeaState


class CoCreateMessage(StrictModel):
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: IsoDateTime | None = None


class BaseIdeaMetadata(StrictModel):
    schema_version: int = ENVELOPE_SCHEMA_VERSION
    status: ArtifactStatus = ArtifactStatus.missing
    revision: RevisionNumber = 1
    markdown_ref: MarkdownRef
    dependency_pins: list[DependencyPin] = Field(default_factory=list)
    accepted_at: IsoDateTime | None = None
    accepted_by: str | None = None


class CoCreateDocument(StrictModel):
    """`co_create.json`: working state, không phải canon cho tới khi Finalize Idea."""

    schema_version: int = ENVELOPE_SCHEMA_VERSION
    status: CoCreateStatus = CoCreateStatus.working
    idea_state: IdeaState | None = None
    messages: list[CoCreateMessage] = Field(default_factory=list)
    base_idea: BaseIdeaMetadata | None = None


# ---------------------------------------------------------------------------
# Foundation schemas (schemas.md mục 2)
# ---------------------------------------------------------------------------


class PremisePayload(StrictModel):
    title: str
    logline: str
    dramatic_question: str = ""
    themes: list[str] = Field(default_factory=list)
    tone_contract: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)


class PublicProfile(StrictModel):
    """Field writer-safe của Character."""

    description: str = ""
    traits: list[str] = Field(default_factory=list)
    voice: str = ""
    known_history: str = ""


class Character(StrictModel):
    character_id: StableId
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    role: str
    tier: CharacterTier = CharacterTier.supporting
    effective_from_chapter: ChapterNumber = 1
    status: EntryStatus = EntryStatus.accepted
    public_profile: PublicProfile
    writer_profile: dict[str, Any] = Field(default_factory=dict)
    author_only: dict[str, Any] = Field(default_factory=dict)
    future_direction: dict[str, Any] = Field(default_factory=dict)


class CharactersPayload(StrictModel):
    characters: list[Character] = Field(default_factory=list)


class WorldRule(StrictModel):
    world_rule_id: StableId
    category: str
    summary: str
    content: str | dict[str, Any]
    boundary: str = ""
    effective_from_chapter: ChapterNumber = 1
    visibility: Visibility = Visibility.writer_safe
    writer_projection: str | dict[str, Any] | None = None
    author_only: dict[str, Any] = Field(default_factory=dict)


class WorldRulesPayload(StrictModel):
    world_rules: list[WorldRule] = Field(default_factory=list)


class PlannedPlanting(StrictModel):
    chapter_id: StableId
    surface_instruction: str
    visibility: Visibility = Visibility.skeleton_only


class ForeshadowEntry(StrictModel):
    foreshadow_id: StableId
    label: str
    truth_author_only: str | dict[str, Any]
    planned_planting: list[PlannedPlanting] = Field(default_factory=list)
    planned_payoff: dict[str, Any] | None = None
    effective_from_chapter: ChapterNumber = 1
    writer_visibility: Visibility = Visibility.skeleton_only
    status: ForeshadowStatus = ForeshadowStatus.active


class ForeshadowPayload(StrictModel):
    foreshadows: list[ForeshadowEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Planning schemas (schemas.md mục 3)
# ---------------------------------------------------------------------------


class RelationshipDirection(StrictModel):
    """Hướng quan hệ **tương lai** trong plan, không phải current state."""

    relationship_id: StableId | None = None
    character_ids: list[StableId]
    arc_direction: str
    target_state: str
    notes: str = ""


class ArcPlan(StrictModel):
    arc_id: StableId
    title: str
    chapter_range: ChapterRange
    goal: str
    core_conflict: str
    start_state: str
    end_state: str
    major_reveals: list[str] = Field(default_factory=list)
    character_ids: list[StableId] = Field(default_factory=list)
    world_rule_ids: list[StableId] = Field(default_factory=list)
    foreshadow_ids: list[StableId] = Field(default_factory=list)
    relationship_directions: list[RelationshipDirection] = Field(default_factory=list)


class VolumePlan(StrictModel):
    volume_id: StableId
    title: str
    theme: str
    goal: str
    arcs: list[ArcPlan]


class LongPlanPayload(StrictModel):
    volumes: list[VolumePlan]
    global_threads: list[dict[str, Any]] = Field(default_factory=list)


class ChapterPlan(StrictModel):
    chapter_id: StableId
    chapter_number: ChapterNumber
    title: str
    summary: str
    hook: str = ""
    #: Item string là beat; item object là yêu cầu viết `{language, pov, length_guidance}`.
    outline: list[str | dict[str, Any]] = Field(default_factory=list)
    character_ids: list[StableId] = Field(default_factory=list)
    world_rule_ids: list[StableId] = Field(default_factory=list)
    foreshadow_ids: list[StableId] = Field(default_factory=list)
    threads: list[str | dict[str, Any]] = Field(default_factory=list)
    relationship_changes: list[RelationshipDirection] = Field(default_factory=list)
    chapter_goal: str
    planned_ending: str = ""


class ShortPlanPayload(StrictModel):
    arc_id: StableId
    chapters: list[ChapterPlan]


# ---------------------------------------------------------------------------
# Skeleton (schemas.md mục 4)
# ---------------------------------------------------------------------------


class ForeshadowSurface(StrictModel):
    """Cầu nối an toàn từ secret author-only sang Writer."""

    foreshadow_id: StableId
    surface_instruction: str
    #: Mức reveal dạng mô tả (ví dụ `hint_only`), không phải quyền tự reveal.
    reveal_policy: str


class SkeletonSection(StrictModel):
    section_id: StableId
    index: int = Field(ge=1)
    type: str
    instruction: str
    purpose: str
    purpose_visibility: Visibility = Visibility.planner_only
    writer_notes: list[str] = Field(default_factory=list)
    author_only_notes: list[str] = Field(default_factory=list)
    required_beats: list[str] = Field(default_factory=list)
    forbidden_moves: list[str] = Field(default_factory=list)
    character_ids: list[StableId] = Field(default_factory=list)
    world_rule_ids: list[StableId] = Field(default_factory=list)
    foreshadow_ids: list[StableId] = Field(default_factory=list)
    foreshadow_surfaces: list[ForeshadowSurface] = Field(default_factory=list)


class WriterContextPolicy(StrictModel):
    """Backend không cho output nới hai cờ này (schemas.md mục 4)."""

    include_author_only: Literal[False] = False
    include_future_plan: Literal[False] = False


class SkeletonPayload(StrictModel):
    chapter_id: StableId
    chapter_number: ChapterNumber
    sections: list[SkeletonSection] = Field(min_length=1)
    global_constraints: list[str] = Field(default_factory=list)
    writer_context_policy: WriterContextPolicy = Field(default_factory=WriterContextPolicy)


# ---------------------------------------------------------------------------
# Chapter, prose và review (schemas.md mục 5)
# ---------------------------------------------------------------------------


class ProseRevision(StrictModel):
    revision: RevisionNumber
    markdown_ref: MarkdownRef
    source_type: SourceType
    is_complete: bool = False
    created_at: IsoDateTime
    dependency_pins: list[DependencyPin] = Field(default_factory=list)


class HumanReviewRecord(StrictModel):
    prose_revision: RevisionNumber
    reviewed_at: IsoDateTime
    reviewed_by: str = "user"
    notes: str = ""
    valid_for_current_revision: bool = True


class FinalCandidate(StrictModel):
    prose_revision: RevisionNumber
    markdown_ref: MarkdownRef
    created_at: IsoDateTime
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.missing


class FinalRevision(StrictModel):
    revision: RevisionNumber
    source_prose_revision: RevisionNumber
    markdown_ref: MarkdownRef
    reconciled_at: IsoDateTime


class ReviewReportRef(StrictModel):
    artifact_id: str
    revision: RevisionNumber
    prose_revision: RevisionNumber


class ChapterMetadata(StrictModel):
    """`chapters/ch_xxxx/chapter.json`."""

    schema_version: int = CHAPTER_SCHEMA_VERSION
    chapter_id: StableId
    chapter_number: ChapterNumber
    title: str
    status: ChapterStatus = ChapterStatus.planned
    previous_chapter_id: StableId | None = None
    short_plan_pin: DependencyPin
    skeleton_pin: DependencyPin | None = None
    drafts: list[ProseRevision] = Field(default_factory=list)
    current_draft_revision: RevisionNumber | None = None
    human_review: HumanReviewRecord | None = None
    ai_review_reports: list[ReviewReportRef] = Field(default_factory=list)
    final_candidate: FinalCandidate | None = None
    final_revision: FinalRevision | None = None
    reconciliation_pin: DependencyPin | None = None
    #: App-owned: chuẩn bị trước cho Skeleton candidate (schemas.md mục 4.1).
    preparation_context: PreparationContext | None = None
    history_refs: list[str] = Field(default_factory=list)

    def draft_revision(self, revision: int) -> ProseRevision | None:
        for draft in self.drafts:
            if draft.revision == revision:
                return draft
        return None

    @property
    def current_draft(self) -> ProseRevision | None:
        if self.current_draft_revision is None:
            return None
        return self.draft_revision(self.current_draft_revision)


class ReviewSource(StrictModel):
    authority_kind: str
    artifact_id: str | None = None
    revision: RevisionNumber | None = None
    field_path: str | None = None


class ReviewEvidence(StrictModel):
    prose_revision: RevisionNumber
    quote: str | None = None
    section_id: str | None = None


class ReviewIssue(StrictModel):
    issue_id: StableId
    severity: Severity
    category: str
    source: ReviewSource
    evidence: ReviewEvidence
    message: str
    suggested_action: str = ""
    status: ReviewIssueStatus = ReviewIssueStatus.open


class ReviewReportPayload(StrictModel):
    chapter_id: StableId
    prose_revision: RevisionNumber
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str


class RewriteSectionRequest(StrictModel):
    chapter_id: StableId
    prose_revision: RevisionNumber
    section_id: StableId | None = None
    selected_text: str | None = None
    instruction: str
    constraints: list[str] = Field(default_factory=list)
    context_pins: list[DependencyPin] = Field(default_factory=list)


class RewriteSectionPayload(StrictModel):
    replacement_markdown: str
    notes: list[str] = Field(default_factory=list)
    changed_intent: bool = False


# ---------------------------------------------------------------------------
# State, reconciliation, rolling, impact (schemas.md mục 6)
# ---------------------------------------------------------------------------


class SourceFinalRevision(StrictModel):
    chapter_id: StableId
    revision: RevisionNumber


class TimelineEntry(StrictModel):
    timeline_id: StableId
    chapter_id: StableId
    chapter_number: ChapterNumber
    time: str
    location: str
    status: str
    source_final_revision: SourceFinalRevision
    accepted_at: IsoDateTime
    stale: bool = False


class CurrentTimelineDocument(StrictModel):
    schema_version: int = ENVELOPE_SCHEMA_VERSION
    latest_final_chapter: int = Field(default=0, ge=0)
    latest_consistent_chapter: int = Field(default=0, ge=0)
    entries: list[TimelineEntry] = Field(default_factory=list)


class RelationshipHistoryEntry(StrictModel):
    chapter_id: StableId
    chapter_number: ChapterNumber
    current: str
    source_final_revision: RevisionNumber


class RelationshipState(StrictModel):
    relationship_id: StableId
    #: Đúng hai character ID khác nhau; không dùng tên hiển thị.
    character_ids: list[StableId]
    current: str
    last_updated_chapter: ChapterNumber
    history: list[RelationshipHistoryEntry] = Field(default_factory=list)
    stale: bool = False


class RelationshipStateDocument(StrictModel):
    schema_version: int = ENVELOPE_SCHEMA_VERSION
    latest_consistent_chapter: int = Field(default=0, ge=0)
    relationships: list[RelationshipState] = Field(default_factory=list)


def reconciled_chapter_numbers(chapters: Iterable[ChapterMetadata]) -> set[int]:
    """Số chương đang thật sự `final_reconciled` (đọc metadata, không tin counter).

    Cần hàm này vì timeline/relationship giữ **số chương** chứ không giữ status:
    một chapter có thể đã rời `final_reconciled` (retcon, `review_required`) trong
    khi `state/current_timeline.json` vẫn ghi `latest_final_chapter` cũ. Mọi nơi
    suy "state thật còn tới chương nào" phải lọc lại qua metadata, nếu không guard
    và context builder sẽ tin vào state không còn tồn tại.
    """
    return {
        chapter.chapter_number
        for chapter in chapters
        if chapter.status is ChapterStatus.final_reconciled
    }


def consistent_chapter_number(
    *,
    timeline: CurrentTimelineDocument,
    relationships: RelationshipStateDocument,
    reconciled_numbers: set[int],
) -> int:
    """Chương cao nhất có state chain hợp lệ, đã đối chiếu lại chapter metadata.

    `latest_consistent_chapter` của timeline/relationship là field dẫn xuất
    (storage.md mục 8) và có thể còn giá trị cũ sau khi chương đổi status. Vì vậy
    giá trị đó chỉ được **hạ trần** xuống chương `final_reconciled` cao nhất thật
    sự và bị bỏ hẳn nếu chương đó không còn final.
    """
    latest_final = max(reconciled_numbers, default=0)

    def normalize(value: int) -> int:
        if value <= 0:
            return 0
        if value in reconciled_numbers:
            return value
        if value > latest_final:
            # Field dẫn xuất còn cao hơn state thật (chương chưa từng reconcile).
            return latest_final
        return 0

    timeline_consistent = normalize(
        timeline.latest_consistent_chapter or timeline.latest_final_chapter
    )
    if not relationships.relationships:
        return timeline_consistent
    relationship_consistent = normalize(
        relationships.latest_consistent_chapter
        or max(
            (item.last_updated_chapter for item in relationships.relationships), default=0
        )
    )
    return min(timeline_consistent, relationship_consistent)


class ExcludedDueToEffectiveChapter(StrictModel):
    item_kind: str
    item_id: StableId
    effective_from_chapter: ChapterNumber


class RelationshipVersionRef(StrictModel):
    relationship_id: StableId
    last_updated_chapter: ChapterNumber


class ChapterContextSnapshot(StrictModel):
    snapshot_id: StableId
    for_chapter_id: StableId
    for_chapter_number: ChapterNumber
    created_from_action: str
    dependency_pins: list[DependencyPin] = Field(default_factory=list)
    timeline_entry_ids: list[StableId] = Field(default_factory=list)
    relationship_versions: list[RelationshipVersionRef] = Field(default_factory=list)
    effective_character_ids: list[StableId] = Field(default_factory=list)
    effective_world_rule_ids: list[StableId] = Field(default_factory=list)
    effective_foreshadow_ids: list[StableId] = Field(default_factory=list)
    excluded_due_to_effective_chapter: list[ExcludedDueToEffectiveChapter] = Field(
        default_factory=list
    )
    writer_projection_hash: str | None = None
    preparation_context: PreparationContext | None = None
    created_at: IsoDateTime | None = None


class FinalCandidateRef(StrictModel):
    """Binding opaque tới bản prose đã đóng băng."""

    prose_revision: RevisionNumber
    markdown_ref: MarkdownRef


class TimelineProposal(StrictModel):
    time: str
    location: str
    status: str


class RelationshipUpdate(StrictModel):
    relationship_id: StableId | None = None
    character_ids: list[StableId]
    current: str


class ReconciliationPayload(StrictModel):
    chapter_id: StableId
    chapter_number: ChapterNumber
    source_final_candidate: FinalCandidateRef
    timeline: TimelineProposal
    relationship_updates: list[RelationshipUpdate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ReviewedChapterRange(StrictModel):
    start: ChapterNumber
    end: ChapterNumber

    @model_validator(mode="after")
    def _check_order(self) -> ReviewedChapterRange:
        if self.start > self.end:
            raise ValueError("reviewed_chapter_range.start phải <= end")
        return self


class RollingDeviation(StrictModel):
    chapter_id: StableId
    planned: str
    actual: str
    evidence: str


class ShortPlanChange(StrictModel):
    chapter_id: StableId
    changes: dict[str, Any]
    reason: str


class RelationshipPlanChange(StrictModel):
    chapter_id: StableId
    relationship_changes: list[RelationshipDirection]
    reason: str


class BlockedByAuthority(StrictModel):
    chapter_id: StableId | None = None
    authority: str
    reason: str


class RollingPatchPayload(StrictModel):
    status: RollingStatus
    reviewed_chapter_range: ReviewedChapterRange
    deviations: list[RollingDeviation] = Field(default_factory=list)
    short_plan_changes: list[ShortPlanChange] = Field(default_factory=list)
    relationship_plan_changes: list[RelationshipPlanChange] = Field(default_factory=list)
    blocked_by_authority: list[BlockedByAuthority] = Field(default_factory=list)


class SourceChange(StrictModel):
    item_kind: str
    item_id: StableId
    from_revision: RevisionNumber | None = None
    to_candidate_revision: RevisionNumber | None = None


class ImpactAffectedItem(StrictModel):
    item_kind: str
    item_id: StableId
    reason: str
    severity: Severity
    suggested_action: str = ""


class ImpactReportPayload(StrictModel):
    source_change: SourceChange
    affected_items: list[ImpactAffectedItem] = Field(default_factory=list)
    risk_summary: str
    suggested_actions: list[str] = Field(default_factory=list)


class StructuredOutputError(StrictModel):
    """Lỗi parse/validate structured output; accepted state không đổi."""

    error_id: str
    artifact_id: str | None = None
    candidate_revision: RevisionNumber | None = None
    raw_output_ref: str | None = None
    errors: list[ValidationIssue] = Field(default_factory=list)
    created_at: IsoDateTime
    retryable: bool = True


# ---------------------------------------------------------------------------
# Transaction và lock (storage.md mục 4, 6)
# ---------------------------------------------------------------------------


class WriteSetEntry(StrictModel):
    target: str
    staged: str | None = None
    before: str | None = None
    expected_old_hash: str | None = None
    staged_hash: str | None = None


class OperationManifest(StrictModel):
    operation_id: str
    operation_type: str
    status: OperationStatus = OperationStatus.preparing
    created_at: IsoDateTime
    updated_at: IsoDateTime
    base_fingerprints: dict[str, str] = Field(default_factory=dict)
    write_set: list[WriteSetEntry] = Field(default_factory=list)
    applied_paths: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None


class ProjectLock(StrictModel):
    operation_id: str
    operation_type: str
    created_at: IsoDateTime
    heartbeat_at: IsoDateTime | None = None


# ---------------------------------------------------------------------------
# Registry artifact_type -> payload model
# ---------------------------------------------------------------------------

ARTIFACT_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "premise": PremisePayload,
    "characters": CharactersPayload,
    "world_rules": WorldRulesPayload,
    "foreshadow": ForeshadowPayload,
    "long_plan": LongPlanPayload,
    "short_plan": ShortPlanPayload,
    "skeleton": SkeletonPayload,
    "review_report": ReviewReportPayload,
    "rewrite_section": RewriteSectionPayload,
    "reconciliation": ReconciliationPayload,
    "rolling_patch": RollingPatchPayload,
    "impact_report": ImpactReportPayload,
}

#: Scope dùng trong `DependencyPin.scope`.
DEPENDENCY_SCOPES: frozenset[str] = frozenset(
    {
        "base_idea",
        "premise",
        "characters",
        "world_rules",
        "foreshadow",
        "long_plan",
        "short_plan",
        "skeleton",
        "rolling_patch",
        "review_report",
        "reconciliation",
        "timeline_as_of",
        "relationship_as_of",
        "prose_revision",
        "impact_report",
    }
)


# ---------------------------------------------------------------------------
# Stable ID và operation ID
# ---------------------------------------------------------------------------

#: Prefix chuẩn cho từng loại entity. Backend cấp ID, không LLM.
ID_PREFIXES: dict[str, str] = {
    "project": "proj",
    "character": "char",
    "world_rule": "rule",
    "foreshadow": "fs",
    "volume": "vol",
    "arc": "arc",
    "chapter": "ch",
    "section": "section",
    "relationship": "rel",
    "revision": "rev",
    "snapshot": "snapshot",
    "timeline": "timeline",
    "thread": "thread",
    "operation": "op",
    "error": "err",
    "issue": "issue",
}

_TEMPORARY_ID_RE = re.compile(r"^tmp_(volume|arc|section)_(\d+)$")

#: Kind entity dùng ID tạm trong output LLM trước khi backend map (schemas.md mục 4.1).
TEMPORARY_ID_KINDS: frozenset[str] = frozenset({"volume", "arc", "section"})


def format_stable_id(prefix: str, number: int) -> str:
    """`("char", 7)` -> `char_0007`."""
    if number < 1:
        raise ValueError("số thứ tự stable ID phải >= 1")
    return f"{prefix}_{number:04d}"


def next_stable_id(prefix: str, existing: Iterable[str]) -> str:
    """ID kế tiếp chưa dùng, tính theo số lớn nhất đã có cùng prefix."""
    highest = 0
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    for value in existing:
        match = pattern.match(value)
        if match:
            highest = max(highest, int(match.group(1)))
    return format_stable_id(prefix, highest + 1)


def reserve_id_pool(prefix: str, count: int, existing: Iterable[str]) -> list[str]:
    """Reserve `count` ID mới liên tiếp. Pool được để dư, không bắt dùng hết."""
    if count < 0:
        raise ValueError("count phải >= 0")
    highest = 0
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    for value in existing:
        match = pattern.match(value)
        if match:
            highest = max(highest, int(match.group(1)))
    return [format_stable_id(prefix, highest + offset) for offset in range(1, count + 1)]


def is_temporary_id(value: str) -> bool:
    """ID tạm `tmp_volume_<n>` / `tmp_arc_<n>` / `tmp_section_<n>` của LLM."""
    return _TEMPORARY_ID_RE.match(value) is not None


def temporary_id_kind(value: str) -> str | None:
    """Trả `volume`/`arc`/`section` nếu là ID tạm, ngược lại None."""
    match = _TEMPORARY_ID_RE.match(value)
    return match.group(1) if match else None


def generate_operation_id() -> str:
    """Idempotency key cho write action first-class (storage.md mục 4)."""
    return f"{ID_PREFIXES['operation']}_{uuid4().hex}"


def generate_error_id() -> str:
    return f"{ID_PREFIXES['error']}_{uuid4().hex[:12]}"


def generate_snapshot_id() -> str:
    return f"{ID_PREFIXES['snapshot']}_{uuid4().hex[:12]}"


def now_iso() -> str:
    """Timestamp ISO 8601 local có offset, dùng thống nhất cho metadata app.

    T03 chốt lưu local time kèm offset (ví dụ fixture T02 dùng `+07:00`) để
    người dùng đọc history dễ hơn UTC thuần.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")
