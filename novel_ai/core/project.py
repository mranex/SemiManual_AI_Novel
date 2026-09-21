"""Project, layout đường dẫn và vòng đời project cho Manual AI Novel (T09).

File này hiện thực phần "Project layout" của `docs/design/storage.md` mục 2:

- ``ProjectPaths`` là nguồn duy nhất biết vị trí file/thư mục trong một project;
  mọi thuộc tính đều là ``Path`` tuyệt đối nằm trong project root;
- ``Project.create`` tạo project mới (stable ``project_id``, ``project.json``,
  ``co_create.json`` rỗng và toàn bộ thư mục layout);
- ``Project.open`` mở project theo slug **hoặc** ``project_id`` và đọc lại
  metadata app-owned; không suy đoán lifecycle từ sự tồn tại của file;
- ``resolve_project_dir`` từ chối mọi đường dẫn thoát project root; ``list_projects``
  bỏ qua thư mục hỏng kèm cảnh báo thay vì crash.

Không có luật lifecycle ở đây: file này chỉ đọc/ghi metadata project qua
``novel_ai.core.storage``.
"""

from __future__ import annotations

import re
import unicodedata
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from novel_ai.core import storage, validation
from novel_ai.core.models import (
    CoCreateDocument,
    ProjectConfig,
    generate_operation_id,
    next_stable_id,
    now_iso,
)
from novel_ai.core.storage import (
    MalformedDocumentError,
    PathSafetyError,
    StorageError,
    ensure_within_project,
    read_json,
    save_co_create,
    write_json_atomic,
)
from novel_ai.core.validation import DocumentError

__all__ = [
    "Project",
    "ProjectExistsError",
    "ProjectNotFoundError",
    "ProjectPathError",
    "ProjectPaths",
    "list_projects",
    "resolve_project_dir",
    "slugify",
]

#: Ký tự không được xuất hiện trong slug/project_id (dấu phân cách và ký tự lạ).
_UNSAFE_NAME_RE = re.compile(r"[\\/]")


class ProjectPathError(ValueError):
    """Định danh project không hợp lệ hoặc thoát project root."""


class ProjectError(RuntimeError):
    """Lỗi project nói chung."""

    code = "project_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ProjectNotFoundError(ProjectError):
    """Không tìm thấy project theo slug/project_id."""

    code = "project_not_found"


class ProjectExistsError(ProjectError):
    """Project cùng slug đã tồn tại; không ghi đè dữ liệu."""

    code = "project_exists"


def slugify(title: str) -> str:
    """Chuyển tên truyện thành slug an toàn cho tên thư mục.

    Bỏ dấu tiếng Việt (NFD + xóa combining mark, ``đ`` -> ``d``), chỉ giữ
    ``[a-z0-9-]``, gộp dấu gạch; fallback ``"project"`` khi không còn ký tự nào.
    """
    text = unicodedata.normalize("NFD", str(title))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("đ", "d").replace("Đ", "D").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "project"


def _validate_project_name(value: str) -> str:
    """Chuẩn hóa và kiểm tra định danh project trước khi ghép đường dẫn."""
    text = str(value or "").strip()
    if not text:
        raise ProjectPathError("Định danh project không được rỗng.")
    if _UNSAFE_NAME_RE.search(text):
        raise ProjectPathError(
            f"Định danh project không được chứa dấu phân cách thư mục: {value!r}."
        )
    if text in {".", ".."} or text.startswith("."):
        raise ProjectPathError(f"Định danh project không hợp lệ: {value!r}.")
    if len(text) > 128:
        raise ProjectPathError("Định danh project quá dài (tối đa 128 ký tự).")
    if any(char in text for char in '<>:"|?*'):
        raise ProjectPathError(f"Định danh project chứa ký tự không hợp lệ: {value!r}.")
    return text


