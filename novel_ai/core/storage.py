"""Storage, atomic write, transaction và recovery cho Manual AI Novel (T09).

File này hiện thực contract `docs/design/storage.md`:

- mọi đường dẫn dữ liệu truyện là tương đối từ project root và không được thoát
  khỏi root (``ensure_within_project``);
- ghi một file theo atomic replace: serialize vào ``<name>.tmp.<operation_id>``,
  parse lại nếu là JSON, ``os.replace``, đọc lại và so hash, xóa temp còn dư;
- trước khi thay accepted revision, file cũ được copy byte-for-byte vào
  ``history/<operation_id>/before/...`` kèm ``manifest.json`` để audit/recovery;
- multi-file commit dùng ``.ops/pending/<operation_id>/`` + staged + manifest,
  retry cùng ``operation_id`` là idempotent (không merge trùng);
- project-level lock ở ``.locks/project.lock`` chặn hai write action đồng thời.

Quyết định triển khai (đã ghi ở bàn giao T09):

- **Fingerprint** là SHA-256 của nội dung file (hex). ``file_fingerprint`` trả
  ``""`` khi file chưa tồn tại, nên "chưa có file" và "file rỗng" là hai giá trị
  khác nhau. Vì accepted revision nằm trong chính nội dung file, hash nội dung
  bao luôn revision/status metadata liên quan.
- **History manifest** dùng đúng model ``OperationManifest`` của T08. Với ghi
  một file, ``WriteSetEntry.staged`` là ``None`` (temp đã xóa sau replace) và
  ``staged_hash`` là hash nội dung đã thay; với transaction nhiều file,
  ``staged`` trỏ file staged còn giữ trong ``.ops/pending`` cho tới khi commit.
- **Idempotency** kiểm tra theo cặp ``(operation_id, target)``: retry cùng
  operation_id sau khi đã committed là no-op, không tạo revision thứ hai.
- **Stale candidate**: ``save_artifact`` từ chối ghi khi accepted revision trên
  disk đã mới hơn revision nền của envelope/candidate, hoặc khi dependency pin
  lệch accepted revision hiện tại — lỗi ``stale_candidate``, không merge.
- **Lock stale**: lock quá ``LOCK_STALE_SECONDS`` mà **không** kèm pending
  manifest được xóa an toàn khi acquire; nếu còn pending manifest thì phải
  ``recover_pending`` trước (``RecoveryRequired``).

Module này chỉ phụ thuộc ``core.models`` và ``core.validation``; ``core.lifecycle``
và ``core.project`` dùng lại nó, không có vòng import.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from novel_ai.core import validation
from novel_ai.core.models import (
    ArtifactEnvelope,
    ArtifactStatus,
    ChapterContextSnapshot,
    ChapterMetadata,
    CoCreateDocument,
    CurrentTimelineDocument,
    DependencyPin,
    OperationManifest,
    OperationStatus,
    ProjectLock,
    RelationshipStateDocument,
    WriteSetEntry,
    generate_operation_id,
    now_iso,
)
from novel_ai.core.validation import DocumentError, UnsupportedSchemaVersion

__all__ = [
    "LOCK_STALE_SECONDS",
    "LockError",
    "MalformedDocumentError",
    "OperationHandle",
    "PathSafetyError",
    "RecoveryReport",
    "RecoveryRequired",
    "StaleCandidateError",
    "StorageError",
    "acquire_lock",
    "artifact_relpath",
    "artifact_type_from_id",
    "begin_operation",
    "commit_operation",
    "current_artifact_revisions",
    "ensure_within_project",
    "file_fingerprint",
    "is_locked",
    "list_artifact_ids",
    "list_chapter_ids",
    "load_artifact",
    "load_chapter",
    "load_co_create",
    "load_relationships",
    "load_snapshots",
    "load_timeline",
    "needs_recovery",
    "pending_operation_ids",
    "pin_mismatch_messages",
    "read_json",
    "read_lock",
    "read_text",
    "recover_pending",
    "release_lock",
    "requires_manual_recovery",
    "save_artifact",
    "save_chapter",
    "save_co_create",
    "save_raw_output",
    "save_relationships",
    "save_snapshot",
    "save_timeline",
    "write_json_atomic",
    "write_markdown",
    "write_text_atomic",
]

#: Sau bao nhiêu giây thì một lock không heartbeat được coi là stale (MVP local).
LOCK_STALE_SECONDS = 900

#: Trạng thái recovery của `RecoveryReport`.
RECOVERY_CLEAN = "clean"
RECOVERY_RECOVERED = "recovered"
RECOVERY_MANUAL = "manual_required"


class StorageError(RuntimeError):
    """Lỗi storage nói chung. Message tiếng Việt, có ``code`` ổn định để UI phân loại."""

    code = "storage_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class PathSafetyError(StorageError):
    """Đường dẫn thoát project root hoặc không hợp lệ."""

    code = "path_outside_project"


class LockError(StorageError):
    """Project đang có write action khác giữ lock."""

    code = "project_locked"


class RecoveryRequired(StorageError):
    """Còn pending operation/lock stale cần recovery trước khi ghi tiếp."""

    code = "recovery_required"

    def __init__(
        self,
        message: str,
        *,
        operation_ids: Iterable[str] | None = None,
        manual_required: Iterable[str] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.operation_ids: list[str] = list(operation_ids or [])
        self.manual_required: list[str] = list(manual_required or [])


class MalformedDocumentError(StorageError):
    """File JSON hỏng hoặc sai contract. Không bao giờ tự reset/ghi đè dữ liệu."""

    code = "malformed_document"

    def __init__(
        self,
        message: str,
        *,
        path: Path | str | None = None,
        code: str | None = None,
        errors: Iterable[Any] = (),
    ) -> None:
        location = f" [{path}]" if path is not None else ""
        super().__init__(f"{message}{location}", code=code)
        self.path = Path(path) if path is not None else None
        self.errors: list[Any] = list(errors)


class StaleCandidateError(StorageError, ValueError):
    """Candidate dựa trên revision/fingerprint cũ; phải reject chứ không merge."""

    code = "stale_candidate"


# ---------------------------------------------------------------------------
# Đường dẫn
# ---------------------------------------------------------------------------

#: Artifact có đúng một file cố định trong project.
_FIXED_ARTIFACT_FILES: dict[str, str] = {
    "premise": "architect/premise.json",
    "characters": "architect/characters.json",
    "world_rules": "architect/world_rules.json",
    "foreshadow": "architect/foreshadow.json",
    "long_plan": "plans/long_plan.json",
    "short_plan": "plans/short_plan.json",
}

#: Artifact có ID dạng ``<type>_<scope_id>``; giá trị là mẫu đường dẫn tương đối.
_PREFIXED_ARTIFACT_LAYOUT: dict[str, str] = {
    "skeleton": "chapters/{scope}/skeleton.json",
    "review_report": "chapters/{scope}/review_reports/{artifact_id}.json",
    "reconciliation": "chapters/{scope}/reconcile/{artifact_id}.json",
    "impact_report": "chapters/{scope}/impact/{artifact_id}.json",
    "rewrite_section": "chapters/{scope}/rewrite/{artifact_id}.json",
    "rolling_patch": "plans/rolling/{artifact_id}.json",
}

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def normalize_relpath(relpath: str | Path) -> str:
    """Chuẩn hóa đường dẫn tương đối kiểu POSIX; từ chối ``..``/absolute."""
    text = str(relpath).replace("\\", "/").strip()
    if not text:
        raise PathSafetyError("Đường dẫn tương đối không được rỗng.", code="invalid_relpath")
    if _WINDOWS_DRIVE_RE.match(text) or text.startswith("/"):
        raise PathSafetyError(f"Chỉ nhận đường dẫn tương đối trong project root: {relpath!r}.")
    parts = [part for part in PurePosixPath(text).parts if part not in ("", ".")]
    if not parts:
        raise PathSafetyError(f"Đường dẫn không hợp lệ: {relpath!r}.")
    if any(part == ".." for part in parts):
        raise PathSafetyError(f"Đường dẫn không được chứa '..': {relpath!r}.")
    return "/".join(parts)


def ensure_within_project(project_root: Path, path: Path) -> Path:
    """Trả path tuyệt đối đã resolve và chắc chắn nằm trong ``project_root``."""
    root = Path(project_root)
    if not root.is_absolute():
        root = root.resolve()
    else:
        root = root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(
            f"Đường dẫn thoát project root: {path} (root {root})."
        ) from exc
    return resolved


def _project_relpath(project_root: Path, relpath: str | Path) -> tuple[str, Path]:
    normalized = normalize_relpath(relpath)
    target = ensure_within_project(project_root, project_root / normalized)
    return normalized, target


def artifact_relpath(artifact_id: str, artifact_type: str) -> str:
    """Đường dẫn tương đối của artifact theo layout T03."""
    if not artifact_id or not artifact_type:
        raise StorageError("artifact_id và artifact_type đều bắt buộc.", code="invalid_artifact")
    fixed = _FIXED_ARTIFACT_FILES.get(artifact_type)
    if fixed is not None:
        return fixed
    template = _PREFIXED_ARTIFACT_LAYOUT.get(artifact_type)
    if template is None:
        raise StorageError(
            f"artifact_type `{artifact_type}` chưa có vị trí lưu trong layout T03.",
            code="unknown_artifact_type",
        )
    prefix = f"{artifact_type}_"
    if not artifact_id.startswith(prefix) or len(artifact_id) <= len(prefix):
        raise StorageError(
            f"artifact_id `{artifact_id}` không khớp artifact_type `{artifact_type}` "
            f"(phải có dạng `{prefix}<id>`).",
            code="invalid_artifact_id",
        )
    scope = artifact_id[len(prefix):]
    return template.format(scope=scope, artifact_id=artifact_id)


def artifact_type_from_id(artifact_id: str) -> str:
    """Suy ``artifact_type`` từ ID artifact (dùng khi chỉ có ID, ví dụ ``load_artifact``)."""
    if artifact_id in _FIXED_ARTIFACT_FILES:
        return artifact_id
    for artifact_type in _PREFIXED_ARTIFACT_LAYOUT:
        if artifact_id.startswith(f"{artifact_type}_"):
            return artifact_type
    raise StorageError(
        f"Không suy được artifact_type từ artifact_id `{artifact_id}`.",
        code="unknown_artifact_type",
    )


# ---------------------------------------------------------------------------
# Đọc/ghi cơ bản
# ---------------------------------------------------------------------------


def _json_bytes(data: Any) -> bytes:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    return (text + "\n").encode("utf-8")


def read_text(path: Path | str) -> str:
    """Đọc text UTF-8; lỗi/missing được báo rõ, không trả giá trị rỗng thầm lặng."""
    target = Path(path)
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise StorageError(f"Không thấy file: {target}", code="file_not_found") from exc
    except UnicodeDecodeError as exc:
        raise MalformedDocumentError(
            "File không phải UTF-8 hợp lệ.", path=target, code="invalid_encoding"
        ) from exc
    except OSError as exc:  # pragma: no cover - lỗi FS hiếm
        raise StorageError(f"Không đọc được file {target}: {exc}") from exc


def read_json(path: Path | str) -> Any:
    """Đọc JSON; JSON hỏng raise ``MalformedDocumentError`` có kèm đường dẫn."""
    target = Path(path)
    raw = read_text(target)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedDocumentError(
            f"JSON hỏng ở dòng {exc.lineno}, cột {exc.colno}: {exc.msg}. "
            "Không tự reset hay ghi đè dữ liệu.",
            path=target,
            code="invalid_json",
        ) from exc


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path | str) -> str:
    """SHA-256 hex của nội dung file; ``""`` nếu file chưa tồn tại."""
    target = Path(path)
    if not target.is_file():
        return ""
    return _sha256_file(target)


def _atomic_replace_bytes(target: Path, data: bytes, *, operation_id: str) -> str:
    """Ghi ``data`` vào ``target`` bằng temp cùng thư mục + ``os.replace``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp.{operation_id}")
    try:
        temp.write_bytes(data)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    return _sha256_bytes(data)


