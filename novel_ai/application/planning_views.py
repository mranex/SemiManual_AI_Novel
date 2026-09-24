"""Read model và lựa chọn planning/foundation thuần Python."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novel_ai.core.models import (ArcPlan, ArtifactStatus, ChapterPlan, LongPlanPayload, RollingPatchPayload, ShortPlanPayload, VolumePlan)
from novel_ai.core.project import Project
from novel_ai.application.workspace_state import envelope_or_none


def compact_json(value: Any) -> str:
    """JSON ngắn để hiển thị contract từng chapter."""
    data = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return json.dumps(data, ensure_ascii=False, sort_keys=True)

ID_KIND_LABELS: dict[str, str] = {
    "char": "nhân vật",
    "rule": "world rule",
    "fs": "foreshadow",
    "vol": "volume",
    "arc": "arc",
    "ch": "chapter",
    "section": "section",
    "rel": "quan hệ",
}


def build_id_index(project: Project) -> dict[str, str]:
    """`entity_id -> nhãn dễ đọc` từ accepted foundation (không đọc candidate).

    Dùng để UI hiển thị `char_0001 (Sở Dương)` thay vì chỉ ID trần.
    """
    index: dict[str, str] = {}
    characters = envelope_or_none(project, "characters")
    if characters is not None and characters.accepted_revision is not None:
        for item in getattr(characters.accepted_revision.payload, "characters", []) or []:
            index[item.character_id] = item.display_name
    rules = envelope_or_none(project, "world_rules")
    if rules is not None and rules.accepted_revision is not None:
        for item in getattr(rules.accepted_revision.payload, "world_rules", []) or []:
            index[item.world_rule_id] = item.summary or item.category
    foreshadows = envelope_or_none(project, "foreshadow")
    if foreshadows is not None and foreshadows.accepted_revision is not None:
        for item in getattr(foreshadows.accepted_revision.payload, "foreshadows", []) or []:
            index[item.foreshadow_id] = item.label
    return index


#: Tên field selector → prefix stable ID hợp lệ trong accepted index.
SELECTOR_PREFIXES: dict[str, str] = {
    "character_ids": "char",
    "world_rule_ids": "rule",
    "foreshadow_ids": "fs",
}


def selector_options(project: Project) -> dict[str, list[str]]:
    """`{field_name: [ID đúng loại]}` cho `editor.render_model_form(selectors=...)`.

    Selector chỉ được chứa ID **đúng loại** (T39): trước đây page truyền cả index
    trộn nên user có thể chọn `rule_0001` cho `character_ids` rồi bị validator từ
    chối. Danh sách vẫn lấy từ accepted foundation, không từ candidate.
    """
    index = build_id_index(project)
    options: dict[str, list[str]] = {}
    for field, prefix in SELECTOR_PREFIXES.items():
        options[field] = sorted(
            entity_id for entity_id in index if str(entity_id).startswith(f"{prefix}_")
        )
    return options


def format_id(entity_id: str, index: Mapping[str, str] | None = None) -> str:
    """`char_0001` -> `char_0001 (nhân vật: Sở Dương)` khi có trong index."""
    label = (index or {}).get(str(entity_id))
    prefix = str(entity_id).split("_", 1)[0]
    kind = ID_KIND_LABELS.get(prefix)
    parts = [part for part in (kind, label) if part]
    if not parts:
        return str(entity_id)
    return f"{entity_id} ({' · '.join(parts)})"


def format_id_list(ids: Sequence[str], index: Mapping[str, str] | None = None) -> str:
    """Danh sách ID dễ đọc; rỗng thì nói rõ chưa tham chiếu entity nào."""
    if not ids:
        return "— (không tham chiếu ID nào)"
    return ", ".join(format_id(item, index) for item in ids)


def format_chapter_option(chapter_id: str, chapter_number: int, *, title: str = "") -> str:
    """Nhãn lựa chọn chapter: số chương trước, ID sau, kèm tiêu đề nếu có."""
    suffix = f" — {title}" if title else ""
    return f"Chương {chapter_number} · `{chapter_id}`{suffix}"


def _arc_entries(payload: LongPlanPayload | None) -> list[tuple[VolumePlan, ArcPlan]]:
    if payload is None:
        return []
    return [
        (volume, arc)
        for volume in payload.volumes
        for arc in volume.arcs
    ]


def accepted_long_plan(project: Project) -> LongPlanPayload | None:
    """Payload Long Plan accepted (None nếu chưa có hoặc đang stale/chưa accept)."""
    envelope = envelope_or_none(project, "long_plan")
    if (
        envelope is None
        or envelope.accepted_revision is None
        or envelope.status is not ArtifactStatus.accepted
    ):
        return None
    return envelope.accepted_revision.payload


def arc_options(project: Project) -> list[dict[str, Any]]:
    """Lựa chọn arc (kèm volume) từ Long Plan accepted; rỗng nếu chưa có.

    Hàm thuần: page dùng để dựng selectbox, test dùng để kiểm tra nhãn và phạm vi
    chương mà arc cho phép.
    """
    payload = accepted_long_plan(project)
    result: list[dict[str, Any]] = []
    for volume, arc in _arc_entries(payload):
        result.append(
            {
                "arc_id": arc.arc_id,
                "volume_id": volume.volume_id,
                "label": (
                    f"{volume.title} · {arc.title} · `{arc.arc_id}` "
                    f"(ch.{arc.chapter_range.start}–{arc.chapter_range.end})"
                ),
                "chapter_start": arc.chapter_range.start,
                "chapter_end": arc.chapter_range.end,
            }
        )
    return result


def volume_arc_options(project: Project) -> list[dict[str, Any]]:
    """Lựa chọn volume/arc cho Long Plan: mọi volume + arc của **candidate/accepted**."""
    envelope = envelope_or_none(project, "long_plan")
    payload = None
    if envelope is not None:
        revision = envelope.accepted_revision or envelope.candidate_revision
        payload = revision.payload if revision is not None else None
    result: list[dict[str, Any]] = []
    for volume, arc in _arc_entries(payload):
        result.append(
            {
                "volume_id": volume.volume_id,
                "volume_title": volume.title,
                "arc_id": arc.arc_id,
                "arc_title": arc.title,
                "chapter_start": arc.chapter_range.start,
                "chapter_end": arc.chapter_range.end,
                "label": (
                    f"{volume.title} · {arc.title} · `{arc.arc_id}` · "
                    f"ch.{arc.chapter_range.start}–{arc.chapter_range.end}"
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Short Plan: chapter constraints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChapterConstraintDraft:
    """Yêu cầu viết user nhập cho một assigned chapter (chưa validate)."""

    chapter_id: str
    chapter_number: int
    language: str = ""
    pov: str = ""
    length_guidance: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.language or self.pov or self.length_guidance)

    def as_payload(self) -> dict[str, Any]:
        """Object `{chapter_id, language, pov, length_guidance}` cho service."""
        return {
            "chapter_id": self.chapter_id,
            "language": self.language,
            "pov": self.pov,
            "length_guidance": self.length_guidance,
        }

    def as_label(self) -> str:
        parts = [
            value
            for value in (
                f"language: {self.language}" if self.language else "",
                f"pov: {self.pov}" if self.pov else "",
                f"length: {self.length_guidance}" if self.length_guidance else "",
            )
            if value
        ]
        base = f"Chương {self.chapter_number} · `{self.chapter_id}`"
        return f"{base} — {'; '.join(parts)}" if parts else f"{base} — (chưa đặt yêu cầu)"


def build_chapter_constraints(
    assigned_chapters: Sequence[Mapping[str, Any]],
    values: Mapping[str, Mapping[str, Any]],
    *,
    default_language: str = "",
) -> list[ChapterConstraintDraft]:
    """Dựng danh sách `ChapterConstraintDraft` cho các chapter được giao.

    `values` là `chapter_id -> {language, pov, length_guidance}` do UI thu được.
    Chapter không có giá trị nào thì nhận `default_language` của project (không
    tự bịa pov/length).
    """
    drafts: list[ChapterConstraintDraft] = []
    for item in assigned_chapters:
        chapter_id = str(item["chapter_id"])
        raw = values.get(chapter_id, {})
        language = str(raw.get("language", "") or "").strip() or default_language
        drafts.append(
            ChapterConstraintDraft(
                chapter_id=chapter_id,
                chapter_number=int(item["chapter_number"]),
                language=language,
                pov=str(raw.get("pov", "") or "").strip(),
                length_guidance=str(raw.get("length_guidance", "") or "").strip(),
            )
        )
    return drafts


def chapter_constraints_payload(
    drafts: Sequence[ChapterConstraintDraft],
) -> list[dict[str, Any]]:
    """Payload `chapter_constraints` gửi service (bỏ chapter không có yêu cầu nào)."""
    return [draft.as_payload() for draft in drafts if not draft.is_empty]


def chapter_constraints_summary(
    drafts: Sequence[ChapterConstraintDraft],
) -> list[str]:
    """Nhãn từng chapter + yêu cầu viết để hiển thị lại trước khi generate."""
    return [draft.as_label() for draft in drafts]


def assigned_chapter_rows(
    chapters: Sequence[ChapterPlan] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Chuẩn hoá assigned chapters (object service cấp) thành dict hiển thị."""
    rows: list[dict[str, Any]] = []
    for item in chapters:
        if isinstance(item, Mapping):
            rows.append(
                {
                    "chapter_id": str(item["chapter_id"]),
                    "chapter_number": int(item["chapter_number"]),
                    "title": str(item.get("title", "")),
                }
            )
        else:
            rows.append(
                {
                    "chapter_id": item.chapter_id,
                    "chapter_number": item.chapter_number,
                    "title": item.title,
                }
            )
    return sorted(rows, key=lambda row: row["chapter_number"])