def resolve_project_dir(projects_root: Path, project_id_or_slug: str) -> Path:
    """Trả thư mục project tuyệt đối; từ chối mọi path escape.

    Hàm chỉ resolve + kiểm tra an toàn (không kiểm tra tồn tại) để ``create``
    dùng được cho project chưa có.
    """
    root = Path(projects_root).expanduser().resolve()
    name = _validate_project_name(project_id_or_slug)
    candidate = ensure_within_project(root, root / name)
    if candidate == root:
        raise ProjectPathError("Định danh project không được trỏ vào chính projects root.")
    return candidate


@dataclass
class ProjectPaths:
    """Toàn bộ vị trí file/thư mục của một project (layout T03)."""

    root: Path

    project_json: Path = field(init=False)
    co_create_json: Path = field(init=False)
    base_idea_md: Path = field(init=False)
    base_idea_meta_json: Path = field(init=False)
    premise_json: Path = field(init=False)
    characters_json: Path = field(init=False)
    world_rules_json: Path = field(init=False)
    foreshadow_json: Path = field(init=False)
    long_plan_json: Path = field(init=False)
    short_plan_json: Path = field(init=False)
    rolling_dir: Path = field(init=False)
    chapters_dir: Path = field(init=False)
    state_dir: Path = field(init=False)
    timeline_json: Path = field(init=False)
    relationships_json: Path = field(init=False)
    snapshots_dir: Path = field(init=False)
    raw_dir: Path = field(init=False)
    history_dir: Path = field(init=False)
    ops_pending_dir: Path = field(init=False)
    ops_done_dir: Path = field(init=False)
    lock_path: Path = field(init=False)

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        self.root = root
        self.project_json = root / "project.json"
        self.co_create_json = root / "co_create.json"
        self.base_idea_md = root / "idea" / "base_idea.md"
        self.base_idea_meta_json = root / "idea" / "base_idea.meta.json"
        self.premise_json = root / "architect" / "premise.json"
        self.characters_json = root / "architect" / "characters.json"
        self.world_rules_json = root / "architect" / "world_rules.json"
        self.foreshadow_json = root / "architect" / "foreshadow.json"
        self.long_plan_json = root / "plans" / "long_plan.json"
        self.short_plan_json = root / "plans" / "short_plan.json"
        self.rolling_dir = root / "plans" / "rolling"
        self.chapters_dir = root / "chapters"
        self.state_dir = root / "state"
        self.timeline_json = root / "state" / "current_timeline.json"
        self.relationships_json = root / "state" / "relationships.json"
        self.snapshots_dir = root / "state" / "snapshots"
        self.raw_dir = root / "raw"
        self.history_dir = root / "history"
        self.ops_pending_dir = root / ".ops" / "pending"
        self.ops_done_dir = root / ".ops" / "done"
        self.lock_path = root / ".locks" / "project.lock"

    # -- chapter ---------------------------------------------------------
    def chapter_dir(self, chapter_id: str) -> Path:
        """``chapters/<chapter_id>/`` (từ chối ``..``/dấu phân cách)."""
        return self.chapters_dir / _chapter_segment(chapter_id)

    def chapter_json(self, chapter_id: str) -> Path:
        return self.chapter_dir(chapter_id) / "chapter.json"

    def skeleton_json(self, chapter_id: str) -> Path:
        return self.chapter_dir(chapter_id) / "skeleton.json"

    def drafts_dir(self, chapter_id: str) -> Path:
        return self.chapter_dir(chapter_id) / "drafts"

    def final_dir(self, chapter_id: str) -> Path:
        return self.chapter_dir(chapter_id) / "final"

    def review_reports_dir(self, chapter_id: str) -> Path:
        return self.chapter_dir(chapter_id) / "review_reports"

    def reconcile_dir(self, chapter_id: str) -> Path:
        return self.chapter_dir(chapter_id) / "reconcile"

    def retcon_dir(self, chapter_id: str) -> Path:
        return self.chapter_dir(chapter_id) / "retcon"

    # -- tiện ích --------------------------------------------------------
    def layout_dirs(self) -> list[Path]:
        """Các thư mục phải tồn tại sau ``Project.ensure_layout``."""
        return [
            self.root / "idea",
            self.root / "architect",
            self.root / "plans",
            self.rolling_dir,
            self.chapters_dir,
            self.state_dir,
            self.snapshots_dir,
            self.raw_dir,
            self.history_dir,
            self.ops_pending_dir,
            self.ops_done_dir,
            self.lock_path.parent,
        ]

    def artifact_path(self, artifact_id: str, artifact_type: str) -> Path:
        """Đường dẫn tuyệt đối của một artifact theo layout T03."""
        relpath = storage.artifact_relpath(artifact_id, artifact_type)
        return ensure_within_project(self.root, self.root / relpath)


