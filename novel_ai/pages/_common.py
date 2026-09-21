"""Helper dùng chung cho các page T20–T22.

Module này giữ hai loại helper:

1. **Hàm thuần** (không import Streamlit): chọn envelope/revision để hiển thị,
   serialize payload để user sửa, dựng input từ session state, format ID dễ đọc,
   và dựng view Rolling Plan. Nhờ vậy phần logic "page dựng input gì" test được
   bằng `pytest` thường, không cần AppTest.
2. **Hàm render nhỏ** dùng Streamlit: hiển thị lỗi service theo field, giữ dữ
   liệu đang sửa khi validation lỗi, và khối cảnh báo/ghi chú.

Luật giữ nguyên trong module:

- không đọc/ghi file project (chỉ gọi `novel_ai.core.storage` để **đọc** state
  hiển thị, giống `novel_ai.ui.project_tree`);
- không gọi LLM, không mutate state, không chạy luật lifecycle;
- không tự sửa state để che lỗi service.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novel_ai.core import storage
from novel_ai.core.models import (
    ArcPlan,
    ArtifactEnvelope,
    ArtifactStatus,
    ChapterPlan,
    LongPlanPayload,
    RollingPatchPayload,
    ShortPlanPayload,
    VolumePlan,
)
from novel_ai.core.project import Project
from novel_ai.services import (
    GuardError,
    LLMUnavailableError,
    ServiceError,
    ValidationFailure,
)

__all__ = [
    "AUTO_ACCEPT_NOTE_STRUCTURED",
    "AUTO_ACCEPT_NOTE_UNRELATED_TO_PROSE",
    "ID_KIND_LABELS",
    "STATUS_LABELS",
    "ChapterConstraintDraft",
    "RollingChangeView",
    "RollingView",
    "accepted_long_plan",
    "append_entries_payload",
    "arc_options",
    "artifact_caption",
    "artifact_stale_notes",
    "assigned_chapter_rows",
    "build_chapter_constraints",
    "build_id_index",
    "build_rolling_view",
    "candidate_update_hint",
    "candidate_update_hint_for_envelope",
    "chapter_constraints_payload",
    "chapter_constraints_summary",
    "clear_session_keys",
    "compact_json",
    "editor_key",
    "envelope_candidate_json_text",
    "envelope_or_none",
    "format_chapter_option",
    "format_id",
    "format_id_list",
    "parse_json_payload",
    "payload_json_text",
    "render_error_list",
    "render_service_error",
    "selected_revision_text",
    "short_plan_reference_lines",
    "sync_text_editor",
    "volume_arc_options",
    "working_state_from_values",
]

#: Nhãn trạng thái lifecycle cho UI (giữ nguyên thuật ngữ contract).
STATUS_LABELS: dict[str, str] = {
    ArtifactStatus.missing.value: "chưa có revision",
    ArtifactStatus.draft.value: "candidate (chưa accept)",
    ArtifactStatus.accepted.value: "accepted",
    ArtifactStatus.stale.value: "stale (cần review/reaccept)",
    ArtifactStatus.rejected.value: "rejected",
}

#: Nhãn tiếng Việt cho prefix stable ID, để UI không hiển thị `char_0001` trống nghĩa.
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

#: Câu giải thích Auto Accept dùng lại ở mọi page foundation/planning.
AUTO_ACCEPT_NOTE_STRUCTURED = (
    "Auto Accept structured (`project.config.auto_accept_structured`) chỉ áp cho "
    "**structured output** của workspace này: khi bật, service tự accept candidate "
    "sau khi schema/ID/scope/pin hợp lệ. Guard nằm ở backend, không phải ở nút UI."
)
AUTO_ACCEPT_NOTE_UNRELATED_TO_PROSE = (
    "Auto Accept **không** liên quan prose hay Human Review: nó không finalize "
    "chương, không đánh dấu Human Review hoàn tất và không thay gate review của "
    "Writer/Finalize."
)

_JSON_KWARGS: dict[str, Any] = {"ensure_ascii": False, "indent": 2, "sort_keys": False}


# ---------------------------------------------------------------------------
# Đọc state để hiển thị
# ---------------------------------------------------------------------------


def envelope_or_none(project: Project, artifact_id: str) -> ArtifactEnvelope[Any] | None:
    """Envelope đã lưu của artifact, hoặc `None` nếu chưa có file."""
    return storage.load_artifact(project, artifact_id)


def artifact_caption(
    envelope: ArtifactEnvelope[Any] | None, *, label: str
) -> str:
    """Câu mô tả trạng thái artifact: revision nào đang accepted, candidate nào đang chờ."""
    if envelope is None:
        return f"`{label}`: chưa có revision nào (status `missing`)."
    parts = [f"`{label}`: status `{envelope.status.value}`"]
    if envelope.accepted_revision is not None:
        parts.append(f"accepted r{envelope.accepted_revision.revision}")
    if envelope.candidate_revision is not None:
        parts.append(f"candidate r{envelope.candidate_revision.revision} (chưa thay accepted)")
    if not envelope.accepted_revision and not envelope.candidate_revision:
        parts.append("không có revision")
    return " · ".join(parts) + "."


def artifact_stale_notes(envelope: ArtifactEnvelope[Any] | None) -> list[str]:
    """Lý do stale để UI nói rõ vì sao artifact không dùng được cho action phụ thuộc."""
    if envelope is None:
        return []
    return [
        f"stale do `{reason.source_artifact_id}` r{reason.source_revision}: {reason.reason}"
        for reason in envelope.stale_reasons
    ]


def selected_revision_text(
    envelope: ArtifactEnvelope[Any] | None, *, which: str
) -> str:
    """Revision đang được chọn (`accepted`/`candidate`) hoặc thông báo thiếu."""
    if envelope is None:
        return ""
    revision = (
        envelope.accepted_revision if which == "accepted" else envelope.candidate_revision
    )
    if revision is None:
        return f"Chưa có bản `{which}`."
    accepted_at = f" · accepted_at {revision.accepted_at}" if revision.accepted_at else ""
    return f"Bản `{which}` r{revision.revision} · nguồn `{revision.payload_source.source_type.value}`{accepted_at}"


def candidate_update_hint() -> str:
    """Nhắc user rằng nội dung generate chỉ là candidate cho tới khi accept."""
    return (
        "Nội dung AI vừa sinh là **candidate**, chỉ thành canon sau khi bạn bấm "
        "`Accept` (hoặc Auto Accept structured bật và candidate hợp lệ)."
    )


# ---------------------------------------------------------------------------
# Serialize payload cho editor JSON
# ---------------------------------------------------------------------------


def payload_json_text(payload: Any) -> str:
    """Payload -> text JSON dễ đọc để user sửa tay."""
    if payload is None:
        return ""
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    return json.dumps(data, **_JSON_KWARGS)


def compact_json(value: Any) -> str:
    """JSON một dòng cho bảng/caption gọn."""
    data = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def parse_json_payload(text: str) -> tuple[Any | None, str | None]:
    """Parse text editor thành object; trả `(object, lỗi)` chứ không raise vào UI.

    Lỗi parse được trả về dạng chuỗi tiếng Việt nói rõ đây là JSON sai định dạng;
    page giữ nguyên text đang sửa để user chỉnh tiếp.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, "Editor đang rỗng; cần JSON trước khi gửi action."
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, f"JSON không hợp lệ ở dòng {exc.lineno}, cột {exc.colno}: {exc.msg}."
    if not isinstance(parsed, Mapping):
        return None, "JSON phải là một object (dictionary) ở mức gốc, không phải list hay scalar."
    return parsed, None