def write_json_atomic(path: Path | str, data: Any, *, operation_id: str) -> None:
    """Atomic replace cho một file JSON: temp -> parse lại -> replace -> verify."""
    target = Path(path)
    payload = _json_bytes(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp.{operation_id}")
    try:
        temp.write_bytes(payload)
        try:
            json.loads(temp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MalformedDocumentError(
                "Nội dung JSON serialize ra không parse lại được; hủy ghi để giữ file cũ.",
                path=target,
                code="invalid_json",
            ) from exc
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    if file_fingerprint(target) != _sha256_bytes(payload):  # pragma: no cover - phòng hờ
        raise StorageError(f"File sau khi replace không khớp nội dung staged: {target}")


def write_text_atomic(path: Path | str, text: str, *, operation_id: str) -> None:
    """Atomic replace cho một file text UTF-8."""
    target = Path(path)
    _atomic_replace_bytes(target, text.encode("utf-8"), operation_id=operation_id)


def write_markdown(project: Any, relpath: str, text: str, *, operation_id: str) -> str:
    """Ghi markdown trong project (có snapshot history) và trả relpath đã dùng."""
    normalized, _target = _project_relpath(project.root, relpath)
    _write_bytes_with_history(
        project,
        normalized,
        text.encode("utf-8"),
        operation_id=operation_id,
        operation_type="write_markdown",
    )
    return normalized


# ---------------------------------------------------------------------------
# Raw output
# ---------------------------------------------------------------------------


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(label)).strip("_")
    return cleaned or "output"


def save_raw_output(
    project: Any,
    *,
    operation_id: str,
    text: str,
    label: str,
    day: str | None = None,
) -> str:
    """Lưu raw output vào ``raw/YYYY-MM-DD/<operation_id>_<label>.txt``.

    Raw output được ghi trước parse/validate để người dùng xử lý khi schema sai;
    nó không làm accepted state thay đổi.
    """
    if not operation_id:
        raise StorageError("save_raw_output cần operation_id để truy vết.", code="invalid_operation")
    day_text = day or datetime.now().astimezone().date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(day_text)):
        raise StorageError(f"day phải có dạng YYYY-MM-DD, nhận {day_text!r}.", code="invalid_day")
    relpath = f"raw/{day_text}/{_safe_label(operation_id)}_{_safe_label(label)}.txt"
    normalized, target = _project_relpath(project.root, relpath)
    _atomic_replace_bytes(target, str(text).encode("utf-8"), operation_id=operation_id)
    return normalized


# ---------------------------------------------------------------------------
# History snapshot cho ghi một file
# ---------------------------------------------------------------------------


def _history_dir(project: Any, operation_id: str) -> Path:
    return project.paths.history_dir / operation_id


def _history_manifest_path(project: Any, operation_id: str) -> Path:
    return _history_dir(project, operation_id) / "manifest.json"


def _read_history_manifest(project: Any, operation_id: str) -> OperationManifest | None:
    path = _history_manifest_path(project, operation_id)
    if not path.is_file():
        return None
    return _parse_operation_manifest(read_json(path), path)


def _parse_operation_manifest(data: Any, path: Path) -> OperationManifest:
    try:
        return OperationManifest.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise MalformedDocumentError(
            f"Manifest operation không hợp lệ: {exc}", path=path, code="invalid_manifest"
        ) from exc


