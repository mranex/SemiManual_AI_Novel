"""Deterministic context builder và secret filtering (T12).

Hiện thực `docs/design/context.md`: context được chọn bằng rule + stable ID,
không RAG/embedding/similarity, không để LLM tự tìm dữ liệu. Mỗi lần build trả
về một `ContextBundle` gồm payload đóng gói cho prompt, dependency pins,
included/excluded ID, projection hash và budget report — đủ để tái hiện request
và phát hiện leak.

Nguyên tắc đã hiện thực ở đây:

- Chọn entity theo ID explicit (từ ChapterPlan/Long Plan/Skeleton) và theo
  `effective_from_chapter <= chapter_number`; phần bị loại vì tương lai được
  ghi vào `excluded_due_to_effective_chapter`.
- Writer chỉ nhận projection an toàn: `instruction`, `writer_notes`,
  `required_beats`, `forbidden_moves`, `foreshadow_surfaces.surface_instruction`,
  `purpose` khi `purpose_visibility == writer_safe`; không nhận
  `author_only`, `truth_author_only`, `future_direction`, `author_only_notes`,
  `planned_payoff`, full plan hay lore chưa hiệu lực.
- Sau khi dựng projection Writer, bundle tự chạy
  `validate_writer_projection`; nếu có field cấm thì raise `ContextError`
  (`code="secret_leak"`) thay vì gửi prompt. Đây là guard backend, không phải
  kiểm tra ở UI.
- Chapter N dùng state actual as-of N-1. Khi chưa có actual (chương trước chưa
  final_reconciled), Short Plan/Skeleton dùng mode `provisional` với
  `planned_bridge` tách riêng; Writer **không** chấp nhận provisional.
- Budget: P0 (hard constraints, guard) không bao giờ bị cắt. Vượt budget thì
  giảm P3 rồi P2; nếu vẫn vượt thì raise `context_budget_exceeded` kèm danh
  sách phần gây vượt, không cắt giữa field/section.

Module này không gọi LLM, không ghi project và không tự chạy bước tiếp.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from novel_ai.core import storage
from novel_ai.core.models import (
    ArcPlan,
    BaseIdeaMetadata,
    ChapterContextSnapshot,
    ChapterMetadata,
    ChapterPlan,
    ChapterStatus,
    Character,
    CoCreateDocument,
    ContextBasis,
    ContextMode,
    CurrentTimelineDocument,
    DependencyPin,
    ExcludedDueToEffectiveChapter,
    ForeshadowEntry,
    LongPlanPayload,
    PlannedBridgeEntry,
    PreparationContext,
    PremisePayload,
    ProjectConfig,
    RelationshipStateDocument,
    RelationshipVersionRef,
    RewriteSectionRequest,
    ShortPlanPayload,
    SourceChange,
    TimelineEntry,
    Visibility,
    VolumePlan,
    WorldRule,
    consistent_chapter_number,
    generate_snapshot_id,
    now_iso,
    reconciled_chapter_numbers,
)
from novel_ai.core.project import Project
from novel_ai.core.validation import (
    build_reference_index,
    partition_by_effective_chapter,
    summarize_errors,
    validate_writer_projection,
)

#: Giới hạn ký tự mặc định cho một prompt payload. Đủ rộng cho hai chương MVP;
#: vượt ngưỡng thì giảm P3/P2 rồi báo lỗi rõ thay vì cắt âm thầm.
DEFAULT_CONTEXT_BUDGET_CHARS = 60_000

#: Số chapter final gần nhất đưa vào context (context.md mục 3.3).
RECENT_FINAL_SUMMARIES_DEFAULT = 3


class ContextError(RuntimeError):
    """Lỗi context có mã ổn định. Service/UI phân loại theo `code`."""

    def __init__(self, message: str, *, code: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details: dict[str, Any] = dict(details or {})


@dataclass
class BudgetReport:
    """Kết quả áp budget: phần nào bị rút gọn, phần nào là hard constraint."""

    estimated_chars: int
    limit_chars: int
    exceeded: bool = False
    reduced: list[str] = field(default_factory=list)
    blocking_sections: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "estimated_chars": self.estimated_chars,
            "limit_chars": self.limit_chars,
            "exceeded": self.exceeded,
            "reduced": list(self.reduced),
            "blocking_sections": list(self.blocking_sections),
        }


@dataclass
class ContextBundle:
    """Context đã build: payload + pins + bằng chứng chống leak."""

    context_id: str
    context_kind: str
    payload: dict[str, Any]
    dependency_pins: list[DependencyPin]
    included_ids: dict[str, list[str]] = field(default_factory=dict)
    excluded_due_to_effective_chapter: list[ExcludedDueToEffectiveChapter] = field(
        default_factory=list
    )
    projection_hash: str = ""
    budget_report: BudgetReport | None = None
    for_chapter_id: str | None = None
    for_chapter_number: int | None = None
    mode: ContextMode = ContextMode.actual
    preparation_context: PreparationContext | None = None
    warnings: list[str] = field(default_factory=list)

    def to_snapshot(self, *, created_from_action: str, created_at: str | None = None) -> ChapterContextSnapshot:
        """Snapshot của context đã dùng, dùng cho `state/snapshots/`.

        `relationship_versions` được suy từ chính payload `relationships_as_of` đã
        gửi vào prompt (trước đây luôn là `[]` vì `included_ids` không có key
        `relationships`, nên snapshot không tái hiện được state đã dùng — F-B7 của
        `docs/design/review-findings-t24.md`).
        """
        if self.for_chapter_id is None or self.for_chapter_number is None:
            raise ContextError(
                "Chỉ snapshot context gắn với một chapter cụ thể.",
                code="snapshot_requires_chapter",
            )
        return ChapterContextSnapshot(
            snapshot_id=generate_snapshot_id(),
            for_chapter_id=self.for_chapter_id,
            for_chapter_number=self.for_chapter_number,
            created_from_action=created_from_action,
            dependency_pins=self.dependency_pins,
            timeline_entry_ids=[
                str(item) for item in self.included_ids.get("timeline_entries", [])
            ],
            relationship_versions=self._relationship_version_refs(),
            effective_character_ids=self.included_ids.get("characters", []),
            effective_world_rule_ids=self.included_ids.get("world_rules", []),
            effective_foreshadow_ids=self.included_ids.get("foreshadows", []),
            excluded_due_to_effective_chapter=self.excluded_due_to_effective_chapter,
            writer_projection_hash=self.projection_hash or None,
            preparation_context=self.preparation_context,
            created_at=created_at or now_iso(),
        )

    def _relationship_version_refs(self) -> list[RelationshipVersionRef]:
        """`relationships_as_of` trong payload ⇒ ref cho snapshot (bỏ entry lỗi)."""
        refs: list[RelationshipVersionRef] = []
        for item in self.payload.get("relationships_as_of") or []:
            if not isinstance(item, Mapping):
                continue
            relationship_id = item.get("relationship_id")
            chapter = item.get("last_updated_chapter")
            if relationship_id is None or chapter is None:
                continue
            refs.append(
                RelationshipVersionRef(
                    relationship_id=str(relationship_id),
                    last_updated_chapter=int(chapter),
                )
            )
        return refs


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return str(value)


@dataclass
class _State:
    """Ảnh chụp chỉ-đọc của accepted state, phục vụ chọn context."""

    project: Project
    config: ProjectConfig
    base_idea_markdown: str = ""
    base_idea_meta: BaseIdeaMetadata | None = None
    co_create: CoCreateDocument | None = None
    premise: PremisePayload | None = None
    characters: list[Character] = field(default_factory=list)
    world_rules: list[WorldRule] = field(default_factory=list)
    foreshadows: list[ForeshadowEntry] = field(default_factory=list)
    long_plan: LongPlanPayload | None = None
    short_plan: ShortPlanPayload | None = None
    chapters: list[ChapterMetadata] = field(default_factory=list)
    timeline: CurrentTimelineDocument = field(default_factory=CurrentTimelineDocument)
    relationships: RelationshipStateDocument = field(
        default_factory=RelationshipStateDocument
    )
    revisions: dict[str, int] = field(default_factory=dict)
    stale_artifact_ids: list[str] = field(default_factory=list)

    # -- load ---------------------------------------------------------------

    @classmethod
    def load(cls, project: Project) -> _State:
        state = cls(project=project, config=project.config)
        state.chapters = _load_chapters(project)
        state.timeline = storage.load_timeline(project)
        state.relationships = storage.load_relationships(project)
        state.co_create = _load_optional(
            project, project.paths.co_create_json, "co_create.json"
        )
        if project.paths.base_idea_md.is_file():
            state.base_idea_markdown = storage.read_text(project.paths.base_idea_md)
        if project.paths.base_idea_meta_json.is_file():
            state.base_idea_meta = _parse_optional(
                BaseIdeaMetadata, project.paths.base_idea_meta_json
            )
        state.premise = state._accepted("premise", PremisePayload)
        characters_payload = state._accepted("characters", Any)
        state.characters = list(getattr(characters_payload, "characters", []) or [])
        rules_payload = state._accepted("world_rules", Any)
        state.world_rules = list(getattr(rules_payload, "world_rules", []) or [])
        foreshadow_payload = state._accepted("foreshadow", Any)
        state.foreshadows = list(getattr(foreshadow_payload, "foreshadows", []) or [])
        state.long_plan = state._accepted("long_plan", LongPlanPayload)
        state.short_plan = state._accepted("short_plan", ShortPlanPayload)
        return state

    def _accepted(self, artifact_id: str, model_cls: Any) -> Any:
        envelope = storage.load_artifact(self.project, artifact_id)
        if envelope is None:
            return None
        if envelope.accepted_revision is None:
            return None
        if envelope.status.value == "stale":
            # Stale không được dùng cho action phụ thuộc (workflow.md mục 5.2).
            self.stale_artifact_ids.append(artifact_id)
            return None
        if envelope.status.value != "accepted":
            return None
        self.revisions[artifact_id] = envelope.accepted_revision.revision
        payload = envelope.accepted_revision.payload
        if model_cls is Any:
            return payload
        if isinstance(payload, model_cls):
            return payload
        return model_cls.model_validate(payload.model_dump(mode="json"))

    # -- lookups ------------------------------------------------------------

    def chapter(self, chapter_id: str) -> ChapterMetadata | None:
        for chapter in self.chapters:
            if chapter.chapter_id == chapter_id:
                return chapter
        return None

    def chapter_by_number(self, number: int) -> ChapterMetadata | None:
        for chapter in self.chapters:
            if chapter.chapter_number == number:
                return chapter
        return None

    def chapter_plan(self, chapter_id: str) -> ChapterPlan | None:
        if self.short_plan is None:
            return None
        for plan in self.short_plan.chapters:
            if plan.chapter_id == chapter_id:
                return plan
        return None

    def arc(self, arc_id: str) -> tuple[ArcPlan, VolumePlan] | None:
        if self.long_plan is None:
            return None
        for volume in self.long_plan.volumes:
            for arc in volume.arcs:
                if arc.arc_id == arc_id:
                    return arc, volume
        return None

    def arc_of_chapter(self, chapter_id: str) -> str | None:
        if self.short_plan is None:
            return None
        for plan in self.short_plan.chapters:
            if plan.chapter_id == chapter_id:
                return self.short_plan.arc_id
        return None

    def latest_final_reconciled_number(self) -> int:
        """Chương `final_reconciled` cao nhất theo metadata.

        Đây chỉ là **status** của chapter; muốn biết state chain thật còn tới đâu
        (đã trừ phần stale sau retcon) thì dùng `latest_consistent_chapter()`.
        """
        numbers = [
            chapter.chapter_number
            for chapter in self.chapters
            if chapter.status is ChapterStatus.final_reconciled
        ]
        return max(numbers, default=0)

    def latest_consistent_chapter(self) -> int:
        """Chương cao nhất có state chain hợp lệ theo **cả** metadata chapter.

        `latest_consistent_chapter` là field dẫn xuất (storage.md mục 8): khi
        chưa được set (0) thì suy từ `latest_final_chapter`/entry mới nhất, vì
        chưa có gì bị đánh dấu stale. Ngoài ra giá trị này còn bị đối chiếu lại
        với status thật của chapter: sau retcon, timeline có thể vẫn ghi số
        chương cũ trong khi chương đó đã rời `final_reconciled`.
        """
        return consistent_chapter_number(
            timeline=self.timeline,
            relationships=self.relationships,
            reconciled_numbers=reconciled_chapter_numbers(self.chapters),
        )

    def skeleton(self, chapter_id: str):
        envelope = storage.load_artifact(self.project, f"skeleton_{chapter_id}")
        if envelope is None or envelope.accepted_revision is None:
            return None
        if envelope.status.value != "accepted":
            if envelope.status.value == "stale":
                self.stale_artifact_ids.append(f"skeleton_{chapter_id}")
            return None
        self.revisions[f"skeleton_{chapter_id}"] = envelope.accepted_revision.revision
        return envelope.accepted_revision.payload

    def pins(self) -> list[DependencyPin]:
        pins: list[DependencyPin] = []
        if self.base_idea_meta is not None and self.base_idea_meta.revision:
            pins.append(
                DependencyPin(
                    artifact_id="base_idea",
                    revision=self.base_idea_meta.revision,
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
            revision = self.revisions.get(artifact_id)
            if revision is not None:
                pins.append(
                    DependencyPin(artifact_id=artifact_id, revision=revision, scope=scope)
                )
        if self.timeline.latest_final_chapter:
            pins.append(
                DependencyPin(
                    artifact_id="current_timeline",
                    revision=self.timeline.latest_final_chapter,
                    scope="timeline_as_of",
                )
            )
        if self.relationships.relationships:
            pins.append(
                DependencyPin(
                    artifact_id="relationships",
                    revision=max(
                        (item.last_updated_chapter for item in self.relationships.relationships),
                        default=0,
                    )
                    or 1,
                    scope="relationship_as_of",
                )
            )
        return pins

    def reference_index(self):
        return build_reference_index(
            characters=self.characters,
            world_rules=self.world_rules,
            foreshadows=self.foreshadows,
            long_plan_arcs=[
                arc for volume in (self.long_plan.volumes if self.long_plan else []) for arc in volume.arcs
            ],
            long_plan_volumes=self.long_plan.volumes if self.long_plan else [],
            short_plan_chapters=self.short_plan.chapters if self.short_plan else [],
            chapters=self.chapters,
            relationships=self.relationships,
        )


def _load_chapters(project: Project) -> list[ChapterMetadata]:
    chapters: list[ChapterMetadata] = []
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is not None:
            chapters.append(chapter)
    chapters.sort(key=lambda item: item.chapter_number)
    return chapters


def _load_optional(project: Project, path, document_name: str):
    if not path.is_file():
        return None
    return _parse_optional(CoCreateDocument, path)


def _parse_optional(model_cls: Any, path):
    data = storage.read_json(path)
    return model_cls.model_validate(data)


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------


def _select_effective(
    items: Sequence[Any],
    *,
    chapter_number: int,
    kind: str,
    id_of,
    effective_of,
) -> tuple[list[Any], list[ExcludedDueToEffectiveChapter]]:
    included, excluded_raw = partition_by_effective_chapter(
        items,
        chapter_number=chapter_number,
        kind=kind,
        id_of=id_of,
        effective_of=effective_of,
    )
    excluded = [ExcludedDueToEffectiveChapter.model_validate(item) for item in excluded_raw]
    return included, excluded


def _restrict_by_ids(
    items: Sequence[Any],
    ids: Sequence[str] | None,
    *,
    id_of,
) -> list[Any]:
    """Giữ đúng các entry được ID selector chỉ định; None nghĩa là không lọc thêm."""
    if ids is None:
        return list(items)
    wanted = set(ids)
    return [item for item in items if id_of(item) in wanted]


def _timeline_as_of(state: _State, chapter_number: int) -> list[TimelineEntry]:
    return [
        entry
        for entry in state.timeline.entries
        if entry.chapter_number <= chapter_number and not entry.stale
    ]


def _relationships_as_of(state: _State, chapter_number: int):
    result = []
    for relationship in state.relationships.relationships:
        if relationship.stale or relationship.last_updated_chapter > chapter_number:
            continue
        result.append(relationship)
    return result


def _recent_final_summaries(state: _State, *, before_chapter_number: int, limit: int):
    entries = [
        entry
        for entry in state.timeline.entries
        if entry.chapter_number < before_chapter_number and not entry.stale
    ]
    entries.sort(key=lambda item: item.chapter_number, reverse=True)
    return [
        {
            "chapter_id": entry.chapter_id,
            "chapter_number": entry.chapter_number,
            "summary": entry.status,
        }
        for entry in entries[:limit]
    ]


def _previous_final_summary(state: _State, chapter_number: int):
    if chapter_number <= 1:
        return None
    summary = _recent_final_summaries(state, before_chapter_number=chapter_number, limit=1)
    return summary[0] if summary else None


def _base_idea_constraints(state: _State) -> list[str]:
    constraints: list[str] = []
    if state.co_create is not None and state.co_create.idea_state is not None:
        constraints.extend(state.co_create.idea_state.constraints)
    return constraints


def _premise_constraints(state: _State) -> list[str]:
    if state.premise is None:
        return []
    return list(state.premise.hard_constraints)


def _writer_section_projection(section) -> dict[str, Any]:
    """Section Writer nhận: chỉ field writer-safe (context.md mục 6)."""
    projection: dict[str, Any] = {
        "section_id": section.section_id,
        "instruction": section.instruction,
        "writer_notes": list(section.writer_notes),
        "required_beats": list(section.required_beats),
        "forbidden_moves": list(section.forbidden_moves),
        "foreshadow_surfaces": [
            surface.surface_instruction for surface in section.foreshadow_surfaces
        ],
    }
    if section.purpose_visibility.value == "writer_safe":
        projection["purpose"] = section.purpose
    return projection


def _character_writer_projection(character: Character) -> dict[str, Any]:
    return {
        "character_id": character.character_id,
        "display_name": character.display_name,
        "public_profile": character.public_profile.model_dump(mode="json"),
        "writer_profile": _jsonable(character.writer_profile),
    }


def _world_rule_writer_projection(rule: WorldRule) -> dict[str, Any]:
    """Projection writer-safe của một world rule.

    `WorldRule.visibility` quyết định field nào được tới Writer (schemas.md mục 2.3):

    - `writer_safe`: `writer_projection` nếu có, ngược lại `content`;
    - `skeleton_only` / `planner_only` / `author_only`: **không bao giờ** gửi
      `content` (đó là author truth). Chỉ gửi `writer_projection` khi nó được khai
      tường minh, vì field đó là bản diễn đạt an toàn do planner viết.

    Trước đây hàm này fallback thẳng sang `content` cho mọi visibility, nên một rule
    `author_only` có hiệu lực từ chương đang viết sẽ đẩy nguyên secret vào prompt
    Writer; `validate_writer_projection` không bắt được vì nó chỉ soi **tên key**,
    không phân loại được giá trị string.
    """
    projection: dict[str, Any] = {
        "world_rule_id": rule.world_rule_id,
        "summary": rule.summary,
        "boundary": rule.boundary,
    }
    # Không gửi `content` (author truth) trừ khi rule được khai là `writer_safe`.
    # Không có bản diễn đạt an toàn thì bỏ hẳn field, không gửi null để prompt
    # không hiểu nhầm là "rule này không có nội dung".
    if rule.writer_projection is not None:
        projection["writer_projection"] = _jsonable(rule.writer_projection)
    elif rule.visibility is Visibility.writer_safe:
        projection["writer_projection"] = _jsonable(rule.content)
    return projection


def _characters_for_chapter(
    state: _State, chapter_number: int, ids: Sequence[str] | None = None
):
    selected = _restrict_by_ids(state.characters, ids, id_of=lambda item: item.character_id)
    return _select_effective(
        selected,
        chapter_number=chapter_number,
        kind="character",
        id_of=lambda item: item.character_id,
        effective_of=lambda item: item.effective_from_chapter,
    )


def _world_rules_for_chapter(
    state: _State, chapter_number: int, ids: Sequence[str] | None = None
):
    selected = _restrict_by_ids(state.world_rules, ids, id_of=lambda item: item.world_rule_id)
    return _select_effective(
        selected,
        chapter_number=chapter_number,
        kind="world_rule",
        id_of=lambda item: item.world_rule_id,
        effective_of=lambda item: item.effective_from_chapter,
    )


def _foreshadows_for_chapter(
    state: _State, chapter_number: int, ids: Sequence[str] | None = None
):
    selected = _restrict_by_ids(
        state.foreshadows, ids, id_of=lambda item: item.foreshadow_id
    )
    return _select_effective(
        selected,
        chapter_number=chapter_number,
        kind="foreshadow",
        id_of=lambda item: item.foreshadow_id,
        effective_of=lambda item: item.effective_from_chapter,
    )


def _recent_window(state: _State, *, before_chapter_number: int, limit: int):
    return _recent_final_summaries(
        state, before_chapter_number=before_chapter_number, limit=limit
    )


def _context_basis(state: _State, chapter_number: int) -> ContextBasis:
    """`actual` khi đã có state hợp lệ trước N, ngược lại `provisional` + bridge.

    Nền là `latest_consistent_chapter()` chứ không phải chương `final_reconciled`
    cao nhất: sau retcon, các chapter sau chương retcon **vẫn**
    `final_reconciled` về metadata nhưng state chain của chúng đã bị đánh dấu
    stale (`timeline.latest_consistent_chapter` bị hạ xuống). Nếu chỉ nhìn status
    `chapter.json`, basis sẽ khai `actual` cho một chương mà timeline thật chỉ còn
    tới chương retcon, và plan/Skeleton dựng trên state thiếu vẫn accept được.

    `provisional` nghĩa là chương trước **chưa** có state hợp lệ: caller (Skeleton,
    Short Plan) phải giữ `preparation_context` để guard accept từ chối candidate.
    """
    latest_actual = max(state.latest_consistent_chapter(), 0)
    if chapter_number - 1 <= latest_actual:
        return ContextBasis(mode=ContextMode.actual, actual_through_chapter=chapter_number - 1)
    bridge: list[PlannedBridgeEntry] = []
    for number in range(latest_actual + 1, chapter_number):
        chapter = state.chapter_by_number(number)
        plan = (
            state.chapter_plan(chapter.chapter_id)
            if chapter is not None
            else _plan_by_number(state, number)
        )
        if plan is None:
            continue
        bridge.append(
            PlannedBridgeEntry(
                chapter_id=plan.chapter_id,
                chapter_number=plan.chapter_number,
                summary=plan.summary,
            )
        )
    return ContextBasis(
        mode=ContextMode.provisional,
        actual_through_chapter=latest_actual,
        planned_bridge=bridge,
    )


def _plan_by_number(state: _State, number: int) -> ChapterPlan | None:
    if state.short_plan is None:
        return None
    for plan in state.short_plan.chapters:
        if plan.chapter_number == number:
            return plan
    return None


def _require(condition: bool, message: str, *, code: str, **details: Any) -> None:
    if not condition:
        raise ContextError(message, code=code, details=details)


def _finalize_bundle(
    bundle: ContextBundle,
    *,
    sections: Sequence[tuple[str, int, Any]],
    budget_chars: int | None,
    writer_projection: bool = False,
) -> ContextBundle:
    """Áp budget theo priority rồi chốt payload + hash."""
    limit = budget_chars or DEFAULT_CONTEXT_BUDGET_CHARS
    reduced: list[str] = []
    payload: dict[str, Any] = {}
    blockers: list[str] = []

    def size_of(values: Mapping[str, Any]) -> int:
        return len(json.dumps(_jsonable(values), ensure_ascii=False, sort_keys=True))

    # P0 luôn được giữ; P3 rồi P2 bị rút gọn nếu vượt budget.
    kept: list[tuple[str, int, Any]] = []
    for name, priority, value in sections:
        kept.append((name, priority, value))
    current = size_of({name: value for name, _priority, value in kept})
    if current > limit:
        for drop_priority in (3, 2):
            if current <= limit:
                break
            for name, priority, value in list(kept):
                if priority != drop_priority:
                    continue
                kept = [item for item in kept if item[0] != name]
                reduced.append(name)
            current = size_of({name: value for name, _priority, value in kept})
    payload = {name: _jsonable(value) for name, _priority, value in kept}
    final_size = size_of(payload)
    if final_size > limit:
        blockers = [name for name, priority, _value in kept if priority == 0]
        raise ContextError(
            "Context vượt budget và không thể giảm thêm vì phần còn lại là hard constraint P0. "
            "Cần giảm bớt plan/Skeleton hoặc tăng budget.",
            code="context_budget_exceeded",
            details={"blocking_sections": blockers, "estimated_chars": final_size, "limit_chars": limit},
        )
    bundle.payload = payload
    bundle.budget_report = BudgetReport(
        estimated_chars=final_size,
        limit_chars=limit,
        exceeded=False,
        reduced=reduced,
        blocking_sections=blockers,
    )
    bundle.projection_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if writer_projection:
        result = validate_writer_projection(payload, base_path="")
        _require(
            result.is_valid,
            "Writer projection chứa field cấm (author-only/future): " + summarize_errors(result),
            code="secret_leak",
            errors=[item.model_dump(mode="json") for item in result.errors],
        )
    return bundle


def _bundle(kind: str, *, state: _State, **kwargs: Any) -> ContextBundle:
    pins = state.pins()
    warnings: list[str] = []
    if state.stale_artifact_ids:
        warnings.append(
            "Artifact stale không được dùng làm context: "
            + ", ".join(sorted(set(state.stale_artifact_ids)))
        )
    return ContextBundle(
        context_id=f"ctx_{kind}_{hashlib.sha256((kind + now_iso()).encode()).hexdigest()[:12]}",
        context_kind=kind,
        payload={},
        dependency_pins=pins,
        warnings=warnings,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_co_create_context(
    project: Project,
    *,
    user_message: str,
    conversation_summary: str = "",
    budget_chars: int | None = None,
) -> ContextBundle:
    """Co-create chỉ đọc working state, không đọc story artifact khác."""
    state = _State.load(project)
    idea_state = state.co_create.idea_state.model_dump(mode="json") if (
        state.co_create and state.co_create.idea_state
    ) else None
    if not conversation_summary and state.co_create is not None:
        turns = state.co_create.messages[-6:]
        conversation_summary = "\n".join(f"{turn.role}: {turn.content}" for turn in turns)
    bundle = _bundle("co_create", state=state)
    return _finalize_bundle(
        bundle,
        sections=[
            ("language", 0, state.config.default_language),
            ("genre_prompt", 0, state.config.genre_prompt_id),
            ("conversation_summary", 1, conversation_summary),
            ("current_idea_state", 1, idea_state),
            ("user_message", 0, user_message),
        ],
        budget_chars=budget_chars,
    )


def build_foundation_context(
    project: Project,
    artifact_type: str,
    *,
    action: str = "generate",
    chapter_number: int = 1,
    assigned_ids: Sequence[str] | None = None,
    user_instruction: str = "",
    previous: Any = None,
    budget_chars: int | None = None,
) -> ContextBundle:
    """Context cho Premise / Characters / World Rules / Foreshadow.

    Không nhận Writer draft hay prose (context.md mục 4).
    """
    supported = {"premise", "characters", "world_rules", "foreshadow"}
    _require(
        artifact_type in supported,
        f"Foundation context không hỗ trợ artifact_type `{artifact_type}`.",
        code="unknown_context_kind",
    )
    state = _State.load(project)
    _require(
        bool(state.base_idea_markdown.strip()),
        "Base Idea chưa accepted; không thể generate foundation.",
        code="missing_dependency",
    )
    sections: list[tuple[str, int, Any]] = [
        ("action", 0, action),
        ("language", 0, state.config.default_language),
        ("genre_prompt", 0, state.config.genre_prompt_id),
        ("base_idea_markdown", 0, state.base_idea_markdown),
        ("user_instruction", 1, user_instruction),
    ]
    if artifact_type == "premise":
        sections.append(("previous_premise", 2, previous or _payload_dump(state.premise)))
    elif artifact_type == "characters":
        sections.extend(
            [
                ("premise", 1, _payload_dump(state.premise)),
                ("existing_characters", 2, [_jsonable(item) for item in state.characters]),
                ("assigned_character_ids", 0, list(assigned_ids or [])),
                ("effective_from_chapter", 0, chapter_number),
            ]
        )
    elif artifact_type == "world_rules":
        sections.extend(
            [
                ("premise", 1, _payload_dump(state.premise)),
                ("existing_world_rules", 2, [_jsonable(item) for item in state.world_rules]),
                ("assigned_world_rule_ids", 0, list(assigned_ids or [])),
                ("effective_from_chapter", 0, chapter_number),
            ]
        )
    else:
        sections.extend(
            [
                ("premise", 1, _payload_dump(state.premise)),
                ("characters", 1, [_jsonable(item) for item in state.characters]),
                ("world_rules", 1, [_jsonable(item) for item in state.world_rules]),
                ("existing_foreshadows", 2, [_jsonable(item) for item in state.foreshadows]),
                ("assigned_foreshadow_ids", 0, list(assigned_ids or [])),
                ("effective_from_chapter", 0, chapter_number),
            ]
        )
    bundle = _bundle(f"architect_{artifact_type}", state=state)
    return _finalize_bundle(bundle, sections=sections, budget_chars=budget_chars)


def build_long_plan_context(
    project: Project,
    *,
    planning_scope: Mapping[str, int],
    assigned_volume_ids: Sequence[str] | None = None,
    assigned_arc_ids: Sequence[str] | None = None,
    action: str = "generate",
    user_instruction: str = "",
    previous: Any = None,
    budget_chars: int | None = None,
) -> ContextBundle:
    state = _State.load(project)
    _require(
        state.premise is not None,
        "Premise chưa accepted; không thể generate Long Plan.",
        code="missing_dependency",
    )
    start = int(planning_scope.get("start", 1))
    end = int(planning_scope.get("end", start))
    characters, excluded_characters = _characters_for_chapter(state, end)
    rules, excluded_rules = _world_rules_for_chapter(state, end)
    foreshadows, excluded_foreshadows = _foreshadows_for_chapter(state, end)
    bundle = _bundle(
        "long_plan",
        state=state,
        included_ids={
            "characters": [item.character_id for item in characters],
            "world_rules": [item.world_rule_id for item in rules],
            "foreshadows": [item.foreshadow_id for item in foreshadows],
        },
        excluded_due_to_effective_chapter=excluded_characters + excluded_rules + excluded_foreshadows,
    )
    return _finalize_bundle(
        bundle,
        sections=[
            ("action", 0, action),
            ("language", 0, state.config.default_language),
            ("genre_prompt", 0, state.config.genre_prompt_id),
            ("base_idea_markdown", 0, state.base_idea_markdown),
            ("premise", 0, _payload_dump(state.premise)),
            ("characters", 1, [_jsonable(item) for item in characters]),
            ("world_rules", 1, [_jsonable(item) for item in rules]),
            ("foreshadows", 1, [_jsonable(item) for item in foreshadows]),
            ("relationships_as_of", 1, [_jsonable(item) for item in _relationships_as_of(state, end)]),
            ("planning_scope", 0, {"start": start, "end": end}),
            ("assigned_volume_ids", 0, list(assigned_volume_ids or [])),
            ("assigned_arc_ids", 0, list(assigned_arc_ids or [])),
            ("previous_long_plan", 2, previous or _payload_dump(state.long_plan)),
            ("user_instruction", 1, user_instruction),
        ],
        budget_chars=budget_chars,
    )


def build_short_plan_context(
    project: Project,
    *,
    arc_id: str,
    assigned_chapters: Sequence[Mapping[str, Any]],
    chapter_constraints: Sequence[Mapping[str, Any]] = (),
    action: str = "generate",
    user_instruction: str = "",
    previous: Any = None,
    mode: ContextMode | None = None,
    budget_chars: int | None = None,
) -> ContextBundle:
    state = _State.load(project)
    _require(
        state.long_plan is not None,
        "Long Plan chưa accepted; không thể generate Short Plan.",
        code="missing_dependency",
    )
    arc_entry = state.arc(arc_id)
    _require(arc_entry is not None, f"Arc `{arc_id}` không có trong Long Plan accepted.", code="missing_dependency")
    arc, volume = arc_entry  # type: ignore[misc]
    numbers = [int(item["chapter_number"]) for item in assigned_chapters]
    _require(bool(numbers), "Short Plan cần ít nhất một chapter được giao.", code="missing_dependency")
    target_end = max(numbers)
    # Basis của Short Plan là basis **khắt khe nhất** trong range: nếu một chapter
    # được giao vẫn chưa có actual trước nó thì cả candidate là `provisional`
    # (D014), không chỉ chapter đầu range. Lấy `min(numbers)` sẽ bỏ sót trường hợp
    # chương đầu đã final nhưng chương sau thì chưa.
    basis = _context_basis(state, target_end)
    if mode is not None:
        basis = basis.model_copy(update={"mode": mode})
    characters, excluded_characters = _characters_for_chapter(state, target_end, arc.character_ids)
    rules, excluded_rules = _world_rules_for_chapter(state, target_end, arc.world_rule_ids)
    foreshadows, excluded_foreshadows = _foreshadows_for_chapter(
        state, target_end, arc.foreshadow_ids
    )
    bundle = _bundle(
        "short_plan",
        state=state,
        mode=basis.mode,
        preparation_context=PreparationContext(
            context_basis=basis, dependency_pins=state.pins()
        )
        if basis.mode is ContextMode.provisional
        else None,
        included_ids={
            "characters": [item.character_id for item in characters],
            "world_rules": [item.world_rule_id for item in rules],
            "foreshadows": [item.foreshadow_id for item in foreshadows],
        },
        excluded_due_to_effective_chapter=excluded_characters + excluded_rules + excluded_foreshadows,
    )
    return _finalize_bundle(
        bundle,
        sections=[
            ("action", 0, action),
            ("language", 0, state.config.default_language),
            ("base_idea_markdown", 0, state.base_idea_markdown),
            ("premise", 0, _payload_dump(state.premise)),
            ("current_arc", 0, _jsonable(arc)),
            ("characters", 1, [_jsonable(item) for item in characters]),
            ("world_rules", 1, [_jsonable(item) for item in rules]),
            ("foreshadows", 1, [_jsonable(item) for item in foreshadows]),
            ("timeline_as_of", 1, [_jsonable(item) for item in _timeline_as_of(state, basis.actual_through_chapter)]),
            (
                "relationships_as_of",
                1,
                [_jsonable(item) for item in _relationships_as_of(state, basis.actual_through_chapter)],
            ),
            ("recent_finalized_summaries", 2, _recent_window(state, before_chapter_number=min(numbers), limit=RECENT_FINAL_SUMMARIES_DEFAULT)),
            ("assigned_chapters", 0, [_jsonable(dict(item)) for item in assigned_chapters]),
            ("chapter_constraints", 0, [_jsonable(dict(item)) for item in chapter_constraints]),
            ("context_basis", 0, basis.model_dump(mode="json")),
            ("previous_short_plan", 2, previous or _payload_dump(state.short_plan)),
            ("user_instruction", 1, user_instruction),
        ],
        budget_chars=budget_chars,
    )


def build_rolling_context(
    project: Project,
    *,
    arc_id: str,
    eligible_chapters: Sequence[Mapping[str, Any]],
    reviewed_chapter_range: Mapping[str, int],
    allow_relationship_replan: bool,
    user_instruction: str = "",
    budget_chars: int | None = None,
) -> ContextBundle:
    state = _State.load(project)
    _require(
        state.short_plan is not None,
        "Short Plan chưa accepted; Rolling Plan cần plan hiện hành.",
        code="missing_dependency",
    )
    arc_entry = state.arc(arc_id)
    _require(arc_entry is not None, f"Arc `{arc_id}` không có trong Long Plan accepted.", code="missing_dependency")
    arc, _volume = arc_entry  # type: ignore[misc]
    latest_consistent = state.latest_consistent_chapter()
    start = int(reviewed_chapter_range.get("start", 1))
    bundle = _bundle("rolling_plan", state=state)
    return _finalize_bundle(
        bundle,
        sections=[
            ("current_arc", 0, _jsonable(arc)),
            ("current_short_plan", 0, _payload_dump(state.short_plan)),
            ("timeline_as_of", 0, [_jsonable(item) for item in _timeline_as_of(state, latest_consistent)]),
            (
                "relationships_as_of",
                0,
                [_jsonable(item) for item in _relationships_as_of(state, latest_consistent)],
            ),
            ("recent_finalized_summaries", 2, _recent_window(state, before_chapter_number=start + 1, limit=RECENT_FINAL_SUMMARIES_DEFAULT)),
            ("reviewed_chapter_range", 0, {"start": start, "end": int(reviewed_chapter_range.get("end", start))}),
            ("latest_consistent_chapter", 0, latest_consistent),
            ("eligible_chapters", 0, [_jsonable(dict(item)) for item in eligible_chapters]),
            ("allow_relationship_replan", 0, allow_relationship_replan),
            ("user_instruction", 1, user_instruction),
        ],
        budget_chars=budget_chars,
    )


def build_skeleton_context(
    project: Project,
    *,
    chapter_id: str,
    assigned_section_ids: Sequence[str] | None = None,
    action: str = "generate",
    user_instruction: str = "",
    previous: Any = None,
    mode: ContextMode | None = None,
    budget_chars: int | None = None,
) -> ContextBundle:
    """Skeleton được biết secret để thiết kế surface, nhưng phải ghi visibility."""
    state = _State.load(project)
    plan = state.chapter_plan(chapter_id)
    _require(
        plan is not None,
        f"Chapter `{chapter_id}` không có trong Short Plan accepted.",
        code="missing_dependency",
    )
    chapter_number = plan.chapter_number
    basis = _context_basis(state, chapter_number)
    if mode is not None:
        basis = basis.model_copy(update={"mode": mode})
    characters, excluded_characters = _characters_for_chapter(
        state, chapter_number, plan.character_ids
    )
    rules, excluded_rules = _world_rules_for_chapter(state, chapter_number, plan.world_rule_ids)
    foreshadows, excluded_foreshadows = _foreshadows_for_chapter(
        state, chapter_number, plan.foreshadow_ids
    )
    arc_entry = state.arc(state.short_plan.arc_id) if state.short_plan else None
    bundle = _bundle(
        "skeleton",
        state=state,
        for_chapter_id=chapter_id,
        for_chapter_number=chapter_number,
        mode=basis.mode,
        preparation_context=PreparationContext(context_basis=basis, dependency_pins=state.pins())
        if basis.mode is ContextMode.provisional
        else None,
        included_ids={
            "characters": [item.character_id for item in characters],
            "world_rules": [item.world_rule_id for item in rules],
            "foreshadows": [item.foreshadow_id for item in foreshadows],
        },
        excluded_due_to_effective_chapter=excluded_characters + excluded_rules + excluded_foreshadows,
    )
    return _finalize_bundle(
        bundle,
        sections=[
            ("action", 0, action),
            ("base_idea_constraints", 0, _base_idea_constraints(state)),
            ("premise_constraints", 0, _premise_constraints(state)),
            ("current_arc", 1, _jsonable(arc_entry[0]) if arc_entry else None),
            ("chapter_plan", 0, _jsonable(plan)),
            ("timeline_as_of", 1, [_jsonable(item) for item in _timeline_as_of(state, basis.actual_through_chapter)]),
            (
                "relationships_as_of",
                1,
                [_jsonable(item) for item in _relationships_as_of(state, basis.actual_through_chapter)],
            ),
            ("previous_final_summary", 2, _previous_final_summary(state, chapter_number)),
            ("characters", 1, [_jsonable(item) for item in characters]),
            ("world_rules", 1, [_jsonable(item) for item in rules]),
            ("foreshadows", 1, [_jsonable(item) for item in foreshadows]),
            ("assigned_section_ids", 0, list(assigned_section_ids or [])),
            ("context_basis", 0, basis.model_dump(mode="json")),
            ("previous_skeleton", 2, previous or _payload_dump(state.skeleton(chapter_id))),
            ("user_instruction", 1, user_instruction),
        ],
        budget_chars=budget_chars,
    )


def build_writer_context(
    project: Project,
    *,
    chapter_id: str,
    user_instruction: str = "",
    budget_chars: int | None = None,
) -> ContextBundle:
    """Projection nhỏ và an toàn cho Writer (context.md mục 6)."""
    state = _State.load(project)
    chapter = state.chapter(chapter_id)
    _require(chapter is not None, f"Chapter `{chapter_id}` không tồn tại.", code="missing_chapter")
    chapter_number = chapter.chapter_number
    skeleton = state.skeleton(chapter_id)
    _require(
        skeleton is not None,
        "Writer chỉ chạy khi Skeleton accepted và còn hiệu lực.",
        code="missing_dependency",
    )
    _require(
        chapter.skeleton_pin is not None,
        "Chapter chưa được đánh dấu `skeleton_ready`; Skeleton chưa gắn với chapter này.",
        code="missing_dependency",
    )
    _require(
        chapter.skeleton_pin.revision == state.revisions.get(f"skeleton_{chapter_id}"),
        "Skeleton pin của chapter lệch với Skeleton accepted hiện tại; cần review/reaccept.",
        code="stale_dependency",
    )
    if chapter_number > 1:
        previous = state.chapter_by_number(chapter_number - 1)
        _require(
            previous is not None and previous.status is ChapterStatus.final_reconciled,
            f"Chương {chapter_number - 1} chưa `final_reconciled`; Writer chương {chapter_number} bị khóa.",
            code="stale_dependency",
        )
        latest_consistent = state.latest_consistent_chapter()
        _require(
            latest_consistent >= chapter_number - 1,
            "State chain sau retcon chưa được rebuild tới chương trước; Writer bị khóa.",
            code="stale_dependency",
        )
    basis = _context_basis(state, chapter_number)
    _require(
        basis.mode is ContextMode.actual,
        "Writer cần state actual trước chương; candidate provisional không được gửi Writer.",
        code="provisional_not_writer_ready",
    )
    characters, excluded_characters = _characters_for_chapter(state, chapter_number)
    rules, excluded_rules = _world_rules_for_chapter(state, chapter_number)
    bundle = _bundle(
        "writer",
        state=state,
        for_chapter_id=chapter_id,
        for_chapter_number=chapter_number,
        mode=ContextMode.actual,
        included_ids={
            "characters": [item.character_id for item in characters],
            "world_rules": [item.world_rule_id for item in rules],
            "timeline_entries": [
                entry.timeline_id for entry in _timeline_as_of(state, chapter_number - 1)
            ],
        },
        excluded_due_to_effective_chapter=excluded_characters + excluded_rules,
    )
    return _finalize_bundle(
        bundle,
        sections=[
            ("chapter_id", 0, chapter_id),
            ("chapter_number", 0, chapter_number),
            ("title", 0, chapter.title),
            ("base_idea_constraints", 0, _base_idea_constraints(state)),
            ("premise_constraints", 0, _premise_constraints(state)),
            (
                "skeleton",
                0,
                {
                    "sections": [_writer_section_projection(item) for item in skeleton.sections],
                    "global_constraints": list(skeleton.global_constraints),
                },
            ),
            ("characters", 1, [_character_writer_projection(item) for item in characters]),
            ("world_rules", 1, [_world_rule_writer_projection(item) for item in rules]),
            (
                "timeline_as_of",
                1,
                [_jsonable(item) for item in _timeline_as_of(state, chapter_number - 1)],
            ),
            (
                "relationships_as_of",
                1,
                [_jsonable(item) for item in _relationships_as_of(state, chapter_number - 1)],
            ),
            ("previous_final_summary", 2, _previous_final_summary(state, chapter_number)),
            ("style", 0, state.config.writing_style_id),
            ("user_instruction", 1, user_instruction),
        ],
        budget_chars=budget_chars,
        writer_projection=True,
    )


def build_review_context(
    project: Project,
    *,
    chapter_id: str,
    prose_revision: int,
    prose_markdown: str,
    review_focus: str = "",
    budget_chars: int | None = None,
) -> ContextBundle:
    state = _State.load(project)
    chapter = state.chapter(chapter_id)
    _require(chapter is not None, f"Chapter `{chapter_id}` không tồn tại.", code="missing_chapter")
    chapter_number = chapter.chapter_number
    skeleton = state.skeleton(chapter_id)
    characters, excluded_characters = _characters_for_chapter(state, chapter_number)
    rules, excluded_rules = _world_rules_for_chapter(state, chapter_number)
    constraint_sources = [
        {
            "authority_kind": "base_idea",
            "artifact_id": "base_idea",
            "revision": state.base_idea_meta.revision if state.base_idea_meta else 1,
            "content": _base_idea_constraints(state),
        },
        {
            "authority_kind": "premise",
            "artifact_id": "premise",
            "revision": state.revisions.get("premise", 1),
            "content": _premise_constraints(state),
        },
        {
            "authority_kind": "skeleton",
            "artifact_id": f"skeleton_{chapter_id}",
            "revision": state.revisions.get(f"skeleton_{chapter_id}", 1),
            "content": {
                "global_constraints": list(skeleton.global_constraints) if skeleton else [],
                "sections": [_writer_section_projection(item) for item in skeleton.sections]
                if skeleton
                else [],
            },
        },
    ]
    bundle = _bundle(
        "review",
        state=state,
        for_chapter_id=chapter_id,
        for_chapter_number=chapter_number,
        included_ids={
            "characters": [item.character_id for item in characters],
            "world_rules": [item.world_rule_id for item in rules],
        },
        excluded_due_to_effective_chapter=excluded_characters + excluded_rules,
    )
    return _finalize_bundle(
        bundle,
        sections=[
            ("chapter_id", 0, chapter_id),
            ("prose_revision", 0, prose_revision),
            ("prose_markdown", 0, prose_markdown),
            ("constraint_sources", 0, constraint_sources),
            (
                "skeleton",
                0,
                {
                    "sections": [_writer_section_projection(item) for item in skeleton.sections],
                    "global_constraints": list(skeleton.global_constraints),
                }
                if skeleton
                else None,
            ),
            ("characters", 1, [_character_writer_projection(item) for item in characters]),
            ("world_rules", 1, [_world_rule_writer_projection(item) for item in rules]),
            (
                "timeline_as_of",
                1,
                [_jsonable(item) for item in _timeline_as_of(state, chapter_number - 1)],
            ),
            (
                "relationships_as_of",
                1,
                [_jsonable(item) for item in _relationships_as_of(state, chapter_number - 1)],
            ),
            ("previous_final_summary", 2, _previous_final_summary(state, chapter_number)),
            ("style", 0, state.config.writing_style_id),
            ("review_focus", 1, review_focus),
        ],
        budget_chars=budget_chars,
        writer_projection=True,
    )


def build_rewrite_context(
    project: Project,
    *,
    request: RewriteSectionRequest,
    target_markdown: str,
    surrounding_before: str = "",
    surrounding_after: str = "",
    budget_chars: int | None = None,
) -> ContextBundle:
    state = _State.load(project)
    chapter = state.chapter(request.chapter_id)
    _require(chapter is not None, f"Chapter `{request.chapter_id}` không tồn tại.", code="missing_chapter")
    chapter_number = chapter.chapter_number
    skeleton = state.skeleton(request.chapter_id)
    characters, excluded_characters = _characters_for_chapter(state, chapter_number)
    rules, excluded_rules = _world_rules_for_chapter(state, chapter_number)
    bundle = _bundle(
        "rewrite_section",
        state=state,
        for_chapter_id=request.chapter_id,
        for_chapter_number=chapter_number,
        included_ids={
            "characters": [item.character_id for item in characters],
            "world_rules": [item.world_rule_id for item in rules],
        },
        excluded_due_to_effective_chapter=excluded_characters + excluded_rules,
    )
    return _finalize_bundle(
        bundle,
        sections=[
            ("request", 0, request.model_dump(mode="json")),
            ("target_markdown", 0, target_markdown),
            ("surrounding_before", 2, surrounding_before),
            ("surrounding_after", 2, surrounding_after),
            (
                "skeleton",
                0,
                {
                    "sections": [_writer_section_projection(item) for item in skeleton.sections],
                    "global_constraints": list(skeleton.global_constraints),
                }
                if skeleton
                else None,
            ),
            ("base_idea_constraints", 0, _base_idea_constraints(state)),
            ("premise_constraints", 0, _premise_constraints(state)),
            ("characters", 1, [_character_writer_projection(item) for item in characters]),
            ("world_rules", 1, [_world_rule_writer_projection(item) for item in rules]),
            (
                "timeline_as_of",
                1,
                [_jsonable(item) for item in _timeline_as_of(state, chapter_number - 1)],
            ),
            (
                "relationships_as_of",
                1,
                [_jsonable(item) for item in _relationships_as_of(state, chapter_number - 1)],
            ),
            ("style", 0, state.config.writing_style_id),
        ],
        budget_chars=budget_chars,
        writer_projection=True,
    )


def build_reconcile_context(
    project: Project,
    *,
    chapter_id: str,
    final_candidate_markdown: str,
    budget_chars: int | None = None,
) -> ContextBundle:
    state = _State.load(project)
    chapter = state.chapter(chapter_id)
    _require(chapter is not None, f"Chapter `{chapter_id}` không tồn tại.", code="missing_chapter")
    chapter_number = chapter.chapter_number
    characters, excluded_characters = _characters_for_chapter(state, chapter_number)
    bundle = _bundle(
        "reconcile",
        state=state,
        for_chapter_id=chapter_id,
        for_chapter_number=chapter_number,
        included_ids={"characters": [item.character_id for item in characters]},
        excluded_due_to_effective_chapter=excluded_characters,
    )
    return _finalize_bundle(
        bundle,
        sections=[
            ("chapter_id", 0, chapter_id),
            ("chapter_number", 0, chapter_number),
            (
                "source_final_candidate",
                0,
                {
                    "prose_revision": chapter.final_candidate.prose_revision
                    if chapter.final_candidate
                    else chapter.current_draft_revision or 0,
                    "markdown_ref": chapter.final_candidate.markdown_ref
                    if chapter.final_candidate
                    else "",
                },
            ),
            ("final_candidate_markdown", 0, final_candidate_markdown),
            (
                "timeline_as_of",
                0,
                [_jsonable(item) for item in _timeline_as_of(state, chapter_number - 1)],
            ),
            (
                "relationships_as_of",
                0,
                [_jsonable(item) for item in _relationships_as_of(state, chapter_number - 1)],
            ),
            (
                "known_characters",
                1,
                [
                    {
                        "character_id": item.character_id,
                        "display_name": item.display_name,
                        "aliases": list(item.aliases),
                    }
                    for item in characters
                ],
            ),
        ],
        budget_chars=budget_chars,
    )


def build_retcon_impact_context(
    project: Project,
    *,
    source_change: SourceChange,
    before_content: str,
    after_content: str,
    downstream_items: Sequence[Mapping[str, Any]],
    analysis_scope: str = "",
    budget_chars: int | None = None,
) -> ContextBundle:
    state = _State.load(project)
    change_chapter_number = 0
    if source_change.item_kind.startswith("chapter"):
        chapter = state.chapter(source_change.item_id)
        if chapter is not None:
            change_chapter_number = chapter.chapter_number
    return _finalize_bundle(
        _bundle(
            "retcon_impact",
            state=state,
            for_chapter_id=source_change.item_id if change_chapter_number else None,
            for_chapter_number=change_chapter_number or None,
            mode=ContextMode.actual,
        ),
        sections=[
            ("source_change", 0, source_change.model_dump(mode="json")),
            ("before_content", 0, before_content),
            ("after_content", 0, after_content),
            ("downstream_items", 1, [_jsonable(dict(item)) for item in downstream_items]),
            (
                "state_before_change",
                0,
                {
                    "timeline_as_of": [
                        _jsonable(item)
                        for item in _timeline_as_of(state, max(change_chapter_number - 1, 0))
                    ],
                    "relationships_as_of": [
                        _jsonable(item)
                        for item in _relationships_as_of(state, max(change_chapter_number - 1, 0))
                    ],
                },
            ),
            ("analysis_scope", 1, analysis_scope),
        ],
        budget_chars=budget_chars,
    )


def _payload_dump(payload: Any) -> Any:
    if payload is None:
        return None
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        return _jsonable(dump(mode="json"))
    return _jsonable(payload)


__all__ = [
    "BudgetReport",
    "ContextBundle",
    "ContextError",
    "DEFAULT_CONTEXT_BUDGET_CHARS",
    "RECENT_FINAL_SUMMARIES_DEFAULT",
    "build_co_create_context",
    "build_foundation_context",
    "build_long_plan_context",
    "build_reconcile_context",
    "build_reconcile_context",
    "build_retcon_impact_context",
    "build_review_context",
    "build_rewrite_context",
    "build_rolling_context",
    "build_short_plan_context",
    "build_skeleton_context",
    "build_writer_context",
]
