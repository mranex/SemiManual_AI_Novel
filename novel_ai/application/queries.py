"""Read model thuần Python cho nhiều UI; mọi query chỉ đọc project files."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from novel_ai.config import AppConfig
from novel_ai.core import storage
from novel_ai.core.project import Project, list_projects
from novel_ai.application import arbiter, chapter_state, planning_views, project_tree, status, workspace_state
from novel_ai.services import writer
from novel_ai.services import long_planner, short_planner

Audience = Literal["author", "writer"]


class QueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class QueryResult:
    kind: str
    project_id: str | None
    data: dict[str, Any] = field(default_factory=dict)
    needs_recovery: bool = False
    read_only: bool = False
    write_blocked: bool = False


def project_tree_nodes(project: Project) -> list[project_tree.TreeNode]:
    """Internal view model cho Streamlit renderer, không mutate."""
    return project_tree.build_project_tree(project)


def arbiter_report(project: Project) -> arbiter.ArbiterReport:
    """Rule based suggestions; không chạy bước tiếp theo."""
    return arbiter.analyze(project)


def _tree_data(nodes: list[project_tree.TreeNode]) -> list[dict[str, Any]]:
    return [{**node.as_dict(), "children": _tree_data(node.children)} for node in nodes]


def _recovery_flags(project: Project) -> dict[str, Any]:
    pending = storage.pending_operation_ids(project)
    needs_recovery = storage.needs_recovery(project)
    read_only = storage.requires_manual_recovery(project)
    return {
        "pending_operation_ids": pending,
        "needs_recovery": needs_recovery,
        "read_only": read_only,
        "write_blocked": needs_recovery or read_only,
    }


def recovery_flags(project: Project) -> dict[str, Any]:
    """Trạng thái recovery thuần read cho shell; action recovery nằm riêng."""
    return _recovery_flags(project)


class ProjectQueries:
    """Queries chỉ nhận stable project ID, lấy root từ cấu hình app nội bộ."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def list_projects(self) -> QueryResult:
        warnings: list[str] = []
        rows = list_projects(self.config.projects_root, warnings_out=warnings)
        safe_rows = [
            {key: row[key] for key in ("project_id", "title", "slug", "current_chapter")}
            for row in rows
        ]
        # Không trả warning của core vì nó có thể chứa absolute path/exception text.
        return QueryResult("list_projects", None, {"projects": safe_rows, "skipped_count": len(warnings)})

    def resolve(self, project_id: str) -> Project:
        if not isinstance(project_id, str) or not project_id.strip():
            raise QueryError("invalid_project_id", "Cần stable project_id.")
        # Project.open cũng hỗ trợ slug; boundary này chỉ chấp nhận ID đã liệt kê.
        for row in list_projects(self.config.projects_root):
            if row["project_id"] == project_id:
                return Project.open(self.config.projects_root, str(row["slug"]))
        raise QueryError("project_not_found", "Không tìm thấy project_id trong root đã cấu hình.")

    def _result(self, kind: str, project: Project, data: dict[str, Any]) -> QueryResult:
        flags = _recovery_flags(project)
        return QueryResult(
            kind,
            project.config.project_id,
            data,
            needs_recovery=flags["needs_recovery"],
            read_only=flags["read_only"],
            write_blocked=flags["write_blocked"],
        )

    def open_project(self, project_id: str) -> QueryResult:
        project = self.resolve(project_id)
        config = project.config
        return self._result(
            "open_project",
            project,
            {
                "project_id": config.project_id,
                "title": config.title,
                "slug": project.slug,
                "current_chapter": config.current_chapter,
                "default_language": config.default_language,
                "default_pov": config.default_pov,
                "default_length_guidance": config.default_length_guidance,
                "auto_accept_structured": config.auto_accept_structured,
                "fingerprint": storage.file_fingerprint(project.paths.project_json),
                "pending_operation_ids": storage.pending_operation_ids(project),
            },
        )

    def foundation(self, project_id: str) -> QueryResult:
        """Persisted Co-create working state and accepted Base Idea for the author UI."""
        project = self.resolve(project_id)
        document = workspace_state.co_create_document(project)
        markdown, metadata = workspace_state.base_idea_view(project)
        return self._result("foundation", project, {
            "co_create": {
                "status": document.status.value,
                "idea_state": document.idea_state.model_dump(mode="json") if document.idea_state else None,
                "messages": [item.model_dump(mode="json") for item in document.messages],
            },
            "base_idea_markdown": markdown,
            "base_idea_metadata": {"status": metadata.status.value, "revision": metadata.revision,
                                   "accepted_at": metadata.accepted_at, "accepted_by": metadata.accepted_by} if metadata else None,
            "fingerprint": storage.file_fingerprint(project.paths.co_create_json),
        })

    def planning(self, project_id: str) -> QueryResult:
        """Options and user writing contract for Long/Short Plan forms; read only."""
        project = self.resolve(project_id)
        long_plan = planning_views.accepted_long_plan(project)
        assignments: dict[str, list[dict[str, Any]]] = {}
        if long_plan is not None:
            for volume in long_plan.volumes:
                for arc in volume.arcs:
                    try:
                        assignments[arc.arc_id] = short_planner.resolve_assigned_chapters(project, arc)
                    except Exception as exc:
                        if getattr(exc, "code", "") != "no_chapters_to_plan":
                            raise
                        assignments[arc.arc_id] = []
        return self._result("planning", project, {
            "planning_scope": long_planner.stored_planning_scope(project),
            "arcs": planning_views.arc_options(project),
            "assigned_chapters": assignments,
            "writing_defaults": short_planner.project_writing_defaults(project),
            "eligible_rolling": short_planner.eligible_chapters_for_rolling(project),
            "allow_relationship_replan": project.config.allow_relationship_replan,
        })

    def tree(self, project_id: str) -> QueryResult:
        project = self.resolve(project_id)
        return self._result("project_tree", project, {"nodes": _tree_data(project_tree_nodes(project))})

    def arbiter(self, project_id: str) -> QueryResult:
        project = self.resolve(project_id)
        report = arbiter_report(project)
        return self._result(
            "arbiter_status",
            project,
            {
                "report": report.as_dict(),
                "summary": arbiter.summarize(report),
                "status_rows": arbiter.status_rows(project, report),
            },
        )

    def status(self, project_id: str, *, workspace: str = "", chapter_id: str | None = None) -> QueryResult:
        project = self.resolve(project_id)
        view = status.build_status_bar(project, config=self.config, workspace=workspace, chapter_id=chapter_id)
        return self._result("status", project, view.as_dict())

    def artifact(self, project_id: str, artifact_id: str, *, audience: Audience = "author") -> QueryResult:
        project = self.resolve(project_id)
        if audience not in ("author", "writer"):
            raise QueryError("invalid_audience", "Audience không hợp lệ.")
        if not artifact_id or "/" in artifact_id or "\\" in artifact_id:
            raise QueryError("invalid_artifact_id", "Artifact ID không hợp lệ.")
        envelope = storage.load_artifact(project, artifact_id)
        if envelope is None:
            return self._result("artifact", project, {"artifact_id": artifact_id, "status": "missing", "accepted": None, "candidate": None, "stale_reasons": [], "fingerprint": ""})

        def revision_data(revision: Any) -> dict[str, Any] | None:
            if revision is None:
                return None
            data = {
                "revision": revision.revision,
                "validation_state": revision.validation.state.value,
                "planning_scope": revision.planning_scope.model_dump(mode="json") if revision.planning_scope else None,
            }
            # Writer-facing query không nhận full author truth/future plot.
            if audience == "author":
                payload = revision.payload
                data["payload"] = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
            return data

        return self._result(
            "artifact",
            project,
            {
                "artifact_id": artifact_id,
                "artifact_type": envelope.artifact_type,
                "status": envelope.status.value,
                "fingerprint": storage.file_fingerprint(project.paths.artifact_path(artifact_id, envelope.artifact_type)),
                "accepted": revision_data(envelope.accepted_revision),
                "candidate": revision_data(envelope.candidate_revision),
                "stale_reasons": [item.model_dump(mode="json") for item in envelope.stale_reasons],
            },
        )

    def chapter(self, project_id: str, chapter_id: str, *, audience: Audience = "author") -> QueryResult:
        project = self.resolve(project_id)
        if audience not in ("author", "writer"):
            raise QueryError("invalid_audience", "Audience không hợp lệ.")
        if not chapter_id or "/" in chapter_id or "\\" in chapter_id:
            raise QueryError("invalid_chapter_id", "Chapter ID không hợp lệ.")
        chapter = storage.load_chapter(project, chapter_id)
        if chapter is None:
            return self._result("chapter", project, {"chapter_id": chapter_id, "status": "missing", "fingerprint": ""})
        draft = chapter.current_draft
        data = {
            "chapter_id": chapter.chapter_id,
            "chapter_number": chapter.chapter_number,
            "title": chapter.title,
            "status": chapter.status.value,
            "fingerprint": storage.file_fingerprint(project.paths.chapter_json(chapter_id)),
            "current_draft_revision": chapter.current_draft_revision,
            "draft_complete": draft.is_complete if draft else None,
            "human_review_revision": chapter.human_review.prose_revision if chapter.human_review else None,
            "final_candidate_revision": chapter.final_candidate.prose_revision if chapter.final_candidate else None,
            "final_revision": chapter.final_revision.revision if chapter.final_revision else None,
            "retcon_open": chapter_state.retcon_marker(project, chapter_id) is not None,
        }
        if audience == "author":
            data["short_plan_pin"] = chapter.short_plan_pin.model_dump(mode="json")
            data["skeleton_pin"] = chapter.skeleton_pin.model_dump(mode="json") if chapter.skeleton_pin else None
            data["draft_text"] = chapter_state.prose_text(project, chapter_id, chapter.current_draft_revision)
            data["human_review"] = chapter.human_review.model_dump(mode="json") if chapter.human_review else None
            data["ai_review_reports"] = [item.model_dump(mode="json") for item in chapter.ai_review_reports]
            data["final_candidate"] = {"prose_revision": chapter.final_candidate.prose_revision,
                                       "reconciliation_status": chapter.final_candidate.reconciliation_status.value} if chapter.final_candidate else None
            data["final_text"] = chapter_state.read_text_relpath(project, chapter.final_revision.markdown_ref) if chapter.final_revision else ""
        return self._result("chapter", project, data)

    def recovery(self, project_id: str) -> QueryResult:
        project = self.resolve(project_id)
        return self._result("recovery", project, _recovery_flags(project))

    def revision(self, project_id: str) -> QueryResult:
        """Read-only revision dashboard, with no project file paths in the DTO."""
        project = self.resolve(project_id)
        history = chapter_state.history_entries(project)
        return self._result("revision", project, {
            "blockers": chapter_state.stale_blockers(project),
            "chapters": [
                {**vars(row), "note": row.note(),
                 "retcon": (
                     {key: marker.get(key) for key in ("status", "replaces_final_revision",
                                                      "retcon_draft_revision", "started_at", "final_still_canon")}
                     if (marker := chapter_state.retcon_marker(project, row.chapter_id)) else None
                 )}
                for row in chapter_state.version_rows(project)
            ],
            "artifacts": chapter_state.artifact_rows(project),
            "snapshots": chapter_state.snapshot_rows(project),
            "history": [
                {key: entry.get(key) for key in ("operation_id", "operation_type", "status", "created_at")}
                | {"applied_count": len(entry.get("applied_paths") or []),
                   "before_count": len(entry.get("before_files") or [])}
                for entry in history[-20:]
            ],
        })

    def latest_writer_operation(self, project_id: str, chapter_id: str) -> QueryResult:
        """Thông tin phục hồi Writer để surface hiển thị sau khi mở lại app."""
        project = self.resolve(project_id)
        if not chapter_id or "/" in chapter_id or "\\" in chapter_id:
            raise QueryError("invalid_chapter_id", "Chapter ID không hợp lệ.")
        record = writer.latest_operation_for(project, chapter_id)
        if not record:
            return self._result("latest_writer_operation", project, {"operation": None})
        allowed = ("operation_type", "operation_id", "revision", "is_complete",
                   "stream_status", "reason", "raw_output_ref")
        safe = {key: record[key] for key in allowed if key in record}
        raw_ref = safe.get("raw_output_ref")
        if isinstance(raw_ref, str) and (raw_ref.startswith(("/", "\\")) or ":" in raw_ref):
            safe["raw_output_ref"] = None
        return self._result("latest_writer_operation", project, {"operation": safe})