def _upsert_history_manifest(
    project: Any,
    *,
    operation_id: str,
    operation_type: str,
    relpath: str,
    expected_old_hash: str,
    staged_hash: str,
    status: OperationStatus,
    result: dict[str, Any] | None = None,
) -> OperationManifest:
    manifest = _read_history_manifest(project, operation_id)
    stamp = now_iso()
    if manifest is None:
        manifest = OperationManifest(
            operation_id=operation_id,
            operation_type=operation_type,
            status=status,
            created_at=stamp,
            updated_at=stamp,
        )
    entries = {entry.target: entry for entry in manifest.write_set}
    entries[relpath] = WriteSetEntry(
        target=relpath,
        staged=None,
        before=f"history/{operation_id}/before/{relpath}",
        expected_old_hash=expected_old_hash,
        staged_hash=staged_hash,
    )
    manifest.write_set = list(entries.values())
    manifest.base_fingerprints[relpath] = expected_old_hash
    if status is OperationStatus.committed:
        applied = set(manifest.applied_paths)
        applied.add(relpath)
        manifest.applied_paths = [target for target in entries if target in applied]
    manifest.status = status
    manifest.updated_at = stamp
    if result is not None:
        manifest.result = result
    write_json_atomic(
        _history_manifest_path(project, operation_id),
        manifest,
        operation_id=f"{operation_id}.history",
    )
    return manifest


def _check_write_lock(project: Any, operation_id: str) -> None:
    """Từ chối write action khác khi project đang có lock còn hiệu lực.

    Lock thuộc **cùng** ``operation_id`` được bỏ qua để service có thể vừa giữ
    transaction vừa ghi file đơn lẻ trong cùng action.
    """
    lock = read_lock(project)
    if lock is None or lock.operation_id == operation_id:
        return
    if _lock_is_stale(lock):
        return
    raise LockError(
        f"Project đang bị khóa bởi {lock.operation_id} ({lock.operation_type}); "
        "không ghi thêm write action khác cho tới khi lock được release."
    )


def _write_bytes_with_history(
    project: Any,
    relpath: str,
    data: bytes,
    *,
    operation_id: str,
    operation_type: str,
    json_check: bool = False,
) -> str:
    """Ghi một file trong project kèm snapshot history + idempotency theo operation.

    - Nội dung giống hệt bản đang có là no-op (retry cùng action không tạo
      revision/history trùng).
    - History snapshot (`history/<operation_id>/before/...` + manifest) chỉ được
      ghi khi thực sự **thay** một file đã có; ghi file mới không tạo history.
    - Ledger của operation được ghi vào `.ops/done/<operation_id>.json` cho **mọi**
      lần ghi (kể cả ghi file mới chưa có history). Không có ledger này thì
      idempotency theo `operation_id` không kiểm được cho write một file, và retry
      cùng operation id với nội dung khác sẽ ghi đè revision đã commit.
    """
    normalized, target = _project_relpath(project.root, relpath)
    new_hash = _sha256_bytes(data)
    if file_fingerprint(target) == new_hash:
        return new_hash
    if _is_already_applied(project, operation_id, normalized, new_hash):
        return new_hash
    # Retry cùng operation_id nhưng nội dung khác: từ chối, không ghi đè bản đã commit.
    _check_operation_idempotency(project, operation_id, normalized, new_hash)
    _check_write_lock(project, operation_id)
    expected_old_hash = file_fingerprint(target)
    had_previous = bool(expected_old_hash)
    before_path = _history_dir(project, operation_id) / "before" / normalized
    if had_previous:
        try:
            before_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, before_path)
        except OSError as exc:
            raise StorageError(
                f"Không copy được history snapshot cho {normalized}; file target giữ nguyên ({exc})."
            ) from exc
    try:
        _upsert_history_manifest(
            project,
            operation_id=operation_id,
            operation_type=operation_type,
            relpath=normalized,
            expected_old_hash=expected_old_hash,
            staged_hash=new_hash,
            status=OperationStatus.staged,
        )
    except Exception as exc:
        raise StorageError(
            f"Không ghi được history manifest cho {normalized}; file target giữ nguyên ({exc})."
        ) from exc
    temp = target.with_name(f"{target.name}.tmp.{operation_id}")
    try:
        _atomic_replace_bytes(target, data, operation_id=operation_id)
        if json_check:
            json.loads(read_text(target))
        if file_fingerprint(target) != new_hash:  # pragma: no cover - phòng hờ
            raise StorageError(f"File sau replace không khớp staged: {target}")
    except Exception as exc:
        if temp.exists():  # pragma: no cover - _atomic_replace_bytes đã dọn
            temp.unlink()
        try:
            _upsert_history_manifest(
                project,
                operation_id=operation_id,
                operation_type=operation_type,
                relpath=normalized,
                expected_old_hash=expected_old_hash,
                staged_hash=new_hash,
                status=OperationStatus.aborted,
                result={"reason": "replace_failed", "error": str(exc)},
            )
        except Exception:  # pragma: no cover - manifest cũng không ghi được
            pass
        if isinstance(exc, StorageError):
            raise
        raise StorageError(
            f"Ghi file thất bại, file cũ giữ nguyên: {target} ({exc})"
        ) from exc
    manifest = _upsert_history_manifest(
        project,
        operation_id=operation_id,
        operation_type=operation_type,
        relpath=normalized,
        expected_old_hash=expected_old_hash,
        staged_hash=new_hash,
        status=OperationStatus.committed,
        result={"reason": "written", "operation_type": operation_type},
    )
    _write_done_manifest(project, manifest)
    return new_hash


def _operation_manifests_for(project: Any, operation_id: str) -> list[OperationManifest]:
    manifests: list[OperationManifest] = []
    done = _read_done_manifest(project, operation_id)
    if done is not None:
        manifests.append(done)
    history = _read_history_manifest(project, operation_id)
    if history is not None:
        manifests.append(history)
    return manifests


def _is_already_applied(
    project: Any, operation_id: str, relpath: str, new_hash: str
) -> bool:
    """Retry cùng ``(operation_id, target)`` với cùng nội dung là no-op idempotent."""
    target = project.root / relpath
    if file_fingerprint(target) != new_hash:
        return False
    for manifest in _operation_manifests_for(project, operation_id):
        if manifest.status is not OperationStatus.committed:
            continue
        for entry in manifest.write_set:
            if entry.target == relpath and entry.staged_hash == new_hash:
                return True
    return False


def _committed_hash_for(
    project: Any, operation_id: str, relpath: str
) -> str | None:
    """Hash đã commit cho ``(operation_id, target)``, hoặc ``None`` nếu chưa commit.

    Dùng để phát hiện retry cùng ``operation_id`` nhưng **khác nội dung**: đó không
    phải idempotent replay mà là một write mới mạo danh operation cũ, và phải bị
    từ chối thay vì ghi đè revision đã commit.
    """
    for manifest in _operation_manifests_for(project, operation_id):
        if manifest.status is not OperationStatus.committed:
            continue
        for entry in manifest.write_set:
            if entry.target == relpath and entry.staged_hash:
                return entry.staged_hash
    return None


def _check_operation_idempotency(
    project: Any, operation_id: str, relpath: str, new_hash: str
) -> None:
    """Từ chối khi cùng ``operation_id`` đã commit target này với nội dung khác.

    ``storage.md`` mục 4 chốt retry cùng ``operation_id`` phải trả kết quả cũ và
    không merge trùng. Trước đây guard chỉ kiểm "target hiện tại đã bằng nội dung
    mới chưa", nên retry cùng operation id với payload khác vẫn ghi được revision
    mới đè accepted revision đã commit.
    """
    committed = _committed_hash_for(project, operation_id, relpath)
    if committed is not None and committed != new_hash:
        raise StaleCandidateError(
            f"{relpath}: operation `{operation_id}` đã commit nội dung khác "
            f"({committed[:12]}… ≠ {new_hash[:12]}…); từ chối ghi để không đè bản đã "
            "commit. Retry/regenerate phải dùng operation_id mới."
        )


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


def _parse_artifact_document(data: Any, path: Path) -> ArtifactEnvelope[Any]:
    try:
        return validation.parse_artifact_document(data)
    except UnsupportedSchemaVersion as exc:
        raise MalformedDocumentError(
            f"schema_version của artifact không được hỗ trợ: {exc}", path=path,
            code="unsupported_schema_version", errors=exc.errors,
        ) from exc
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"Artifact không đúng contract: {exc}", path=path, code="invalid_document",
            errors=exc.errors,
        ) from exc


