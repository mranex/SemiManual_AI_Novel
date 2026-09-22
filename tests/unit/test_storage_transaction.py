"""Transaction, failure injection, lock và recovery (T09).

Test dùng `tmp_path` và monkeypatch `os.replace` để mô phỏng lỗi ghi đĩa; không
gọi API ngoài và không đụng dữ liệu người dùng.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from novel_ai.core import lifecycle, storage
from novel_ai.core.models import (
    OperationStatus,
    PayloadSource,
    SourceType,
    ValidationResult,
    ValidationState,
)
from novel_ai.core.project import Project
from novel_ai.core.storage import (
    LockError,
    PathSafetyError,
    RecoveryRequired,
    StorageError,
)

_STAMP = "2026-09-19T12:00:00+07:00"


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def _source() -> PayloadSource:
    return PayloadSource(source_type=SourceType.llm, prompt_id="architect.premise.v1")


def _accepted_premise(title: str = "Nồi Canh") -> object:
    # `now=_STAMP` (không dùng đồng hồ thật): nếu `created_at` lấy giờ hệ thống thì hai
    # lần gọi "cùng nội dung" ở test idempotency có thể lệch nhau khi qua giây, làm
    # `save_artifact` từ chối vì hash khác — test flaky theo đồng hồ (phát hiện ở T40).
    envelope = lifecycle.new_artifact("premise", "premise", now=_STAMP)
    candidate = lifecycle.set_candidate(
        envelope,
        {"title": title, "logline": "L"},
        source=_source(),
        validation=_valid(),
        now=_STAMP,
    )
    return lifecycle.accept_candidate(candidate, validation=_valid(), now=_STAMP)


def _new_project(tmp_path: Path) -> Project:
    return Project.create(tmp_path / "projects", "Nồi Canh Bên Đường")


def _replace_boom(target_paths: list[Path]):
    """`os.replace` giả lập lỗi chỉ cho các target path được chỉ định."""
    real = os.replace
    normalized = {Path(path) for path in target_paths}

    def _replace(src: object, dst: object) -> None:
        if Path(dst) in normalized:
            raise OSError("lỗi ghi đĩa giả lập")
        real(src, dst)

    return _replace


def _replace_all_boom():
    """`os.replace` giả lập lỗi cho **mọi** target."""

    def _replace(src: object, dst: object) -> None:
        raise OSError("lỗi ghi đĩa giả lập")

    return _replace


def _tree_fingerprint(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _temp_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if ".tmp." in path.name]


# ---------------------------------------------------------------------------
# Failure injection trước replace
# ---------------------------------------------------------------------------


def test_save_artifact_failure_before_replace_keeps_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _new_project(tmp_path)
    storage.save_artifact(project, _accepted_premise("Bản A"), operation_id="op_accept_1")
    before = project.paths.premise_json.read_text(encoding="utf-8")

    loaded = storage.load_artifact(project, "premise")
    candidate = lifecycle.set_candidate(
        loaded, {"title": "Bản B", "logline": "L"}, source=_source(), validation=_valid()
    )
    acceptance = lifecycle.accept_candidate(candidate, validation=_valid(), now=_STAMP)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", _replace_boom([project.paths.premise_json]))
        with pytest.raises(StorageError):
            storage.save_artifact(project, acceptance, operation_id="op_accept_2")

    # Accepted cũ nguyên vẹn, không có temp sót lại, manifest ghi abort.
    assert project.paths.premise_json.read_text(encoding="utf-8") == before
    reloaded = storage.load_artifact(project, "premise")
    assert reloaded.accepted_revision.revision == 1
    assert reloaded.accepted_revision.payload.title == "Bản A"
    assert _temp_files(project.root) == []

    history_manifest = json.loads(
        (project.paths.history_dir / "op_accept_2" / "manifest.json").read_text(encoding="utf-8")
    )
    assert history_manifest["status"] == OperationStatus.aborted.value
    before_copy = (
        project.paths.history_dir / "op_accept_2" / "before" / "architect" / "premise.json"
    )
    assert before_copy.read_text(encoding="utf-8") == before


def test_save_artifact_with_broken_filesystem_still_keeps_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mọi `os.replace` đều lỗi: vẫn phải là StorageError và không mất dữ liệu."""
    project = _new_project(tmp_path)
    storage.save_artifact(project, _accepted_premise("Bản A"), operation_id="op_accept_seed")
    before = project.paths.premise_json.read_text(encoding="utf-8")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", _replace_all_boom())
        with pytest.raises(StorageError):
            storage.save_artifact(
                project, _accepted_premise("Bản B"), operation_id="op_accept_broken"
            )

    assert project.paths.premise_json.read_text(encoding="utf-8") == before
    assert storage.load_artifact(project, "premise").accepted_revision.payload.title == "Bản A"
    assert _temp_files(project.root) == []


