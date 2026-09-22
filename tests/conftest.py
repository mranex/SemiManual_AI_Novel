"""Fixture chung cho test offline (T07, cô lập dotenv T26).

Quy ước:

- Test không bao giờ đọc/ghi `projects/` thật của người dùng; mọi dữ liệu cần
  cho test dùng `tmp_path`.
- Biến môi trường `NOVEL_AI_*` bị xoá trước mỗi test để shell/`.env` của máy
  không làm kết quả phụ thuộc môi trường, và để test không lỡ dùng API key thật.
- **Dotenv trên disk cũng bị cô lập**: `novel_ai.config._dotenv_path` bị trỏ về
  `tmp_path/.env` trong mọi test, nên `.env` thật ở repo root (có thể bật provider
  thật) không bao giờ được đọc. Test nào muốn kiểm hành vi dotenv thì tự ghi
  `tmp_path/.env` — đó chính là file mà suite đọc.
- Cache `get_config()` bị xoá trước mỗi test để cấu hình của test có hiệu lực.
- Mặc định vẫn là `FakeLLMClient`; đường provider thật chỉ chạy qua transport stub
  trong `tests/unit/test_llm_adapter.py`, không có request mạng nào.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from novel_ai import config as config_module
from novel_ai.config import AppConfig, get_config, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Ví dụ/contract do T02 công bố. Test đọc trực tiếp để ví dụ contract và test
#: không lệch nhau; `tests/fixtures/` chỉ chứa case bổ sung của T08 trở đi.
DESIGN_EXAMPLES = REPO_ROOT / "docs" / "design" / "examples"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: Hàm seam dotenv **thật** của production, lấy trước khi fixture autouse
#: monkeypatch nó. Test cần kiểm hành vi mặc định dùng fixture `real_dotenv_path`.
_REAL_DOTENV_PATH = config_module._dotenv_path


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def t02_valid_document() -> dict[str, Any]:
    """`linked_project_valid.json`: project hai chương, có secret và lore chương 100."""
    return _load_json(DESIGN_EXAMPLES / "linked_project_valid.json")


@pytest.fixture(scope="session")
def t02_invalid_cases() -> list[dict[str, Any]]:
    """`invalid_examples.json`: case sai có path/code mong đợi."""
    return list(_load_json(DESIGN_EXAMPLES / "invalid_examples.json")["invalid_cases"])


@pytest.fixture(scope="session")
def t08_negative_cases() -> dict[str, Any]:
    """Case âm bổ sung của T08 (parse/validate theo artifact_type + helper)."""
    return _load_json(FIXTURES_DIR / "negative_cases.json")


@pytest.fixture(autouse=True)
def isolate_novel_ai_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cô lập môi trường, dotenv trên disk và cache cấu hình cho mọi test.

    Xoá biến `NOVEL_AI_*` là **chưa đủ**: `load_config(env=None)` còn đọc `.env`
    thật ở repo root. Nếu máy đang cấu hình provider thật, test tưởng đang offline
    vẫn dựng `OpenAICompatibleClient`. Vì vậy seam `_dotenv_path` được trỏ về
    `tmp_path/.env` (mặc định không tồn tại) — test nào cần dotenv thì ghi chính
    file đó.
    """
    for name in list(os.environ):
        if name.startswith("NOVEL_AI_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        config_module, "_dotenv_path", lambda root: tmp_path / ".env"
    )
    # UI cache app config 1 lần; test phải bắt đầu từ cache sạch để env của test có hiệu lực.
    get_config.cache_clear()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def real_dotenv_path():
    """Hàm `novel_ai.config._dotenv_path` của production (chưa bị monkeypatch).

    Dùng để kiểm mặc định thật (`<root>/.env`) trong khi phần còn lại của suite
    chạy trên seam đã bị chuyển sang `tmp_path`.
    """
    return _REAL_DOTENV_PATH


@pytest.fixture
def temp_projects_root(tmp_path: Path) -> Path:
    """Data root giả trong thư mục tạm, thay cho `projects/` thật."""
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def app_config(monkeypatch: pytest.MonkeyPatch, temp_projects_root: Path) -> AppConfig:
    """App config trỏ vào data root tạm, mặc định vẫn là fake LLM."""
    monkeypatch.setenv("NOVEL_AI_PROJECTS_ROOT", str(temp_projects_root))
    return load_config(repo_root=REPO_ROOT)


@pytest.fixture
def t09_linked_project(tmp_path: Path, t02_valid_document: dict[str, Any]):
    """Project trong `tmp_path` seed **file thô** theo layout T03 từ ví dụ T02.

    Dùng cho round-trip storage/lifecycle: dữ liệu trên disk đúng contract
    T02/T03 trước khi T09 đọc, nên test không tự chứng minh vòng ghi của chính nó.
    """
    from novel_ai.core import storage
    from novel_ai.core.project import Project

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project = Project.create(projects_root, "Nồi Canh Bên Đường", project_id="proj_0001")
    document = t02_valid_document

    def _write(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    _write(project.paths.project_json, document["project"])
    _write(project.paths.co_create_json, document["co_create"])
    project.reload()
    for raw in document["artifacts"].values():
        relpath = storage.artifact_relpath(raw["artifact_id"], raw["artifact_type"])
        _write(project.root / relpath, raw)
    for chapter_id, raw in document["chapters"].items():
        _write(project.paths.chapter_json(chapter_id), raw)
    _write(project.paths.timeline_json, document["state"]["current_timeline"])
    _write(project.paths.relationships_json, document["state"]["relationships"])
    for snapshot in document["state"]["snapshots"]:
        _write(project.paths.snapshots_dir / f"{snapshot['snapshot_id']}.json", snapshot)
    return project