def short_plan_reference_lines(
    payload: ShortPlanPayload, index: Mapping[str, str] | None = None
) -> list[str]:
    """Mô tả từng ChapterPlan của Short Plan với ID dễ đọc (character/rule/foreshadow)."""
    lines: list[str] = []
    for chapter in payload.chapters:
        lines.append(
            f"**Chương {chapter.chapter_number}** · `{chapter.chapter_id}` — {chapter.title}"
        )
        lines.append(f"  - Tóm tắt: {chapter.summary}")
        lines.append(f"  - Mục tiêu: {chapter.chapter_goal}")
        if chapter.hook:
            lines.append(f"  - Hook: {chapter.hook}")
        lines.append(f"  - Nhân vật: {format_id_list(chapter.character_ids, index)}")
        lines.append(f"  - World rule: {format_id_list(chapter.world_rule_ids, index)}")
        lines.append(f"  - Foreshadow: {format_id_list(chapter.foreshadow_ids, index)}")
        if chapter.relationship_changes:
            lines.append(
                "  - Hướng quan hệ (tương lai): "
                + "; ".join(
                    f"{format_id_list(item.character_ids, index)} → {item.target_state}"
                    for item in chapter.relationship_changes
                )
            )
        for item in chapter.outline:
            if isinstance(item, Mapping):
                lines.append(f"  - Contract viết: {compact_json(item)}")
            else:
                lines.append(f"  - Beat: {item}")
    return lines