def load_artifact(project: Any, artifact_id: str) -> ArtifactEnvelope[Any] | None:
    """Đọc envelope artifact; ``None`` nếu file chưa tồn tại."""
    artifact_type = artifact_type_from_id(artifact_id)
    relpath = artifact_relpath(artifact_id, artifact_type)
    target = ensure_within_project(project.root, project.root / relpath)
    if not target.is_file():
        return None
    envelope = _parse_artifact_document(read_json(target), target)
    if envelope.artifact_id != artifact_id:
        raise MalformedDocumentError(
            f"File chứa artifact_id `{envelope.artifact_id}` nhưng được load như "
            f"`{artifact_id}`; không tự di chuyển/ghi đè.",
            path=target,
            code="artifact_id_mismatch",
        )
    if envelope.artifact_type != artifact_type:
        raise MalformedDocumentError(
            f"File khai artifact_type `{envelope.artifact_type}` nhưng vị trí tương ứng "
            f"`{artifact_type}`; không tự sửa dữ liệu.",
            path=target,
            code="artifact_type_mismatch",
        )
    return envelope


def save_artifact(project: Any, envelope: ArtifactEnvelope[Any], *, operation_id: str) -> None:
    """Lưu envelope artifact: validate contract, snapshot history, atomic replace.

    Từ chối ``stale_candidate`` nếu envelope/candidate dựa trên revision cũ hơn
    accepted revision đang có trên disk, hoặc dependency pin lệch revision hiện
    tại. Retry cùng ``(operation_id, target)`` là no-op.
    """
    if not operation_id:
        raise StorageError("save_artifact cần operation_id.", code="invalid_operation")
    data = envelope.model_dump(mode="json")
    relpath = artifact_relpath(envelope.artifact_id, envelope.artifact_type)
    target = ensure_within_project(project.root, project.root / relpath)
    _parse_artifact_document(data, target)
    payload = _json_bytes(data)
    new_hash = _sha256_bytes(payload)
    normalized, target = _project_relpath(project.root, relpath)
    if _is_already_applied(project, operation_id, normalized, new_hash):
        return
    disk = load_artifact(project, envelope.artifact_id)
    _check_candidate_freshness(project, envelope, disk)
    _write_bytes_with_history(
        project,
        normalized,
        payload,
        operation_id=operation_id,
        operation_type="save_artifact",
        json_check=True,
    )


def _check_candidate_freshness(
    project: Any,
    incoming: ArtifactEnvelope[Any],
    disk: ArtifactEnvelope[Any] | None,
) -> None:
    """Guard chống accept/merge candidate cũ (storage.md mục 4 và 7)."""
    disk_accepted = (
        disk.accepted_revision.revision
        if disk is not None and disk.accepted_revision is not None
        else 0
    )
    incoming_accepted = incoming.accepted_revision
    if incoming_accepted is not None and incoming_accepted.revision < disk_accepted:
        raise StaleCandidateError(
            f"{incoming.artifact_id}: accepted revision r{incoming_accepted.revision} cũ hơn "
            f"r{disk_accepted} đang có trên disk; từ chối ghi đè, cần review lại."
        )
    candidate = incoming.candidate_revision
    accepting_new = (
        incoming_accepted is not None and incoming_accepted.revision > disk_accepted
    )
    if candidate is not None and candidate.revision <= disk_accepted:
        raise StaleCandidateError(
            f"{incoming.artifact_id}: candidate r{candidate.revision} không mới hơn accepted "
            f"r{disk_accepted} trên disk; candidate đã stale, không accept như còn mới."
        )
    pins: list[DependencyPin] = []
    if candidate is not None:
        pins = list(candidate.dependency_pins)
    elif accepting_new and incoming_accepted is not None:
        pins = list(incoming_accepted.dependency_pins)
    if not pins:
        return
    mismatches = pin_mismatch_messages(pins, current_artifact_revisions(project))
    if mismatches:
        raise StaleCandidateError(
            f"{incoming.artifact_id}: dependency pin lệch accepted revision hiện tại "
            f"({'; '.join(mismatches)}); từ chối merge candidate cũ."
        )


def list_artifact_ids(project: Any) -> list[str]:
    """Liệt kê artifact_id đang có file trong project (không parse nội dung)."""
    ids: set[str] = set()
    for artifact_type, relpath in _FIXED_ARTIFACT_FILES.items():
        if (project.root / relpath).is_file():
            ids.add(artifact_type)
    chapters_dir = project.paths.chapters_dir
    if chapters_dir.is_dir():
        for chapter_dir in sorted(path for path in chapters_dir.iterdir() if path.is_dir()):
            if (chapter_dir / "skeleton.json").is_file():
                ids.add(f"skeleton_{chapter_dir.name}")
            for subdir in ("review_reports", "reconcile", "impact", "rewrite"):
                directory = chapter_dir / subdir
                if directory.is_dir():
                    for item in sorted(directory.glob("*.json")):
                        ids.add(item.stem)
    rolling_dir = project.paths.rolling_dir
    if rolling_dir.is_dir():
        for item in sorted(rolling_dir.glob("*.json")):
            ids.add(item.stem)
    return sorted(ids)


def current_artifact_revisions(project: Any) -> dict[str, int]:
    """Map ``artifact_id``/``base_idea``/``chapter_<id>`` -> revision accepted hiện tại.

    Chỉ dùng cho freshness check; artifact hỏng bị bỏ qua để một file lỗi không
    chặn toàn bộ guard (file hỏng vẫn raise khi load trực tiếp).
    """
    revisions: dict[str, int] = {}
    for artifact_id in list_artifact_ids(project):
        try:
            envelope = load_artifact(project, artifact_id)
        except MalformedDocumentError:
            continue
        if envelope is not None and envelope.accepted_revision is not None:
            revisions[artifact_id] = envelope.accepted_revision.revision
    co_create = load_co_create(project)
    if (
        co_create is not None
        and co_create.base_idea is not None
        and co_create.base_idea.status is ArtifactStatus.accepted
    ):
        revisions["base_idea"] = co_create.base_idea.revision
    for chapter_id in list_chapter_ids(project):
        chapter = load_chapter(project, chapter_id)
        if chapter is None:
            continue
        drafts = max((draft.revision for draft in chapter.drafts), default=0)
        revision = chapter.current_draft_revision or drafts
        if revision:
            revisions[f"chapter_{chapter_id}"] = revision
    return revisions


def pin_mismatch_messages(
    pins: Iterable[DependencyPin], current: Mapping[str, int]
) -> list[str]:
    """Pin nào lệch revision hiện tại thì trả message; pin không rõ thì bỏ qua."""
    messages: list[str] = []
    for pin in pins:
        expected = current.get(pin.artifact_id)
        if expected is not None and expected != pin.revision:
            messages.append(
                f"{pin.artifact_id}: pin r{pin.revision} nhưng accepted hiện tại là r{expected} "
                f"(scope {pin.scope})"
            )
    return messages


# ---------------------------------------------------------------------------
# Co-create, chapter và state
# ---------------------------------------------------------------------------


def load_co_create(project: Any) -> CoCreateDocument | None:
    """Đọc ``co_create.json``; ``None`` nếu chưa có."""
    target = ensure_within_project(project.root, project.paths.co_create_json)
    if not target.is_file():
        return None
    data = read_json(target)
    try:
        return validation.parse_co_create_document(data)
    except UnsupportedSchemaVersion as exc:
        raise MalformedDocumentError(
            f"schema_version co_create.json không được hỗ trợ: {exc}",
            path=target, code="unsupported_schema_version", errors=exc.errors,
        ) from exc
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"co_create.json không đúng contract: {exc}", path=target,
            code="invalid_document", errors=exc.errors,
        ) from exc


