"""Read model project tree thuần Python, tách từ UI ở A02.

Phần dữ liệu (`build_project_tree`, `flatten`, `WORKSPACES`) là hàm Python thuần
đọc state qua storage; phần `render` mới dùng Streamlit. Nhờ vậy rule hiển thị
trạng thái (draft/accepted/stale/partial) test được mà không cần UI.

Cây phải phản ánh **metadata lifecycle**, không suy từ sự tồn tại của file:
một `skeleton.json` có mặt nhưng `status = draft` vẫn hiển thị là candidate.

Pane trái của shell vẽ **cây thật** (group cấp ngoài = `st.expander`, tầng sâu
hơn = dòng có dấu đậm + glyph trạng thái + tiền tố nhánh) thay vì danh sách radio
phẳng: `st.radio` không lồng được và không hiển thị được thứ bậc. Chọn một node
chỉ là nêu **ngữ cảnh đang xem** và gợi ý workspace liên quan; đổi workspace vẫn
do navbar quyết định.

Bất biến render (BUG-001): **không bao giờ** lồng `st.expander` trong
`st.expander`. Streamlit 1.41 raise `StreamlitAPIException` và làm sập toàn shell
ngay khi project có chapter metadata, nên nhánh sâu dùng markdown có tiền tố.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from novel_ai.core import storage
from novel_ai.core.models import ArtifactStatus, ChapterStatus
from novel_ai.core.project import Project

#: Thứ tự workspace theo luồng chính của spec.
WORKSPACES: tuple[tuple[str, str], ...] = (
    ("co_create", "Co-create"),
    ("architect", "Architect"),
    ("long_plan", "Long Plan"),
    ("short_plan", "Short Plan"),
    ("skeleton", "Skeleton"),
    ("writer", "Writer"),
    ("review", "Review"),
    ("reconcile", "Finalize / Reconcile"),
    ("revision", "Revision / Retcon / Recovery"),
)

#: Prefix hiển thị theo trạng thái artifact để người dùng thấy ngay draft/stale.
_STATUS_BADGE: dict[str, str] = {
    ArtifactStatus.missing.value: "—",
    ArtifactStatus.draft.value: "candidate",
    ArtifactStatus.accepted.value: "accepted",
    ArtifactStatus.stale.value: "STALE",
    ArtifactStatus.rejected.value: "rejected",
}

_CHAPTER_BADGE: dict[str, str] = {
    ChapterStatus.planned.value: "planned",
    ChapterStatus.skeleton_ready.value: "skeleton ready",
    ChapterStatus.draft.value: "draft",
    ChapterStatus.review_required.value: "review required",
    ChapterStatus.finalizing.value: "FINALIZING",
    ChapterStatus.final_reconciled.value: "final",
}

#: Glyph trạng thái cho pane trái (đọc nhanh bằng mắt, không phải nguồn sự thật).
_STATUS_GLYPH: dict[str, str] = {
    ArtifactStatus.missing.value: "○",
    ArtifactStatus.draft.value: "◆",
    ArtifactStatus.accepted.value: "●",
    ArtifactStatus.stale.value: "▲",
    ArtifactStatus.rejected.value: "✕",
}

_CHAPTER_GLYPH: dict[str, str] = {
    ChapterStatus.planned.value: "○",
    ChapterStatus.skeleton_ready.value: "◐",
    ChapterStatus.draft.value: "◆",
    ChapterStatus.review_required.value: "◆",
    ChapterStatus.finalizing.value: "◑",
    ChapterStatus.final_reconciled.value: "●",
}

#: Node kind ⇒ workspace liên quan (nút "Mở workspace" dưới cây).
_KIND_WORKSPACE: dict[str, str] = {
    "idea": "co_create",
    "artifact": "architect",
    "chapter": "skeleton",
    "recovery": "revision",
}

#: Artifact id ⇒ workspace thật sự sửa nó (override `_KIND_WORKSPACE`).
_ARTIFACT_WORKSPACE: dict[str, str] = {
    "base_idea": "co_create",
    "premise": "architect",
    "characters": "architect",
    "world_rules": "architect",
    "foreshadow": "architect",
    "long_plan": "long_plan",
    "short_plan": "short_plan",
}


@dataclass
class TreeNode:
    """Một dòng trong project tree."""

    key: str
    label: str
    kind: str
    status: str = ""
    badge: str = ""
    artifact_id: str | None = None
    chapter_id: str | None = None
    detail: str = ""
    children: list[TreeNode] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "status": self.status,
            "badge": self.badge,
            "artifact_id": self.artifact_id,
            "chapter_id": self.chapter_id,
            "detail": self.detail,
        }


def _artifact_node(project: Project, artifact_id: str, label: str, *, chapter_id: str | None = None) -> TreeNode:
    envelope = storage.load_artifact(project, artifact_id)
    if envelope is None:
        return TreeNode(
            key=artifact_id,
            label=label,
            kind="artifact",
            status=ArtifactStatus.missing.value,
            badge=_STATUS_BADGE[ArtifactStatus.missing.value],
            artifact_id=artifact_id,
            chapter_id=chapter_id,
            detail="chưa có revision nào",
        )
    status = envelope.status.value
    detail_parts: list[str] = []
    if envelope.accepted_revision is not None:
        detail_parts.append(f"accepted r{envelope.accepted_revision.revision}")
    if envelope.candidate_revision is not None:
        detail_parts.append(f"candidate r{envelope.candidate_revision.revision}")
    if envelope.stale_reasons:
        detail_parts.append(f"{len(envelope.stale_reasons)} stale reason")
    return TreeNode(
        key=artifact_id,
        label=label,
        kind="artifact",
        status=status,
        badge=_STATUS_BADGE.get(status, status),
        artifact_id=artifact_id,
        chapter_id=chapter_id,
        detail=" · ".join(detail_parts),
    )


def build_project_tree(project: Project) -> list[TreeNode]:
    """Cây project: idea → foundation → plans → chapters, kèm badge trạng thái."""
    nodes: list[TreeNode] = []

    idea_status = ArtifactStatus.accepted.value if project.paths.base_idea_md.is_file() else ArtifactStatus.missing.value
    nodes.append(
        TreeNode(
            key="base_idea",
            label="Base Idea",
            kind="idea",
            status=idea_status,
            badge=_STATUS_BADGE.get(idea_status, idea_status),
            artifact_id="base_idea",
            detail="accepted" if idea_status == "accepted" else "chưa chốt",
        )
    )

    foundation = TreeNode(key="foundation", label="Foundation", kind="group")
    foundation.children = [
        _artifact_node(project, "premise", "Premise"),
        _artifact_node(project, "characters", "Characters"),
        _artifact_node(project, "world_rules", "World Rules"),
        _artifact_node(project, "foreshadow", "Foreshadow"),
    ]
    nodes.append(foundation)

    plans = TreeNode(key="plans", label="Plans", kind="group")
    plans.children = [
        _artifact_node(project, "long_plan", "Long Plan"),
        _artifact_node(project, "short_plan", "Short Plan"),
    ]
    nodes.append(plans)

    chapters_group = TreeNode(key="chapters", label="Chapters", kind="group")
    for chapter_id in storage.list_chapter_ids(project):
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is None:
            continue
        current = chapter.current_draft
        detail_parts = []
        if chapter.current_draft_revision is not None:
            detail_parts.append(f"prose r{chapter.current_draft_revision}")
        if current is not None and not current.is_complete:
            detail_parts.append("partial")
        if chapter.final_candidate is not None:
            detail_parts.append("final candidate pending")
        chapter_node = TreeNode(
            key=chapter_id,
            label=f"Ch.{chapter.chapter_number} {chapter.title}",
            kind="chapter",
            status=chapter.status.value,
            badge=_CHAPTER_BADGE.get(chapter.status.value, chapter.status.value),
            chapter_id=chapter_id,
            detail=" · ".join(detail_parts),
        )
        chapter_node.children = [
            _artifact_node(
                project, f"skeleton_{chapter_id}", "Skeleton", chapter_id=chapter_id
            ),
            _artifact_node(
                project, f"reconciliation_{chapter_id}", "Reconciliation", chapter_id=chapter_id
            ),
        ]
        chapters_group.children.append(chapter_node)
    nodes.append(chapters_group)

    pending = storage.pending_operation_ids(project)
    if pending:
        nodes.append(
            TreeNode(
                key="pending_operations",
                label="Pending operations",
                kind="recovery",
                status="pending",
                badge="RECOVERY",
                detail=", ".join(pending),
            )
        )
    return nodes


def latest_timeline_labels(project: Project) -> tuple[str, str]:
    """`("chapter N", "synced|stale")` cho panel Arbiter; lỗi đọc ⇒ câu trung tính."""
    try:
        timeline = storage.load_timeline(project)
        relationships = storage.load_relationships(project)
    except storage.StorageError:  # pragma: no cover - file hỏng không làm crash UI
        return "không đọc được", "không đọc được"
    timeline_label = (
        f"chapter {timeline.latest_consistent_chapter or timeline.latest_final_chapter}"
        if (timeline.latest_consistent_chapter or timeline.latest_final_chapter)
        else "chưa có"
    )
    stale = any(entry.stale for entry in timeline.entries)
    relationship_label = "stale" if stale else ("synced" if relationships.relationships else "chưa có")
    return timeline_label, relationship_label


def flatten(nodes: Iterable[TreeNode]) -> list[TreeNode]:
    """Danh sách phẳng theo thứ tự hiển thị (group trước, con sau)."""
    result: list[TreeNode] = []
    for node in nodes:
        result.append(node)
        result.extend(flatten(node.children))
    return result


def artifact_status_map(project: Project) -> dict[str, str]:
    """`artifact_id -> status` cho status bar/Arbiter, đọc từ metadata."""
    result: dict[str, str] = {}
    for node in flatten(build_project_tree(project)):
        if node.artifact_id:
            result[node.artifact_id] = node.status
    return result