# ---------------------------------------------------------------------------
# Rolling Plan: view trước khi apply
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollingChangeView:
    """Một thay đổi Short Plan mà Rolling proposal muốn áp."""

    chapter_id: str
    reason: str
    fields: tuple[str, ...]
    payload: dict[str, Any] = field(default_factory=dict)

    def as_label(self, index: Mapping[str, str] | None = None) -> str:
        return (
            f"`{format_id(self.chapter_id, index)}` — đổi {', '.join(self.fields) or '(không rõ field)'}"
            f" · lý do: {self.reason}"
        )


@dataclass(frozen=True)
class RollingView:
    """Toàn bộ thông tin Rolling cần hiển thị **trước khi** user bấm apply."""

    reviewed_range: tuple[int, int] | None
    eligible_chapters: tuple[dict[str, Any], ...]
    deviations: tuple[dict[str, Any], ...]
    short_plan_changes: tuple[RollingChangeView, ...]
    relationship_changes: tuple[RollingChangeView, ...]
    blocked_by_authority: tuple[dict[str, Any], ...]
    allow_relationship_replan: bool
    proposal_status: str

    @property
    def touches_only_future(self) -> bool:
        """True nếu mọi target nằm trong eligible scope (chỉ future Short Plan)."""
        eligible = {str(item["chapter_id"]) for item in self.eligible_chapters}
        targets = {change.chapter_id for change in self.short_plan_changes}
        targets |= {change.chapter_id for change in self.relationship_changes}
        return targets.issubset(eligible)

    @property
    def out_of_scope_targets(self) -> tuple[str, ...]:
        eligible = {str(item["chapter_id"]) for item in self.eligible_chapters}
        targets = {change.chapter_id for change in self.short_plan_changes}
        targets |= {change.chapter_id for change in self.relationship_changes}
        return tuple(sorted(targets - eligible))

    def summary(self) -> str:
        parts = [
            f"{len(self.short_plan_changes)} thay đổi Short Plan",
            f"{len(self.relationship_changes)} thay đổi hướng quan hệ",
            f"{len(self.deviations)} deviation",
            f"{len(self.blocked_by_authority)} mục bị authority chặn",
        ]
        return " · ".join(parts)


def _change_fields(changes: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(str(key) for key in changes))


def build_rolling_view(
    patch: RollingPatchPayload,
    *,
    eligible_chapters: Sequence[Mapping[str, Any]] = (),
    allow_relationship_replan: bool = True,
) -> RollingView:
    """Dựng view Rolling từ payload proposal + eligible scope hiện tại.

    Hàm thuần: page dùng để hiển thị deviations và **phạm vi future changes**
    trước khi user bấm apply; test dùng để assert target ngoài scope bị nêu rõ.
    """
    range_ = patch.reviewed_chapter_range
    return RollingView(
        reviewed_range=(range_.start, range_.end),
        eligible_chapters=tuple(dict(item) for item in eligible_chapters),
        deviations=tuple(item.model_dump(mode="json") for item in patch.deviations),
        short_plan_changes=tuple(
            RollingChangeView(
                chapter_id=item.chapter_id,
                reason=item.reason,
                fields=_change_fields(item.changes),
                payload=dict(item.changes),
            )
            for item in patch.short_plan_changes
        ),
        relationship_changes=tuple(
            RollingChangeView(
                chapter_id=item.chapter_id,
                reason=item.reason,
                fields=("relationship_changes",),
                payload={
                    "relationship_changes": [
                        direction.model_dump(mode="json")
                        for direction in item.relationship_changes
                    ]
                },
            )
            for item in patch.relationship_plan_changes
        ),
        blocked_by_authority=tuple(
            item.model_dump(mode="json") for item in patch.blocked_by_authority
        ),
        allow_relationship_replan=bool(allow_relationship_replan),
        proposal_status=patch.status.value,
    )