def save_co_create(project: Any, document: CoCreateDocument, *, operation_id: str) -> None:
    """Lưu ``co_create.json`` qua atomic write + history snapshot."""
    data = document.model_dump(mode="json")
    target = ensure_within_project(project.root, project.paths.co_create_json)
    try:
        validation.parse_co_create_document(data)
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"co_create.json không đúng contract: {exc}", path=target,
            code="invalid_document", errors=exc.errors,
        ) from exc
    relpath = str(target.relative_to(project.root)).replace("\\", "/")
    _write_bytes_with_history(
        project, relpath, _json_bytes(data),
        operation_id=operation_id, operation_type="save_co_create", json_check=True,
    )


def load_chapter(project: Any, chapter_id: str) -> ChapterMetadata | None:
    """Đọc ``chapters/<chapter_id>/chapter.json``; ``None`` nếu chưa có."""
    normalized = normalize_relpath(chapter_id)
    if "/" in normalized:
        raise PathSafetyError(f"chapter_id không được chứa dấu '/': {chapter_id!r}.")
    target = ensure_within_project(project.root, project.paths.chapter_json(normalized))
    if not target.is_file():
        return None
    try:
        return validation.parse_chapter_metadata(read_json(target))
    except UnsupportedSchemaVersion as exc:
        raise MalformedDocumentError(
            f"schema_version chapter.json không được hỗ trợ: {exc}", path=target,
            code="unsupported_schema_version", errors=exc.errors,
        ) from exc
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"chapter.json không đúng contract: {exc}", path=target,
            code="invalid_document", errors=exc.errors,
        ) from exc


def _check_chapter_freshness(
    incoming: ChapterMetadata, disk: ChapterMetadata | None
) -> None:
    """Chặn ghi chapter.json dựa trên metadata cũ hơn bản đang có trên disk.

    Không có "base fingerprint" cho prose, nên freshness của chapter dùng
    revision metadata: `current_draft_revision` không được lùi và
    `final_revision` đã commit không được biến mất hay lùi revision.
    """
    if disk is None:
        return
    disk_draft = disk.current_draft_revision or max(
        (draft.revision for draft in disk.drafts), default=0
    )
    incoming_draft = incoming.current_draft_revision or max(
        (draft.revision for draft in incoming.drafts), default=0
    )
    if disk_draft and incoming_draft < disk_draft:
        raise StaleCandidateError(
            f"{incoming.chapter_id}: metadata gửi lên dựa trên draft r{incoming_draft} "
            f"nhưng disk đang ở r{disk_draft}; từ chối ghi để không đè prose mới hơn."
        )
    if disk.final_revision is not None:
        if incoming.final_revision is None:
            raise StaleCandidateError(
                f"{incoming.chapter_id}: disk đã có final_revision r{disk.final_revision.revision}; "
                "không ghi metadata làm mất final manuscript (cần action retcon rõ ràng)."
            )
        if incoming.final_revision.revision < disk.final_revision.revision:
            raise StaleCandidateError(
                f"{incoming.chapter_id}: final_revision r{incoming.final_revision.revision} cũ hơn "
                f"r{disk.final_revision.revision} trên disk."
            )


def save_chapter(project: Any, chapter: ChapterMetadata, *, operation_id: str) -> None:
    """Lưu ``chapter.json``: validate contract, snapshot history, atomic replace.

    Từ chối ``stale_candidate`` nếu metadata gửi lên dựa trên draft/final revision
    cũ hơn bản đang có trên disk.
    """
    if not operation_id:
        raise StorageError("save_chapter cần operation_id.", code="invalid_operation")
    data = chapter.model_dump(mode="json")
    target = ensure_within_project(project.root, project.paths.chapter_json(chapter.chapter_id))
    try:
        validation.parse_chapter_metadata(data)
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"chapter.json không đúng contract: {exc}", path=target,
            code="invalid_document", errors=exc.errors,
        ) from exc
    _check_chapter_freshness(chapter, load_chapter(project, chapter.chapter_id))
    relpath = str(target.relative_to(project.root)).replace("\\", "/")
    _write_bytes_with_history(
        project, relpath, _json_bytes(data),
        operation_id=operation_id, operation_type="save_chapter", json_check=True,
    )


def list_chapter_ids(project: Any) -> list[str]:
    """Liệt kê chapter_id có ``chapter.json`` trong project."""
    chapters_dir = project.paths.chapters_dir
    if not chapters_dir.is_dir():
        return []
    return sorted(
        directory.name
        for directory in chapters_dir.iterdir()
        if directory.is_dir() and (directory / "chapter.json").is_file()
    )


def load_timeline(project: Any) -> CurrentTimelineDocument:
    """Đọc ``state/current_timeline.json``; thiếu file trả document rỗng hợp lệ.

    Không tự tạo file: "chưa có state" khác "state rỗng", và guard đọc metadata
    chứ không suy từ sự tồn tại của file.
    """
    target = ensure_within_project(project.root, project.paths.timeline_json)
    if not target.is_file():
        return CurrentTimelineDocument()
    try:
        return validation.parse_timeline_document(read_json(target))
    except UnsupportedSchemaVersion as exc:
        raise MalformedDocumentError(
            f"schema_version current_timeline.json không được hỗ trợ: {exc}", path=target,
            code="unsupported_schema_version", errors=exc.errors,
        ) from exc
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"current_timeline.json không đúng contract: {exc}", path=target,
            code="invalid_document", errors=exc.errors,
        ) from exc


def save_timeline(
    project: Any, document: CurrentTimelineDocument, *, operation_id: str
) -> None:
    """Lưu current timeline qua atomic write + history snapshot."""
    data = document.model_dump(mode="json")
    target = ensure_within_project(project.root, project.paths.timeline_json)
    try:
        validation.parse_timeline_document(data)
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"current_timeline.json không đúng contract: {exc}", path=target,
            code="invalid_document", errors=exc.errors,
        ) from exc
    relpath = str(target.relative_to(project.root)).replace("\\", "/")
    _write_bytes_with_history(
        project, relpath, _json_bytes(data),
        operation_id=operation_id, operation_type="save_timeline", json_check=True,
    )


def load_relationships(project: Any) -> RelationshipStateDocument:
    """Đọc ``state/relationships.json``; thiếu file trả document rỗng hợp lệ."""
    target = ensure_within_project(project.root, project.paths.relationships_json)
    if not target.is_file():
        return RelationshipStateDocument()
    try:
        return validation.parse_relationship_document(read_json(target))
    except UnsupportedSchemaVersion as exc:
        raise MalformedDocumentError(
            f"schema_version relationships.json không được hỗ trợ: {exc}", path=target,
            code="unsupported_schema_version", errors=exc.errors,
        ) from exc
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"relationships.json không đúng contract: {exc}", path=target,
            code="invalid_document", errors=exc.errors,
        ) from exc


def save_relationships(
    project: Any, document: RelationshipStateDocument, *, operation_id: str
) -> None:
    """Lưu relationship state qua atomic write + history snapshot."""
    data = document.model_dump(mode="json")
    target = ensure_within_project(project.root, project.paths.relationships_json)
    try:
        validation.parse_relationship_document(data)
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"relationships.json không đúng contract: {exc}", path=target,
            code="invalid_document", errors=exc.errors,
        ) from exc
    relpath = str(target.relative_to(project.root)).replace("\\", "/")
    _write_bytes_with_history(
        project, relpath, _json_bytes(data),
        operation_id=operation_id, operation_type="save_relationships", json_check=True,
    )


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def _snapshot_action_slug(action: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(action).lower()).strip("_")
    return cleaned or "context"