def _chapter_segment(chapter_id: str) -> str:
    text = str(chapter_id or "").strip()
    if not text or text in {".", ".."} or _UNSAFE_NAME_RE.search(text) or "/" in text:
        raise ProjectPathError(f"chapter_id không hợp lệ: {chapter_id!r}.")
    return text


def _project_ids_in(projects_root: Path) -> list[str]:
    """Đọc ``project_id`` của các project hiện có (bỏ qua thư mục hỏng)."""
    if not projects_root.is_dir():
        return []
    ids: list[str] = []
    for directory in sorted(projects_root.iterdir()):
        if not directory.is_dir() or not (directory / "project.json").is_file():
            continue
        try:
            data = read_json(directory / "project.json")
        except StorageError:
            continue
        project_id = data.get("project_id") if isinstance(data, dict) else None
        if isinstance(project_id, str) and project_id:
            ids.append(project_id)
    return ids


def _find_project_dir(projects_root: Path, project_id_or_slug: str) -> Path:
    """Tìm thư mục project theo slug trước, rồi theo ``project_id``."""
    root = Path(projects_root)
    directory_hint = resolve_project_dir(root, project_id_or_slug)
    if (directory_hint / "project.json").is_file():
        return directory_hint
    if not root.is_dir():
        raise ProjectNotFoundError(
            f"Projects root chưa tồn tại: {root}."
        )
    for directory in sorted(root.iterdir()):
        candidate = directory / "project.json"
        if not candidate.is_file():
            continue
        try:
            data = read_json(candidate)
        except StorageError:
            continue
        if isinstance(data, dict) and data.get("project_id") == project_id_or_slug:
            return directory.resolve()
    raise ProjectNotFoundError(
        f"Không tìm thấy project `{project_id_or_slug}` trong {root}."
    )


