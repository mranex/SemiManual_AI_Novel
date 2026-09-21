"""Arbiter rule-based (T19).

Arbiter là **hàm Python dựa trên state/rule**: nó đọc lifecycle, trạng thái
artifact/chapter, stale reason, pending operation và rolling reminder rồi gợi ý
bước tiếp theo. Arbiter:

- **không** gọi LLM;
- **không** mutate state (chỉ đọc);
- **không** tự chạy bước tiếp — UI hiển thị gợi ý, người dùng bấm action.

Module này cố tình không import Streamlit để test được thuần Python
(`tests/unit/test_arbiter.py`) và để domain không phụ thuộc UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from novel_ai.core import storage
from novel_ai.core.models import (
    ArtifactStatus,
    ChapterStatus,
    ProjectConfig,
)
from novel_ai.core.project import Project
from novel_ai.ui import project_tree

#: Workspace id dùng để UI route người dùng tới đúng page.
WORKSPACES = (
    "co_create",
    "architect",
    "long_plan",
    "short_plan",
    "skeleton",
    "writer",
    "review",
    "reconcile",
    "revision",
)


@dataclass(frozen=True)
class ArbiterSuggestion:
    """Một gợi ý hành động. Không phải mệnh lệnh và không tự thực thi."""

    code: str
    label: str
    workspace: str
    reason: str
    priority: int = 50
    artifact_id: str | None = None
    chapter_id: str | None = None
    #: True khi gợi ý này bắt buộc trước khi viết tiếp (gate cứng ở backend).
    blocking: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "label": self.label,
            "workspace": self.workspace,
            "reason": self.reason,
            "priority": self.priority,
            "artifact_id": self.artifact_id,
            "chapter_id": self.chapter_id,
            "blocking": self.blocking,
        }


@dataclass
class ArbiterReport:
    """Kết quả phân tích: gợi ý đã sắp theo priority + tình trạng project."""

    project_id: str
    current_chapter: int
    suggestions: list[ArbiterSuggestion] = field(default_factory=list)
    stale_artifact_ids: list[str] = field(default_factory=list)
    pending_operation_ids: list[str] = field(default_factory=list)
    needs_recovery: bool = False
    read_only: bool = False
    rolling_due: bool = False
    artifact_status: dict[str, str] = field(default_factory=dict)
    chapter_status: dict[str, str] = field(default_factory=dict)

    @property
    def next_step(self) -> ArbiterSuggestion | None:
        return self.suggestions[0] if self.suggestions else None

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "current_chapter": self.current_chapter,
            "suggestions": [item.as_dict() for item in self.suggestions],
            "stale_artifact_ids": list(self.stale_artifact_ids),
            "pending_operation_ids": list(self.pending_operation_ids),
            "needs_recovery": self.needs_recovery,
            "read_only": self.read_only,
            "rolling_due": self.rolling_due,
            "artifact_status": dict(self.artifact_status),
            "chapter_status": dict(self.chapter_status),
        }


#: Artifact nền tảng theo thứ tự authority và nhãn tiếng Việt cho UI.
FOUNDATION_STEPS: tuple[tuple[str, str, str], ...] = (
    ("premise", "Premise", "architect"),
    ("characters", "Characters", "architect"),
    ("world_rules", "World Rules", "architect"),
    ("foreshadow", "Foreshadow", "architect"),
)

_PLANNING_STEPS: tuple[tuple[str, str, str], ...] = (
    ("long_plan", "Long Plan", "long_plan"),
    ("short_plan", "Short Plan", "short_plan"),
)


def _suggestion_for_artifact(artifact_id: str, label: str, workspace: str, status: str) -> ArbiterSuggestion | None:
    """Gợi ý generate/review/accept theo trạng thái envelope."""
    if status == ArtifactStatus.missing.value:
        return ArbiterSuggestion(
            code=f"generate_{artifact_id}",
            label=f"Generate {label}",
            workspace=workspace,
            reason=f"{label} chưa có accepted revision.",
            priority=30,
            artifact_id=artifact_id,
        )
    if status == ArtifactStatus.draft.value:
        return ArbiterSuggestion(
            code=f"review_{artifact_id}_candidate",
            label=f"Review candidate {label}",
            workspace=workspace,
            reason=f"{label} đang có candidate chưa accept; accepted cũ (nếu có) chưa đổi.",
            priority=20,
            artifact_id=artifact_id,
            blocking=True,
        )
    if status == ArtifactStatus.stale.value:
        return ArbiterSuggestion(
            code=f"reaccept_{artifact_id}",
            label=f"Review/Reaccept {label}",
            workspace="revision",
            reason=f"{label} đang stale: upstream đổi, cần user review trước khi dùng tiếp.",
            priority=10,
            artifact_id=artifact_id,
            blocking=True,
        )
    if status == ArtifactStatus.rejected.value:
        return ArbiterSuggestion(
            code=f"regenerate_{artifact_id}",
            label=f"Regenerate {label}",
            workspace=workspace,
            reason=f"Candidate {label} đã bị reject.",
            priority=30,
            artifact_id=artifact_id,
        )
    return None


def _chapter_suggestions(project: Project, chapter_id: str, chapter) -> list[ArbiterSuggestion]:
    suggestions: list[ArbiterSuggestion] = []
    status = chapter.status
    if status is ChapterStatus.planned:
        suggestions.append(
            ArbiterSuggestion(
                code="generate_skeleton",
                label="Generate Skeleton",
                workspace="skeleton",
                reason="Chapter đã có Short Plan nhưng chưa có Skeleton accepted.",
                priority=40,
                chapter_id=chapter_id,
                blocking=True,
            )
        )
    elif status is ChapterStatus.skeleton_ready:
        suggestions.append(
            ArbiterSuggestion(
                code="generate_writer_draft",
                label="Generate Writer draft",
                workspace="writer",
                reason="Skeleton đã accepted; Writer có thể chạy nếu chương trước đã final_reconciled.",
                priority=45,
                chapter_id=chapter_id,
            )
        )
    elif status is ChapterStatus.draft:
        current = chapter.current_draft
        if current is not None and not current.is_complete:
            suggestions.append(
                ArbiterSuggestion(
                    code="continue_partial_draft",
                    label="Continue partial draft",
                    workspace="writer",
                    reason="Bản nháp dở: stream bị ngắt nên chưa thể review hay finalize.",
                    priority=40,
                    chapter_id=chapter_id,
                    blocking=True,
                )
            )
        else:
            suggestions.append(
                ArbiterSuggestion(
                    code="review_draft",
                    label="Review chapter draft",
                    workspace="review",
                    reason="Có bản nháp nhưng chưa sẵn sàng review/finalize.",
                    priority=40,
                    chapter_id=chapter_id,
                )
            )
    elif status is ChapterStatus.review_required:
        review = chapter.human_review
        valid = (
            review is not None
            and review.valid_for_current_revision
            and review.prose_revision == chapter.current_draft_revision
        )
        if not valid:
            suggestions.append(
                ArbiterSuggestion(
                    code="human_review_required",
                    label="Human review chapter",
                    workspace="review",
                    reason=(
                        "Human Review là gate cứng và phải gắn đúng prose revision hiện tại. "
                        "AI Review chỉ là báo cáo hỗ trợ."
                    ),
                    priority=35,
                    chapter_id=chapter_id,
                    blocking=True,
                )
            )
        else:
            suggestions.append(
                ArbiterSuggestion(
                    code="finalize_chapter",
                    label="Finalize chapter",
                    workspace="review",
                    reason="Prose revision hiện tại đã được human review; có thể Finalize.",
                    priority=40,
                    chapter_id=chapter_id,
                )
            )
    elif status is ChapterStatus.finalizing:
        candidate = chapter.final_candidate
        status_text = candidate.reconciliation_status.value if candidate else "missing"
        if status_text == "accepted":
            suggestions.append(
                ArbiterSuggestion(
                    code="retry_reconcile_commit",
                    label="Retry reconciliation commit",
                    workspace="reconcile",
                    reason="Reconciliation đã accept nhưng commit chưa hoàn tất; thử lại an toàn.",
                    priority=15,
                    chapter_id=chapter_id,
                    blocking=True,
                )
            )
        else:
            suggestions.append(
                ArbiterSuggestion(
                    code="reconcile_chapter",
                    label="Accept/Retry reconciliation",
                    workspace="reconcile",
                    reason=(
                        "Chapter đang finalizing: final candidate đã đóng băng nhưng chưa là canon "
                        "cho tới khi reconciliation commit."
                    ),
                    priority=15,
                    chapter_id=chapter_id,
                    blocking=True,
                )
            )
    return suggestions


def analyze(project: Project, *, config: ProjectConfig | None = None) -> ArbiterReport:
    """Đọc state và trả gợi ý next step. Không mutate, không gọi LLM."""
    config = config or project.config
    report = ArbiterReport(project_id=config.project_id, current_chapter=config.current_chapter)

    # 0. Recovery/pending trước mọi thứ: không cho ghi khi transaction dở.
    if storage.needs_recovery(project):
        pending = storage.pending_operation_ids(project)
        report.needs_recovery = True
        report.pending_operation_ids = list(pending)
        report.read_only = storage.requires_manual_recovery(project)
        report.suggestions.append(
            ArbiterSuggestion(
                code="recover_pending_operation" if not report.read_only else "manual_recovery_required",
                label="Recovery transaction dở" if not report.read_only else "Cần recovery thủ công",
                workspace="revision",
                reason=(
                    "Có operation chưa commit xong. Backend phải recovery trước khi cho ghi tiếp; "
                    "chapter chỉ được coi là final_reconciled khi manifest committed."
                ),
                priority=0,
                blocking=True,
            )
        )
        return report

    if config.auto_accept_structured:
        report.suggestions.append(
            ArbiterSuggestion(
                code="auto_accept_note",
                label="Auto Accept structured đang bật",
                workspace="co_create",
                reason=(
                    "Auto Accept chỉ áp cho structured output đã validate; prose, Human Review "
                    "và Finalize vẫn luôn cần người dùng."
                ),
                priority=90,
            )
        )

    # 1. Co-create → Base Idea.
    if not project.paths.base_idea_md.is_file():
        report.suggestions.append(
            ArbiterSuggestion(
                code="finalize_base_idea",
                label="Chốt Base Idea",
                workspace="co_create",
                reason="Base Idea là authority cao nhất và phải accepted trước khi xây foundation.",
                priority=5,
                blocking=True,
            )
        )
    else:
        # 2. Foundation.
        for artifact_id, label, workspace in FOUNDATION_STEPS:
            envelope = storage.load_artifact(project, artifact_id)
            status = envelope.status.value if envelope is not None else ArtifactStatus.missing.value
            report.artifact_status[artifact_id] = status
            if status == ArtifactStatus.stale.value:
                report.stale_artifact_ids.append(artifact_id)
            suggestion = _suggestion_for_artifact(artifact_id, label, workspace, status)
            if suggestion is not None:
                report.suggestions.append(suggestion)

        # 3. Plan.
        for artifact_id, label, workspace in _PLANNING_STEPS:
            envelope = storage.load_artifact(project, artifact_id)
            status = envelope.status.value if envelope is not None else ArtifactStatus.missing.value
            report.artifact_status[artifact_id] = status
            if status == ArtifactStatus.stale.value:
                report.stale_artifact_ids.append(artifact_id)
            suggestion = _suggestion_for_artifact(artifact_id, label, workspace, status)
            if suggestion is not None:
                report.suggestions.append(suggestion)

    # 4. Chapter hiện hành: chapter chưa final_reconciled nhỏ nhất.
    chapters = []
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is not None:
            chapters.append(chapter)
    chapters.sort(key=lambda item: item.chapter_number)
    for chapter in chapters:
        report.chapter_status[chapter.chapter_id] = chapter.status.value
    active = next(
        (item for item in chapters if item.status is not ChapterStatus.final_reconciled), None
    )
    if active is not None:
        skeleton_id = f"skeleton_{active.chapter_id}"
        skeleton_envelope = storage.load_artifact(project, skeleton_id)
        skeleton_status = (
            skeleton_envelope.status.value
            if skeleton_envelope is not None
            else ArtifactStatus.missing.value
        )
        report.artifact_status[skeleton_id] = skeleton_status
        if skeleton_status == ArtifactStatus.stale.value:
            report.stale_artifact_ids.append(skeleton_id)
            report.suggestions.append(
                ArbiterSuggestion(
                    code="reaccept_skeleton",
                    label="Review/Reaccept Skeleton",
                    workspace="skeleton",
                    reason="Skeleton của chapter đang stale; Writer chỉ chạy với Skeleton accepted/fresh.",
                    priority=10,
                    chapter_id=active.chapter_id,
                    blocking=True,
                )
            )
        elif skeleton_status == ArtifactStatus.draft.value:
            report.suggestions.append(
                ArbiterSuggestion(
                    code="accept_skeleton",
                    label="Review/accept Skeleton candidate",
                    workspace="skeleton",
                    reason="Chapter cần Skeleton accepted trước khi Writer chạy.",
                    priority=20,
                    chapter_id=active.chapter_id,
                    blocking=True,
                )
            )
        report.suggestions.extend(_chapter_suggestions(project, active.chapter_id, active))

    # 5. Rolling reminder (không tự schedule, không tự apply).
    latest_final = max(
        (item.chapter_number for item in chapters if item.status is ChapterStatus.final_reconciled),
        default=0,
    )
    if latest_final and config.rolling_plan_every >= 1:
        if latest_final % config.rolling_plan_every == 0:
            report.rolling_due = True
            report.suggestions.append(
                ArbiterSuggestion(
                    code="rolling_plan_due",
                    label="Rolling Plan review",
                    workspace="short_plan",
                    reason=(
                        f"Đã final {latest_final} chương (mốc {config.rolling_plan_every}). "
                        "Đây chỉ là nhắc; user bấm action và apply chỉ đụng future Short Plan."
                    ),
                    priority=70,
                )
            )

    report.suggestions.sort(key=lambda item: (item.priority, item.code))
    return report


def summarize(report: ArbiterReport) -> str:
    """Một dòng tiếng Việt cho status bar."""
    if report.read_only:
        return "Project đang read-only: cần recovery thủ công trước khi ghi tiếp."
    if report.needs_recovery:
        return "Có transaction dở cần recovery trước khi ghi tiếp."
    step = report.next_step
    if step is None:
        return "Không có gợi ý; mọi bước hiện tại đã hoàn tất."
    return f"Bước gợi ý: {step.label} — {step.reason}"


def format_artifact_statuses(report: ArbiterReport) -> Mapping[str, str]:
    return dict(report.artifact_status)


# ---------------------------------------------------------------------------
# Panel Arbiter (spec mục 28–29)
# ---------------------------------------------------------------------------

#: Thứ tự dòng trạng thái cố định của panel, theo authority giảm dần.
STATUS_ORDER: tuple[tuple[str, str], ...] = (
    ("base_idea", "Base Idea"),
    ("premise", "Premise"),
    ("characters", "Characters"),
    ("world_rules", "World Rules"),
    ("foreshadow", "Foreshadow"),
    ("long_plan", "Long Plan"),
    ("short_plan", "Short Plan"),
)

#: `status -> (glyph, nhãn hiển thị)`. Glyph giống ký hiệu trong spec mục 29.
_STATUS_MARK: dict[str, tuple[str, str]] = {
    ArtifactStatus.accepted.value: ("✔", "accepted"),
    ArtifactStatus.draft.value: ("◆", "candidate"),
    ArtifactStatus.stale.value: ("▲", "STALE"),
    ArtifactStatus.rejected.value: ("✕", "rejected"),
    ArtifactStatus.missing.value: ("○", "chưa có"),
    "pending": ("▶", "đang dở"),
    "ready": ("✔", "ready"),
    "required": ("○", "cần review"),
    "final": ("✔", "final"),
    "finalizing": ("◑", "finalizing"),
    "not_applicable": ("·", "chưa tới bước"),
}


def _mark(status: str | None) -> tuple[str, str]:
    return _STATUS_MARK.get(status or ArtifactStatus.missing.value, ("○", status or "chưa có"))


def _chapter_artifact_status(project: Project, report: ArbiterReport, chapter_id: str) -> dict[str, str]:
    """Trạng thái `skeleton`/`writer`/`review` của chapter đang xét."""
    skeleton_id = f"skeleton_{chapter_id}"
    skeleton = report.artifact_status.get(skeleton_id)
    if skeleton is None:
        envelope = storage.load_artifact(project, skeleton_id)
        skeleton = envelope.status.value if envelope is not None else ArtifactStatus.missing.value
    metadata = storage.load_chapter(project, chapter_id)
    writer = (
        "pending"
        if metadata is not None and metadata.current_draft_revision is not None
        else "not_applicable"
    )
    if metadata is None:
        review = "not_applicable"
    elif metadata.status is ChapterStatus.review_required:
        review = "ready"
    elif metadata.status is ChapterStatus.draft:
        review = "required"
    else:
        review = "not_applicable"
    return {"skeleton": skeleton, "writer": writer, "review": review}


def status_rows(project: Project, report: ArbiterReport) -> list[tuple[str, str, str]]:
    """Dòng trạng thái cho panel Arbiter: `(nhãn, glyph, nhãn trạng thái)`.

    Bám spec mục 29: liệt kê state hiện tại của từng tầng theo authority. Chỉ đọc
    state, không suy diễn thêm luật — phần quyết định vẫn là `analyze`.
    """
    rows: list[tuple[str, str, str]] = []
    base_idea = (
        ArtifactStatus.accepted.value
        if project.paths.base_idea_md.is_file()
        else ArtifactStatus.missing.value
    )
    for artifact_id, label in STATUS_ORDER:
        status = base_idea if artifact_id == "base_idea" else report.artifact_status.get(artifact_id)
        glyph, text = _mark(status)
        rows.append((label, glyph, text))

    active = _active_chapter_id(project, report)
    if active is not None:
        chapter_rows = _chapter_artifact_status(project, report, active)
        for key, label in (
            ("skeleton", "Skeleton"),
            ("writer", "Writer Draft"),
            ("review", "Review"),
        ):
            glyph, text = _mark(chapter_rows[key])
            rows.append((f"{label} {_chapter_number(project, active)}", glyph, text))
    timeline, relationship = project_tree.latest_timeline_labels(project)
    rows.append(("Timeline", "·", timeline))
    rows.append(("Relationship", "·", relationship))
    return rows


def _chapter_number(project: Project, chapter_id: str) -> str:
    chapter = storage.load_chapter(project, chapter_id)
    return f"Ch.{chapter.chapter_number}" if chapter is not None else chapter_id


def _active_chapter_id(project: Project, report: ArbiterReport) -> str | None:
    """Chapter đang được Arbiter xét: suggestion có `chapter_id`, nếu không thì chapter mở."""
    for suggestion in report.suggestions:
        if suggestion.chapter_id:
            return suggestion.chapter_id
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is not None and chapter.status is not ChapterStatus.final_reconciled:
            return chapter_id
    return None


def next_step_lines(project: Project, report: ArbiterReport) -> list[tuple[str, str, str]]:
    """`(workspace, nhãn, lý do)` cho các gợi ý, đã sắp theo priority của Arbiter."""
    return [(item.workspace, item.label, item.reason) for item in report.suggestions]
