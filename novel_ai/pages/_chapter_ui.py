"""Helper dùng chung cho 5 workspace chương: Skeleton, Writer, Review, Reconcile, Revision.

Module này thuộc T21/T22 và chỉ phục vụ `novel_ai/pages/{skeleton,writer,review,
reconcile,revision}.py`. Nó giữ hai loại helper:

1. **Hàm thuần** (không import Streamlit): chọn chapter để hiển thị, mô tả trạng
   thái chapter/artifact, gate Writer, hàng đợi finalizing, trạng thái recovery,
   snapshot/history và **operation_id ổn định** cho action UI.
2. **Hàm render nhỏ** dùng Streamlit: input "Lần chạy" (chống double submit) và
   vài dòng cảnh báo dùng lại ở nhiều page.

Luật giữ nguyên trong module:

- chỉ **đọc** state để hiển thị (qua `novel_ai.core.storage` và các hàm đọc của
  service), không ghi file, không gọi LLM, không chạy luật lifecycle;
- không tự sửa state để che lỗi service;
- `operation_id` của action UI là **hàm thuần của input người dùng** (xem
  `stable_operation_id`), nên bấm lặp cùng một action không tạo revision thứ hai:
  service replay theo `operation_id`. Muốn một request mới với cùng input thì
  người dùng đổi số "Lần chạy" (`attempt_input`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novel_ai.core import lifecycle, storage
from novel_ai.core.models import (
    ChapterMetadata,
    ChapterStatus,
    DependencyPin,
    ReconciliationStatus,
)
from novel_ai.core.project import Project
from novel_ai.services import reconcile as reconcile_service
from novel_ai.services import revision as revision_service
from novel_ai.ui import arbiter

__all__ = [
    "ATTEMPT_LABEL",
    "CHAPTER_STATUS_NOTES",
    "FINALIZE_LOCK_NOTE",
    "PARTIAL_NOTE",
    "PROVISIONAL_NOTE",
    "RecoveryState",
    "ReconcileQueueItem",
    "VersionRow",
    "WriterGate",
    "attempt_input",
    "begin_click",
    "chapter_by_id",
    "chapter_label",
    "chapter_rows",
    "chapter_select",
    "chapter_status_note",
    "downstream_state_version",
    "end_click",
    "payload_fingerprint",
    "state_signature",
    "final_chapters",
    "history_entries",
    "human_review_line",
    "pending_view",
    "pin_lines",
    "pin_mismatches",
    "prose_text",
    "provisional_basis",
    "read_json_relpath",
    "read_text_relpath",
    "reconcile_queue",
    "recovery_state",
    "retcon_marker",
    "run_action",
    "set_note",
    "show_note",
    "snapshot_rows",
    "stable_operation_id",
    "stale_blockers",
    "stream_supported",
    "upstream_stale_scope",
    "version_rows",
    "writer_gate",
]

#: Nhãn input "Lần chạy" dùng ở mọi action gọi LLM.
ATTEMPT_LABEL = "Lần chạy (đổi số để tạo request mới với cùng input)"

#: Giải thích trạng thái chapter cho người dùng (workflow.md mục 3).
CHAPTER_STATUS_NOTES: dict[str, str] = {
    ChapterStatus.planned.value: (
        "`planned`: chưa có Skeleton accepted nên Writer bị khóa."
    ),
    ChapterStatus.skeleton_ready.value: (
        "`skeleton_ready`: Skeleton accepted/fresh; Writer mở nếu chương trước đã "
        "`final_reconciled`."
    ),
    ChapterStatus.draft.value: (
        "`draft`: có prose nhưng chưa review được — thường là partial do stream đứt."
    ),
    ChapterStatus.review_required.value: (
        "`review_required`: prose complete; cần Human Review trước khi Finalize."
    ),
    ChapterStatus.finalizing.value: (
        "`finalizing`: final candidate đã đóng băng, reconciliation chưa commit; "
        "chương sau **vẫn khóa**."
    ),
    ChapterStatus.final_reconciled.value: (
        "`final_reconciled`: final manuscript + accepted state đã commit; chương sau "
        "có thể mở."
    ),
}

#: Câu nhắc bất biến D003 dùng ở Review/Reconcile.
FINALIZE_LOCK_NOTE = (
    "Sau Finalize, chapter chỉ ở `finalizing`; chương sau **vẫn khóa** cho tới khi "
    "reconciliation commit thành công (D003)."
)

#: Câu nhắc partial không finalize được (workflow.md mục 5.5).
PARTIAL_NOTE = (
    "Draft **partial** (stream đứt/chưa hoàn tất) không finalize được: backend "
    "`lifecycle.guard_review` và `reviewer.ready_to_finalize` đều từ chối, nên UI "
    "không hiện nút Finalize khả dụng. Hãy Continue hoặc Regenerate/Save draft."
)

#: Cảnh báo candidate provisional (D014, context.md mục 5.4).
PROVISIONAL_NOTE = (
    "Candidate **provisional** (chương trước chưa `final_reconciled`) chỉ là chuẩn "
    "bị trước: **không dùng được cho Writer** và **không accept được** cho tới khi "
    "bạn regenerate/review trên context `actual`."
)


# ---------------------------------------------------------------------------
# Chapter: chọn và mô tả
# ---------------------------------------------------------------------------


def chapter_rows(project: Project) -> list[dict[str, Any]]:
    """Mọi chapter của project, sort theo số chương, kèm state cần hiển thị."""
    rows: list[dict[str, Any]] = []
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is None:
            continue
        draft = chapter.current_draft
        rows.append(
            {
                "chapter_id": chapter.chapter_id,
                "chapter_number": chapter.chapter_number,
                "title": chapter.title,
                "status": chapter.status.value,
                "current_draft_revision": chapter.current_draft_revision,
                "is_complete": bool(draft is not None and draft.is_complete),
                "has_final_candidate": chapter.final_candidate is not None,
                "final_revision": (
                    chapter.final_revision.revision if chapter.final_revision else None
                ),
            }
        )
    rows.sort(key=lambda row: row["chapter_number"])
    return rows


def chapter_label(row: Mapping[str, Any]) -> str:
    """Nhãn selectbox: số chương trước, stable ID sau, kèm prose/trạng thái."""
    parts = [f"Chương {row['chapter_number']}", f"`{row['chapter_id']}`"]
    if row.get("title"):
        parts.append(str(row["title"]))
    suffix = f"`{row['status']}`"
    if row.get("current_draft_revision"):
        suffix += f" · prose r{row['current_draft_revision']}"
        suffix += " (complete)" if row.get("is_complete") else " (partial)"
    return " — ".join(parts) + f" · {suffix}"


def chapter_by_id(project: Project, chapter_id: str) -> ChapterMetadata | None:
    """Chapter metadata hoặc `None` (chỉ đọc)."""
    return storage.load_chapter(project, chapter_id)


def chapter_status_note(status: str) -> str:
    """Một câu giải thích trạng thái chapter; fallback trung tính nếu lạ."""
    return CHAPTER_STATUS_NOTES.get(status, f"`{status}`: trạng thái chapter.")


def chapter_select(
    key: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    label: str = "Chapter",
    statuses: Sequence[str] | None = None,
) -> str | None:
    """Selectbox chọn chapter; trả `chapter_id` hoặc `None` nếu không có lựa chọn.

    Chỉ truyền `index` khi widget chưa có state, để lựa chọn của người dùng không
    bị ghi đè ở rerun sau (cùng cách T19 xử lý radio workspace).
    """
    import streamlit as st

    filtered = [
        row for row in rows if statuses is None or str(row["status"]) in set(statuses)
    ]
    if not filtered:
        return None
    options = [str(row["chapter_id"]) for row in filtered]
    labels = {str(row["chapter_id"]): chapter_label(row) for row in filtered}
    extra: dict[str, Any] = {}
    if st.session_state.get(key) not in options:
        extra["index"] = 0
    chosen = st.selectbox(
        label,
        options,
        format_func=lambda value: labels.get(value, value),
        key=key,
        **extra,
    )
    return str(chosen)


def payload_fingerprint(payload: Any) -> str:
    """Hash ngắn của payload để biết editor cần nạp lại nội dung hay không."""
    import json

    if payload is None:
        return "none"
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    digest = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest[:12]


def human_review_line(chapter: ChapterMetadata) -> str:
    """Mô tả Human Review hiện có và còn hiệu lực hay không (D002)."""
    review = chapter.human_review
    if review is None:
        return "Human Review: chưa có (gate cứng của Finalize)."
    valid = (
        review.valid_for_current_revision
        and chapter.current_draft_revision is not None
        and review.prose_revision == chapter.current_draft_revision
    )
    state = "còn hiệu lực" if valid else "**hết hiệu lực** cho revision hiện tại"
    return (
        f"Human Review: prose r{review.prose_revision} · {state} · "
        f"reviewed_by `{review.reviewed_by}` · {review.reviewed_at}"
    )


def final_chapters(project: Project) -> list[dict[str, Any]]:
    """Chapter đã có Final Manuscript (retcon chỉ bắt đầu từ đây)."""
    return [row for row in chapter_rows(project) if row.get("final_revision")]


def retcon_marker(project: Project, chapter_id: str) -> dict[str, Any] | None:
    """Marker "đang retcon" của chapter, hoặc `None` (chỉ đọc)."""
    try:
        return revision_service.retcon_state(project, chapter_id=chapter_id)
    except storage.StorageError:  # pragma: no cover - marker hỏng không được làm crash UI
        return None


# ---------------------------------------------------------------------------
# Skeleton: context basis và pins
# ---------------------------------------------------------------------------


def provisional_basis(chapter: ChapterMetadata) -> dict[str, Any] | None:
    """`context_basis` của candidate Skeleton nếu có (mode/pins); `None` nếu sạch."""
    preparation = chapter.preparation_context
    if preparation is None:
        return None
    basis = preparation.context_basis
    return {
        "mode": basis.mode.value,
        "actual_through_chapter": basis.actual_through_chapter,
        "planned_bridge": [
            {
                "chapter_id": item.chapter_id,
                "chapter_number": item.chapter_number,
                "summary": item.summary,
            }
            for item in basis.planned_bridge
        ],
        "dependency_pins": [pin.model_dump(mode="json") for pin in preparation.dependency_pins],
    }


def is_provisional(chapter: ChapterMetadata) -> bool:
    """True nếu candidate hiện tại còn dấu chuẩn bị trước (mode `provisional`)."""
    basis = provisional_basis(chapter)
    return bool(basis is not None and basis.get("mode") == "provisional")


def pin_lines(pins: Iterable[DependencyPin]) -> list[str]:
    """Pin dạng dòng dễ đọc: `artifact_id r<revision> (scope, chapter)`."""
    lines: list[str] = []
    for pin in pins:
        scope = f", scope `{pin.scope}`" if pin.scope else ""
        chapter = f", chapter `{pin.chapter_id}`" if pin.chapter_id else ""
        lines.append(f"`{pin.artifact_id}` r{pin.revision}{scope}{chapter}")
    return lines


def pin_mismatches(project: Project, pins: Sequence[DependencyPin]) -> list[str]:
    """Pin lệch revision hiện tại (chỉ đọc); dùng để cảnh báo trước khi accept."""
    if not pins:
        return []
    return storage.pin_mismatch_messages(pins, storage.current_artifact_revisions(project))


# ---------------------------------------------------------------------------
# Gate Writer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriterGate:
    """Kết quả gate Writer: quyết định của backend + gợi ý Arbiter liên quan."""

    allowed: bool
    code: str
    reasons: tuple[str, ...]
    arbiter_blockers: tuple[str, ...]
    arbiter_next: str


def writer_gate(project: Project, chapter_id: str) -> WriterGate:
    """Gate thật (backend) + gợi ý Arbiter cho chapter.

    `lifecycle.guard_writer` là nguồn quyết định (Skeleton accepted/fresh, chương
    trước `final_reconciled`, state chain không stale). `arbiter.analyze` chỉ đọc
    state để hiển thị "bước tiếp theo" và blocker liên quan — UI không tự suy luận
    lại luật.
    """
    guard = lifecycle.guard_writer(project, chapter_id)
    report = arbiter.analyze(project, config=project.config)
    blockers: list[str] = []
    for item in report.suggestions:
        if not item.blocking:
            continue
        if item.chapter_id not in (None, chapter_id):
            continue
        blockers.append(f"{item.label} — {item.reason}")
    next_step = report.next_step
    next_text = (
        f"{next_step.label} (workspace `{next_step.workspace}`) — {next_step.reason}"
        if next_step is not None
        else "Không có gợi ý: mọi bước hiện tại đã hoàn tất."
    )
    return WriterGate(
        allowed=guard.allowed,
        code=guard.code,
        reasons=tuple(guard.reasons),
        arbiter_blockers=tuple(blockers),
        arbiter_next=next_text,
    )


# ---------------------------------------------------------------------------
# Prose: đọc để hiển thị
# ---------------------------------------------------------------------------


def prose_text(project: Project, chapter_id: str, revision: int | None) -> str:
    """Markdown của một prose revision; `""` nếu chưa có/không đọc được (chỉ đọc).

    Dùng đúng hàm đọc của `services.writer` để UI không tự ghép đường dẫn file.
    """
    if revision is None:
        return ""
    from novel_ai.services import writer as writer_service

    try:
        return writer_service.draft_markdown(project, chapter_id, int(revision))
    except Exception:  # GuardError/StorageError khi file thiếu: UI hiển thị rỗng
        return ""


def read_text_relpath(project: Project, relpath: str) -> str:
    """Đọc text trong project theo relpath cho mục đích hiển thị; lỗi ⇒ `""`."""
    try:
        target = storage.ensure_within_project(project.root, project.root / relpath)
    except storage.StorageError:
        return ""
    if not target.is_file():
        return ""
    try:
        return storage.read_text(target)
    except storage.StorageError:  # pragma: no cover - file hỏng không làm crash UI
        return ""


def read_json_relpath(project: Project, relpath: str) -> Any | None:
    """Đọc JSON trong project theo relpath cho mục đích hiển thị; lỗi ⇒ `None`."""
    try:
        target = storage.ensure_within_project(project.root, project.root / relpath)
    except storage.StorageError:
        return None
    if not target.is_file():
        return None
    try:
        return storage.read_json(target)
    except storage.StorageError:  # pragma: no cover - file hỏng không làm crash UI
        return None


# ---------------------------------------------------------------------------
# Reconcile: hàng đợi finalizing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileQueueItem:
    """Một chapter đang `finalizing` + trạng thái reconciliation của nó."""

    chapter_id: str
    chapter_number: int
    title: str
    status: str
    final_candidate_prose_revision: int | None
    final_candidate_markdown_ref: str | None
    final_candidate_reconciliation_status: str
    reconciliation_artifact_status: str
    candidate_revision: int | None
    accepted_revision: int | None

    def label(self) -> str:
        proposal = (
            f"proposal r{self.candidate_revision}"
            if self.candidate_revision
            else "chưa có proposal"
        )
        return (
            f"Chương {self.chapter_number} · `{self.chapter_id}` — {self.title} · "
            f"final candidate prose r{self.final_candidate_prose_revision} · {proposal} · "
            f"artifact `{self.reconciliation_artifact_status}`"
        )


def reconcile_queue(project: Project) -> list[ReconcileQueueItem]:
    """Hàng đợi finalize/reconcile: chapter đang `finalizing`, sort theo số chương."""
    items: list[ReconcileQueueItem] = []
    for row in chapter_rows(project):
        if row["status"] != ChapterStatus.finalizing.value:
            continue
        info = pending_view(project, str(row["chapter_id"]))
        candidate = (info or {}).get("final_candidate") or {}
        items.append(
            ReconcileQueueItem(
                chapter_id=str(row["chapter_id"]),
                chapter_number=int(row["chapter_number"]),
                title=str(row.get("title") or ""),
                status=str(row["status"]),
                final_candidate_prose_revision=candidate.get("prose_revision"),
                final_candidate_markdown_ref=candidate.get("markdown_ref"),
                final_candidate_reconciliation_status=str(
                    candidate.get("reconciliation_status")
                    or ReconciliationStatus.missing.value
                ),
                reconciliation_artifact_status=str(
                    (info or {}).get("reconciliation_status")
                    or "missing"
                ),
                candidate_revision=(info or {}).get("candidate_revision"),
                accepted_revision=(info or {}).get("accepted_revision"),
            )
        )
    return items


def pending_view(project: Project, chapter_id: str) -> dict[str, Any] | None:
    """Trạng thái finalizing/reconciliation của chapter (hàm đọc của service)."""
    try:
        return reconcile_service.pending_reconciliation(project, chapter_id=chapter_id)
    except storage.StorageError:  # pragma: no cover - file hỏng không làm crash UI
        return None


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryState:
    """Trạng thái recovery của project (chỉ đọc)."""

    needs_recovery: bool
    manual: bool
    pending: tuple[str, ...]

    def summary(self) -> str:
        if self.manual:
            return (
                "Project **read-only**: có transaction không tự recovery được "
                f"(pending: {', '.join(self.pending) or 'không đọc được manifest'})."
            )
        if self.needs_recovery:
            return (
                "Có transaction dở cần recovery trước khi ghi tiếp "
                f"(pending: {', '.join(self.pending) or 'chỉ còn lock stale'})."
            )
        return "Không có transaction dở; project ghi được."


def recovery_state(project: Project) -> RecoveryState:
    """Đọc `needs_recovery`/`requires_manual_recovery` + pending ids (chỉ đọc)."""
    try:
        pending = tuple(storage.pending_operation_ids(project))
    except storage.StorageError:  # pragma: no cover
        pending = ()
    return RecoveryState(
        needs_recovery=bool(storage.needs_recovery(project)),
        manual=bool(storage.requires_manual_recovery(project)),
        pending=pending,
    )


# ---------------------------------------------------------------------------
# Revision: stale chain, version và history
# ---------------------------------------------------------------------------


def stale_blockers(project: Project) -> list[dict[str, Any]]:
    """Artifact/state đang stale chặn Writer (hàm đọc của `services.revision`)."""
    try:
        return revision_service.downstream_blockers(project)
    except storage.StorageError:  # pragma: no cover
        return []


def downstream_state_version(project: Project) -> tuple[Any, ...]:
    """Chữ ký của state chain (timeline/relationship) để retry rebuild không replay nhầm.

    Sau mỗi retcon + `reset_consistency_after_retcon`, `latest_consistent_chapter` và
    cờ `stale` đổi, nên chữ ký đổi và action `reconcile_downstream` nhận
    `operation_id` mới thay vì replay kết quả cũ.
    """
    try:
        timeline = storage.load_timeline(project)
        timeline_part: Any = (
            timeline.latest_final_chapter,
            timeline.latest_consistent_chapter,
            tuple((entry.chapter_id, entry.stale) for entry in timeline.entries),
        )
    except storage.StorageError:  # pragma: no cover
        timeline_part = None
    try:
        relationships = storage.load_relationships(project)
        relationship_part: Any = (
            relationships.latest_consistent_chapter,
            tuple(
                (item.relationship_id, item.last_updated_chapter, item.stale)
                for item in relationships.relationships
            ),
        )
    except storage.StorageError:  # pragma: no cover
        relationship_part = None
    return (timeline_part, relationship_part)


#: Phạm vi stale giải thích trước khi user revise upstream (storage.md mục 10).
UPSTREAM_STALE_SCOPE: dict[str, tuple[str, ...]] = {
    "base_idea": (
        "Premise, Characters, World Rules, Foreshadow",
        "Long Plan, Short Plan",
        "Skeleton và draft/review **chưa final**",
        "Snapshot/context của chương tương lai",
        "**Không** đụng: Final Manuscript đã final, raw và history",
    ),
    "premise": (
        "Long Plan, Short Plan",
        "Skeleton và draft/review **chưa final**",
        "**Không** đụng: Base Idea và Final Manuscript",
    ),
    "chapter_retcon": (
        "Timeline/relationship entry của chương sau",
        "Reconciliation proposal + snapshot downstream",
        "Skeleton/draft/review tương lai theo dependency scope",
        "**Không** rewrite final prose của chương sau (D004)",
    ),
}


def upstream_stale_scope(key: str) -> tuple[str, ...]:
    """Danh sách hệ quả stale hiển thị **trước** khi user bấm action revise."""
    return UPSTREAM_STALE_SCOPE.get(key, ())


@dataclass(frozen=True)
class VersionRow:
    """Một dòng đối chiếu phiên bản: accepted cũ, candidate mới, finalizing."""

    chapter_id: str
    chapter_number: int
    title: str
    status: str
    accepted_prose_revision: int | None
    candidate_prose_revision: int | None
    finalizing_prose_revision: int | None
    final_revision: int | None
    human_review_revision: int | None
    reconciliation_accepted_revision: int | None
    retcon_open: bool

    def note(self) -> str:
        parts: list[str] = [f"status `{self.status}`"]
        if self.accepted_prose_revision is not None:
            parts.append(f"accepted prose r{self.accepted_prose_revision}")
        if self.final_revision is not None:
            parts.append(f"final manuscript r{self.final_revision}")
        if self.finalizing_prose_revision is not None:
            parts.append(f"finalizing (chờ commit) prose r{self.finalizing_prose_revision}")
        if self.candidate_prose_revision is not None:
            parts.append(f"candidate mới prose r{self.candidate_prose_revision}")
        if self.retcon_open:
            parts.append("đang retcon: final cũ vẫn canon")
        if self.human_review_revision is not None:
            parts.append(f"human review r{self.human_review_revision}")
        return " · ".join(parts)


def version_rows(project: Project) -> list[VersionRow]:
    """Đối chiếu accepted cũ / candidate mới / `finalizing` cho từng chapter."""
    rows: list[VersionRow] = []
    for row in chapter_rows(project):
        chapter = storage.load_chapter(project, str(row["chapter_id"]))
        if chapter is None:  # pragma: no cover - vừa đọc được ở trên
            continue
        marker = retcon_marker(project, chapter.chapter_id)
        accepted_prose = (
            chapter.final_revision.source_prose_revision if chapter.final_revision else None
        )
        candidate_prose: int | None = None
        if marker is not None:
            value = marker.get("retcon_draft_revision")
            candidate_prose = int(value) if isinstance(value, int) else None
        elif (
            chapter.current_draft_revision is not None
            and chapter.current_draft_revision != accepted_prose
        ):
            candidate_prose = chapter.current_draft_revision
        rows.append(
            VersionRow(
                chapter_id=chapter.chapter_id,
                chapter_number=chapter.chapter_number,
                title=chapter.title,
                status=chapter.status.value,
                accepted_prose_revision=accepted_prose,
                candidate_prose_revision=candidate_prose,
                finalizing_prose_revision=(
                    chapter.final_candidate.prose_revision if chapter.final_candidate else None
                ),
                final_revision=(
                    chapter.final_revision.revision if chapter.final_revision else None
                ),
                human_review_revision=(
                    chapter.human_review.prose_revision if chapter.human_review else None
                ),
                reconciliation_accepted_revision=(
                    chapter.reconciliation_pin.revision
                    if chapter.reconciliation_pin is not None
                    else None
                ),
                retcon_open=marker is not None,
            )
        )
    return rows


def artifact_rows(project: Project) -> list[dict[str, Any]]:
    """Trạng thái mọi artifact envelope (accepted/candidate) để đối chiếu."""
    rows: list[dict[str, Any]] = []
    for artifact_id in storage.list_artifact_ids(project):
        envelope = storage.load_artifact(project, artifact_id)
        if envelope is None:  # pragma: no cover
            continue
        rows.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": envelope.artifact_type,
                "status": envelope.status.value,
                "accepted_revision": (
                    envelope.accepted_revision.revision
                    if envelope.accepted_revision is not None
                    else None
                ),
                "candidate_revision": (
                    envelope.candidate_revision.revision
                    if envelope.candidate_revision is not None
                    else None
                ),
                "stale_reasons": [
                    f"{item.source_artifact_id} r{item.source_revision}: {item.reason}"
                    for item in envelope.stale_reasons
                ],
            }
        )
    rows.sort(key=lambda item: str(item["artifact_id"]))
    return rows


def snapshot_rows(project: Project) -> list[dict[str, Any]]:
    """Snapshot context đã dùng (đọc từ `storage.load_snapshots`)."""
    try:
        snapshots = storage.load_snapshots(project)
    except storage.StorageError:  # pragma: no cover - snapshot hỏng không làm crash UI
        return []
    return [
        {
            "snapshot_id": item.snapshot_id,
            "for_chapter_id": item.for_chapter_id,
            "for_chapter_number": item.for_chapter_number,
            "created_from_action": item.created_from_action,
            "created_at": item.created_at,
            "dependency_pins": len(item.dependency_pins),
            "effective_character_ids": len(item.effective_character_ids),
            "excluded_count": len(item.excluded_due_to_effective_chapter),
        }
        for item in snapshots
    ]


def history_entries(project: Project) -> list[dict[str, Any]]:
    """Manifest trong `history/<operation_id>/` để đối chiếu bản trước khi thay."""
    directory = project.paths.history_dir
    if not directory.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for op_dir in sorted(directory.iterdir()):
        if not op_dir.is_dir():
            continue
        record: dict[str, Any] = {
            "operation_id": op_dir.name,
            "operation_type": None,
            "status": None,
            "created_at": None,
            "applied_paths": [],
            "before_files": [],
        }
        manifest_path = op_dir / "manifest.json"
        if manifest_path.is_file():
            try:
                data = storage.read_json(manifest_path)
            except storage.StorageError:  # pragma: no cover
                data = None
            if isinstance(data, Mapping):
                record["operation_type"] = data.get("operation_type")
                record["status"] = data.get("status")
                record["created_at"] = data.get("created_at")
                record["applied_paths"] = list(data.get("applied_paths") or [])
        before_dir = op_dir / "before"
        if before_dir.is_dir():
            record["before_files"] = sorted(
                str(item.relative_to(op_dir)).replace("\\", "/")
                for item in before_dir.rglob("*")
                if item.is_file()
            )
        entries.append(record)
    return entries


# ---------------------------------------------------------------------------
# Action UI: chống double submit
# ---------------------------------------------------------------------------


def stable_operation_id(
    workspace: str, action: str, target: str, *parts: Any
) -> str:
    """`operation_id` tất định từ input người dùng (chống double submit).

    Cùng workspace/action/target/parts ⇒ cùng `operation_id`, nên service trả kết
    quả cũ (replay) thay vì gọi LLM lần hai hoặc tạo revision thứ hai. Action gọi
    LLM nhận thêm số "Lần chạy" (`attempt_input`) trong `parts` để người dùng chủ
    động tạo request mới với cùng input.
    """
    joined = "|".join([str(workspace), str(action), str(target), *(str(item) for item in parts)])
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]
    return f"op_ui_{digest}"


def attempt_input(key: str, *, default: int = 1, help_text: str = "") -> int:
    """Number input "Lần chạy" cho action gọi LLM; trả số nguyên >= 1.

    Widget này chỉ là UI working state: đổi số là cách **tường minh** để chạy lại
    cùng input (thay vì bấm lặp nút, vốn được replay theo `operation_id`).
    """
    import streamlit as st

    return int(
        st.number_input(
            ATTEMPT_LABEL,
            min_value=1,
            max_value=999,
            value=int(default),
            step=1,
            key=key,
            help=help_text
            or (
                "Bấm lặp nút với cùng input sẽ replay theo operation_id (không gọi LLM "
                "lần hai). Đổi số này để thực sự tạo request mới."
            ),
        )
    )


def stream_supported(client: Any) -> bool:
    """True nếu client có `stream` gọi được (UI chỉ hiện lựa chọn stream khi có)."""
    return callable(getattr(client, "stream", None))


#: Khóa session_state cho ghi chú ngắn sống qua một rerun (UI working state).
NOTE_KEY_TEMPLATE = "novel_ai_note_{workspace}"


def set_note(workspace: str, text: str) -> None:
    """Lưu ghi chú ngay-sau-action để render ở đầu page sau khi rerun."""
    import streamlit as st

    st.session_state[NOTE_KEY_TEMPLATE.format(workspace=workspace)] = str(text)


def show_note(workspace: str) -> None:
    """Render rồi xóa ghi chú của workspace (không lặp lại ở rerun sau)."""
    import streamlit as st

    key = NOTE_KEY_TEMPLATE.format(workspace=workspace)
    text = st.session_state.pop(key, None)
    if text:
        st.info(str(text))


# ---------------------------------------------------------------------------
# Chống double submit ở tầng UI
# ---------------------------------------------------------------------------

#: Khóa session_state giữ dấu "action này đã chạy cho state hiện tại".
RUN_GUARD_KEY = "novel_ai_run_guard"

#: Thông báo khi bấm lặp bị bỏ qua (không phải lỗi, chỉ là chống submit trùng).
REPEAT_NOTE = (
    "Action này **đã chạy** cho state hiện tại và state chưa đổi kể từ đó, nên lần bấm "
    "lặp được bỏ qua để không gọi API lần hai hay tạo revision thứ hai. Nếu bạn thật sự "
    "muốn chạy lại cùng input, đổi số `Lần chạy` (hoặc sửa nội dung) rồi bấm lại."
)


def state_signature(project: Project) -> tuple[Any, ...]:
    """Chữ ký trạng thái bền rẻ tiền: chapter/artifact revision + state count.

    Dùng để nhận ra "state chưa đổi kể từ lần chạy trước"; không phải fingerprint
    byte-for-byte và chỉ đọc file (không mutate).
    """
    chapters: list[tuple[Any, ...]] = []
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is None:  # pragma: no cover
            continue
        chapters.append(
            (
                chapter.chapter_id,
                chapter.status.value,
                chapter.current_draft_revision,
                len(chapter.drafts),
                chapter.human_review.prose_revision if chapter.human_review else None,
                (
                    chapter.final_candidate.prose_revision
                    if chapter.final_candidate is not None
                    else None
                ),
                chapter.final_revision.revision if chapter.final_revision else None,
            )
        )
    artifacts: list[tuple[Any, ...]] = []
    for artifact_id in storage.list_artifact_ids(project):
        envelope = storage.load_artifact(project, artifact_id)
        if envelope is None:  # pragma: no cover
            continue
        artifacts.append(
            (
                artifact_id,
                envelope.status.value,
                envelope.accepted_revision.revision if envelope.accepted_revision else None,
                envelope.candidate_revision.revision if envelope.candidate_revision else None,
                len(envelope.stale_reasons),
            )
        )
    try:
        timeline = storage.load_timeline(project)
        timeline_part: Any = (
            timeline.latest_final_chapter,
            timeline.latest_consistent_chapter,
            tuple((entry.chapter_id, entry.stale) for entry in timeline.entries),
        )
    except storage.StorageError:  # pragma: no cover
        timeline_part = None
    try:
        relationships = storage.load_relationships(project)
        relationship_part: Any = (
            relationships.latest_consistent_chapter,
            tuple(
                (item.relationship_id, item.last_updated_chapter, item.stale)
                for item in relationships.relationships
            ),
        )
    except storage.StorageError:  # pragma: no cover
        relationship_part = None
    return (
        tuple(chapters),
        tuple(artifacts),
        timeline_part,
        relationship_part,
        tuple(recovery_state(project).pending),
    )


def begin_click(
    key: str, *, intent: Any, project: Project
) -> tuple[bool, Any]:
    """Chuẩn bị một click: trả `(blocked, state_before)`.

    `blocked=True` khi đúng `intent` này đã chạy thành công và state chưa đổi kể từ
    đó — tức người dùng bấm lặp/double submit. Page chỉ cần `return` và UI đã hiện
    thông báo trung thực, không gọi service lần hai.
    """
    import streamlit as st

    current = state_signature(project)
    record = st.session_state.get(RUN_GUARD_KEY)
    if isinstance(record, dict):
        entry = record.get(key)
        if (
            isinstance(entry, dict)
            and entry.get("intent") == intent
            and entry.get("state") == current
        ):
            st.info(REPEAT_NOTE)
            return True, current
    return False, current


def end_click(
    key: str,
    *,
    intent: Any,
    project: Project,
    state_before: Any,
    remember: bool = False,
) -> None:
    """Ghi dấu action đã chạy.

    Mặc định chỉ ghi khi action **thực sự** đổi state, để lỗi/no-op vẫn retry được.
    `remember=True` dùng cho action thành công mà không đổi state bền (ví dụ
    `rewrite_section` chỉ trả candidate trong session state): vẫn phải chặn bấm lặp.
    """
    import streamlit as st

    after = state_signature(project)
    if not remember and after == state_before:
        return
    record = st.session_state.get(RUN_GUARD_KEY)
    updated = dict(record) if isinstance(record, dict) else {}
    updated[key] = {"intent": intent, "state": after}
    st.session_state[RUN_GUARD_KEY] = updated


def run_action(
    key: str,
    *,
    intent: Any,
    project: Project,
    call: Callable[[], Any],
    remember: bool = False,
) -> tuple[bool, Any]:
    """Chạy `call()` với guard chống bấm lặp; trả `(blocked, result)`.

    `ServiceError` được để page bắt vì mỗi action cần thông báo "bước tiếp theo"
    riêng. Khi `blocked=True`, service **không** được gọi và UI đã hiện thông báo.
    """
    blocked, state_before = begin_click(key, intent=intent, project=project)
    if blocked:
        return True, None
    result = call()
    end_click(
        key, intent=intent, project=project, state_before=state_before, remember=remember
    )
    return False, result