@dataclass
class Project:
    """Project đã mở: root, ``project.json`` đã parse và layout đường dẫn."""

    root: Path
    config: ProjectConfig
    paths: ProjectPaths

    # -- tạo/mở ----------------------------------------------------------
    @classmethod
    def create(
        cls,
        projects_root: Path,
        title: str,
        *,
        default_language: str = "vi",
        genre_prompt_id: str = "custom",
        writing_style_id: str = "default",
        project_id: str | None = None,
        now: str | None = None,
    ) -> Project:
        """Tạo project mới trong ``projects_root`` theo slug của ``title``."""
        root = Path(projects_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        directory = resolve_project_dir(root, slugify(title))
        if (directory / "project.json").is_file():
            raise ProjectExistsError(
                f"Project đã tồn tại ở {directory}; không ghi đè dữ liệu người dùng."
            )
        stamp = now or now_iso()
        resolved_project_id = project_id or next_stable_id("proj", _project_ids_in(root))
        config = ProjectConfig(
            project_id=resolved_project_id,
            title=title,
            default_language=default_language,
            genre_prompt_id=genre_prompt_id,
            writing_style_id=writing_style_id,
            created_at=stamp,
            updated_at=stamp,
        )
        project = cls(root=directory, config=config, paths=ProjectPaths(directory))
        project.ensure_layout()
        write_json_atomic(
            project.paths.project_json,
            config,
            operation_id=f"create_{resolved_project_id}",
        )
        if not project.paths.co_create_json.is_file():
            save_co_create(
                project,
                CoCreateDocument(),
                operation_id=f"create_{resolved_project_id}",
            )
        return project

    @classmethod
    def open(cls, projects_root: Path, project_id_or_slug: str) -> Project:
        """Mở project theo slug hoặc ``project_id`` và đọc ``project.json``."""
        directory = _find_project_dir(Path(projects_root).expanduser(), project_id_or_slug)
        target = ensure_within_project(directory, directory / "project.json")
        if not target.is_file():
            raise ProjectNotFoundError(f"Thư mục {directory} không có project.json.")
        config = cls._read_config(target)
        return cls(root=directory, config=config, paths=ProjectPaths(directory))

    @staticmethod
    def _read_config(path: Path) -> ProjectConfig:
        try:
            return validation.parse_project_config(read_json(path))
        except DocumentError as exc:
            raise MalformedDocumentError(
                f"project.json không đúng contract: {exc}",
                path=path,
                code="invalid_document",
                errors=exc.errors,
            ) from exc

    # -- layout ----------------------------------------------------------
    def ensure_layout(self) -> None:
        """Tạo mọi thư mục layout (idempotent), không đụng tới file dữ liệu."""
        for directory in self.paths.layout_dirs():
            ensure_within_project(self.root, directory).mkdir(parents=True, exist_ok=True)

    # -- config ----------------------------------------------------------
    def reload(self) -> ProjectConfig:
        """Đọc lại ``project.json`` từ disk (ví dụ sau khi process khác ghi)."""
        target = ensure_within_project(self.root, self.paths.project_json)
        if not target.is_file():
            raise ProjectNotFoundError(f"Không thấy {target}.")
        self.config = self._read_config(target)
        return self.config

    def update_config(self, **changes: Any) -> ProjectConfig:
        """Cập nhật ``project.json`` qua storage và bump ``updated_at``.

        Field lạ hoặc sai kiểu bị ``novel_ai.core.validation`` từ chối; dữ liệu
        trên disk chỉ đổi khi ghi atomic thành công.
        """
        data = self.config.model_dump(mode="json")
        data.update(changes)
        data["updated_at"] = now_iso()
        try:
            config = validation.parse_project_config(data)
        except DocumentError as exc:
            raise MalformedDocumentError(
                f"project.json sau cập nhật không hợp lệ: {exc}",
                path=self.paths.project_json,
                code="invalid_document",
                errors=exc.errors,
            ) from exc
        write_json_atomic(
            self.paths.project_json, config, operation_id=generate_operation_id()
        )
        self.config = config
        return config

    # -- tiện ích --------------------------------------------------------
    @property
    def slug(self) -> str:
        return self.root.name

    def to_summary(self) -> dict[str, Any]:
        """Bản tóm tắt cho UI/arbiter, không chứa secret."""
        return {
            "project_id": self.config.project_id,
            "title": self.config.title,
            "slug": self.slug,
            "path": str(self.root),
            "current_chapter": self.config.current_chapter,
        }


def list_projects(
    projects_root: Path, *, warnings_out: list[str] | None = None
) -> list[dict[str, Any]]:
    """Liệt kê project hợp lệ; thư mục hỏng bị bỏ qua kèm cảnh báo.

    Trả list dict ``{project_id, title, slug, path, current_chapter}`` đã sort
    theo slug. Không mutate gì trên disk.
    """
    root = Path(projects_root).expanduser()
    projects: list[dict[str, Any]] = []
    if not root.is_dir():
        return projects
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        if not (directory / "project.json").is_file():
            continue
        try:
            project = Project.open(root, directory.name)
        except (StorageError, ProjectError, ValueError, OSError) as exc:
            message = (
                f"Bỏ qua thư mục project không đọc được `{directory.name}`: {exc}"
            )
            if warnings_out is not None:
                warnings_out.append(message)
            warnings.warn(message, RuntimeWarning, stacklevel=2)
            continue
        projects.append(project.to_summary())
    projects.sort(key=lambda item: item["slug"])
    return projects
