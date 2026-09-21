"""Artifact lifecycle, guard backend và stale propagation cho Manual AI Novel (T09).

File này hiện thực `docs/design/workflow.md` mục 2–3, 5 và `docs/design/storage.md`
mục 4, 10:

- đặt tên artifact ổn định (``artifact_id_for``);
- transition candidate/accept/reject/reaccept/stale trên envelope T08;
- guard Writer/Review/Finalize nằm ở backend (D011), không dựa vào UI hay sự tồn
  tại của file;
- ``mark_downstream_stale`` chỉ **đánh dấu** stale theo bảng propagation, không
  rewrite/regenerate bất kỳ artifact hay prose nào.

Nguyên tắc:

- Candidate không bao giờ ghi đè accepted revision; accept là một revision mới.
- ``accept_candidate`` từ chối candidate ``validation.state = invalid``.
- Guard trả ``GuardResult`` (dữ liệu) để UI hiển thị lý do; service gọi
  ``raise_if_blocked()`` khi cần raise. ``GuardBlockedError`` mang ``code`` và
  ``reasons`` để tầng service map sang ``services.GuardError``.
- Stale nghĩa là "cần người dùng review lại", không phải "nội dung sai" và
  không phải xóa dữ liệu.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from novel_ai.core import storage
from novel_ai.core.models import (
    ArtifactEnvelope,
    ArtifactRevision,
    ArtifactStatus,
    ChapterMetadata,
    ChapterRange,
    ChapterStatus,
    DependencyPin,
    PayloadSource,
    PreparationContext,
    StaleReason,
    ValidationResult,
    ValidationState,
    consistent_chapter_number,
    now_iso,
    reconciled_chapter_numbers,
)
from novel_ai.core.project import Project
from novel_ai.core.validation import (
    DocumentError,
    issues_from_pydantic_error,
    payload_model_for,
    validate_artifact_payload,
)

__all__ = [
    "STALE_CHANGES",
    "GuardBlockedError",
    "GuardResult",
    "LifecycleError",
    "accept_candidate",
    "artifact_id_for",
    "guard_finalize",
    "guard_review",
    "guard_skeleton_for_chapter",
    "guard_writer",
    "mark_downstream_stale",
    "mark_stale",
    "new_artifact",
    "reaccept",
    "reject_candidate",
    "set_candidate",
    "stale_dependencies",
    "stale_pin_mismatches",
]

#: Artifact có đúng một instance trong project.
SINGLETON_ARTIFACT_TYPES: tuple[str, ...] = (
    "premise",
    "characters",
    "world_rules",
    "foreshadow",
    "long_plan",
    "short_plan",
)

#: Artifact gắn với một chapter_id.
CHAPTER_ARTIFACT_TYPES: tuple[str, ...] = (
    "skeleton",
    "review_report",
    "reconciliation",
    "impact_report",
    "rewrite_section",
)

#: Change code được ``mark_downstream_stale`` chấp nhận (bảng storage.md mục 10).
STALE_CHANGE_BASE_IDEA_REVISE = "base_idea_revise"
STALE_CHANGE_PREMISE_REVISE = "premise_revise"
STALE_CHANGE_CHARACTERS_APPEND = "characters_append"
STALE_CHANGE_WORLD_RULES_APPEND = "world_rules_append"
STALE_CHANGE_FORESHADOW_APPEND = "foreshadow_append"
STALE_CHANGE_LONG_PLAN_REPLACE = "long_plan_replace"
STALE_CHANGE_SHORT_PLAN_REPLACE = "short_plan_replace"
STALE_CHANGE_SKELETON_REPLACE = "skeleton_replace"
STALE_CHANGE_CHAPTER_FINALIZE = "chapter_finalize"
STALE_CHANGE_RETCON = "retcon"

STALE_CHANGES: frozenset[str] = frozenset(
    {
        STALE_CHANGE_BASE_IDEA_REVISE,
        STALE_CHANGE_PREMISE_REVISE,
        STALE_CHANGE_CHARACTERS_APPEND,
        STALE_CHANGE_WORLD_RULES_APPEND,
        STALE_CHANGE_FORESHADOW_APPEND,
        STALE_CHANGE_LONG_PLAN_REPLACE,
        STALE_CHANGE_SHORT_PLAN_REPLACE,
        STALE_CHANGE_SKELETON_REPLACE,
        STALE_CHANGE_CHAPTER_FINALIZE,
        STALE_CHANGE_RETCON,
    }
)

#: Change append → (artifact_type nguồn, field ID trong payload, field FK ở đích).
_APPEND_SOURCES: dict[str, tuple[str, str, str]] = {
    STALE_CHANGE_CHARACTERS_APPEND: ("characters", "character_id", "character_ids"),
    STALE_CHANGE_WORLD_RULES_APPEND: ("world_rules", "world_rule_id", "world_rule_ids"),
    STALE_CHANGE_FORESHADOW_APPEND: ("foreshadow", "foreshadow_id", "foreshadow_ids"),
}

#: Change broad-mark → các family artifact bị stale (không tính append).
_PROPAGATION_FAMILIES: dict[str, tuple[str, ...]] = {
    STALE_CHANGE_BASE_IDEA_REVISE: (
        "premise",
        "characters",
        "world_rules",
        "foreshadow",
        "long_plan",
        "short_plan",
        "skeleton",
        "review_report",
    ),
    STALE_CHANGE_PREMISE_REVISE: (
        "long_plan",
        "short_plan",
        "skeleton",
        "review_report",
    ),
    STALE_CHANGE_LONG_PLAN_REPLACE: ("short_plan", "skeleton", "review_report"),
    STALE_CHANGE_SHORT_PLAN_REPLACE: ("skeleton", "review_report"),
    STALE_CHANGE_SKELETON_REPLACE: ("review_report",),
    STALE_CHANGE_CHAPTER_FINALIZE: (),
    STALE_CHANGE_RETCON: ("skeleton", "review_report"),
}

#: Change cần biết chapter của artifact nguồn và mới đánh dấu từ chapter đó trở đi.
_FORWARD_FROM_SOURCE_CHAPTER: frozenset[str] = frozenset(
    {STALE_CHANGE_RETCON, STALE_CHANGE_SKELETON_REPLACE}
)


class LifecycleError(RuntimeError):
    """Transition lifecycle không hợp lệ (ví dụ accept khi không có candidate)."""

    code = "lifecycle_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})


class GuardBlockedError(LifecycleError):
    """Guard backend chặn action. Tầng service map sang ``services.GuardError``."""

    code = "guard_blocked"

    def __init__(self, result: GuardResult) -> None:
        message = "; ".join(result.reasons) or f"Guard chặn action ({result.code})."
        super().__init__(message, code=result.code, details={"reasons": list(result.reasons)})
        self.result = result
        self.reasons = list(result.reasons)


@dataclass
class GuardResult:
    """Kết quả guard deterministic: cho phép hay không, mã lý do, và giải thích."""

    allowed: bool
    code: str = "ok"
    reasons: list[str] = field(default_factory=list)

    def raise_if_blocked(self) -> None:
        """Raise ``GuardBlockedError`` nếu guard không cho phép."""
        if not self.allowed:
            raise GuardBlockedError(self)


# ---------------------------------------------------------------------------
# Artifact ID
# ---------------------------------------------------------------------------


def artifact_id_for(
    artifact_type: str,
    *,
    chapter_id: str | None = None,
    arc_id: str | None = None,
) -> str:
    """ID artifact ổn định theo type và scope chapter/arc.

    ``rolling_patch`` nhận scope qua ``arc_id`` (hoặc ``chapter_id`` khi caller
    chỉ có một tham số), khớp ``artifact_id_for("rolling_patch", chapter_id="arc_0001")``.
    """
    if artifact_type in SINGLETON_ARTIFACT_TYPES:
        return artifact_type
    if artifact_type == "rolling_patch":
        scope = arc_id or chapter_id
        if not scope:
            raise LifecycleError(
                "rolling_patch cần arc_id (hoặc chapter_id chứa arc_id).",
                code="missing_scope",
            )
        return f"rolling_patch_{scope}"
    if artifact_type in CHAPTER_ARTIFACT_TYPES:
        if not chapter_id:
            raise LifecycleError(
                f"artifact_type `{artifact_type}` cần chapter_id.", code="missing_scope"
            )
        return f"{artifact_type}_{chapter_id}"
    raise LifecycleError(
        f"artifact_type `{artifact_type}` không có quy tắc đặt ID.", code="unknown_artifact_type"
    )


def _chapter_id_from_artifact_id(artifact_id: str) -> str | None:
    for prefix in CHAPTER_ARTIFACT_TYPES:
        marker = f"{prefix}_"
        if artifact_id.startswith(marker) and len(artifact_id) > len(marker):
            return artifact_id[len(marker):]
    marker = "chapter_"
    if artifact_id.startswith(marker) and len(artifact_id) > len(marker):
        return artifact_id[len(marker):]
    return None


def _chapter_number_of_artifact(project: Project, artifact_id: str) -> int | None:
    chapter_id = _chapter_id_from_artifact_id(artifact_id)
    if chapter_id is None:
        return None
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is not None:
        return chapter.chapter_number
    match = re.search(r"(\d+)$", chapter_id)
    return int(match.group(1)) if match else None


def _chapter_status(project: Project, chapter_id: str) -> ChapterStatus | None:
    chapter = storage.load_chapter(project, chapter_id)
    return chapter.status if chapter is not None else None


def _is_final_chapter(project: Project, chapter_id: str | None) -> bool:
    if not chapter_id:
        return False
    return _chapter_status(project, chapter_id) is ChapterStatus.final_reconciled


def _chapter_number_of_artifact_payload(envelope: ArtifactEnvelope[Any] | None) -> int | None:
    if envelope is None:
        return None
    for revision in (envelope.accepted_revision, envelope.candidate_revision):
        if revision is None:
            continue
        payload = revision.payload
        number = getattr(payload, "chapter_number", None)
        if isinstance(number, int):
            return number
        if isinstance(payload, Mapping) and isinstance(payload.get("chapter_number"), int):
            return int(payload["chapter_number"])
    return None


# ---------------------------------------------------------------------------
# Transition
# ---------------------------------------------------------------------------


def new_artifact(
    artifact_type: str, artifact_id: str, *, now: str | None = None
) -> ArtifactEnvelope[Any]:
    """Envelope rỗng ``status=missing`` cho một artifact mới.

    ``now`` được giữ trong chữ ký cho đồng bộ API; envelope T08 chưa có
    timestamp riêng nên chưa dùng tới (revision mới mới có ``created_at``).
    """
    if not artifact_type or not artifact_id:
        raise LifecycleError("Cần artifact_type và artifact_id.", code="invalid_artifact")
    return ArtifactEnvelope[Any](
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        status=ArtifactStatus.missing,
    )


def _coerce_payload(artifact_type: str, payload: Any) -> Any:
    """Ép payload candidate về đúng model contract của ``artifact_type``.

    Candidate phải giữ payload dạng model (không phải dict thô) để revision sau
    đọc được bằng attribute và serialize ổn định; payload sai shape bị từ chối
    rõ ràng thay vì lưu dict méo (raw output đã được lưu riêng để debug).
    """
    try:
        model_cls = payload_model_for(artifact_type)
    except DocumentError as exc:
        raise LifecycleError(
            f"artifact_type `{artifact_type}` chưa có payload model trong contract.",
            code="unknown_artifact_type",
            details={"errors": [item.model_dump(mode="json") for item in exc.errors]},
        ) from exc
    if isinstance(payload, model_cls):
        return payload
    try:
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        issues = issues_from_pydantic_error(exc, base_path="/payload")
        raise LifecycleError(
            f"Payload candidate cho `{artifact_type}` sai contract "
            f"({len(issues)} lỗi); lưu raw output và sửa trước khi accept.",
            code="invalid_candidate_payload",
            details={"errors": [item.model_dump(mode="json") for item in issues]},
        ) from exc


def set_candidate(
    envelope: ArtifactEnvelope[Any],
    payload: Any,
    *,
    source: PayloadSource,
    dependency_pins: Iterable[DependencyPin] = (),
    validation: ValidationResult | None = None,
    preparation_context: PreparationContext | None = None,
    now: str | None = None,
) -> ArtifactEnvelope[Any]:
    """Đặt candidate mới, **giữ nguyên** accepted revision.

    Candidate revision = accepted revision + 1; nếu ``source.id_map`` trùng
    mapping của candidate đang có (retry cùng run) thì giữ nguyên revision cũ
    để retry không sinh revision trùng.

    ``preparation_context`` là metadata app-owned cho candidate chuẩn bị trước
    (Short Plan/Skeleton dựng ở mode `provisional`, `schemas.md` mục 4.1). Nó nằm
    trong `ArtifactRevision` để guard accept đọc được **kể cả khi caller không
    truyền chapter metadata**.
    """
    updated = envelope.model_copy(deep=True)
    current = updated.candidate_revision
    base = updated.accepted_revision.revision if updated.accepted_revision else 0
    revision = base + 1
    if (
        current is not None
        and source.id_map
        and current.payload_source.id_map == source.id_map
    ):
        revision = current.revision
    updated.candidate_revision = ArtifactRevision(
        revision=revision,
        payload=_coerce_payload(updated.artifact_type, payload),
        payload_source=source,
        dependency_pins=list(dependency_pins),
        validation=validation or ValidationResult(),
        preparation_context=preparation_context,
        created_at=now or now_iso(),
    )
    updated.status = ArtifactStatus.draft
    return updated


def accept_candidate(
    envelope: ArtifactEnvelope[Any],
    *,
    accepted_by: str = "user",
    validation: ValidationResult | None = None,
    now: str | None = None,
) -> ArtifactEnvelope[Any]:
    """Chuyển candidate thành accepted revision; xóa ``stale_reasons``.

    Idempotent theo nghĩa hàm thuần: gọi hai lần trên cùng envelope đầu vào cho
    cùng kết quả, không tạo revision thứ hai.

    Candidate phải có kết quả validation **thật** (`valid`) mới được vào canon:
    `not_checked` bị từ chối thay vì mặc nhiên hợp lệ (F-B6 của
    `docs/design/review-findings-t24.md`). Guard nằm ở backend để đường accept nào
    cũng phải validate structured output trước khi merge.
    """
    updated = envelope.model_copy(deep=True)
    candidate = updated.candidate_revision
    if candidate is None:
        raise LifecycleError(
            f"{updated.artifact_id}: không có candidate để accept.", code="missing_candidate"
        )
    effective = validation or candidate.validation
    if effective.state is ValidationState.not_checked:
        raise LifecycleError(
            f"{updated.artifact_id}: candidate r{candidate.revision} chưa được validate; "
            "phải validate structured output trước khi accept.",
            code="validation_required",
        )
    if effective.state is ValidationState.invalid:
        raise LifecycleError(
            f"{updated.artifact_id}: candidate r{candidate.revision} không hợp lệ "
            f"({len(effective.errors)} lỗi); accepted state giữ nguyên.",
            code="invalid_candidate",
            details={"errors": [error.model_dump(mode="json") for error in effective.errors]},
        )
    stamp = now or now_iso()
    updated.accepted_revision = candidate.model_copy(
        update={
            "accepted_at": stamp,
            "accepted_by": accepted_by,
            "validation": effective,
        }
    )
    updated.candidate_revision = None
    updated.status = ArtifactStatus.accepted
    updated.stale_reasons = []
    return updated


def reject_candidate(envelope: ArtifactEnvelope[Any]) -> ArtifactEnvelope[Any]:
    """Reject candidate: ``rejected`` nếu đã từng có accepted, ngược lại ``missing``.

    Accepted revision cũ giữ nguyên (không xóa dữ liệu để audit).
    """
    updated = envelope.model_copy(deep=True)
    had_accepted = updated.accepted_revision is not None
    updated.candidate_revision = None
    updated.status = ArtifactStatus.rejected if had_accepted else ArtifactStatus.missing
    return updated


def mark_stale(
    envelope: ArtifactEnvelope[Any],
    *,
    source_artifact_id: str,
    source_revision: int,
    reason: str,
    affected_range: ChapterRange | Mapping[str, Any] | Sequence[int] | None = None,
    can_reaccept: bool = True,
    now: str | None = None,
) -> ArtifactEnvelope[Any]:
    """Đánh dấu accepted revision cần review lại (không xóa nội dung)."""
    if not source_artifact_id:
        raise LifecycleError("mark_stale cần source_artifact_id.", code="invalid_stale_source")
    updated = envelope.model_copy(deep=True)
    updated.stale_reasons = [
        *updated.stale_reasons,
        StaleReason(
            source_artifact_id=source_artifact_id,
            source_revision=source_revision,
            reason=reason,
            affected_range=_normalize_range(affected_range),
            created_at=now or now_iso(),
            can_reaccept=can_reaccept,
        ),
    ]
    if updated.accepted_revision is not None:
        updated.status = ArtifactStatus.stale
    return updated


def reaccept(
    envelope: ArtifactEnvelope[Any],
    *,
    dependency_pins: Iterable[DependencyPin] = (),
    now: str | None = None,
) -> ArtifactEnvelope[Any]:
    """Validate lại nội dung accepted cũ rồi accept với pins mới.

    Nội dung giữ nguyên; revision metadata tăng 1 và ``stale_reasons`` bị xóa.
    Nếu validation deterministic không còn pass thì raise, artifact vẫn stale.
    """
    updated = envelope.model_copy(deep=True)
    accepted = updated.accepted_revision
    if accepted is None:
        raise LifecycleError(
            f"{updated.artifact_id}: reaccept cần accepted revision để review lại.",
            code="missing_accepted",
        )
    result = validate_artifact_payload(updated.artifact_type, accepted.payload)
    if result.state is ValidationState.invalid:
        raise LifecycleError(
            f"{updated.artifact_id}: nội dung cũ không còn pass validation deterministic; "
            "cần regenerate thay vì reaccept.",
            code="reaccept_validation_failed",
            details={"errors": [error.model_dump(mode="json") for error in result.errors]},
        )
    stamp = now or now_iso()
    new_pins = list(dependency_pins) or list(accepted.dependency_pins)
    updated.accepted_revision = accepted.model_copy(
        update={
            "revision": accepted.revision + 1,
            "dependency_pins": new_pins,
            "validation": result,
            "accepted_at": stamp,
            "accepted_by": "user",
        }
    )
    updated.status = ArtifactStatus.accepted
    updated.stale_reasons = []
    return updated


def _normalize_range(
    affected_range: ChapterRange | Mapping[str, Any] | Sequence[int] | None,
) -> ChapterRange | None:
    if affected_range is None:
        return None
    if isinstance(affected_range, ChapterRange):
        return affected_range
    if isinstance(affected_range, Mapping):
        return ChapterRange(start=int(affected_range["start"]), end=int(affected_range["end"]))
    values = list(affected_range)
    if len(values) != 2:
        raise LifecycleError(
            "affected_range phải là ChapterRange, {start, end} hoặc (start, end).",
            code="invalid_range",
        )
    return ChapterRange(start=int(values[0]), end=int(values[1]))


def stale_pin_mismatches(
    pins: Iterable[DependencyPin], current: Mapping[str, int]
) -> list[str]:
    """Pin nào lệch revision accepted hiện tại thì trả message (rỗng = còn fresh)."""
    return storage.pin_mismatch_messages(pins, current)


def stale_dependencies(
    project: Project,
    pins: Iterable[DependencyPin],
    *,
    current: Mapping[str, int] | None = None,
) -> list[str]:
    """Lý do pin không còn fresh: revision lệch **hoặc** target không `accepted`.

    ``stale_pin_mismatches`` chỉ so revision; guard reaccept/regenerate cần thêm
    trạng thái (workflow.md mục 4.5: artifact chỉ được reaccept khi dependency đã
    accepted trở lại). Pin trỏ tới thứ không phải envelope artifact (``base_idea``,
    ``current_timeline``, ``chapter_<id>``) được bỏ qua vì không có status riêng.
    """
    pin_list = list(pins)
    revisions = (
        dict(current)
        if current is not None
        else storage.current_artifact_revisions(project)
    )
    reasons = list(storage.pin_mismatch_messages(pin_list, revisions))
    for pin in pin_list:
        try:
            envelope = storage.load_artifact(project, pin.artifact_id)
        except storage.StorageError:
            continue
        if envelope is None:
            continue
        if envelope.status is not ArtifactStatus.accepted:
            reasons.append(
                f"{pin.artifact_id}: status hiện tại là `{envelope.status.value}`, "
                f"cần `accepted` trước khi dùng lại (scope {pin.scope})"
            )
    return reasons


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def guard_skeleton_for_chapter(project: Project, chapter_id: str) -> GuardResult:
    """Skeleton của chapter phải ``accepted`` và không stale."""
    artifact_id = artifact_id_for("skeleton", chapter_id=chapter_id)
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or envelope.accepted_revision is None:
        return GuardResult(
            False,
            "skeleton_missing",
            [f"Chưa có Skeleton accepted cho {chapter_id} (`{artifact_id}`)."],
        )
    if envelope.status is ArtifactStatus.stale:
        return GuardResult(
            False,
            "skeleton_stale",
            [
                f"Skeleton `{artifact_id}` đang stale: "
                + "; ".join(item.reason for item in envelope.stale_reasons)
            ],
        )
    if envelope.status is not ArtifactStatus.accepted:
        return GuardResult(
            False,
            "skeleton_not_accepted",
            [f"Skeleton `{artifact_id}` có status `{envelope.status.value}`, chưa accepted."],
        )
    payload = envelope.accepted_revision.payload
    payload_chapter = getattr(payload, "chapter_id", None)
    if isinstance(payload_chapter, str) and payload_chapter != chapter_id:
        return GuardResult(
            False,
            "skeleton_chapter_mismatch",
            [
                f"Skeleton `{artifact_id}` khai chapter_id `{payload_chapter}`, không khớp {chapter_id}."
            ],
        )
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        return GuardResult(
            False,
            "chapter_missing",
            [f"Chưa có chapter metadata cho {chapter_id}; không biết Skeleton nào đang hiệu lực."],
        )
    if chapter.skeleton_pin is None:
        # Không có pin nghĩa là Skeleton chưa từng được accept cho chapter này.
        # Trước đây nhánh này bị bỏ qua, nên chapter.json cũ/import tay vẫn finalize
        # được bằng Skeleton chưa bind (guard bypass khi gọi service trực tiếp).
        return GuardResult(
            False,
            "skeleton_pin_missing",
            [
                f"Chapter {chapter_id} chưa có `skeleton_pin`; Skeleton chưa được accept "
                "cho chapter này. Chạy accept Skeleton trước."
            ],
        )
    pin = chapter.skeleton_pin
    if pin.artifact_id != artifact_id or pin.revision != envelope.accepted_revision.revision:
        return GuardResult(
            False,
            "skeleton_pin_mismatch",
            [
                f"Chapter {chapter_id} đang pin `{pin.artifact_id}` r{pin.revision} nhưng "
                f"`{artifact_id}` accepted là r{envelope.accepted_revision.revision}; "
                "cần cập nhật pin (accept Skeleton) trước khi dùng."
            ],
        )
    return GuardResult(
        True,
        "ok",
        [
            f"Skeleton `{artifact_id}` r{envelope.accepted_revision.revision} accepted và fresh."
        ],
    )


def _all_chapters(project: Project) -> list[ChapterMetadata]:
    """Mọi chapter metadata của project (chỉ đọc, sort theo số chương)."""
    chapters: list[ChapterMetadata] = []
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is not None:
            chapters.append(chapter)
    chapters.sort(key=lambda item: item.chapter_number)
    return chapters


def _state_chain_guard(project: Project, chapter_number: int) -> GuardResult:
    """Timeline/relationship chain tới chương trước target phải không stale."""
    up_to = chapter_number - 1
    timeline = storage.load_timeline(project)
    relationships = storage.load_relationships(project)
    stale_entries = [
        entry for entry in timeline.entries if entry.chapter_number <= up_to and entry.stale
    ]
    if stale_entries:
        return GuardResult(
            False,
            "timeline_entry_stale",
            [
                "Timeline entry đang stale: "
                + ", ".join(f"{entry.timeline_id} (chương {entry.chapter_number})" for entry in stale_entries)
            ],
        )
    # Dùng `latest_consistent_chapter` khi nó đã được set (kể cả sau retcon, khi nó
    # bị hạ xuống) và chỉ fallback sang `latest_final_chapter` khi chưa set. Lấy
    # `max` hai field sẽ vô hiệu hoá guard: sau retcon `latest_final_chapter` vẫn
    # cao nên Writer sẽ chạy trên state chain cũ (storage.md mục 8).
    #
    # Timeline/relationship giữ **số chương** chứ không giữ status, nên giá trị
    # dẫn xuất còn có thể trỏ tới một chương đã rời `final_reconciled`. Guard đối
    # chiếu lại qua metadata chapter (`consistent_chapter_number`) để không cho
    # Writer chạy trên state của chương không còn final.
    reconciled = reconciled_chapter_numbers(_all_chapters(project))
    consistent = consistent_chapter_number(
        timeline=timeline,
        relationships=relationships,
        reconciled_numbers=reconciled,
    )
    if timeline.entries and consistent < up_to:
        return GuardResult(
            False,
            "timeline_not_consistent",
            [
                f"Timeline mới nhất quán tới chương {consistent}, cần tới chương {up_to} "
                "trước khi viết chương này."
            ],
        )
    stale_relationships = [item for item in relationships.relationships if item.stale]
    if stale_relationships:
        return GuardResult(
            False,
            "relationship_stale",
            [
                "Relationship state đang stale: "
                + ", ".join(item.relationship_id for item in stale_relationships)
            ],
        )
    if relationships.relationships:
        newest = max(item.last_updated_chapter for item in relationships.relationships)
        rel_consistent = (
            relationships.latest_consistent_chapter
            if relationships.latest_consistent_chapter in reconciled
            else (newest if newest in reconciled else 0)
        )
        if rel_consistent < up_to:
            return GuardResult(
                False,
                "relationship_not_consistent",
                [
                    f"Relationship state mới nhất quán tới chương {rel_consistent}, "
                    f"cần tới chương {up_to}."
                ],
            )
    return GuardResult(True, "ok", [])


def guard_writer(project: Project, chapter_id: str) -> GuardResult:
    """Writer chỉ chạy khi Skeleton accepted/fresh và previous chapter đã final.

    Chương 1 dùng baseline rỗng (không cần previous chapter). Chương N > 1 cần
    chương N-1 ``final_reconciled`` **và** timeline/relationship chain tới N-1
    không stale.
    """
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        return GuardResult(
            False, "chapter_missing", [f"Chưa có chapter metadata cho {chapter_id}."]
        )
    skeleton = guard_skeleton_for_chapter(project, chapter_id)
    if not skeleton.allowed:
        return GuardResult(False, skeleton.code, skeleton.reasons)
    if chapter.chapter_number > 1:
        previous_id = chapter.previous_chapter_id
        if not previous_id:
            return GuardResult(
                False,
                "previous_chapter_missing",
                [
                    f"Chương {chapter.chapter_number} thiếu previous_chapter_id; "
                    "không suy đoán chương trước từ tên file."
                ],
            )
        previous = storage.load_chapter(project, previous_id)
        if previous is None:
            return GuardResult(
                False,
                "previous_chapter_missing",
                [f"Không đọc được chapter trước `{previous_id}`."],
            )
        if previous.status is not ChapterStatus.final_reconciled:
            return GuardResult(
                False,
                "previous_chapter_not_finalized",
                [
                    f"Chương {previous.chapter_number} đang `{previous.status.value}`; "
                    "Writer chương sau chỉ unlock khi chương trước `final_reconciled`."
                ],
            )
        state = _state_chain_guard(project, chapter.chapter_number)
        if not state.allowed:
            return state
    return GuardResult(
        True,
        "ok",
        skeleton.reasons
        + [f"Chapter {chapter.chapter_number} đủ điều kiện chạy Writer."],
    )


def guard_review(project: Project, chapter_id: str) -> GuardResult:
    """AI Review/Human Review cần draft hiện tại đã complete."""
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        return GuardResult(
            False, "chapter_missing", [f"Chưa có chapter metadata cho {chapter_id}."]
        )
    draft = chapter.current_draft
    if draft is None:
        return GuardResult(
            False, "draft_missing", [f"{chapter_id} chưa có prose revision nào."]
        )
    if not draft.is_complete:
        return GuardResult(
            False,
            "draft_incomplete",
            [
                f"Draft r{draft.revision} của {chapter_id} chưa complete "
                "(stream dở/partial không được coi là review_required)."
            ],
        )
    return GuardResult(True, "ok", [f"Draft r{draft.revision} của {chapter_id} đã complete."])


def guard_finalize(project: Project, chapter_id: str) -> GuardResult:
    """Finalize cần draft complete, Human Review đúng revision và Skeleton fresh."""
    chapter = storage.load_chapter(project, chapter_id)
    if chapter is None:
        return GuardResult(
            False, "chapter_missing", [f"Chưa có chapter metadata cho {chapter_id}."]
        )
    review = guard_review(project, chapter_id)
    if not review.allowed:
        return review
    if chapter.status is ChapterStatus.final_reconciled:
        return GuardResult(
            False,
            "chapter_already_final",
            [
                f"{chapter_id} đã `final_reconciled`; sửa bản final phải đi qua action retcon."
            ],
        )
    human = chapter.human_review
    if human is None:
        return GuardResult(
            False,
            "human_review_missing",
            [f"{chapter_id} chưa có Human Review; Human Review là gate cứng của Finalize."],
        )
    if (
        chapter.current_draft_revision is None
        or human.prose_revision != chapter.current_draft_revision
        or not human.valid_for_current_revision
    ):
        return GuardResult(
            False,
            "human_review_stale",
            [
                f"Human Review gắn prose r{human.prose_revision} nhưng draft hiện tại là "
                f"r{chapter.current_draft_revision}; cần review lại revision hiện tại."
            ],
        )
    skeleton = guard_skeleton_for_chapter(project, chapter_id)
    if not skeleton.allowed:
        return skeleton
    return GuardResult(
        True,
        "ok",
        [
            f"{chapter_id} đủ điều kiện finalize (draft r{chapter.current_draft_revision}, "
            "Human Review hợp lệ, Skeleton fresh)."
        ],
    )


# ---------------------------------------------------------------------------
# Stale propagation
# ---------------------------------------------------------------------------


def mark_downstream_stale(
    project: Project,
    *,
    source_artifact_id: str,
    source_revision: int,
    change: str,
    now: str | None = None,
) -> list[str]:
    """Đánh dấu stale các artifact downstream theo bảng storage.md mục 10.

    Chỉ ghi stale metadata (``status=stale`` + ``stale_reasons``); không rewrite
    plan/skeleton/prose và không tự regenerate. Trả ID artifact **mới** được
    đánh dấu để caller/UI báo cho người dùng; gọi lần hai với cùng source trả [].
    """
    if change not in STALE_CHANGES:
        raise LifecycleError(
            f"change `{change}` không nằm trong bảng stale propagation "
            f"{sorted(STALE_CHANGES)}.",
            code="unknown_stale_change",
        )
    targets = _propagation_targets(project, source_artifact_id, change)
    operation_id = f"stale_{source_artifact_id}_r{source_revision}"
    marked: list[str] = []
    for artifact_id, affected_range, entity_reason in targets:
        if artifact_id == source_artifact_id:
            continue
        envelope = storage.load_artifact(project, artifact_id)
        if envelope is None:
            continue
        if envelope.accepted_revision is None and envelope.candidate_revision is None:
            continue
        if envelope.status is ArtifactStatus.rejected:
            continue
        if _already_marked(envelope, source_artifact_id, source_revision):
            continue
        reason = (
            f"{change}: `{source_artifact_id}` r{source_revision} thay đổi nên artifact này "
            "cần user review/reaccept hoặc regenerate trước khi dùng tiếp."
        )
        if entity_reason:
            reason = f"{reason} {entity_reason}"
        updated = mark_stale(
            envelope,
            source_artifact_id=source_artifact_id,
            source_revision=source_revision,
            reason=reason,
            affected_range=affected_range,
            now=now,
        )
        storage.save_artifact(project, updated, operation_id=operation_id)
        marked.append(artifact_id)
    return marked


def _already_marked(
    envelope: ArtifactEnvelope[Any], source_artifact_id: str, source_revision: int
) -> bool:
    return envelope.status is ArtifactStatus.stale and any(
        item.source_artifact_id == source_artifact_id and item.source_revision == source_revision
        for item in envelope.stale_reasons
    )


def _propagation_targets(
    project: Project, source_artifact_id: str, change: str
) -> list[tuple[str, ChapterRange | None, str | None]]:
    """Danh sách ``(artifact_id, affected_range, ghi chú)`` cần đánh dấu stale."""
    if change in _APPEND_SOURCES:
        return _append_targets(project, source_artifact_id, change)
    families = _PROPAGATION_FAMILIES[change]
    if not families:
        return []
    source_chapter = (
        _chapter_number_of_artifact(project, source_artifact_id)
        if change in _FORWARD_FROM_SOURCE_CHAPTER
        else None
    )
    targets: list[tuple[str, ChapterRange | None, str | None]] = []
    for artifact_id in storage.list_artifact_ids(project):
        family = _family_of(artifact_id)
        if family not in families:
            continue
        if change is STALE_CHANGE_SKELETON_REPLACE:
            # Chỉ draft/review của chính chapter có skeleton bị thay.
            if _chapter_id_from_artifact_id(artifact_id) != _chapter_id_from_artifact_id(
                source_artifact_id
            ):
                continue
        if change is STALE_CHANGE_RETCON and source_chapter is not None:
            number = _chapter_number_of_artifact(project, artifact_id) or (
                _chapter_number_of_artifact_payload(storage.load_artifact(project, artifact_id))
            )
            if number is None or number <= source_chapter:
                continue
            if family in {"skeleton", "review_report"}:
                targets.append((artifact_id, ChapterRange(start=number, end=number), None))
                continue
        if change not in {STALE_CHANGE_RETCON} and family == "review_report":
            # Bảng storage.md mục 10: chỉ "draft/review chưa final" bị stale;
            # Skeleton vẫn bị đánh dấu dù chapter đã final vì final prose
            # không bị rewrite nhưng Skeleton là derived artifact của plan.
            chapter_id = _chapter_id_from_artifact_id(artifact_id)
            if _is_final_chapter(project, chapter_id):
                continue
        targets.append((artifact_id, None, None))
    return targets


def _family_of(artifact_id: str) -> str:
    for family in CHAPTER_ARTIFACT_TYPES:
        if artifact_id.startswith(f"{family}_"):
            return family
    if artifact_id == "rolling_patch" or artifact_id.startswith("rolling_patch_"):
        return "rolling_patch"
    return artifact_id


def _append_targets(
    project: Project, source_artifact_id: str, change: str
) -> list[tuple[str, ChapterRange | None, str | None]]:
    """Đánh dấu theo selector + hiệu lực chương cho append entry mới.

    Chỉ artifact **có tham chiếu** tới entry mới và có chapter/range ``>=``
    ``effective_from_chapter`` của entry đó mới bị stale (storage.md mục 10).
    """
    artifact_type, _, fk_field = _APPEND_SOURCES[change]
    if _family_of(source_artifact_id) != artifact_type:
        raise LifecycleError(
            f"change `{change}` cần source artifact thuộc `{artifact_type}`, "
            f"nhận `{source_artifact_id}`.",
            code="stale_source_mismatch",
        )
    envelope = storage.load_artifact(project, source_artifact_id)
    revision = None
    if envelope is not None:
        revision = envelope.accepted_revision or envelope.candidate_revision
    if revision is None:
        return []
    effective = dict(_entity_entries(revision.payload, artifact_type))
    if not effective:
        return []
    affected_ids = set(effective)
    targets: list[tuple[str, ChapterRange | None, str | None]] = []
    affected_chapters: set[int] = set()
    for artifact_id in storage.list_artifact_ids(project):
        family = _family_of(artifact_id)
        if family not in {"long_plan", "short_plan", "skeleton"}:
            continue
        hits = _artifact_entity_hits(project, artifact_id, family, affected_ids, fk_field)
        relevant = {item: effective[item] for item in hits if item in effective}
        if not relevant:
            continue
        start = min(relevant.values())
        note = "Entry mới hiệu lực từ chương " + ", ".join(
            f"{key}->{value}" for key, value in sorted(relevant.items())
        )
        if family == "skeleton":
            number = _chapter_number_of_artifact(project, artifact_id)
            if number is not None and number < start:
                continue
            high = number or start
            targets.append(
                (artifact_id, ChapterRange(start=max(start, high), end=high), note)
            )
            affected_chapters.add(high)
            continue
        if family == "short_plan":
            eligible = [
                number
                for number in _short_plan_chapter_numbers(
                    storage.load_artifact(project, artifact_id)
                )
                if number >= start
            ]
            if not eligible:
                continue
            targets.append(
                (artifact_id, ChapterRange(start=min(eligible), end=max(eligible)), note)
            )
            continue
        ranges = _long_plan_arc_ranges(
            storage.load_artifact(project, artifact_id), affected_ids, fk_field
        )
        eligible_ranges = [(low, high) for low, high in ranges if high >= start]
        if not eligible_ranges:
            continue
        targets.append(
            (
                artifact_id,
                ChapterRange(
                    start=min(low for low, _ in eligible_ranges),
                    end=max(high for _, high in eligible_ranges),
                ),
                note,
            )
        )
    # Review report chỉ stale khi chapter tương ứng có skeleton bị đánh dấu.
    for artifact_id in storage.list_artifact_ids(project):
        if _family_of(artifact_id) != "review_report":
            continue
        number = _chapter_number_of_artifact(project, artifact_id)
        chapter_id = _chapter_id_from_artifact_id(artifact_id)
        if number is None or number not in affected_chapters:
            continue
        if _is_final_chapter(project, chapter_id):
            continue
        targets.append((artifact_id, ChapterRange(start=number, end=number), None))
    return targets


def _entity_entries(payload: Any, artifact_type: str) -> list[tuple[str, int]]:
    """``[(entity_id, effective_from_chapter)]`` của payload foundation."""
    if artifact_type == "characters":
        return [
            (item.character_id, item.effective_from_chapter)
            for item in getattr(payload, "characters", None) or []
        ]
    if artifact_type == "world_rules":
        return [
            (item.world_rule_id, item.effective_from_chapter)
            for item in getattr(payload, "world_rules", None) or []
        ]
    if artifact_type == "foreshadow":
        return [
            (item.foreshadow_id, item.effective_from_chapter)
            for item in getattr(payload, "foreshadows", None) or []
        ]
    return []


def _artifact_entity_hits(
    project: Project,
    artifact_id: str,
    family: str,
    affected_ids: set[str],
    fk_field: str,
) -> set[str]:
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None or envelope.accepted_revision is None:
        return set()
    payload = envelope.accepted_revision.payload
    hits: set[str] = set()
    if family == "skeleton":
        for section in getattr(payload, "sections", None) or []:
            hits |= set(getattr(section, fk_field, None) or [])
    elif family == "short_plan":
        for chapter in getattr(payload, "chapters", None) or []:
            hits |= set(getattr(chapter, fk_field, None) or [])
    elif family == "long_plan":
        for volume in getattr(payload, "volumes", None) or []:
            for arc in getattr(volume, "arcs", None) or []:
                hits |= set(getattr(arc, fk_field, None) or [])
    return hits & affected_ids


def _short_plan_chapter_numbers(envelope: ArtifactEnvelope[Any] | None) -> list[int]:
    if envelope is None or envelope.accepted_revision is None:
        return []
    payload = envelope.accepted_revision.payload
    return [chapter.chapter_number for chapter in getattr(payload, "chapters", None) or []]


def _long_plan_arc_ranges(
    envelope: ArtifactEnvelope[Any] | None, affected_ids: set[str], fk_field: str
) -> list[tuple[int, int]]:
    if envelope is None or envelope.accepted_revision is None:
        return []
    payload = envelope.accepted_revision.payload
    ranges: list[tuple[int, int]] = []
    for volume in getattr(payload, "volumes", None) or []:
        for arc in getattr(volume, "arcs", None) or []:
            if set(getattr(arc, fk_field, None) or []) & affected_ids:
                ranges.append((arc.chapter_range.start, arc.chapter_range.end))
    return ranges
