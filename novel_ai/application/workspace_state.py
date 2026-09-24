"""Read model foundation và trạng thái commit cho các workspace tác giả."""

from __future__ import annotations

from typing import Any

from novel_ai.core import storage
from novel_ai.core.models import ArtifactEnvelope, CoCreateDocument
from novel_ai.core.project import Project


def envelope_or_none(project: Project, artifact_id: str) -> ArtifactEnvelope[Any] | None:
    """Envelope typed để editor tác giả hiển thị accepted/candidate riêng."""
    return storage.load_artifact(project, artifact_id)


def co_create_document(project: Project) -> CoCreateDocument:
    """Working state; mở workspace không tạo file mới."""
    return storage.load_co_create(project) or CoCreateDocument()


def base_idea_view(project: Project) -> tuple[str, Any]:
    """Base Idea markdown và metadata accepted từ disk, không suy từ UI state."""
    document = co_create_document(project)
    if not project.paths.base_idea_md.is_file():
        return "", document.base_idea
    try:
        return storage.read_text(project.paths.base_idea_md), document.base_idea
    except storage.StorageError as exc:
        return f"(không đọc được `idea/base_idea.md`: {exc.code})", document.base_idea


def commit_state_counts(project: Project) -> tuple[str, str]:
    """Tóm tắt actual timeline/relationship sau Reconcile commit."""
    try:
        timeline = storage.load_timeline(project)
        timeline_text = (
            f"{len(timeline.entries)} entry · latest_consistent_chapter "
            f"{timeline.latest_consistent_chapter}"
        )
    except storage.StorageError:
        timeline_text = "không đọc được"
    try:
        relationships = storage.load_relationships(project)
        relationship_text = (
            f"{len(relationships.relationships)} state · latest_consistent_chapter "
            f"{relationships.latest_consistent_chapter}"
        )
    except storage.StorageError:
        relationship_text = "không đọc được"
    return timeline_text, relationship_text
