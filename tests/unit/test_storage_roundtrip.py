"""Round-trip storage: artifact, chapter, state, snapshot, malformed và version (T09).

Fixture `t09_linked_project` seed **file thô** từ `docs/design/examples/linked_project_valid.json`
theo layout T03, nên test kiểm tra đúng việc đọc dữ liệu contract thật.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_ai.core import lifecycle, storage
from novel_ai.core.models import (
    ArtifactStatus,
    ChapterContextSnapshot,
    CoCreateStatus,
    PayloadSource,
    SourceType,
    ValidationResult,
    ValidationState,
)
from novel_ai.core.project import Project
from novel_ai.core.storage import MalformedDocumentError, PathSafetyError, StorageError


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def _assert_contains(actual: Any, expected: Any, path: str = "") -> None:
    """Mọi field của ví dụ contract phải còn nguyên giá trị.

    Model được phép thêm field app-owned (ví dụ `DependencyPin.chapter_id = null`,
    `PayloadSource.id_map = {}`); điều không được phép là mất field hoặc đổi giá trị.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: kiểu không khớp"
        for key, value in expected.items():
            assert key in actual, f"Model làm mất field contract `{path}/{key}`"
            _assert_contains(actual[key], value, f"{path}/{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: kiểu không khớp"
        assert len(actual) == len(expected), f"{path}: số phần tử lệch"
        for position, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            _assert_contains(actual_item, expected_item, f"{path}/{position}")
    else:
        assert actual == expected, f"{path}: giá trị bị đổi ({actual!r} != {expected!r})"


# ---------------------------------------------------------------------------
# Round-trip từ ví dụ contract T02
# ---------------------------------------------------------------------------


def test_linked_project_artifacts_round_trip(
    t09_linked_project: Project, t02_valid_document: dict[str, Any]
) -> None:
    for raw in t02_valid_document["artifacts"].values():
        artifact_id = raw["artifact_id"]
        envelope = storage.load_artifact(t09_linked_project, artifact_id)
        assert envelope is not None, artifact_id
        assert envelope.artifact_id == artifact_id
        assert envelope.artifact_type == raw["artifact_type"]
        assert envelope.status.value == raw["status"], artifact_id
        for field in ("accepted_revision", "candidate_revision"):
            expected_revision = raw[field]
            actual_revision = getattr(envelope, field)
            if expected_revision is None:
                assert actual_revision is None, f"{artifact_id}.{field}"
                continue
            assert actual_revision is not None, f"{artifact_id}.{field}"
            assert actual_revision.revision == expected_revision["revision"]
            assert (
                actual_revision.payload_source.source_type.value
                == expected_revision["payload_source"]["source_type"]
            )
            _assert_contains(
                actual_revision.payload.model_dump(mode="json"),
                expected_revision["payload"],
                f"{artifact_id}.{field}.payload",
            )
            assert len(actual_revision.dependency_pins) == len(
                expected_revision["dependency_pins"]
            )


def test_linked_project_chapters_round_trip(
    t09_linked_project: Project, t02_valid_document: dict[str, Any]
) -> None:
    assert storage.list_chapter_ids(t09_linked_project) == ["ch_0001", "ch_0002"]
    for chapter_id, raw in t02_valid_document["chapters"].items():
        chapter = storage.load_chapter(t09_linked_project, chapter_id)
        assert chapter is not None, chapter_id
        assert chapter.chapter_id == raw["chapter_id"]
        assert chapter.chapter_number == raw["chapter_number"]
        assert chapter.status.value == raw["status"]
        assert chapter.current_draft_revision == raw["current_draft_revision"]
        assert (chapter.human_review is None) == (raw["human_review"] is None)
        assert [draft.revision for draft in chapter.drafts] == [
            draft["revision"] for draft in raw["drafts"]
        ]
        assert (chapter.final_revision is None) == (raw["final_revision"] is None)


def test_linked_project_state_round_trip(
    t09_linked_project: Project, t02_valid_document: dict[str, Any]
) -> None:
    timeline = storage.load_timeline(t09_linked_project)
    raw_timeline = t02_valid_document["state"]["current_timeline"]
    assert timeline.latest_final_chapter == raw_timeline["latest_final_chapter"]
    assert [entry.timeline_id for entry in timeline.entries] == [
        entry["timeline_id"] for entry in raw_timeline["entries"]
    ]
    assert timeline.entries[0].source_final_revision.revision == 1

    relationships = storage.load_relationships(t09_linked_project)
    raw_relationships = t02_valid_document["state"]["relationships"]
    assert [item.relationship_id for item in relationships.relationships] == [
        item["relationship_id"] for item in raw_relationships["relationships"]
    ]
    assert relationships.relationships[0].history[0].source_final_revision == 1


def test_linked_project_snapshots_round_trip(
    t09_linked_project: Project, t02_valid_document: dict[str, Any]
) -> None:
    snapshots = storage.load_snapshots(t09_linked_project)
    assert len(snapshots) == len(t02_valid_document["state"]["snapshots"])
    snapshot = snapshots[0]
    assert snapshot.snapshot_id == "snapshot_0001"
    assert snapshot.for_chapter_id == "ch_0001"
    assert snapshot.effective_character_ids == ["char_0001"]
    excluded = {
        (item.item_kind, item.item_id, item.effective_from_chapter)
        for item in snapshot.excluded_due_to_effective_chapter
    }
    assert ("character", "char_0002", 2) in excluded
    assert ("world_rule", "rule_0100", 100) in excluded


def test_list_artifact_ids_covers_fixture(t09_linked_project: Project) -> None:
    ids = storage.list_artifact_ids(t09_linked_project)

    assert "premise" in ids
    assert "short_plan" in ids
    assert "skeleton_ch_0001" in ids
    assert "review_report_ch_0001" in ids
    assert "reconciliation_ch_0001" in ids
    assert "rolling_patch_arc_0001" in ids
    assert "impact_report_ch_0001_retcon" in ids


def test_artifact_relpath_layout_and_unknown_type() -> None:
    assert storage.artifact_relpath("premise", "premise") == "architect/premise.json"
    assert storage.artifact_relpath("long_plan", "long_plan") == "plans/long_plan.json"
    assert storage.artifact_relpath("skeleton_ch_0001", "skeleton") == (
        "chapters/ch_0001/skeleton.json"
    )
    assert storage.artifact_relpath("review_report_ch_0001", "review_report") == (
        "chapters/ch_0001/review_reports/review_report_ch_0001.json"
    )
    assert storage.artifact_relpath("rolling_patch_arc_0001", "rolling_patch") == (
        "plans/rolling/rolling_patch_arc_0001.json"
    )

    with pytest.raises(StorageError):
        storage.artifact_relpath("khong_co", "khong_co_type")
    with pytest.raises(StorageError):
        storage.artifact_relpath("premise", "skeleton")
    with pytest.raises(StorageError):
        storage.artifact_type_from_id("khong_co_id")


def test_save_artifact_rejects_wrong_artifact_id_for_type(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")
    envelope = lifecycle.new_artifact("skeleton", "skeleton_ch_0001")
    candidate = lifecycle.set_candidate(
        envelope,
        {
            "chapter_id": "ch_0001",
            "chapter_number": 1,
            "sections": [
                {
                    "section_id": "section_0001",
                    "index": 1,
                    "type": "action",
                    "instruction": "Viết.",
                    "purpose": "Mở đầu.",
                }
            ],
        },
        source=PayloadSource(source_type=SourceType.llm, prompt_id="skeleton.v1"),
    )
    mismatched = candidate.model_copy(update={"artifact_id": "premise"})

    with pytest.raises(StorageError):
        storage.save_artifact(project, mismatched, operation_id="op_wrong_id")
    assert storage.load_artifact(project, "premise") is None


# ---------------------------------------------------------------------------
# Ghi rồi đọc lại: candidate/accepted không mất metadata
# ---------------------------------------------------------------------------


def test_save_artifact_keeps_candidate_then_accepted(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")
    envelope = lifecycle.new_artifact("premise", "premise")
    candidate = lifecycle.set_candidate(
        envelope,
        {"title": "Nồi Canh", "logline": "Một bác sĩ tỉnh dậy ở giới khác."},
        source=PayloadSource(source_type=SourceType.llm, prompt_id="architect.premise.v1"),
    )
    storage.save_artifact(project, candidate, operation_id="op_premise_candidate")

    staged = storage.load_artifact(project, "premise")
    assert staged is not None
    assert staged.status is ArtifactStatus.draft
    assert staged.accepted_revision is None
    assert staged.candidate_revision is not None
    assert staged.candidate_revision.revision == 1
    assert staged.candidate_revision.payload_source.prompt_id == "architect.premise.v1"

    accepted = lifecycle.accept_candidate(
        staged, validation=_valid(), now="2026-09-19T12:00:00+07:00"
    )
    storage.save_artifact(project, accepted, operation_id="op_premise_accept")

    reloaded = Project.open(project.root.parent, project.slug)
    final = storage.load_artifact(reloaded, "premise")
    assert final is not None
    assert final.status is ArtifactStatus.accepted
    assert final.candidate_revision is None
    assert final.accepted_revision is not None
    assert final.accepted_revision.revision == 1
    assert final.accepted_revision.accepted_by == "user"
    assert final.accepted_revision.accepted_at == "2026-09-19T12:00:00+07:00"
    assert final.accepted_revision.payload.title == "Nồi Canh"


def test_save_chapter_round_trip(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is None
    from novel_ai.core.models import ChapterMetadata, ChapterStatus, DependencyPin

    metadata = ChapterMetadata(
        chapter_id="ch_0001",
        chapter_number=1,
        title="Đường vắng",
        status=ChapterStatus.planned,
        short_plan_pin=DependencyPin(
            artifact_id="short_plan", revision=1, scope="short_plan", chapter_id="ch_0001"
        ),
    )
    storage.save_chapter(project, metadata, operation_id="op_chapter")

    loaded = storage.load_chapter(project, "ch_0001")
    assert loaded is not None
    assert loaded.chapter_id == "ch_0001"
    assert loaded.status is ChapterStatus.planned
    assert loaded.short_plan_pin.artifact_id == "short_plan"
    assert storage.list_chapter_ids(project) == ["ch_0001"]


def test_save_co_create_round_trip(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")
    document = storage.load_co_create(project)
    assert document is not None
    assert document.status is CoCreateStatus.working

    storage.save_co_create(
        project,
        document.model_copy(update={"status": CoCreateStatus.finalized}),
        operation_id="op_co_create",
    )

    assert storage.load_co_create(project).status is CoCreateStatus.finalized


# ---------------------------------------------------------------------------
# Raw output, markdown, fingerprint
# ---------------------------------------------------------------------------


def test_save_raw_output_uses_dated_operation_path(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")

    relpath = storage.save_raw_output(
        project,
        operation_id="op_01jtest",
        text='{"raw": true}',
        label="premise response",
        day="2026-09-19",
    )

    assert relpath == "raw/2026-09-19/op_01jtest_premise_response.txt"
    assert (project.root / relpath).read_text(encoding="utf-8") == '{"raw": true}'
    # Raw output không làm accepted state thay đổi.
    assert storage.load_artifact(project, "premise") is None


def test_save_raw_output_rejects_bad_day(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")

    with pytest.raises(StorageError):
        storage.save_raw_output(
            project, operation_id="op_x", text="x", label="x", day="19-09-2026"
        )


def test_write_markdown_inside_project(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")

    relpath = storage.write_markdown(
        project,
        "chapters/ch_0001/drafts/draft_r0001.md",
        "Bản nháp.",
        operation_id="op_draft",
    )

    assert relpath == "chapters/ch_0001/drafts/draft_r0001.md"
    assert storage.read_text(project.root / relpath) == "Bản nháp."


@pytest.mark.parametrize("bad", ["../ngoai.md", "a/../../ngoai.md", "C:/ngoai.md"])
def test_write_markdown_rejects_path_escape(tmp_path: Path, bad: str) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")

    with pytest.raises(PathSafetyError):
        storage.write_markdown(project, bad, "x", operation_id="op_bad")


def test_ensure_within_project_rejects_outside_path(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(PathSafetyError):
        storage.ensure_within_project(root, tmp_path / "ngoai.json")
    with pytest.raises(PathSafetyError):
        storage.ensure_within_project(root, root / ".." / "ngoai.json")

    assert storage.ensure_within_project(root, root / "state" / "x.json") == (
        root / "state" / "x.json"
    ).resolve()


def test_file_fingerprint_empty_when_missing_and_changes_with_content(tmp_path: Path) -> None:
    target = tmp_path / "file.json"
    assert storage.file_fingerprint(target) == ""

    target.write_text("a", encoding="utf-8")
    first = storage.file_fingerprint(target)
    assert len(first) == 64
    assert storage.file_fingerprint(target) == first

    target.write_text("b", encoding="utf-8")
    assert storage.file_fingerprint(target) != first


def test_read_json_missing_and_invalid(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        storage.read_json(tmp_path / "khong-co.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{ khong phai json", encoding="utf-8")
    with pytest.raises(MalformedDocumentError) as excinfo:
        storage.read_json(broken)
    assert str(broken) in str(excinfo.value)


# ---------------------------------------------------------------------------
# State rỗng không tự tạo file
# ---------------------------------------------------------------------------


def test_missing_state_documents_are_empty_and_not_created(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")

    timeline = storage.load_timeline(project)
    relationships = storage.load_relationships(project)

    assert timeline.entries == []
    assert timeline.latest_final_chapter == 0
    assert relationships.relationships == []
    assert not project.paths.timeline_json.exists()
    assert not project.paths.relationships_json.exists()


# ---------------------------------------------------------------------------
# File hỏng / schema_version không hỗ trợ
# ---------------------------------------------------------------------------


def test_malformed_artifact_json_reports_path_and_keeps_file(
    t09_linked_project: Project,
) -> None:
    target = t09_linked_project.paths.premise_json
    target.write_text("{ khong phai json", encoding="utf-8")

    with pytest.raises(MalformedDocumentError) as excinfo:
        storage.load_artifact(t09_linked_project, "premise")

    assert str(target) in str(excinfo.value)
    assert excinfo.value.path == target
    # Không âm thầm reset/ghi đè project.
    assert target.read_text(encoding="utf-8") == "{ khong phai json"


def test_artifact_without_artifact_type_is_reported(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")
    target = project.paths.premise_json
    target.write_text(json.dumps({"schema_version": 1, "artifact_id": "premise"}), encoding="utf-8")

    with pytest.raises(MalformedDocumentError):
        storage.load_artifact(project, "premise")


@pytest.mark.parametrize(
    "relative_path",
    ["architect/premise.json", "chapters/ch_0001/chapter.json", "state/current_timeline.json"],
)
def test_unsupported_schema_version_is_reported_clearly(
    t09_linked_project: Project, relative_path: str
) -> None:
    target = t09_linked_project.root / relative_path
    data = json.loads(target.read_text(encoding="utf-8"))
    data["schema_version"] = 99
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    loader = {
        "architect/premise.json": lambda: storage.load_artifact(t09_linked_project, "premise"),
        "chapters/ch_0001/chapter.json": lambda: storage.load_chapter(
            t09_linked_project, "ch_0001"
        ),
        "state/current_timeline.json": lambda: storage.load_timeline(t09_linked_project),
    }[relative_path]

    with pytest.raises(MalformedDocumentError) as excinfo:
        loader()

    assert excinfo.value.code == "unsupported_schema_version"
    assert "99" in str(excinfo.value)
    assert str(target) in str(excinfo.value)


def test_unsupported_project_schema_version_is_reported(t09_linked_project: Project) -> None:
    target = t09_linked_project.paths.project_json
    data = json.loads(target.read_text(encoding="utf-8"))
    data["schema_version"] = 99
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(MalformedDocumentError) as excinfo:
        Project.open(t09_linked_project.root.parent, "noi-canh-ben-duong")

    assert "99" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_save_snapshot_naming_and_idempotency(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")
    snapshot = ChapterContextSnapshot(
        snapshot_id="snapshot_0009",
        for_chapter_id="ch_0001",
        for_chapter_number=1,
        created_from_action="generate_skeleton",
        effective_character_ids=["char_0001"],
    )

    relpath = storage.save_snapshot(project, snapshot, operation_id="op_snap_1")
    assert relpath == "state/snapshots/snapshot_ch_0001_generate_skeleton_r0001.json"

    again = storage.save_snapshot(project, snapshot, operation_id="op_snap_2")
    assert again == relpath
    assert storage.load_snapshots(project) == [snapshot]


def test_save_snapshot_rejects_outside_path_is_impossible(tmp_path: Path) -> None:
    """Snapshot luôn nằm trong `state/snapshots/`, không nhận path từ caller."""
    project = Project.create(tmp_path / "projects", "Truyện Một")
    snapshot = ChapterContextSnapshot(
        snapshot_id="snapshot_0010",
        for_chapter_id="ch_0002",
        for_chapter_number=2,
        created_from_action="Generate Writer Draft",
    )

    relpath = storage.save_snapshot(project, snapshot, operation_id="op_snap_3")

    assert relpath.startswith("state/snapshots/snapshot_ch_0002_")
    assert (project.root / relpath).is_file()


def test_load_snapshots_reports_corrupt_file(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "projects", "Truyện Một")
    broken = project.paths.snapshots_dir / "snapshot_broken.json"
    broken.write_text("{ hong", encoding="utf-8")

    with pytest.raises(MalformedDocumentError) as excinfo:
        storage.load_snapshots(project)

    assert str(broken) in str(excinfo.value)