def test_same_operation_id_with_different_content_is_rejected(tmp_path: Path) -> None:
    """Retry cùng `operation_id` nhưng nội dung khác không được ghi đè bản đã commit.

    `storage.md` mục 4: retry cùng `operation_id` phải trả kết quả cũ, không merge
    trùng. Guard cũ chỉ kiểm "target hiện tại đã bằng nội dung mới chưa", nên một
    lần gọi thứ hai cùng operation id với payload khác vẫn ghi được revision mới
    đè accepted revision vừa commit.
    """
    project = _new_project(tmp_path)
    storage.save_artifact(project, _accepted_premise("Bản A"), operation_id="op_same")

    with pytest.raises(StorageError) as excinfo:
        storage.save_artifact(project, _accepted_premise("Bản B"), operation_id="op_same")

    assert excinfo.value.code == "stale_candidate"
    reloaded = storage.load_artifact(project, "premise")
    assert reloaded.accepted_revision.revision == 1
    assert reloaded.accepted_revision.payload.title == "Bản A"

    # Retry cùng operation_id với **đúng** nội dung cũ vẫn là no-op idempotent.
    storage.save_artifact(project, _accepted_premise("Bản A"), operation_id="op_same")
    assert storage.load_artifact(project, "premise").accepted_revision.payload.title == "Bản A"

    # operation_id mới cho nội dung mới vẫn ghi bình thường.
    storage.save_artifact(project, _accepted_premise("Bản B"), operation_id="op_next")
    assert storage.load_artifact(project, "premise").accepted_revision.payload.title == "Bản B"


