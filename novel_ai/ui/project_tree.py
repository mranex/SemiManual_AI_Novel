"""Project tree và navigation theo workspace (T19, pane trái của shell T25).

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


# ---------------------------------------------------------------------------
# Pane trái: glyph, nhãn và điều hướng
# ---------------------------------------------------------------------------


def node_glyph(node: TreeNode) -> str:
    """Glyph trạng thái của node (group/recovery có glyph riêng)."""
    if node.kind == "group":
        return "▾"
    if node.kind == "recovery":
        return "⚠"
    if node.kind == "chapter":
        return _CHAPTER_GLYPH.get(node.status, "○")
    return _STATUS_GLYPH.get(node.status, "○")


def node_workspace(node: TreeNode) -> str | None:
    """Workspace liên quan tới node; `None` cho group."""
    if node.kind == "group":
        return None
    if node.kind == "artifact" and node.artifact_id in _ARTIFACT_WORKSPACE:
        return _ARTIFACT_WORKSPACE[node.artifact_id]
    if node.kind == "artifact" and node.artifact_id:
        # `skeleton_<chapter>` / `reconciliation_<chapter>` → workspace chương.
        if node.artifact_id.startswith("skeleton"):
            return "skeleton"
        if node.artifact_id.startswith("reconciliation"):
            return "reconcile"
        return "architect"
    return _KIND_WORKSPACE.get(node.kind)


def node_line(node: TreeNode) -> str:
    """Một dòng markdown cho node: `glyph **nhãn** — badge (chi tiết)`.

    Badge rỗng (`—` cho artifact chưa có revision) không được lặp lại thành
    `— —`: khi đó chi tiết đã nói rõ trạng thái rồi.
    """
    parts = [f"{node_glyph(node)} **{node.label}**"]
    if node.badge and node.badge != "—":
        parts.append(f"— {node.badge}")
    if node.detail:
        parts.append(f"({node.detail})")
    return " ".join(parts)


def node_count(nodes: Iterable[TreeNode]) -> int:
    """Số node lá (không tính group) để hiển thị tổng quan."""
    return sum(1 for node in flatten(nodes) if node.kind != "group")


#: Tiền tố nhánh cho tầng con của cây (thay cho expander lồng nhau — BUG-001).
_BRANCH_PREFIX: tuple[str, str] = ("├─", "└─")


def subtree_contains(node: TreeNode, key: str | None) -> bool:
    """True nếu `key` là chính node hoặc nằm trong bất kỳ descendant nào."""
    if key is None:
        return False
    if node.key == key:
        return True
    return any(subtree_contains(child, key) for child in node.children)


def _branch_line(node: TreeNode, *, is_last: bool) -> str:
    """Dòng con của một nhánh: tiền tố cây + nội dung markdown của node."""
    return f"{_BRANCH_PREFIX[1] if is_last else _BRANCH_PREFIX[0]} {node_line(node)}"


def _render_group(node: TreeNode, selected: str | None, *, nested: bool) -> None:
    """Vẽ một nhánh cây với **tối đa một tầng** `st.expander`.

    Streamlit 1.41 cấm expander lồng nhau; BUG-001 làm sập toàn shell ngay khi
    project có chapter metadata (nhóm `Chapters` mở expander, rồi từng chapter có
    children mở expander thứ hai). Vì vậy chỉ tầng ngoài cùng dùng expander; tầng
    sâu hơn render bằng dòng markdown có tiền tố nhánh, giữ nguyên hierarchy
    group → chapter → skeleton/reconciliation.
    """
    import streamlit as st

    children = [child for child in node.children if child.kind != "group"]
    label = f"{node.label} ({len(children)})"
    # Nhóm có chapter là vùng đang làm việc: mở sẵn để cây không bị ẩn mặc định.
    expanded = subtree_contains(node, selected) or any(
        child.kind == "chapter" for child in node.children
    )

    def _render_children() -> None:
        for index, child in enumerate(node.children):
            is_last = index == len(node.children) - 1
            if not child.children:
                st.markdown(_branch_line(child, is_last=is_last))
                continue
            st.markdown(node_line(child))
            for inner_index, grandchild in enumerate(child.children):
                st.markdown(
                    _branch_line(
                        grandchild, is_last=inner_index == len(child.children) - 1
                    )
                )

    if nested:
        st.markdown(f"**{label}**")
        _render_children()
        return
    with st.expander(label, expanded=expanded):
        _render_children()


def render(project: Project, *, key: str = "project_tree") -> TreeNode | None:
    """Render pane trái: cây project + chọn node ngữ cảnh (Streamlit).

    Trả node đang được chọn trong `st.session_state[key]` (hoặc `None`). Hàm chỉ
    đọc state và vẽ; không ghi file và không gọi LLM.
    """
    import streamlit as st

    nodes = build_project_tree(project)
    if not nodes:
        st.caption("Project chưa có artifact nào.")
        return None
    rows = flatten(nodes)
    by_key = {node.key: node for node in rows}
    selected = st.session_state.get(key)
    selected_node = by_key.get(selected) if isinstance(selected, str) else None

    st.caption(
        f"{node_count(nodes)} node · nguồn sự thật là file trong project, "
        "không phải session state."
    )
    for node in nodes:
        if node.children:
            _render_group(
                node, selected_node.key if selected_node else None, nested=False
            )
        else:
            st.markdown(node_line(node))

    leaves = [node for node in rows if node.kind != "group"]
    if leaves:
        options = [node.key for node in leaves]
        # Node con của chapter (`Skeleton`/`Reconciliation`) có cùng nhãn ở mọi
        # chapter, nên nhãn selectbox phải kèm chapter để chọn đúng ngữ cảnh.
        chapter_titles = {
            node.key: node.label for node in rows if node.kind == "chapter"
        }
        labels = {}
        for node in leaves:
            suffix = ""
            if node.chapter_id and node.chapter_id in chapter_titles:
                suffix = f" · {chapter_titles[node.chapter_id]}"
            labels[node.key] = f"{node_glyph(node)} {node.label}{suffix} — {node.badge}"
        widget_key = f"{key}_selection"
        # Không truyền `index` động: `index` nằm trong công thức element id của
        # Streamlit, nên đổi nó giữa các rerun làm widget mất state và lựa chọn
        # người dùng vừa bấm bị reset về option đầu. Mặc định ổn định (option đầu)
        # + widget state của Streamlit là đường duy nhất giữ lựa chọn qua rerun.
        choice = st.selectbox(
            "Node đang xem",
            options,
            format_func=lambda value: labels.get(value, value),
            key=widget_key,
            label_visibility="collapsed",
        )
        st.session_state[key] = choice
        selected_node = by_key.get(str(choice)) if choice else None
    if selected_node is not None:
        st.caption(f"Đang xem: {node_line(selected_node)}")
    return selected_node


def workspace_labels() -> list[str]:
    return [label for _key, label in WORKSPACES]


def workspace_key(label: str) -> str:
    for key, name in WORKSPACES:
        if name == label:
            return key
    raise KeyError(label)