def save_snapshot(project: Any, snapshot: ChapterContextSnapshot, *, operation_id: str) -> str:
    """Lưu snapshot vào ``state/snapshots/`` và trả đường dẫn tương đối.

    Tên file theo contract (``snapshot_<chapter>_<action>_r0004.json``); nếu
    snapshot_id đã tồn tại thì ghi lại đúng file cũ (idempotent theo snapshot).
    """
    if not operation_id:
        raise StorageError("save_snapshot cần operation_id.", code="invalid_operation")
    data = snapshot.model_dump(mode="json")
    try:
        validation.parse_model(ChapterContextSnapshot, data)
    except DocumentError as exc:
        raise MalformedDocumentError(
            f"Snapshot không đúng contract: {exc}", code="invalid_document", errors=exc.errors
        ) from exc
    snapshots_dir = project.paths.snapshots_dir
    relpath: str | None = None
    if snapshots_dir.is_dir():
        for item in sorted(snapshots_dir.glob("*.json")):
            try:
                existing = read_json(item)
            except MalformedDocumentError:
                continue
            if isinstance(existing, Mapping) and existing.get("snapshot_id") == snapshot.snapshot_id:
                relpath = str(item.relative_to(project.root)).replace("\\", "/")
                break
    if relpath is None:
        prefix = (
            f"snapshot_{snapshot.for_chapter_id}_{_snapshot_action_slug(snapshot.created_from_action)}"
        )
        sequence = 1
        if snapshots_dir.is_dir():
            sequence += sum(1 for _ in snapshots_dir.glob(f"{prefix}_r*.json"))
        relpath = f"state/snapshots/{prefix}_r{sequence:04d}.json"
    normalized, _target = _project_relpath(project.root, relpath)
    _write_bytes_with_history(
        project, normalized, _json_bytes(data),
        operation_id=operation_id, operation_type="save_snapshot", json_check=True,
    )
    return normalized


def load_snapshots(project: Any) -> list[ChapterContextSnapshot]:
    """Đọc mọi snapshot theo thứ tự tên file; snapshot hỏng raise rõ ràng."""
    snapshots_dir = project.paths.snapshots_dir
    if not snapshots_dir.is_dir():
        return []
    snapshots: list[ChapterContextSnapshot] = []
    for item in sorted(snapshots_dir.glob("*.json")):
        data = read_json(item)
        try:
            snapshots.append(validation.parse_model(ChapterContextSnapshot, data))
        except DocumentError as exc:
            raise MalformedDocumentError(
                f"Snapshot không đúng contract: {exc}", path=item,
                code="invalid_document", errors=exc.errors,
            ) from exc
    return snapshots


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def read_lock(project: Any) -> ProjectLock | None:
    """Đọc ``.locks/project.lock``; ``None`` nếu chưa có lock."""
    target = ensure_within_project(project.root, project.paths.lock_path)
    if not target.is_file():
        return None
    data = read_json(target)
    try:
        return ProjectLock.model_validate(data)
    except Exception as exc:
        raise MalformedDocumentError(
            f"project.lock không hợp lệ: {exc}", path=target, code="invalid_lock"
        ) from exc


def _lock_is_stale(lock: ProjectLock) -> bool:
    reference = lock.heartbeat_at or lock.created_at
    try:
        moment = datetime.fromisoformat(reference)
    except ValueError:  # pragma: no cover - model đã validate ISO
        return True
    now = datetime.now(moment.tzinfo) if moment.tzinfo else datetime.now()
    return (now - moment).total_seconds() > LOCK_STALE_SECONDS


def acquire_lock(project: Any, *, operation_id: str, operation_type: str) -> ProjectLock:
    """Tạo ``project.lock`` bằng atomic create (O_EXCL); từ chối nếu đang khóa.

    Lock stale (quá ``LOCK_STALE_SECONDS``) được xóa an toàn **chỉ khi** không
    còn pending manifest; còn pending thì phải recovery trước.
    """
    if not operation_id or not operation_type:
        raise StorageError("acquire_lock cần operation_id và operation_type.", code="invalid_lock")
    existing = read_lock(project)
    if existing is not None:
        if _lock_is_stale(existing):
            pending = _pending_dir_names(project)
            if pending:
                raise RecoveryRequired(
                    f"Lock của {existing.operation_id} đã stale nhưng còn pending operation "
                    f"{pending}; chạy recover_pending() trước khi ghi tiếp.",
                    operation_ids=pending,
                )
            release_lock(project)
        else:
            raise LockError(
                f"Project đang bị khóa bởi {existing.operation_id} "
                f"({existing.operation_type}); release hoặc recovery trước."
            )
    target = ensure_within_project(project.root, project.paths.lock_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = now_iso()
    lock = ProjectLock(
        operation_id=operation_id,
        operation_type=operation_type,
        created_at=stamp,
        heartbeat_at=stamp,
    )
    try:
        handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LockError(
            f"Project vừa bị khóa bởi write action khác ({target})."
        ) from exc
    try:
        os.write(handle, _json_bytes(lock))
    finally:
        os.close(handle)
    return lock


def release_lock(project: Any) -> None:
    """Xóa lock nếu có (idempotent)."""
    target = ensure_within_project(project.root, project.paths.lock_path)
    try:
        target.unlink()
    except FileNotFoundError:
        return


def is_locked(project: Any) -> bool:
    """True nếu có lock **còn hiệu lực** (lock stale không tính là đang khóa).

    Lock hỏng được coi là đang khóa: không đoán, chỉ đọc/ghi tiếp sau recovery.
    """
    try:
        lock = read_lock(project)
    except MalformedDocumentError:
        return True
    if lock is None:
        return False
    return not _lock_is_stale(lock)


# ---------------------------------------------------------------------------
# Operation manifest, commit và recovery
# ---------------------------------------------------------------------------

_DONE_NAME = "manifest.json"


def _pending_dir(project: Any, operation_id: str) -> Path:
    return project.paths.ops_pending_dir / operation_id


def _pending_manifest_path(project: Any, operation_id: str) -> Path:
    return _pending_dir(project, operation_id) / _DONE_NAME


def _done_manifest_path(project: Any, operation_id: str) -> Path:
    return project.paths.ops_done_dir / f"{operation_id}.json"


def _read_done_manifest(project: Any, operation_id: str) -> OperationManifest | None:
    path = _done_manifest_path(project, operation_id)
    if not path.is_file():
        return None
    return _parse_operation_manifest(read_json(path), path)


def _save_pending(project: Any, manifest: OperationManifest) -> None:
    manifest.updated_at = now_iso()
    path = _pending_manifest_path(project, manifest.operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, manifest, operation_id=f"{manifest.operation_id}.pending")


def _write_done_manifest(project: Any, manifest: OperationManifest) -> None:
    manifest.updated_at = now_iso()
    path = _done_manifest_path(project, manifest.operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, manifest, operation_id=f"{manifest.operation_id}.done")


def pending_operation_ids(project: Any) -> list[str]:
    """Liệt kê ``operation_id`` còn trong ``.ops/pending/`` (chỉ đọc)."""
    pending_dir = project.paths.ops_pending_dir
    if not pending_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in pending_dir.iterdir()
        if entry.is_dir() and _pending_manifest_path(project, entry.name).is_file()
    )


def _pending_dir_names(project: Any) -> list[str]:
    pending_dir = project.paths.ops_pending_dir
    if not pending_dir.is_dir():
        return []
    return sorted(entry.name for entry in pending_dir.iterdir() if entry.is_dir())


def needs_recovery(project: Any) -> bool:
    """True nếu còn pending operation hoặc lock (theo architecture.md mục 7).

    Chỉ đọc; UI/Arbiter gọi hàm này rồi mới quyết định có chạy ``recover_pending``.
    """
    if _pending_dir_names(project):
        return True
    try:
        return read_lock(project) is not None
    except MalformedDocumentError:
        return True


def requires_manual_recovery(project: Any) -> bool:
    """True nếu pending operation không tự recovery được (chỉ đọc, không mutate).

    Ca "cần người xử lý": manifest không đọc được, status đã là
    ``needs_manual_recovery``, staged file thiếu, hoặc target hiện tại không khớp
    cả ``expected_old_hash`` lẫn ``staged_hash`` (bị sửa ngoài app/hỏng).
    """
    try:
        read_lock(project)
    except MalformedDocumentError:
        return True
    for operation_id in _pending_dir_names(project):
        path = _pending_manifest_path(project, operation_id)
        if not path.is_file():
            return True
        try:
            manifest = _parse_operation_manifest(read_json(path), path)
        except MalformedDocumentError:
            return True
        if manifest.status is OperationStatus.needs_manual_recovery:
            return True
        for entry in manifest.write_set:
            if entry.target in manifest.applied_paths:
                continue
            current = file_fingerprint(project.root / entry.target)
            expected = entry.expected_old_hash or ""
            if entry.staged_hash and current == entry.staged_hash:
                continue
            if current != expected:
                return True
            staged = _pending_dir(project, operation_id) / "staged" / entry.target
            if not staged.is_file():
                return True
    return False