def test_history_snapshot_identifies_previous_revision(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    storage.save_artifact(project, _accepted_premise("Bản A"), operation_id="op_accept_1")
    hash_r1 = storage.file_fingerprint(project.paths.premise_json)

    loaded = storage.load_artifact(project, "premise")
    candidate = lifecycle.set_candidate(
        loaded, {"title": "Bản B", "logline": "L"}, source=_source(), validation=_valid()
    )
    acceptance = lifecycle.accept_candidate(candidate, validation=_valid(), now=_STAMP)
    storage.save_artifact(project, acceptance, operation_id="op_accept_2")

    snapshot_path = (
        project.paths.history_dir / "op_accept_2" / "before" / "architect" / "premise.json"
    )
    snapshot = storage.read_json(snapshot_path)
    assert snapshot["accepted_revision"]["revision"] == 1
    assert snapshot["accepted_revision"]["payload"]["title"] == "Bản A"
    assert storage.file_fingerprint(snapshot_path) == hash_r1

    manifest = json.loads(
        (project.paths.history_dir / "op_accept_2" / "manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["write_set"][0]
    assert entry["target"] == "architect/premise.json"
    assert entry["expected_old_hash"] == hash_r1
    assert entry["staged_hash"] == storage.file_fingerprint(project.paths.premise_json)
    assert manifest["status"] == OperationStatus.committed.value


# ---------------------------------------------------------------------------
# Multi-file commit, crash và recovery
# ---------------------------------------------------------------------------


def _begin_two_file_operation(project: Project, operation_id: str):
    handle = storage.begin_operation(
        project, operation_type="finalize_reconcile", operation_id=operation_id
    )
    handle.add_json(
        "state/current_timeline.json",
        {
            "schema_version": 1,
            "latest_final_chapter": 1,
            "latest_consistent_chapter": 1,
            "entries": [],
        },
    )
    handle.add_json(
        "state/relationships.json",
        {"schema_version": 1, "latest_consistent_chapter": 1, "relationships": []},
    )
    return handle


def test_commit_failure_before_any_replace_aborts_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _new_project(tmp_path)
    handle = _begin_two_file_operation(project, "op_tx_fail")
    assert storage.is_locked(project) is True
    assert storage.pending_operation_ids(project) == ["op_tx_fail"]
    assert not project.paths.timeline_json.exists()

    with monkeypatch.context() as patch:
        patch.setattr(
            os,
            "replace",
            _replace_boom([project.paths.timeline_json, project.paths.relationships_json]),
        )
        with pytest.raises(StorageError):
            handle.commit()

    assert not project.paths.timeline_json.exists()
    assert not project.paths.relationships_json.exists()
    assert storage.pending_operation_ids(project) == []
    assert storage.is_locked(project) is False
    assert _temp_files(project.root) == []
    done = json.loads(
        (project.paths.ops_done_dir / "op_tx_fail.json").read_text(encoding="utf-8")
    )
    assert done["status"] == OperationStatus.aborted.value


def test_crash_mid_commit_then_recovery_is_consistent_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _new_project(tmp_path)
    operation_id = "op_crash"
    handle = _begin_two_file_operation(project, operation_id)
    first_target = "state/current_timeline.json"
    staged_timeline = json.loads(
        (
            project.paths.ops_pending_dir / operation_id / "staged" / first_target
        ).read_text(encoding="utf-8")
    )
    assert staged_timeline["latest_final_chapter"] == 1

    real_replace = os.replace
    calls = {"count": 0}

    def _replace(src: object, dst: object) -> None:
        dst_path = Path(dst)
        if dst_path.parent == project.paths.state_dir:
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("crash giữa commit")
        real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", _replace)
        with pytest.raises(RecoveryRequired):
            handle.commit()

    # Target đầu đã đổi, target sau còn cũ; manifest ghi nhận phần đã áp dụng.
    assert json.loads(project.paths.timeline_json.read_text(encoding="utf-8"))[
        "latest_final_chapter"
    ] == 1
    assert not project.paths.relationships_json.exists()
    assert handle.manifest.applied_paths == [first_target]
    assert storage.pending_operation_ids(project) == [operation_id]
    assert storage.needs_recovery(project) is True
    assert storage.requires_manual_recovery(project) is False
    assert storage.is_locked(project) is True

    report = storage.recover_pending(project)
    assert report.status == "recovered"
    assert report.operation_ids == [operation_id]
    assert set(report.applied_paths) == {first_target, "state/relationships.json"}
    assert json.loads(project.paths.timeline_json.read_text(encoding="utf-8"))[
        "latest_final_chapter"
    ] == 1
    assert project.paths.relationships_json.is_file()
    assert storage.pending_operation_ids(project) == []
    assert storage.needs_recovery(project) is False
    assert storage.is_locked(project) is False

    committed = json.loads(
        (project.paths.ops_done_dir / f"{operation_id}.json").read_text(encoding="utf-8")
    )
    assert committed["status"] == OperationStatus.committed.value
    timeline_text = project.paths.timeline_json.read_text(encoding="utf-8")

    # Gọi recovery lần hai: không nhân đôi, không merge trùng.
    second = storage.recover_pending(project)
    assert second.status == "clean"
    assert second.applied_paths == []

    # Retry cùng operation_id: trả kết quả cũ, không ghi lại gì.
    retry = storage.begin_operation(
        project, operation_type="finalize_reconcile", operation_id=operation_id
    )
    assert retry.replayed is True
    retry.add_json(
        "state/current_timeline.json",
        {"schema_version": 1, "latest_final_chapter": 9, "entries": []},
    )
    replay_manifest = retry.commit()
    assert replay_manifest.applied_paths == committed["applied_paths"]
    assert project.paths.timeline_json.read_text(encoding="utf-8") == timeline_text


def test_recovery_requires_manual_when_target_edited_outside_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _new_project(tmp_path)
    operation_id = "op_manual"
    handle = _begin_two_file_operation(project, operation_id)
    real_replace = os.replace
    calls = {"count": 0}

    def _replace(src: object, dst: object) -> None:
        if Path(dst).parent == project.paths.state_dir:
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("crash giữa commit")
        real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", _replace)
        with pytest.raises(RecoveryRequired):
            handle.commit()

    # Người dùng sửa tay file chưa được commit: cả old lẫn staged hash đều không khớp.
    project.paths.relationships_json.write_text('{"sua": "tay"}', encoding="utf-8")

    assert storage.requires_manual_recovery(project) is True
    report = storage.recover_pending(project)

    assert report.status == "manual_required"
    assert f"{operation_id}:state/relationships.json" in report.manual_required
    assert storage.pending_operation_ids(project) == [operation_id]
    assert storage.needs_recovery(project) is True
    # Không đoán: dữ liệu sửa tay vẫn nguyên, timeline đã commit giữ nguyên.
    assert project.paths.relationships_json.read_text(encoding="utf-8") == '{"sua": "tay"}'


def test_abort_leaves_targets_untouched(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    handle = storage.begin_operation(
        project, operation_type="finalize_reconcile", operation_id="op_abort"
    )
    handle.add_text("chapters/ch_0001/final/final_r0001.md", "Prose chưa commit.")

    manifest = handle.abort()

    assert manifest.status is OperationStatus.aborted
    assert not (project.root / "chapters/ch_0001/final/final_r0001.md").exists()
    assert storage.pending_operation_ids(project) == []
    assert storage.is_locked(project) is False
    assert storage.requires_manual_recovery(project) is False


def test_handle_stage_rejects_path_escape(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    handle = storage.begin_operation(
        project, operation_type="save_draft", operation_id="op_escape"
    )

    with pytest.raises(PathSafetyError):
        handle.add_json("../ngoai.json", {"a": 1})
    with pytest.raises(PathSafetyError):
        handle.add_bytes("C:/ngoai.bin", b"x")

    assert storage.pending_operation_ids(project) == ["op_escape"]
    handle.abort()
    assert storage.pending_operation_ids(project) == []


# ---------------------------------------------------------------------------
# Chapter: chặn ghi metadata cũ hơn bản trên disk
# ---------------------------------------------------------------------------


def _chapter_with_draft(revision: int, draft_count: int):
    from novel_ai.core.models import (
        ChapterMetadata,
        ChapterStatus,
        DependencyPin,
        ProseRevision,
        SourceType,
    )

    drafts = [
        ProseRevision(
            revision=number,
            markdown_ref=f"draft_r{number:04d}.md",
            source_type=SourceType.llm,
            is_complete=True,
            created_at=_STAMP,
        )
        for number in range(1, draft_count + 1)
    ]
    return ChapterMetadata(
        chapter_id="ch_0001",
        chapter_number=1,
        title="Đường vắng",
        status=ChapterStatus.review_required,
        short_plan_pin=DependencyPin(
            artifact_id="short_plan", revision=1, scope="short_plan", chapter_id="ch_0001"
        ),
        drafts=drafts,
        current_draft_revision=revision,
    )


def test_save_chapter_refuses_older_draft_metadata(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    chapter = _chapter_with_draft(revision=2, draft_count=2)
    storage.save_chapter(project, chapter, operation_id="op_ch_r2")

    older = chapter.model_copy(
        update={"drafts": [chapter.drafts[0]], "current_draft_revision": 1}
    )
    with pytest.raises(storage.StaleCandidateError):
        storage.save_chapter(project, older, operation_id="op_ch_old")

    assert storage.load_chapter(project, "ch_0001").current_draft_revision == 2


def test_save_chapter_refuses_dropping_final_revision(tmp_path: Path) -> None:
    from novel_ai.core.models import FinalRevision

    project = _new_project(tmp_path)
    chapter = _chapter_with_draft(revision=1, draft_count=1)
    finalized = chapter.model_copy(
        update={
            "final_revision": FinalRevision(
                revision=1,
                source_prose_revision=1,
                markdown_ref="final_r0001.md",
                reconciled_at=_STAMP,
            )
        }
    )
    storage.save_chapter(project, finalized, operation_id="op_ch_final")

    with pytest.raises(storage.StaleCandidateError):
        storage.save_chapter(project, chapter, operation_id="op_ch_drop_final")

    assert storage.load_chapter(project, "ch_0001").final_revision.revision == 1


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def test_lock_blocks_second_write_action_until_release(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    lock = storage.acquire_lock(
        project, operation_id="op_a", operation_type="finalize_reconcile"
    )

    assert lock.operation_id == "op_a"
    assert storage.read_lock(project).operation_type == "finalize_reconcile"
    assert storage.is_locked(project) is True
    assert storage.needs_recovery(project) is True

    with pytest.raises(LockError):
        storage.acquire_lock(project, operation_id="op_b", operation_type="save_draft")
    with pytest.raises(LockError):
        storage.begin_operation(project, operation_type="save_draft", operation_id="op_b")
    with pytest.raises(LockError):
        storage.save_artifact(project, _accepted_premise(), operation_id="op_b")
    with pytest.raises(LockError):
        storage.write_markdown(project, "idea/base_idea.md", "x", operation_id="op_b")

    storage.release_lock(project)
    assert storage.is_locked(project) is False
    assert storage.read_lock(project) is None

    storage.save_artifact(project, _accepted_premise(), operation_id="op_b")
    assert storage.load_artifact(project, "premise") is not None


def test_stale_lock_with_pending_operation_needs_recovery(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    operation_id = "op_pending"
    storage.begin_operation(
        project, operation_type="finalize_reconcile", operation_id=operation_id
    )
    stale_lock = storage.read_lock(project).model_copy(
        update={"created_at": "2020-01-01T00:00:00+07:00", "heartbeat_at": "2020-01-01T00:00:00+07:00"}
    )
    project.paths.lock_path.write_text(
        json.dumps(stale_lock.model_dump(mode="json")), encoding="utf-8"
    )

    assert storage.is_locked(project) is False
    assert storage.needs_recovery(project) is True
    assert storage.requires_manual_recovery(project) is False
    with pytest.raises(RecoveryRequired):
        storage.acquire_lock(project, operation_id="op_c", operation_type="save_draft")

    report = storage.recover_pending(project)

    assert report.status == "recovered"
    assert report.operation_ids == [operation_id]
    assert storage.pending_operation_ids(project) == []
    assert storage.needs_recovery(project) is False
    done = json.loads(
        (project.paths.ops_done_dir / f"{operation_id}.json").read_text(encoding="utf-8")
    )
    assert done["status"] == OperationStatus.aborted.value

    # Sau recovery, retry cùng operation_id được phép bắt đầu lại (op cũ đã abort).
    retry = storage.begin_operation(
        project, operation_type="finalize_reconcile", operation_id=operation_id
    )
    assert retry.replayed is False
    retry.abort()


def test_stale_lock_without_pending_is_cleared(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    storage.acquire_lock(project, operation_id="op_old", operation_type="save_draft")
    stale_lock = storage.read_lock(project).model_copy(
        update={"created_at": "2020-01-01T00:00:00+07:00", "heartbeat_at": "2020-01-01T00:00:00+07:00"}
    )
    project.paths.lock_path.write_text(
        json.dumps(stale_lock.model_dump(mode="json")), encoding="utf-8"
    )

    assert storage.is_locked(project) is False
    assert storage.needs_recovery(project) is True

    report = storage.recover_pending(project)

    assert report.status == "recovered"
    assert storage.read_lock(project) is None
    assert storage.needs_recovery(project) is False

    handle = storage.begin_operation(project, operation_type="save_draft")
    assert handle.operation_id
    handle.abort()


# ---------------------------------------------------------------------------
# Helper chỉ đọc cho UI/Arbiter
# ---------------------------------------------------------------------------


def test_pending_operation_ids_and_requires_manual_recovery_are_read_only(
    tmp_path: Path,
) -> None:
    project = _new_project(tmp_path)
    assert storage.pending_operation_ids(project) == []
    assert storage.requires_manual_recovery(project) is False
    assert storage.needs_recovery(project) is False

    before = _tree_fingerprint(project.root)
    storage.pending_operation_ids(project)
    storage.requires_manual_recovery(project)
    storage.needs_recovery(project)
    assert _tree_fingerprint(project.root) == before

    # Manifest đánh dấu cần người xử lý.
    manual_dir = project.paths.ops_pending_dir / "op_manual_0001"
    manual_dir.mkdir(parents=True, exist_ok=True)
    (manual_dir / "manifest.json").write_text(
        json.dumps(
            {
                "operation_id": "op_manual_0001",
                "operation_type": "finalize_reconcile",
                "status": "needs_manual_recovery",
                "created_at": _STAMP,
                "updated_at": _STAMP,
            }
        ),
        encoding="utf-8",
    )
    assert storage.pending_operation_ids(project) == ["op_manual_0001"]
    assert storage.requires_manual_recovery(project) is True

    # Thư mục pending không có manifest: cũng là ca cần người xử lý.
    broken_dir = project.paths.ops_pending_dir / "op_broken"
    broken_dir.mkdir(parents=True, exist_ok=True)
    assert storage.pending_operation_ids(project) == ["op_manual_0001"]
    assert storage.requires_manual_recovery(project) is True

    # Pending có staged/old khớp nhau thì tự recovery được, không cần người.
    recoverable_dir = project.paths.ops_pending_dir / "op_recoverable"
    recoverable_dir.mkdir(parents=True, exist_ok=True)
    (recoverable_dir / "manifest.json").write_text(
        json.dumps(
            {
                "operation_id": "op_recoverable",
                "operation_type": "finalize_reconcile",
                "status": "committing",
                "created_at": _STAMP,
                "updated_at": _STAMP,
                "write_set": [
                    {
                        "target": "state/current_timeline.json",
                        "staged": ".ops/pending/op_recoverable/staged/state/current_timeline.json",
                        "expected_old_hash": "",
                        "staged_hash": "abc",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    staged = recoverable_dir / "staged" / "state" / "current_timeline.json"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("{}", encoding="utf-8")
    assert storage.requires_manual_recovery(project) is True  # op_manual vẫn còn


def test_pending_with_matching_old_hash_is_recoverable_not_manual(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    operation_id = "op_recoverable_only"
    handle = storage.begin_operation(
        project, operation_type="save_timeline", operation_id=operation_id
    )
    handle.add_json(
        "state/current_timeline.json",
        {"schema_version": 1, "latest_final_chapter": 0, "entries": []},
    )

    assert storage.requires_manual_recovery(project) is False
    assert storage.pending_operation_ids(project) == [operation_id]

    report = storage.recover_pending(project)
    assert report.status == "recovered"
    assert project.paths.timeline_json.is_file()


def test_pending_dir_without_manifest_needs_manual_recovery(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    (project.paths.ops_pending_dir / "op_khong_manifest").mkdir(parents=True, exist_ok=True)

    assert storage.needs_recovery(project) is True
    assert storage.requires_manual_recovery(project) is True
    assert storage.pending_operation_ids(project) == []
    with pytest.raises(RecoveryRequired):
        storage.begin_operation(project, operation_type="save_draft")

    report = storage.recover_pending(project)

    assert report.status == "manual_required"
    assert report.manual_required == ["op_khong_manifest"]


def test_corrupt_lock_blocks_writes_and_asks_for_manual_recovery(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    project.paths.lock_path.write_text("{ hong", encoding="utf-8")

    assert storage.needs_recovery(project) is True
    assert storage.is_locked(project) is True
    assert storage.requires_manual_recovery(project) is True
    with pytest.raises(StorageError):
        storage.save_artifact(project, _accepted_premise(), operation_id="op_any")

    report = storage.recover_pending(project)

    assert report.status == "manual_required"
    assert str(project.paths.lock_path) in report.manual_required[0]


def test_abort_after_commit_does_not_undo_commit(tmp_path: Path) -> None:
    project = _new_project(tmp_path)
    operation_id = "op_committed"
    handle = storage.begin_operation(
        project, operation_type="save_timeline", operation_id=operation_id
    )
    handle.add_json(
        "state/current_timeline.json",
        {"schema_version": 1, "latest_final_chapter": 1, "entries": []},
    )
    committed = handle.commit()

    aborted = handle.abort()

    assert aborted.status is OperationStatus.committed
    done = json.loads(
        (project.paths.ops_done_dir / f"{operation_id}.json").read_text(encoding="utf-8")
    )
    assert done["status"] == committed.status.value == OperationStatus.committed.value