def envelope_candidate_json_text(envelope: ArtifactEnvelope[Any] | None) -> str:
    """Text JSON của candidate hiện có (rỗng nếu chưa có)."""
    if envelope is None or envelope.candidate_revision is None:
        return ""
    return payload_json_text(envelope.candidate_revision.payload)


def editor_key(artifact_type: str, *, workspace: str, variant: str = "candidate") -> str:
    """Khóa widget editor ổn định cho một artifact + variant."""
    return f"novel_ai_editor_{workspace}_{artifact_type}_{variant}"


def clear_session_keys(*keys: str) -> None:
    """Xoá key session state (dùng khi reset editor), không đụng state bền."""
    import streamlit as st

    for key in keys:
        st.session_state.pop(key, None)


def sync_text_editor(
    key: str,
    text: str,
    *,
    version: Any,
) -> str:
    """Trả text đang có trong editor, tự nạp `text` khi version nguồn đổi.

    Cách giữ dữ liệu đang sửa khi validation lỗi: text user nhập nằm trong
    `st.session_state[key]`; page chỉ ghi đè khi *version* của nguồn đổi (revision
    mới, đổi artifact, hoặc user bấm `Nạp lại từ candidate`). Vì vậy rerun và lần
    submit lỗi không làm mất nội dung đang sửa.
    """
    import streamlit as st

    version_key = f"{key}__source"
    current = st.session_state.get(key)
    if current is None or st.session_state.get(version_key) != version:
        st.session_state[key] = text
        st.session_state[version_key] = version
        return text
    return str(current)