@dataclass
class RecoveryReport:
    """Kết quả ``recover_pending``: đủ để UI báo trạng thái mà không parse message."""

    status: str = RECOVERY_CLEAN
    operation_ids: list[str] = field(default_factory=list)
    applied_paths: list[str] = field(default_factory=list)
    manual_required: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (RECOVERY_CLEAN, RECOVERY_RECOVERED)


class OperationHandle:
    """Vùng staging của một write action nhiều file (T17 finalize/reconcile dùng).

    ``add_*`` chỉ ghi vào ``.ops/pending``; target thật chỉ đổi khi ``commit()``.
    Retry cùng ``operation_id`` sau khi đã committed trả lại kết quả cũ và không
    ghi lại gì (idempotent).
    """

    def __init__(
        self,
        project: Any,
        manifest: OperationManifest,
        *,
        replayed: bool = False,
    ) -> None:
        self._project = project
        self.operation_id = manifest.operation_id
        self.operation_type = manifest.operation_type
        self.manifest = manifest
        self.replayed = replayed

    # -- staging ---------------------------------------------------------
    def _stage(self, relpath: str, data: bytes) -> None:
        if self.replayed:
            return
        normalized, _target = _project_relpath(self._project.root, relpath)
        staged = _pending_dir(self._project, self.operation_id) / "staged" / normalized
        staged.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace_bytes(staged, data, operation_id=f"{self.operation_id}.stage")
        entries = {entry.target: entry for entry in self.manifest.write_set}
        entries[normalized] = WriteSetEntry(
            target=normalized,
            staged=f".ops/pending/{self.operation_id}/staged/{normalized}",
            before=f"history/{self.operation_id}/before/{normalized}",
            expected_old_hash=file_fingerprint(self._project.root / normalized),
            staged_hash=_sha256_bytes(data),
        )
        self.manifest.write_set = list(entries.values())
        self.manifest.base_fingerprints[normalized] = (
            entries[normalized].expected_old_hash or ""
        )
        _save_pending(self._project, self.manifest)

    def add_json(self, relpath: str, data: Any) -> None:
        """Stage một file JSON, có parse lại để chắc chắn ghi ra đọc được."""
        payload = _json_bytes(data)
        try:
            json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:  # pragma: no cover - dữ liệu do app tạo
            raise StorageError(f"Dữ liệu JSON không serialize được: {exc}") from exc
        self._stage(relpath, payload)

    def add_text(self, relpath: str, text: str) -> None:
        """Stage một file text UTF-8."""
        self._stage(relpath, str(text).encode("utf-8"))

    def add_bytes(self, relpath: str, data: bytes) -> None:
        """Stage một file nhị phân (dùng cho markdown/prose đã serialize)."""
        self._stage(relpath, bytes(data))

    # -- lifecycle -------------------------------------------------------
    def commit(self) -> OperationManifest:
        """Commit toàn bộ write set (idempotent theo ``operation_id``)."""
        return commit_operation(self._project, self)

    def abort(self) -> OperationManifest:
        """Hủy operation: không target nào đổi, dọn staged, release lock.

        Nếu một phần target đã được replace thì abort bị từ chối bằng
        ``RecoveryRequired``: state đang nửa cũ nửa mới nên phải
        ``recover_pending`` để hoàn tất, không được xóa dấu vết.
        """
        if self.replayed:
            return self.manifest
        return _abort_operation(self._project, self, reason="user_abort")


def begin_operation(
    project: Any,
    *,
    operation_type: str,
    operation_id: str | None = None,
    base_fingerprints: Mapping[str, str] | None = None,
) -> OperationHandle:
    """Bắt đầu write action nhiều file: lock + manifest ``preparing``."""
    if not operation_type:
        raise StorageError("begin_operation cần operation_type.", code="invalid_operation")
    op_id = operation_id or generate_operation_id()
    done = _read_done_manifest(project, op_id)
    if done is not None and done.status is OperationStatus.committed:
        return OperationHandle(project, done, replayed=True)
    lock = read_lock(project)
    if lock is not None:
        if not _lock_is_stale(lock):
            raise LockError(
                f"Project đang bị khóa bởi {lock.operation_id} ({lock.operation_type}); "
                "release hoặc recovery trước."
            )
        pending = _pending_dir_names(project)
        if pending:
            raise RecoveryRequired(
                f"Lock stale của {lock.operation_id} còn kèm pending operation {pending}; "
                "chạy recover_pending() trước.",
                operation_ids=pending,
            )
        release_lock(project)
    pending = _pending_dir_names(project)
    if pending:
        raise RecoveryRequired(
            f"Còn pending operation {pending}; chạy recover_pending() trước khi ghi tiếp.",
            operation_ids=pending,
        )
    acquire_lock(project, operation_id=op_id, operation_type=operation_type)
    stamp = now_iso()
    manifest = OperationManifest(
        operation_id=op_id,
        operation_type=operation_type,
        status=OperationStatus.preparing,
        created_at=stamp,
        updated_at=stamp,
        base_fingerprints=dict(base_fingerprints or {}),
    )
    _save_pending(project, manifest)
    return OperationHandle(project, manifest)


def _record_history_for_transaction(project: Any, manifest: OperationManifest) -> None:
    """Copy bản ``before`` của mọi target **trước** khi replace.

    Chỉ ghi một lần cho mỗi target: nếu bản ``before`` đã tồn tại (retry, recovery,
    hoặc gọi lần hai sau commit) thì giữ nguyên bản gốc. Không có guard này, bản
    ``before`` sẽ bị copy đè bằng nội dung **sau** commit và mất khả năng audit
    (storage.md mục 3: snapshot chụp trạng thái trước khi thay accepted).
    """
    for entry in manifest.write_set:
        destination = _history_dir(project, manifest.operation_id) / "before" / entry.target
        if destination.is_file():
            continue
        source = project.root / entry.target
        if not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    history_path = _history_manifest_path(project, manifest.operation_id)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        history_path, manifest, operation_id=f"{manifest.operation_id}.history"
    )


def _cleanup_target_temps(project: Any, manifest: OperationManifest) -> None:
    for entry in manifest.write_set:
        target = project.root / entry.target
        temp = target.with_name(f"{target.name}.tmp.{manifest.operation_id}")
        if temp.exists():
            temp.unlink()


def _abort_operation(project: Any, handle: OperationHandle, *, reason: str) -> OperationManifest:
    manifest = handle.manifest
    done = _read_done_manifest(project, manifest.operation_id)
    if done is not None and done.status is OperationStatus.committed:
        # Abort sau khi đã commit là no-op: không lật ngược kết quả đã ghi.
        handle.manifest = done
        return done
    if manifest.applied_paths:
        # Đã replace một phần target: abort sạch là bất khả; giữ pending + staged
        # để `recover_pending` hoàn tất commit (crash matrix storage.md mục 7).
        manifest.status = OperationStatus.committing
        _save_pending(project, manifest)
        handle.manifest = manifest
        raise RecoveryRequired(
            f"Operation {manifest.operation_id} đã replace {manifest.applied_paths}; "
            "không thể abort sạch, phải chạy recover_pending() để đưa về state nhất quán.",
            operation_ids=[manifest.operation_id],
        )
    manifest.status = OperationStatus.aborted
    manifest.result = {"reason": reason, "applied_paths": list(manifest.applied_paths)}
    handle.manifest = manifest
    try:
        _write_done_manifest(project, manifest)
    except Exception:  # pragma: no cover - không ghi được dấu vết abort
        # Giữ pending để lần recovery sau ghi lại dấu vết; không target nào đã đổi.
        return manifest
    shutil.rmtree(_pending_dir(project, manifest.operation_id), ignore_errors=True)
    release_lock(project)
    return manifest


