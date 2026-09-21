"""Project create/open, layout T03 và path safety (T09).

Test chỉ dùng `tmp_path`; không đọc/ghi `projects/` thật của người dùng.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ai.core.project import (
    Project,
    ProjectExistsError,
    ProjectNotFoundError,
    ProjectPathError,
    ProjectPaths,
    list_projects,
    resolve_project_dir,
    slugify,
)
from novel_ai.core.storage import MalformedDocumentError


def _projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_removes_vietnamese_diacritics() -> None:
    assert slugify("Nồi Canh Bên Đường") == "noi-canh-ben-duong"
    assert slugify("Đắc Nhân Tâm") == "dac-nhan-tam"
    assert slugify("  Truyện   Dài!!  ") == "truyen-dai"


def test_slugify_fallback_to_project() -> None:
    assert slugify("漢字") == "project"
    assert slugify("") == "project"
    assert slugify("---") == "project"


# ---------------------------------------------------------------------------
# create/open
# ---------------------------------------------------------------------------


def test_create_project_lays_out_contract_paths(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    project = Project.create(root, "Nồi Canh Bên Đường", project_id="proj_0001")

    assert project.root == (root / "noi-canh-ben-duong").resolve()
    paths = project.paths
    for path in (
        paths.project_json,
        paths.co_create_json,
        paths.chapters_dir,
        paths.state_dir,
        paths.snapshots_dir,
        paths.raw_dir,
        paths.history_dir,
        paths.ops_pending_dir,
        paths.ops_done_dir,
        paths.rolling_dir,
        paths.lock_path.parent,
    ):
        assert path.is_absolute(), path
        assert path.exists(), path

    data = json.loads(paths.project_json.read_text(encoding="utf-8"))
    assert data["project_id"] == "proj_0001"
    assert data["schema_version"] == 2
    assert data["current_chapter"] == 1
    assert data["title"] == "Nồi Canh Bên Đường"
    co_create = json.loads(paths.co_create_json.read_text(encoding="utf-8"))
    assert co_create["status"] == "working"


def test_create_assigns_next_project_id(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    first = Project.create(root, "Truyện Một")
    second = Project.create(root, "Truyện Hai")

    assert first.config.project_id == "proj_0001"
    assert second.config.project_id == "proj_0002"


def test_create_then_open_restores_metadata(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    Project.create(root, "Truyện Một", project_id="proj_0007")

    by_slug = Project.open(root, "truyen-mot")
    by_id = Project.open(root, "proj_0007")

    assert by_slug.config.project_id == "proj_0007"
    assert by_slug.config.title == "Truyện Một"
    assert by_slug.config.schema_version == 2
    assert by_id.root == by_slug.root
    assert by_slug.to_summary()["slug"] == "truyen-mot"


def test_reload_and_update_config_persist(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    project = Project.create(
        root, "Truyện Một", project_id="proj_0001", now="2026-01-01T00:00:00+07:00"
    )
    assert project.config.updated_at == "2026-01-01T00:00:00+07:00"

    updated = project.update_config(current_chapter=3, auto_accept_structured=True)
    assert updated.current_chapter == 3
    assert updated.updated_at != "2026-01-01T00:00:00+07:00"

    reopened = Project.open(root, "truyen-mot")
    assert reopened.config.current_chapter == 3
    assert reopened.config.auto_accept_structured is True
    assert reopened.reload().current_chapter == 3


def test_update_config_rejects_unknown_field(tmp_path: Path) -> None:
    project = Project.create(_projects_root(tmp_path), "Truyện Một")

    with pytest.raises(MalformedDocumentError):
        project.update_config(khong_co_field_nay=1)

    # Dữ liệu trên disk không bị đổi.
    assert Project.open(project.root.parent, "truyen-mot").config.current_chapter == 1


def test_create_duplicate_slug_refuses(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    Project.create(root, "Truyện Một")

    with pytest.raises(ProjectExistsError):
        Project.create(root, "Truyện Một")


def test_open_missing_project_raises(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)

    with pytest.raises(ProjectNotFoundError):
        Project.open(root, "khong-ton-tai")


def test_open_missing_projects_root_raises(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        Project.open(tmp_path / "chua-co", "truyen-mot")


def test_open_reports_malformed_project_json(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    project = Project.create(root, "Truyện Một")
    project.paths.project_json.write_text("{ khong phai json", encoding="utf-8")

    with pytest.raises(MalformedDocumentError) as excinfo:
        Project.open(root, "truyen-mot")

    assert str(project.paths.project_json) in str(excinfo.value)
    assert project.paths.project_json.read_text(encoding="utf-8") == "{ khong phai json"


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["..", "../..", "a/b", "a\\b", "/etc/passwd", "con:name", ".hidden", "", "  ", "x/../../y"],
)
def test_resolve_project_dir_rejects_unsafe_names(tmp_path: Path, bad_name: str) -> None:
    root = _projects_root(tmp_path)

    with pytest.raises(ProjectPathError):
        resolve_project_dir(root, bad_name)


def test_resolve_project_dir_rejects_absolute_path_outside_root(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)

    with pytest.raises(ProjectPathError):
        resolve_project_dir(root, str(tmp_path / "outside"))


def test_project_open_rejects_escape(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    Project.create(root, "Truyện Một")

    for bad in ("../..", "..", "../truyen-mot", "a/b"):
        with pytest.raises(ProjectPathError):
            Project.open(root, bad)


def test_resolve_project_dir_accepts_slug(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    assert resolve_project_dir(root, "noi-canh-ben-duong") == (
        root / "noi-canh-ben-duong"
    ).resolve()


@pytest.mark.parametrize("bad_chapter", ["..", "../events", "ch_0001/../../x", "", "a\\b"])
def test_project_paths_chapter_helpers_reject_escape(bad_chapter: str) -> None:
    paths = ProjectPaths(Path("C:/tmp/project"))

    with pytest.raises(ProjectPathError):
        paths.chapter_dir(bad_chapter)


def test_project_paths_chapter_helpers_point_inside_chapter_dir() -> None:
    paths = ProjectPaths(Path("C:/tmp/project"))

    assert paths.chapter_dir("ch_0001") == paths.chapters_dir / "ch_0001"
    assert paths.chapter_json("ch_0001") == paths.chapters_dir / "ch_0001" / "chapter.json"
    assert paths.skeleton_json("ch_0001").name == "skeleton.json"
    assert paths.drafts_dir("ch_0001").name == "drafts"
    assert paths.final_dir("ch_0001").name == "final"
    assert paths.review_reports_dir("ch_0001").name == "review_reports"
    assert paths.reconcile_dir("ch_0001").name == "reconcile"
    assert paths.retcon_dir("ch_0001").name == "retcon"


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


def test_list_projects_skips_corrupt_dir_with_warning(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    Project.create(root, "Truyện Một")
    Project.create(root, "Truyện Hai")
    broken = root / "thu-muc-hong"
    broken.mkdir()
    (broken / "project.json").write_text("{ hong", encoding="utf-8")

    warnings: list[str] = []
    with pytest.warns(RuntimeWarning):
        projects = list_projects(root, warnings_out=warnings)

    assert [item["slug"] for item in projects] == ["truyen-hai", "truyen-mot"]
    assert all(
        set(item) == {"project_id", "title", "slug", "path", "current_chapter"}
        for item in projects
    )
    assert len(warnings) == 1
    assert "thu-muc-hong" in warnings[0]


def test_list_projects_on_missing_root_returns_empty(tmp_path: Path) -> None:
    assert list_projects(tmp_path / "chua-co") == []