def candidate_update_hint_for_envelope(
    envelope: ArtifactEnvelope[Any] | None,
) -> str:
    """Nhắc cập nhật candidate khi đã có accepted (regenerate không ghi đè accepted)."""
    if envelope is not None and envelope.accepted_revision is not None:
        return (
            "Đã có accepted revision: generate lại chỉ tạo **candidate mới**, "
            "accepted giữ nguyên cho tới khi bạn accept candidate đó."
        )
    return candidate_update_hint()


# ---------------------------------------------------------------------------
# Hiển thị lỗi service
# ---------------------------------------------------------------------------


def render_error_list(error: ServiceError) -> None:
    """Hiển thị `ValidationFailure.result.errors` theo field (path JSON Pointer)."""
    import streamlit as st

    result = getattr(error, "result", None)
    errors = list(getattr(result, "errors", []) or [])
    if not errors:
        return
    for issue in errors:
        st.markdown(
            f"- `{issue.path}` — {issue.message} _(code `{issue.code}`)_"
        )


def render_service_error(error: ServiceError, *, next_step: str) -> None:
    """Hiển thị lỗi service theo field + bước tiếp theo; **không** sửa state.

    `GuardError`/`StaleDependencyError` -> chặn action (st.error).
    `ValidationFailure` -> candidate/editor không được ghi, accepted giữ nguyên.
    `LLMUnavailableError` -> raw/error đã lưu, user thử lại hoặc sửa tay.
    """
    import streamlit as st

    details = getattr(error, "details", None) or {}
    if isinstance(error, ValidationFailure):
        st.error(
            f"Dữ liệu gửi lên không qua validation (code `{error.code}`): {error}. "
            "Accepted revision **không** đổi; sửa lại editor hoặc generate lại."
        )
        render_error_list(error)
    elif isinstance(error, LLMUnavailableError):
        st.error(f"LLM không khả dụng (code `{error.code}`): {error}")
    elif isinstance(error, GuardError):
        st.warning(f"Action bị guard chặn (code `{error.code}`): {error}")
    else:
        st.error(f"Service báo lỗi (code `{error.code}`): {error}")
    if details:
        interesting = {
            key: value
            for key, value in details.items()
            if value not in (None, "", [], {}) and key != "artifact_id"
        }
        if interesting:
            st.caption("Chi tiết backend: " + compact_json(interesting))
    st.info(f"Bước tiếp theo: {next_step}")


# ---------------------------------------------------------------------------
# Co-create: dựng `IdeaState` từ input của user
# ---------------------------------------------------------------------------


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def working_state_from_values(
    *,
    genre: Any,
    core_concept: Any,
    tone: Any = "",
    protagonist: Any = "",
    setting: Any = "",
    conflict: Any = "",
    constraints: Any = "",
    open_questions: Any = "",
) -> dict[str, Any]:
    """Dựng payload `IdeaState` từ giá trị form; **không** validate (service làm).

    Hàm thuần để test được: page chỉ nối widget với hàm này và gọi
    `co_create.set_working_state`. Validation thật nằm ở service, nên dữ liệu
    thiếu vẫn được gửi và lỗi hiển thị theo field.
    """
    return {
        "genre": str(genre or "").strip(),
        "tone": str(tone or "").strip(),
        "protagonist": str(protagonist or "").strip(),
        "core_concept": str(core_concept or "").strip(),
        "setting": str(setting or "").strip(),
        "conflict": str(conflict or "").strip(),
        "constraints": _split_lines(constraints),
        "open_questions": _split_lines(open_questions),
    }


# ---------------------------------------------------------------------------
# Architect: append payload
# ---------------------------------------------------------------------------


def append_entries_payload(
    artifact_type: str, entry: Mapping[str, Any]
) -> dict[str, Any]:
    """Bọc một entry mới thành payload append đúng collection của artifact.

    `append_entries` của service nhận payload **cùng shape** với accepted payload
    (ví dụ `{"characters": [...]}`) nên page bọc entry user nhập vào đúng key.
    """
    collection = {
        "characters": "characters",
        "world_rules": "world_rules",
        "foreshadow": "foreshadows",
    }.get(artifact_type)
    if collection is None:
        raise GuardError(
            f"`{artifact_type}` không có collection appendable.",
            code="unknown_artifact_type",
            details={"artifact_type": artifact_type},
        )
    return {collection: [dict(entry)]}


# ---------------------------------------------------------------------------
# Long Plan / Short Plan: lựa chọn và hiển thị ID
# ---------------------------------------------------------------------------


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