def _release_lock_if_owner(project: Any, operation_id: str) -> None:
    lock = read_lock(project)
    if lock is not None and lock.operation_id == operation_id:
        release_lock(project)


def commit_operation(project: Any, handle: OperationHandle) -> OperationManifest:
    """Commit write set theo thứ tự contract storage.md mục 6.

    Lỗi trước khi replace target nào thì abort sạch (accepted cũ nguyên vẹn).
    Lỗi sau khi đã replace một phần thì giữ pending manifest để
    ``recover_pending`` hoàn tất idempotent.
    """
    if handle.replayed:
        return handle.manifest
    op_id = handle.operation_id
    done = _read_done_manifest(project, op_id)
    if done is not None and done.status is OperationStatus.committed:
        handle.manifest = done
        return done
    manifest = handle.manifest
    if not manifest.write_set:
        raise StorageError(
            f"Operation {op_id} không có target nào để commit.", code="empty_operation"
        )
    pending_dir = _pending_dir(project, op_id)
    # 1. Validate staged files và base fingerprints.
    for entry in manifest.write_set:
        staged = pending_dir / "staged" / entry.target
        if not staged.is_file():
            raise StorageError(
                f"Thiếu staged file cho `{entry.target}` của operation {op_id}.",
                code="missing_staged",
            )
        if _sha256_file(staged) != entry.staged_hash:
            raise MalformedDocumentError(
                f"Staged file `{entry.target}` bị thay đổi ngoài app.", code="staged_mismatch"
            )
    stale_targets = [
        entry.target
        for entry in manifest.write_set
        if file_fingerprint(project.root / entry.target) != (entry.expected_old_hash or "")
    ]
    if stale_targets:
        _abort_operation(project, handle, reason="base_fingerprint_mismatch")
        raise StaleCandidateError(
            f"Base fingerprint lệch cho {stale_targets}; từ chối commit để không ghi đè "
            "thay đổi mới hơn."
        )
    # 2. History snapshot trước khi thay bất kỳ target nào.
    try:
        _record_history_for_transaction(project, manifest)
    except Exception as exc:
        _abort_operation(project, handle, reason="history_failed")
        raise StorageError(f"Không ghi được history snapshot: {exc}") from exc
    manifest.status = OperationStatus.staged
    _save_pending(project, manifest)
    manifest.status = OperationStatus.committing
    _save_pending(project, manifest)
    # 3. Atomic replace từng target, cập nhật applied_paths sau mỗi target.
    applied = list(manifest.applied_paths)
    for entry in manifest.write_set:
        if entry.target in applied:
            continue
        data = (pending_dir / "staged" / entry.target).read_bytes()
        try:
            _atomic_replace_bytes(project.root / entry.target, data, operation_id=op_id)
            if file_fingerprint(project.root / entry.target) != entry.staged_hash:
                raise StorageError(f"Target sau replace không khớp staged: {entry.target}")
        except Exception as exc:
            _cleanup_target_temps(project, manifest)
            manifest.applied_paths = list(applied)
            if applied:
                manifest.status = OperationStatus.committing
                _save_pending(project, manifest)
                raise RecoveryRequired(
                    f"Commit {op_id} dở dang sau khi đã ghi {applied}; chạy recover_pending().",
                    operation_ids=[op_id],
                ) from exc
            _abort_operation(project, handle, reason="replace_failed")
            if isinstance(exc, StorageError):
                raise
            raise StorageError(
                f"Commit {op_id} thất bại trước khi target nào đổi; accepted state giữ nguyên."
            ) from exc
        applied.append(entry.target)
        manifest.applied_paths = list(applied)
        _save_pending(project, manifest)
    # 4. Đóng operation.
    manifest.status = OperationStatus.committed
    manifest.result = {
        "reason": "committed",
        "operation_type": manifest.operation_type,
        "applied_paths": list(applied),
    }
    _write_done_manifest(project, manifest)
    shutil.rmtree(pending_dir, ignore_errors=True)
    release_lock(project)
    handle.manifest = manifest
    return manifest


def recover_pending(project: Any) -> RecoveryReport:
    """Recovery pending operation: hoàn tất commit dở hoặc abort op rỗng.

    Idempotent: gọi lần hai không nhân đôi replace, không merge trùng.
    """
    report = RecoveryReport()
    operation_ids = _pending_dir_names(project)
    if not operation_ids:
        try:
            lock = read_lock(project)
        except MalformedDocumentError as exc:
            report.status = RECOVERY_MANUAL
            report.manual_required.append(str(project.paths.lock_path))
            report.messages.append(
                f"project.lock không đọc được ({exc}); cần người xử lý trước khi ghi tiếp."
            )
            return report
        if lock is not None and _lock_is_stale(lock):
            release_lock(project)
            report.status = RECOVERY_RECOVERED
            report.messages.append(f"Đã xóa lock stale của {lock.operation_id}.")
        return report
    manual = False
    for op_id in operation_ids:
        pending_dir = _pending_dir(project, op_id)
        manifest_path = _pending_manifest_path(project, op_id)
        if not manifest_path.is_file():
            manual = True
            report.operation_ids.append(op_id)
            report.manual_required.append(op_id)
            report.messages.append(
                f"{op_id}: pending dir thiếu manifest.json; cần người xử lý."
            )
            continue
        try:
            manifest = _parse_operation_manifest(read_json(manifest_path), manifest_path)
        except MalformedDocumentError as exc:
            manual = True
            report.operation_ids.append(op_id)
            report.manual_required.append(op_id)
            report.messages.append(f"{op_id}: manifest không đọc được ({exc}); cần xử lý tay.")
            continue
        report.operation_ids.append(op_id)
        if manifest.status is OperationStatus.preparing and not manifest.write_set:
            manifest.status = OperationStatus.aborted
            manifest.result = {"reason": "recovered_empty_preparing"}
            _write_done_manifest(project, manifest)
            shutil.rmtree(pending_dir, ignore_errors=True)
            _release_lock_if_owner(project, op_id)
            report.messages.append(f"{op_id}: chưa staged gì, đã abort (accepted state không đổi).")
            continue
        applied = list(manifest.applied_paths)
        ok = True
        for entry in manifest.write_set:
            if entry.target in applied:
                continue
            target = project.root / entry.target
            staged = pending_dir / "staged" / entry.target
            current = file_fingerprint(target)
            if entry.staged_hash and current == entry.staged_hash:
                applied.append(entry.target)
                continue
            if current != (entry.expected_old_hash or "") or not staged.is_file():
                ok = False
                report.manual_required.append(f"{op_id}:{entry.target}")
                continue
            try:
                _atomic_replace_bytes(target, staged.read_bytes(), operation_id=op_id)
            except Exception:  # pragma: no cover - lỗi FS khi recovery
                ok = False
                report.manual_required.append(f"{op_id}:{entry.target}")
                continue
            applied.append(entry.target)
        manifest.applied_paths = list(applied)
        if not ok:
            manual = True
            manifest.status = OperationStatus.needs_manual_recovery
            _save_pending(project, manifest)
            report.messages.append(
                f"{op_id}: target bị sửa ngoài app hoặc thiếu staged; cần recovery thủ công."
            )
            continue
        manifest.status = OperationStatus.committed
        manifest.result = {
            "reason": "recovered",
            "operation_type": manifest.operation_type,
            "applied_paths": list(applied),
        }
        _write_done_manifest(project, manifest)
        shutil.rmtree(pending_dir, ignore_errors=True)
        _release_lock_if_owner(project, op_id)
        report.applied_paths.extend(applied)
        report.messages.append(f"{op_id}: đã hoàn tất commit.")
    if manual:
        report.status = RECOVERY_MANUAL
    elif report.operation_ids:
        report.status = RECOVERY_RECOVERED
    return report
