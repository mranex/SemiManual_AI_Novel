"""Validation cho Manual AI Novel (T08).

File này hiện thực phần **cross-field** của contract `docs/design/schemas.md`
mục 7 cùng các guard deterministic mà service T13–T18 phải dùng lại:

- version document (`schema_version`) được kiểm tra trước khi parse; version
  chưa hỗ trợ bị từ chối rõ ràng, không đoán và không mutate dữ liệu;
- parse document thành model Pydantic, lỗi được chuẩn hóa thành
  `ValidationIssue` có `path` (JSON Pointer), `code`, `message`, `severity`;
- kiểm tra ID trùng, FK resolve, hiệu lực `effective_from_chapter`, cặp
  relationship, `chapter_number` khớp chapter, scope Rolling patch;
- kiểm tra Writer projection không chứa field author-only/future;
- helper chọn entry theo hiệu lực chương và helper policy cho Auto Accept.

Nguyên tắc: validation **không** tự sửa dữ liệu, **không** suy diễn story fact
và **không** kết luận gì về chất lượng ngữ nghĩa. Nó chỉ trả lời: shape có
đúng contract không, ID có resolve không, scope/temporal có hợp lệ không.
`valid` không có nghĩa output đúng về văn chương hay ngữ nghĩa truyện.

Giới hạn đã biết (có chủ ý, không phải bug):

- `purpose` trong Writer projection chỉ bị coi là leak khi có field
  `purpose_visibility` đi kèm và giá trị khác `writer_safe`. Projection không
  mang `purpose_visibility` thì không thể phân biệt bằng máy; T12 phải tự chỉ
  gửi `purpose` khi `purpose_visibility == writer_safe` và test bằng secret
  sentinel.
- Các hàm ở đây không tự gọi `validate_artifact_payload` cho mọi document con.
  Service gọi khi cần; guard backend nằm ở service/lifecycle theo D011.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from novel_ai.core.models import (
    ARTIFACT_PAYLOAD_MODELS,
    CHAPTER_SCHEMA_VERSION,
    ENVELOPE_SCHEMA_VERSION,
    FORBIDDEN_WRITER_FIELDS,
    PROJECT_SCHEMA_VERSION,
    ROLLING_SHORT_PLAN_ALLOWED_FIELDS,
    SUPPORTED_CHAPTER_SCHEMA_VERSIONS,
    SUPPORTED_ENVELOPE_SCHEMA_VERSIONS,
    SUPPORTED_PROJECT_SCHEMA_VERSIONS,
    SUPPORTED_STATE_SCHEMA_VERSIONS,
    ArcPlan,
    ArtifactEnvelope,
    ChapterMetadata,
    ChapterPlan,
    Character,
    CoCreateDocument,
    CurrentTimelineDocument,
    ForeshadowEntry,
    RelationshipState,
    RelationshipStateDocument,
    Severity,
    SkeletonPayload,
    TimelineEntry,
    ValidationIssue,
    ValidationResult,
    ValidationState,
    WorldRule,
    is_temporary_id,
)

# ---------------------------------------------------------------------------
# Lỗi document
# ---------------------------------------------------------------------------


class DocumentError(ValueError):
    """Document không parse/không hợp lệ. Mang theo danh sách lỗi có đường dẫn."""

    def __init__(self, errors: Sequence[ValidationIssue] | str) -> None:
        if isinstance(errors, str):
            issues = [
                ValidationIssue(path="/", code="invalid_document", message=errors, severity=Severity.blocking)
            ]
        else:
            issues = list(errors)
        self.errors: list[ValidationIssue] = issues
        summary = "; ".join(f"{item.path} [{item.code}] {item.message}" for item in issues)
        super().__init__(summary or "document không hợp lệ")


class UnsupportedSchemaVersion(DocumentError):
    """`schema_version` không nằm trong tập hỗ trợ."""


def json_pointer(*parts: object) -> str:
    """Ghép các phần thành JSON Pointer bắt đầu bằng `/`."""
    return append_pointer("", *parts)


def _escape_pointer_part(part: object) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


def append_pointer(path: str, *parts: object) -> str:
    """Nối thêm phần vào một JSON Pointer đã có, escape từng phần mới."""
    base = path.rstrip("/")
    for part in parts:
        if part is None or part == "":
            continue
        base = f"{base}/{_escape_pointer_part(part)}"
    return base or "/"


def read_schema_version(document: Mapping[str, Any]) -> Any:
    """Đọc `schema_version` thô; trả None nếu thiếu."""
    return document.get("schema_version")


def ensure_supported_schema_version(
    document: Mapping[str, Any],
    supported: Iterable[int],
    *,
    document_name: str = "document",
    path: str = "/schema_version",
) -> int:
    """Trả version đã hỗ trợ hoặc raise `UnsupportedSchemaVersion`.

    Không sửa document, không fallback về version mặc định khi thiếu: thiếu
    version cũng là lỗi vì app phải biết mình đang đọc contract nào.
    """
    supported_set = frozenset(supported)
    raw = read_schema_version(document)
    if raw is None:
        raise UnsupportedSchemaVersion(
            [
                ValidationIssue(
                    path=path,
                    code="missing_schema_version",
                    message=(
                        f"{document_name} thiếu `schema_version`; "
                        f"các version được hỗ trợ: {sorted(supported_set)}."
                    ),
                )
            ]
        )
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise UnsupportedSchemaVersion(
            [
                ValidationIssue(
                    path=path,
                    code="invalid_schema_version",
                    message=f"{document_name} có `schema_version` không phải số nguyên.",
                )
            ]
        )
    if raw not in supported_set:
        raise UnsupportedSchemaVersion(
            [
                ValidationIssue(
                    path=path,
                    code="unsupported_schema_version",
                    message=(
                        f"{document_name} dùng schema_version {raw}; "
                        f"build này chỉ hỗ trợ {sorted(supported_set)}. "
                        "Không tự chuyển đổi hay ghi đè dữ liệu."
                    ),
                )
            ]
        )
    return raw


# ---------------------------------------------------------------------------
# Chuẩn hóa lỗi Pydantic
# ---------------------------------------------------------------------------

_PYDANTIC_CODE_MAP: dict[str, str] = {
    "missing": "missing_field",
    "extra_forbidden": "unknown_field",
    "string_pattern_mismatch": "invalid_stable_id",
    "string_type": "invalid_type",
    "int_type": "invalid_type",
    "int_parsing": "invalid_type",
    "float_type": "invalid_type",
    "bool_type": "invalid_type",
    "list_type": "invalid_type",
    "dict_type": "invalid_type",
    "enum": "invalid_enum",
    "literal_error": "invalid_value",
    "greater_than_equal": "out_of_range",
    "less_than_equal": "out_of_range",
    "greater_than": "out_of_range",
    "less_than": "out_of_range",
    "too_short": "invalid_length",
    "too_long": "invalid_length",
    "string_too_short": "invalid_length",
    "value_error": "invalid_value",
    "json_invalid": "invalid_json",
}

#: Thông báo tiếng Việt cho field bị thiếu, tránh lộ chi tiết nội bộ Pydantic.
_MESSAGE_BY_CODE: dict[str, str] = {
    "missing_field": "Thiếu field bắt buộc.",
    "unknown_field": "Field không có trong contract (có thể bị đổi tên hoặc do LLM thêm).",
    "invalid_stable_id": "ID phải là chuỗi ASCII không khoảng trắng; không dùng tên hiển thị làm khóa.",
    "invalid_type": "Sai kiểu dữ liệu.",
    "invalid_enum": "Giá trị không nằm trong enum hợp lệ.",
    "invalid_value": "Giá trị không hợp lệ.",
    "out_of_range": "Giá trị nằm ngoài khoảng cho phép.",
    "invalid_length": "Độ dài không hợp lệ.",
    "invalid_json": "Không parse được JSON.",
}


def issues_from_pydantic_error(
    exc: ValidationError,
    *,
    base_path: str = "/payload",
) -> list[ValidationIssue]:
    """Chuyển `ValidationError` thành danh sách `ValidationIssue` có đường dẫn."""
    issues: list[ValidationIssue] = []
    for error in exc.errors():
        loc = [part for part in error.get("loc", ()) if part != "__root__"]
        path = json_pointer(base_path.strip("/"), *loc) if loc else base_path or "/"
        error_type = str(error.get("type", "value_error"))
        code = _PYDANTIC_CODE_MAP.get(error_type, "invalid_value")
        issues.append(
            ValidationIssue(
                path=path,
                code=code,
                message=f"{_MESSAGE_BY_CODE.get(code, 'Giá trị không hợp lệ.')} ({error.get('msg', '')})",
                severity=Severity.blocking,
            )
        )
    return issues


def result_from_issues(issues: Sequence[ValidationIssue]) -> ValidationResult:
    """`ValidationResult` từ danh sách lỗi; rỗng nghĩa là valid."""
    items = list(issues)
    if not items:
        return ValidationResult(state=ValidationState.valid, errors=[])
    return ValidationResult(state=ValidationState.invalid, errors=items)


def valid_result() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


class IssueCollector:
    """Bộ gom lỗi nhỏ, giữ thứ tự phát hiện để test/UI đọc dễ."""

    def __init__(self) -> None:
        self.issues: list[ValidationIssue] = []

    def add(
        self,
        path: str,
        code: str,
        message: str,
        severity: Severity = Severity.blocking,
    ) -> None:
        self.issues.append(
            ValidationIssue(path=path, code=code, message=message, severity=severity)
        )

    def extend(self, issues: Iterable[ValidationIssue]) -> None:
        self.issues.extend(issues)

    def has_code(self, code: str) -> bool:
        return any(issue.code == code for issue in self.issues)

    def result(self) -> ValidationResult:
        return result_from_issues(self.issues)


# ---------------------------------------------------------------------------
# Parse document
# ---------------------------------------------------------------------------


def parse_model(model_cls: type[BaseModel], data: Any, *, base_path: str = "") -> Any:
    """Parse `data` thành `model_cls`, raise `DocumentError` nếu sai."""
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise DocumentError(issues_from_pydantic_error(exc, base_path=base_path or "/")) from exc


def parse_project_config(data: Mapping[str, Any]) -> Any:
    """Parse `project.json`, từ chối `schema_version` chưa hỗ trợ trước."""
    from novel_ai.core.models import ProjectConfig

    ensure_supported_schema_version(
        data,
        SUPPORTED_PROJECT_SCHEMA_VERSIONS,
        document_name="project.json",
    )
    return parse_model(ProjectConfig, data, base_path="/")


def parse_chapter_metadata(data: Mapping[str, Any]) -> ChapterMetadata:
    ensure_supported_schema_version(
        data,
        SUPPORTED_CHAPTER_SCHEMA_VERSIONS,
        document_name="chapter.json",
    )
    return parse_model(ChapterMetadata, data, base_path="/")


def parse_co_create_document(data: Mapping[str, Any]) -> CoCreateDocument:
    ensure_supported_schema_version(
        data, SUPPORTED_ENVELOPE_SCHEMA_VERSIONS, document_name="co_create.json"
    )
    return parse_model(CoCreateDocument, data, base_path="/")


def parse_timeline_document(data: Mapping[str, Any]) -> CurrentTimelineDocument:
    ensure_supported_schema_version(
        data, SUPPORTED_STATE_SCHEMA_VERSIONS, document_name="current_timeline.json"
    )
    return parse_model(CurrentTimelineDocument, data, base_path="/")


def parse_relationship_document(data: Mapping[str, Any]) -> RelationshipStateDocument:
    ensure_supported_schema_version(
        data, SUPPORTED_STATE_SCHEMA_VERSIONS, document_name="relationships.json"
    )
    return parse_model(RelationshipStateDocument, data, base_path="/")


def payload_model_for(artifact_type: str) -> type[BaseModel]:
    """Model payload theo `artifact_type`; raise nếu type chưa được đăng ký."""
    try:
        return ARTIFACT_PAYLOAD_MODELS[artifact_type]
    except KeyError as exc:
        raise DocumentError(
            [
                ValidationIssue(
                    path="/artifact_type",
                    code="unknown_artifact_type",
                    message=(
                        f"artifact_type `{artifact_type}` không có trong registry "
                        f"{sorted(ARTIFACT_PAYLOAD_MODELS)}."
                    ),
                )
            ]
        ) from exc


def parse_artifact_document(data: Mapping[str, Any]) -> ArtifactEnvelope[Any]:
    """Parse envelope structured, chọn payload model theo `artifact_type`."""
    ensure_supported_schema_version(
        data,
        SUPPORTED_ENVELOPE_SCHEMA_VERSIONS,
        document_name="artifact envelope",
    )
    artifact_type = data.get("artifact_type")
    if not isinstance(artifact_type, str) or not artifact_type:
        raise DocumentError(
            [
                ValidationIssue(
                    path="/artifact_type",
                    code="missing_field",
                    message="Envelope phải có `artifact_type` để chọn payload model.",
                )
            ]
        )
    model_cls = payload_model_for(artifact_type)
    envelope_cls = ArtifactEnvelope[model_cls]  # type: ignore[valid-type]
    try:
        return envelope_cls.model_validate(data)
    except ValidationError as exc:
        raise DocumentError(issues_from_pydantic_error(exc, base_path="")) from exc


# ---------------------------------------------------------------------------
# Reference index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceIndex:
    """Tập ID đã accepted mà validator dùng để resolve FK và hiệu lực chương."""

    character_ids: frozenset[str] = frozenset()
    world_rule_ids: frozenset[str] = frozenset()
    foreshadow_ids: frozenset[str] = frozenset()
    volume_ids: frozenset[str] = frozenset()
    arc_ids: frozenset[str] = frozenset()
    chapter_ids: frozenset[str] = frozenset()
    relationship_ids: frozenset[str] = frozenset()
    character_effective: Mapping[str, int] = field(default_factory=dict)
    world_rule_effective: Mapping[str, int] = field(default_factory=dict)
    foreshadow_effective: Mapping[str, int] = field(default_factory=dict)
    chapter_numbers: Mapping[str, int] = field(default_factory=dict)
    relationship_pairs: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    #: Map chapter_id -> arc_id, dùng cho Rolling scope. Không set thì rỗng.
    chapter_arc: Mapping[str, str] = field(default_factory=dict)

    def chapter_number_of(self, chapter_id: str) -> int | None:
        return self.chapter_numbers.get(chapter_id)

    def chapter_id_of(self, chapter_number: int) -> str | None:
        for chapter_id, number in self.chapter_numbers.items():
            if number == chapter_number:
                return chapter_id
        return None


def build_reference_index(
    *,
    characters: Iterable[Character] | None = None,
    world_rules: Iterable[WorldRule] | None = None,
    foreshadows: Iterable[ForeshadowEntry] | None = None,
    long_plan_arcs: Iterable[ArcPlan] | None = None,
    long_plan_volumes: Iterable[Any] | None = None,
    short_plan_chapters: Iterable[ChapterPlan] | None = None,
    chapters: Iterable[ChapterMetadata] | None = None,
    relationships: RelationshipStateDocument | None = None,
    chapter_arc: Mapping[str, str] | None = None,
) -> ReferenceIndex:
    """Dựng index từ các artifact **đã accepted**.

    Chỉ truyền dữ liệu accepted; candidate/stale không được vào index vì sẽ
    khiến FK "resolve" nhầm vào thứ chưa được duyệt.
    """
    character_list = list(characters or [])
    rule_list = list(world_rules or [])
    foreshadow_list = list(foreshadows or [])
    arc_list = list(long_plan_arcs or [])
    volume_list = list(long_plan_volumes or [])
    chapter_plan_list = list(short_plan_chapters or [])
    chapter_list = list(chapters or [])

    chapter_numbers = {item.chapter_id: item.chapter_number for item in chapter_plan_list}
    for item in chapter_list:
        chapter_numbers.setdefault(item.chapter_id, item.chapter_number)

    relationships_doc = relationships or RelationshipStateDocument()
    return ReferenceIndex(
        character_ids=frozenset(item.character_id for item in character_list),
        world_rule_ids=frozenset(item.world_rule_id for item in rule_list),
        foreshadow_ids=frozenset(item.foreshadow_id for item in foreshadow_list),
        volume_ids=frozenset(
            getattr(item, "volume_id", "") for item in volume_list if getattr(item, "volume_id", "")
        ),
        arc_ids=frozenset(item.arc_id for item in arc_list),
        chapter_ids=frozenset(chapter_numbers),
        relationship_ids=frozenset(
            item.relationship_id for item in relationships_doc.relationships
        ),
        character_effective={
            item.character_id: item.effective_from_chapter for item in character_list
        },
        world_rule_effective={
            item.world_rule_id: item.effective_from_chapter for item in rule_list
        },
        foreshadow_effective={
            item.foreshadow_id: item.effective_from_chapter for item in foreshadow_list
        },
        chapter_numbers=chapter_numbers,
        relationship_pairs={
            item.relationship_id: (item.character_ids[0], item.character_ids[1])
            for item in relationships_doc.relationships
            if len(item.character_ids) == 2
        },
        chapter_arc=dict(chapter_arc or {}),
    )


# ---------------------------------------------------------------------------
# Kiểm tra dùng chung
# ---------------------------------------------------------------------------

_EFFECTIVE_MAP_BY_KIND = {
    "character": "character_effective",
    "world_rule": "world_rule_effective",
    "foreshadow": "foreshadow_effective",
}

_KNOWN_MAP_BY_KIND = {
    "character": "character_ids",
    "world_rule": "world_rule_ids",
    "foreshadow": "foreshadow_ids",
    "arc": "arc_ids",
    "chapter": "chapter_ids",
    "relationship": "relationship_ids",
    "volume": "volume_ids",
}


def _label(kind: str) -> str:
    return {
        "character": "character",
        "world_rule": "world rule",
        "foreshadow": "foreshadow",
        "arc": "arc",
        "chapter": "chapter",
        "relationship": "relationship",
        "volume": "volume",
    }.get(kind, kind)


def check_unique_ids(
    values: Iterable[str],
    *,
    path_for,
    collector: IssueCollector,
    code: str = "duplicate_stable_id",
) -> None:
    """ID phải duy nhất trong scope của type."""
    seen: dict[str, int] = {}
    for index, value in enumerate(values):
        if value in seen:
            collector.add(
                path_for(index),
                code,
                f"ID `{value}` bị lặp (đã xuất hiện ở vị trí {seen[value]}).",
            )
        else:
            seen[value] = index


def check_reference(
    value: str | None,
    kind: str,
    *,
    path: str,
    index: ReferenceIndex | None,
    collector: IssueCollector,
    chapter_number: int | None = None,
) -> None:
    """FK phải resolve trong accepted artifact và hiệu lực cho chapter đang xét."""
    if value is None:
        return
    if index is None:
        return
    known_attr = _KNOWN_MAP_BY_KIND.get(kind)
    known: frozenset[str] = getattr(index, known_attr, frozenset()) if known_attr else frozenset()
    if value not in known and not is_temporary_id(value):
        collector.add(
            path,
            "unknown_reference",
            f"{_label(kind)} `{value}` không resolve trong accepted artifact liên quan.",
        )
        return
    if chapter_number is not None:
        effective_attr = _EFFECTIVE_MAP_BY_KIND.get(kind)
        if effective_attr:
            effective = getattr(index, effective_attr).get(value)
            if effective is not None and effective > chapter_number:
                collector.add(
                    path,
                    "effective_from_future",
                    f"`{value}` chỉ hiệu lực từ chương {effective}, "
                    f"không được dùng ở chương {chapter_number}.",
                )


def validate_effective_selection(
    *,
    for_chapter_number: int,
    index: ReferenceIndex,
    character_ids: Sequence[str] = (),
    world_rule_ids: Sequence[str] = (),
    foreshadow_ids: Sequence[str] = (),
    path_prefix: str = "/payload",
) -> list[ValidationIssue]:
    """Kiểm tra một tập ID được chọn cho chapter N không vượt hiệu lực.

    Dùng cho snapshot/context: lore hiệu lực chương 100 không được nằm trong
    selection của chương 20.
    """
    collector = IssueCollector()
    groups = (
        ("character", "character_ids", character_ids),
        ("world_rule", "effective_world_rule_ids", world_rule_ids),
        ("foreshadow", "effective_foreshadow_ids", foreshadow_ids),
    )
    for kind, field_name, values in groups:
        for position, value in enumerate(values):
            path = json_pointer(path_prefix.strip("/"), field_name, position)
            check_reference(
                value,
                kind,
                path=path,
                index=index,
                collector=collector,
                chapter_number=for_chapter_number,
            )
    return collector.issues


def validate_relationship_pair(
    character_ids: Sequence[str],
    *,
    path: str,
    index: ReferenceIndex | None,
    collector: IssueCollector,
    chapter_number: int | None = None,
) -> None:
    """Cặp relationship phải là hai character ID khác nhau, đã biết và hiệu lực."""
    if len(character_ids) != 2:
        collector.add(
            path,
            "relationship_pair_invalid",
            "Relationship phải tham chiếu đúng hai character ID.",
        )
        return
    first, second = character_ids
    if first == second:
        collector.add(
            path,
            "relationship_pair_invalid",
            "Relationship phải gồm hai character ID khác nhau.",
        )
    for position, value in enumerate(character_ids):
        check_reference(
            value,
            "character",
            path=append_pointer(path, position),
            index=index,
            collector=collector,
            chapter_number=chapter_number,
        )


def partition_by_effective_chapter(
    items: Iterable[Any],
    *,
    chapter_number: int,
    kind: str,
    id_of,
    effective_of,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Chia entry thành `(dùng được cho chương N, bị loại vì hiệu lực tương lai)`.

    Phần bị loại trả về dạng dict khớp `ExcludedDueToEffectiveChapter` để
    snapshot ghi lại lý do loại mà không cần đọc lại artifact.
    """
    included: list[Any] = []
    excluded: list[dict[str, Any]] = []
    for item in items:
        effective = int(effective_of(item))
        if effective <= chapter_number:
            included.append(item)
        else:
            excluded.append(
                {
                    "item_kind": kind,
                    "item_id": id_of(item),
                    "effective_from_chapter": effective,
                }
            )
    return included, excluded


# ---------------------------------------------------------------------------
# Writer projection guard
# ---------------------------------------------------------------------------

_SECRET_MESSAGE: dict[str, str] = {
    "author_only": "Writer context cannot contain author-only truth.",
    "truth_author_only": "Writer context cannot contain author-only truth.",
    "author_only_notes": "Writer context cannot contain author-only notes.",
    "future_direction": "Writer context cannot contain future direction.",
    "planned_payoff": "Writer context cannot contain foreshadow payoff truth.",
    "planned_planting": "Writer context cannot contain foreshadow planting truth.",
    "relationship_directions": "Writer context cannot contain future relationship plan.",
    "relationship_changes": "Writer context cannot contain future relationship plan.",
    "major_reveals": "Writer context cannot contain future plan reveals.",
    "end_state": "Writer context cannot contain future plan state.",
}


def find_forbidden_writer_fields(
    value: Any,
    *,
    base_path: str = "/payload",
    forbidden: frozenset[str] = FORBIDDEN_WRITER_FIELDS,
    extra_paths: frozenset[str] = frozenset(),
) -> list[ValidationIssue]:
    """Quét đệ quy payload Writer tìm field author-only/future."""
    issues: list[ValidationIssue] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                child_path = f"{path}/{key}"
                key_text = str(key)
                if key_text in forbidden or child_path in extra_paths:
                    issues.append(
                        ValidationIssue(
                            path=child_path,
                            code="secret_leak",
                            message=_SECRET_MESSAGE.get(
                                key_text,
                                f"Writer context cannot contain `{key_text}`.",
                            ),
                            severity=Severity.blocking,
                        )
                    )
                    continue
                if (
                    key_text == "purpose"
                    and "purpose_visibility" in node
                    and node.get("purpose_visibility") != "writer_safe"
                ):
                    issues.append(
                        ValidationIssue(
                            path=child_path,
                            code="secret_leak",
                            message=(
                                "Writer context chỉ được chứa `purpose` khi "
                                "purpose_visibility = writer_safe."
                            ),
                            severity=Severity.blocking,
                        )
                    )
                    continue
                walk(child, child_path)
        elif isinstance(node, (list, tuple)):
            for position, child in enumerate(node):
                walk(child, f"{path}/{position}")

    walk(value, base_path.rstrip("/") or "")
    return issues


def validate_writer_projection(
    payload: Any,
    *,
    base_path: str = "/payload",
    extra_paths: frozenset[str] = frozenset(),
) -> ValidationResult:
    """Guard bắt buộc trước khi gửi payload cho Writer."""
    return result_from_issues(
        find_forbidden_writer_fields(payload, base_path=base_path, extra_paths=extra_paths)
    )


def find_secret_text(value: Any, secrets: Iterable[str], *, base_path: str = "/payload") -> list[str]:
    """Trả các path chứa secret sentinel. Dùng để test chống leak theo nội dung."""
    needles = [secret for secret in secrets if secret]
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                walk(child, f"{path}/{key}")
        elif isinstance(node, (list, tuple)):
            for position, child in enumerate(node):
                walk(child, f"{path}/{position}")
        elif isinstance(node, str):
            if any(needle in node for needle in needles):
                found.append(path)

    if not needles:
        return found
    walk(value, base_path.rstrip("/") or "")
    return found


# ---------------------------------------------------------------------------
# Validation theo artifact type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationContext:
    """Tham số ngoài payload mà validator cần để kiểm tra scope/temporal."""

    index: ReferenceIndex | None = None
    chapter_number: int | None = None
    eligible_chapter_ids: frozenset[str] | None = None
    allow_relationship_replan: bool | None = None
    reviewed_chapter_range: tuple[int, int] | None = None


def _validate_outline_contract_items(plan: ChapterPlan, collector: IssueCollector) -> None:
    """Item object trong outline phải là `{language, pov, length_guidance}`."""
    required = ("language", "pov", "length_guidance")
    for position, item in enumerate(plan.outline):
        if not isinstance(item, Mapping):
            continue
        path = json_pointer("payload", "chapters", plan.chapter_id, "outline", position)
        missing = [name for name in required if not str(item.get(name, "")).strip()]
        if missing:
            collector.add(
                path,
                "invalid_outline_contract_item",
                "Item object trong outline phải có language, pov và length_guidance "
                f"không rỗng (thiếu: {', '.join(missing)}).",
            )
        extra = [str(key) for key in item if key not in required]
        if extra:
            collector.add(
                path,
                "unknown_field",
                f"Item contract trong outline chỉ nhận {list(required)}; thấy thêm {extra}.",
            )


def _validate_directions(
    directions: Iterable[Any],
    *,
    path_for,
    collector: IssueCollector,
    index: ReferenceIndex | None,
    chapter_number: int | None,
) -> None:
    for position, direction in enumerate(directions):
        base = path_for(position)
        validate_relationship_pair(
            direction.character_ids,
            path=f"{base}/character_ids",
            index=index,
            collector=collector,
            chapter_number=chapter_number,
        )
        rel_id = direction.relationship_id
        if rel_id is not None:
            check_reference(
                rel_id,
                "relationship",
                path=f"{base}/relationship_id",
                index=index,
                collector=collector,
            )
            if index is not None:
                expected = index.relationship_pairs.get(rel_id)
                if expected is not None and tuple(sorted(expected)) != tuple(
                    sorted(direction.character_ids)
                ):
                    collector.add(
                        f"{base}/relationship_id",
                        "relationship_pair_mismatch",
                        f"`{rel_id}` không khớp cặp character_ids được khai báo.",
                    )


def _validate_characters(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    check_unique_ids(
        (item.character_id for item in payload.characters),
        path_for=lambda position: json_pointer("payload", "characters", position, "character_id"),
        collector=collector,
    )


def _validate_world_rules(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    check_unique_ids(
        (item.world_rule_id for item in payload.world_rules),
        path_for=lambda position: json_pointer("payload", "world_rules", position, "world_rule_id"),
        collector=collector,
    )


def _validate_foreshadows(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    check_unique_ids(
        (item.foreshadow_id for item in payload.foreshadows),
        path_for=lambda position: json_pointer("payload", "foreshadows", position, "foreshadow_id"),
        collector=collector,
    )


def long_plan_horizon_issues(
    payload: Any, scope: Mapping[str, int] | None = None
) -> list[ValidationIssue]:
    """Invariant cấu trúc của Long Plan theo horizon (D017, `schemas.md` mục 3.1).

    Phần không phụ thuộc scope (`empty_long_plan`, `empty_volume`,
    `invalid_chapter_range`) luôn được kiểm. Khi `scope` được truyền, hàm kiểm
    tiếp coverage: arc nằm trong horizon, arc đầu bắt đầu đúng `scope.start`, arc
    cuối kết đúng `scope.end`, và các arc liên tục theo thứ tự volume → arc.

    Đây là invariant **cấu trúc**: nó không đánh giá chất lượng phân rã, không có
    quota số volume/arc và không thay semantic review của người dùng.
    """
    collector = IssueCollector()
    volumes = getattr(payload, "volumes", None) or []
    if not volumes:
        collector.add("/payload/volumes", "empty_long_plan", "Long Plan phải có ít nhất một volume.")
        return collector.issues

    arcs: list[Any] = []
    for volume_position, volume in enumerate(volumes):
        volume_arcs = getattr(volume, "arcs", None) or []
        if not volume_arcs:
            collector.add(
                json_pointer("payload", "volumes", volume_position, "arcs"),
                "empty_volume",
                f"Volume `{volume.volume_id}` phải có ít nhất một arc.",
            )
            continue
        for arc_position, arc in enumerate(volume_arcs):
            base = json_pointer("payload", "volumes", volume_position, "arcs", arc_position)
            if arc.chapter_range.end < arc.chapter_range.start:
                collector.add(
                    f"{base}/chapter_range",
                    "invalid_chapter_range",
                    "chapter_range của arc phải có start <= end.",
                )
            arcs.append(arc)

    if collector.issues or scope is None or not arcs:
        return collector.issues

    start = int(scope["start"])
    end = int(scope["end"])
    for arc in arcs:
        low = arc.chapter_range.start
        high = arc.chapter_range.end
        if low < start or high > end:
            collector.add(
                "/payload/volumes",
                "out_of_scope_arc",
                f"Arc `{arc.arc_id}` có chapter_range {low}-{high} nằm ngoài "
                f"planning_scope {start}-{end}.",
            )
    if collector.issues:
        return collector.issues

    if arcs[0].chapter_range.start != start:
        collector.add(
            "/payload/volumes",
            "uncovered_scope_start",
            f"Arc đầu tiên bắt đầu ở chương {arcs[0].chapter_range.start} nhưng "
            f"planning_scope bắt đầu ở {start}.",
        )
    if arcs[-1].chapter_range.end != end:
        collector.add(
            "/payload/volumes",
            "uncovered_scope_end",
            f"Arc cuối kết ở chương {arcs[-1].chapter_range.end} nhưng planning_scope "
            f"kết ở {end}.",
        )
    for previous, current in zip(arcs, arcs[1:]):
        previous_end = previous.chapter_range.end
        current_start = current.chapter_range.start
        if current_start == previous_end + 1:
            continue
        if current_start <= previous_end:
            collector.add(
                "/payload/volumes",
                "overlap",
                f"Arc `{current.arc_id}` ({current_start}-{current.chapter_range.end}) "
                f"chồng lấn arc `{previous.arc_id}` (kết {previous_end}).",
            )
        else:
            collector.add(
                "/payload/volumes",
                "gap_in_scope",
                f"Thiếu coverage chương {previous_end + 1}-{current_start - 1} giữa arc "
                f"`{previous.arc_id}` và `{current.arc_id}`.",
            )
    return collector.issues


def _validate_long_plan(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    index = context.index
    collector.issues.extend(long_plan_horizon_issues(payload))
    volume_ids: list[str] = []
    arc_ids: list[str] = []
    for volume_position, volume in enumerate(payload.volumes):
        volume_ids.append(volume.volume_id)
        for arc_position, arc in enumerate(volume.arcs):
            arc_ids.append(arc.arc_id)
            base = json_pointer("payload", "volumes", volume_position, "arcs", arc_position)
            for position, character_id in enumerate(arc.character_ids):
                check_reference(
                    character_id,
                    "character",
                    path=f"{base}/character_ids/{position}",
                    index=index,
                    collector=collector,
                )
            for position, rule_id in enumerate(arc.world_rule_ids):
                check_reference(
                    rule_id,
                    "world_rule",
                    path=f"{base}/world_rule_ids/{position}",
                    index=index,
                    collector=collector,
                )
            for position, foreshadow_id in enumerate(arc.foreshadow_ids):
                check_reference(
                    foreshadow_id,
                    "foreshadow",
                    path=f"{base}/foreshadow_ids/{position}",
                    index=index,
                    collector=collector,
                )
            _validate_directions(
                arc.relationship_directions,
                path_for=lambda position, base=base: f"{base}/relationship_directions/{position}",
                collector=collector,
                index=index,
                chapter_number=None,
            )
    check_unique_ids(
        volume_ids,
        path_for=lambda position: json_pointer("payload", "volumes", position, "volume_id"),
        collector=collector,
    )
    seen_arcs: dict[str, int] = {}
    for position, arc_id in enumerate(arc_ids):
        if arc_id in seen_arcs:
            collector.add(
                "/payload",
                "duplicate_stable_id",
                f"arc ID `{arc_id}` bị lặp trong Long Plan.",
            )
        else:
            seen_arcs[arc_id] = position


def _validate_short_plan(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    index = context.index
    check_reference(
        payload.arc_id,
        "arc",
        path="/payload/arc_id",
        index=index,
        collector=collector,
    )
    check_unique_ids(
        (chapter.chapter_id for chapter in payload.chapters),
        path_for=lambda position: json_pointer("payload", "chapters", position, "chapter_id"),
        collector=collector,
    )
    check_unique_ids(
        (str(chapter.chapter_number) for chapter in payload.chapters),
        path_for=lambda position: json_pointer("payload", "chapters", position, "chapter_number"),
        collector=collector,
        code="duplicate_chapter_number",
    )
    for position, chapter in enumerate(payload.chapters):
        base = json_pointer("payload", "chapters", position)
        if is_temporary_id(chapter.chapter_id):
            collector.add(
                f"{base}/chapter_id",
                "temporary_id_not_allowed",
                "Chapter ID do backend cấp từ assigned_chapters; không nhận ID tạm.",
            )
        _validate_outline_contract_items(chapter, collector)
        for character_position, character_id in enumerate(chapter.character_ids):
            check_reference(
                character_id,
                "character",
                path=f"{base}/character_ids/{character_position}",
                index=index,
                collector=collector,
                chapter_number=chapter.chapter_number,
            )
        for rule_position, rule_id in enumerate(chapter.world_rule_ids):
            check_reference(
                rule_id,
                "world_rule",
                path=f"{base}/world_rule_ids/{rule_position}",
                index=index,
                collector=collector,
                chapter_number=chapter.chapter_number,
            )
        for foreshadow_position, foreshadow_id in enumerate(chapter.foreshadow_ids):
            check_reference(
                foreshadow_id,
                "foreshadow",
                path=f"{base}/foreshadow_ids/{foreshadow_position}",
                index=index,
                collector=collector,
                chapter_number=chapter.chapter_number,
            )
        _validate_directions(
            chapter.relationship_changes,
            path_for=lambda item_position, base=base: f"{base}/relationship_changes/{item_position}",
            collector=collector,
            index=index,
            chapter_number=chapter.chapter_number,
        )


def _validate_skeleton(payload: SkeletonPayload, context: ValidationContext, collector: IssueCollector) -> None:
    index = context.index
    chapter_number = context.chapter_number or payload.chapter_number
    if is_temporary_id(payload.chapter_id):
        collector.add(
            "/payload/chapter_id",
            "temporary_id_not_allowed",
            "Skeleton chỉ được tạo cho chapter_id có trong Short Plan accepted.",
        )
    index_number = index.chapter_number_of(payload.chapter_id) if index else None
    if index is not None and index.chapter_ids and payload.chapter_id not in index.chapter_ids:
        collector.add(
            "/payload/chapter_id",
            "unknown_reference",
            f"Skeleton phải thuộc chapter có trong Short Plan accepted; `{payload.chapter_id}` không có.",
        )
    if index_number is not None and index_number != payload.chapter_number:
        collector.add(
            "/payload/chapter_number",
            "chapter_mismatch",
            f"chapter_id `{payload.chapter_id}` phải có chapter_number {index_number}.",
        )
    check_unique_ids(
        (section.section_id for section in payload.sections),
        path_for=lambda position: json_pointer("payload", "sections", position, "section_id"),
        collector=collector,
    )
    check_unique_ids(
        (str(section.index) for section in payload.sections),
        path_for=lambda position: json_pointer("payload", "sections", position, "index"),
        collector=collector,
        code="duplicate_section_index",
    )
    for position, section in enumerate(payload.sections):
        base = json_pointer("payload", "sections", position)
        for character_position, character_id in enumerate(section.character_ids):
            check_reference(
                character_id,
                "character",
                path=f"{base}/character_ids/{character_position}",
                index=index,
                collector=collector,
                chapter_number=chapter_number,
            )
        for rule_position, rule_id in enumerate(section.world_rule_ids):
            check_reference(
                rule_id,
                "world_rule",
                path=f"{base}/world_rule_ids/{rule_position}",
                index=index,
                collector=collector,
                chapter_number=chapter_number,
            )
        for foreshadow_position, foreshadow_id in enumerate(section.foreshadow_ids):
            check_reference(
                foreshadow_id,
                "foreshadow",
                path=f"{base}/foreshadow_ids/{foreshadow_position}",
                index=index,
                collector=collector,
                chapter_number=chapter_number,
            )
        for surface_position, surface in enumerate(section.foreshadow_surfaces):
            check_reference(
                surface.foreshadow_id,
                "foreshadow",
                path=f"{base}/foreshadow_surfaces/{surface_position}/foreshadow_id",
                index=index,
                collector=collector,
                chapter_number=chapter_number,
            )


def _validate_review_report(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    check_unique_ids(
        (issue.issue_id for issue in payload.issues),
        path_for=lambda position: json_pointer("payload", "issues", position, "issue_id"),
        collector=collector,
    )
    for position, issue in enumerate(payload.issues):
        if issue.evidence.prose_revision != payload.prose_revision:
            collector.add(
                json_pointer("payload", "issues", position, "evidence", "prose_revision"),
                "review_revision_mismatch",
                "Evidence phải gắn đúng prose_revision của report.",
            )


def _validate_rewrite(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    if not payload.replacement_markdown.strip():
        collector.add(
            "/payload/replacement_markdown",
            "empty_replacement",
            "Replacement rỗng là no-op; backend không tạo revision mới từ output rỗng.",
        )


def _validate_reconciliation(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    index = context.index
    chapter_number = context.chapter_number
    index_number = index.chapter_number_of(payload.chapter_id) if index else None
    if index_number is not None and index_number != payload.chapter_number:
        collector.add(
            "/payload/chapter_number",
            "chapter_mismatch",
            f"chapter_id {payload.chapter_id} must have chapter_number {index_number}.",
        )
    elif chapter_number is not None and chapter_number != payload.chapter_number:
        collector.add(
            "/payload/chapter_number",
            "chapter_mismatch",
            f"Chapter đang finalize là chương {chapter_number}, không phải {payload.chapter_number}.",
        )
    seen_pairs: set[tuple[str, str]] = set()
    for position, update in enumerate(payload.relationship_updates):
        base = json_pointer("payload", "relationship_updates", position)
        validate_relationship_pair(
            update.character_ids,
            path=f"{base}/character_ids",
            index=index,
            collector=collector,
            chapter_number=payload.chapter_number,
        )
        if len(update.character_ids) == 2:
            key = tuple(sorted(update.character_ids))
            if key in seen_pairs:
                collector.add(
                    base,
                    "duplicate_relationship_update",
                    "Mỗi cặp relationship chỉ được có tối đa một update.",
                )
            seen_pairs.add(key)
        if update.relationship_id is not None:
            check_reference(
                update.relationship_id,
                "relationship",
                path=f"{base}/relationship_id",
                index=index,
                collector=collector,
            )
            if index is not None:
                expected = index.relationship_pairs.get(update.relationship_id)
                if expected is not None and tuple(sorted(expected)) != key:
                    collector.add(
                        f"{base}/relationship_id",
                        "relationship_pair_mismatch",
                        f"`{update.relationship_id}` không khớp cặp character_ids được khai báo.",
                    )


def _validate_rolling_patch(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    eligible = context.eligible_chapter_ids
    allow_relationship_replan = context.allow_relationship_replan
    low, high = context.reviewed_chapter_range or (None, None)

    if low is not None and high is not None:
        if payload.reviewed_chapter_range.start < low or payload.reviewed_chapter_range.end > high:
            collector.add(
                "/payload/reviewed_chapter_range",
                "out_of_scope",
                "Rolling review chỉ được dùng range actual đã cấp cho lần review này.",
            )

    targets: dict[str, list[str]] = {}
    for position, change in enumerate(payload.short_plan_changes):
        base = json_pointer("payload", "short_plan_changes", position)
        if not change.changes:
            collector.add(f"{base}/changes", "empty_change", "Change rỗng không thay đổi gì.")
        unknown = sorted(str(key) for key in change.changes if key not in ROLLING_SHORT_PLAN_ALLOWED_FIELDS)
        if unknown:
            collector.add(
                f"{base}/changes",
                "field_not_allowed",
                f"Rolling patch chỉ được đổi {sorted(ROLLING_SHORT_PLAN_ALLOWED_FIELDS)}; thấy {unknown}.",
            )
        targets.setdefault(change.chapter_id, []).append("short_plan_changes")

    for position, change in enumerate(payload.relationship_plan_changes):
        base = json_pointer("payload", "relationship_plan_changes", position)
        if allow_relationship_replan is False:
            collector.add(
                f"{base}/relationship_changes",
                "relationship_replan_disabled",
                "allow_relationship_replan = false nên không được đề xuất đổi hướng quan hệ.",
            )
        _validate_directions(
            change.relationship_changes,
            path_for=lambda item_position, base=base: f"{base}/relationship_changes/{item_position}",
            collector=collector,
            index=context.index,
            chapter_number=None,
        )
        targets.setdefault(change.chapter_id, []).append("relationship_plan_changes")

    if eligible is not None:
        for chapter_id in sorted(targets):
            if chapter_id not in eligible:
                collector.add(
                    "/payload/short_plan_changes",
                    "out_of_scope",
                    f"Chapter `{chapter_id}` không nằm trong eligible scope của Rolling review.",
                )
    for chapter_id, kinds in sorted(targets.items()):
        if len(kinds) > 1:
            collector.add(
                "/payload",
                "duplicate_chapter_change",
                f"Chapter `{chapter_id}` có nhiều hơn một loại change trong cùng proposal.",
            )

    if payload.status.value == "ok" and (
        payload.deviations
        or payload.short_plan_changes
        or payload.relationship_plan_changes
        or payload.blocked_by_authority
    ):
        collector.add(
            "/payload/status",
            "status_mismatch",
            "status `ok` chỉ hợp lệ khi không có deviation, change hoặc blocked_by_authority.",
        )


def _validate_impact_report(payload: Any, context: ValidationContext, collector: IssueCollector) -> None:
    for position, item in enumerate(payload.affected_items):
        if not item.reason.strip():
            collector.add(
                json_pointer("payload", "affected_items", position, "reason"),
                "missing_evidence",
                "Impact item phải nêu cơ sở (reason), không chỉ nêu tên.",
            )


_VALIDATORS: dict[str, Any] = {
    "characters": _validate_characters,
    "world_rules": _validate_world_rules,
    "foreshadow": _validate_foreshadows,
    "long_plan": _validate_long_plan,
    "short_plan": _validate_short_plan,
    "skeleton": _validate_skeleton,
    "review_report": _validate_review_report,
    "rewrite_section": _validate_rewrite,
    "reconciliation": _validate_reconciliation,
    "rolling_patch": _validate_rolling_patch,
    "impact_report": _validate_impact_report,
}


def validate_artifact_payload(
    artifact_type: str,
    payload: Mapping[str, Any] | BaseModel,
    *,
    context: ValidationContext | None = None,
) -> ValidationResult:
    """Parse + validate cross-field một payload theo `artifact_type`.

    Trả `ValidationResult` thay vì raise để service/UI hiển thị lỗi theo field.
    Shape sai (Pydantic) và lỗi cross-field được gộp trong cùng danh sách.
    """
    context = context or ValidationContext()
    try:
        model_cls = payload_model_for(artifact_type)
    except DocumentError as exc:
        return result_from_issues(exc.errors)
    try:
        parsed = payload if isinstance(payload, model_cls) else model_cls.model_validate(payload)
    except ValidationError as exc:
        return result_from_issues(issues_from_pydantic_error(exc, base_path="/payload"))

    collector = IssueCollector()
    validator = _VALIDATORS.get(artifact_type)
    if validator is not None:
        validator(parsed, context, collector)
    return collector.result()


# ---------------------------------------------------------------------------
# Guard plan không mutate state, Auto Accept, và liệt kê field lạ
# ---------------------------------------------------------------------------

_PLAN_MUTATION_KEYS = frozenset({"relationship_updates", "timeline", "current", "latest_final_chapter"})


def validate_plan_does_not_mutate_state(payload: Mapping[str, Any]) -> ValidationResult:
    """Plan/rolling output không được mang actual state update.

    Short Plan chỉ được chứa `relationship_changes` (hướng tương lai), không
    được chứa `relationship_updates`/`timeline` của Current State.
    """
    collector = IssueCollector()

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                child_path = f"{path}/{key}"
                if str(key) == "relationship_updates" and isinstance(child, (list, tuple)):
                    for position, item in enumerate(child):
                        item_path = f"{child_path}/{position}"
                        target = f"{item_path}/current" if isinstance(item, Mapping) and "current" in item else item_path
                        collector.add(
                            target,
                            "plan_actual_state_mix",
                            "Short Plan can contain relationship_changes as future direction, "
                            "not current relationship updates.",
                        )
                    continue
                if str(key) == "timeline" and "chapter_number" in node:
                    collector.add(
                        child_path,
                        "plan_actual_state_mix",
                        "Plan không được ghi Current Timeline; timeline chỉ cập nhật từ reconciliation.",
                    )
                    continue
                walk(child, child_path)
        elif isinstance(node, (list, tuple)):
            for position, child in enumerate(node):
                walk(child, f"{path}/{position}")

    walk(payload, "/payload")
    return collector.result()


#: Output kind được phép auto accept: chỉ structured artifact.
AUTO_ACCEPT_ALLOWED_OUTPUT_KINDS: frozenset[str] = frozenset(
    {
        "characters",
        "world_rules",
        "foreshadow",
        "premise",
        "long_plan",
        "short_plan",
        "skeleton",
        "rolling_patch",
    }
)

#: Output kind không bao giờ được auto accept (D005).
AUTO_ACCEPT_FORBIDDEN_OUTPUT_KINDS: frozenset[str] = frozenset(
    {"markdown", "prose", "human_review", "review_confirmation"}
)


def validate_auto_accept_scope(
    *,
    auto_accept_structured: bool,
    output_kind: str,
    requested_transition: str | None = None,
) -> ValidationResult:
    """Auto Accept chỉ áp dụng cho structured output đã validate (D005).

    Chỉ những `output_kind` nằm trong `AUTO_ACCEPT_ALLOWED_OUTPUT_KINDS` mới được
    auto accept. Kind ngoài allowlist (report, impact, reconciliation, hoặc typo)
    bị từ chối thay vì mặc định cho qua: allowlist là danh sách đóng nên một
    `output_kind` mới không thể vô tình được auto accept.
    """
    collector = IssueCollector()
    if requested_transition == "final_reconciled":
        collector.add(
            "/payload/requested_transition",
            "auto_accept_cannot_finalize_prose",
            "Auto Accept applies only to structured output and cannot finalize prose.",
        )
        return collector.result()
    if not auto_accept_structured:
        return collector.result()
    if output_kind in AUTO_ACCEPT_FORBIDDEN_OUTPUT_KINDS:
        collector.add(
            "/payload/output_kind",
            "auto_accept_cannot_finalize_prose",
            f"Output kind `{output_kind}` phải do user xác nhận; Auto Accept không áp dụng.",
        )
        return collector.result()
    if output_kind not in AUTO_ACCEPT_ALLOWED_OUTPUT_KINDS:
        collector.add(
            "/payload/output_kind",
            "auto_accept_kind_not_allowed",
            f"`{output_kind}` không nằm trong allowlist auto accept "
            f"{sorted(AUTO_ACCEPT_ALLOWED_OUTPUT_KINDS)}; cần user accept thủ công.",
        )
    return collector.result()


def validate_envelope_candidate(
    envelope: ArtifactEnvelope[Any],
    *,
    context: ValidationContext | None = None,
) -> ValidationResult:
    """Validate `candidate_revision.payload` của một envelope đã parse."""
    if envelope.candidate_revision is None:
        return result_from_issues(
            [
                ValidationIssue(
                    path="/candidate_revision",
                    code="missing_candidate",
                    message="Không có candidate để validate/accept.",
                )
            ]
        )
    return validate_artifact_payload(
        envelope.artifact_type,
        envelope.candidate_revision.payload,
        context=context,
    )


def summarize_errors(result: ValidationResult) -> str:
    """Chuỗi ngắn gọn để log/UI, giữ nguyên đường dẫn field."""
    return "; ".join(f"{item.path}: {item.message}" for item in result.errors)


def has_blocking(result: ValidationResult) -> bool:
    return any(item.severity is Severity.blocking for item in result.errors)


__all__ = [
    "AUTO_ACCEPT_ALLOWED_OUTPUT_KINDS",
    "AUTO_ACCEPT_FORBIDDEN_OUTPUT_KINDS",
    "DocumentError",
    "IssueCollector",
    "ReferenceIndex",
    "UnsupportedSchemaVersion",
    "ValidationContext",
    "build_reference_index",
    "check_reference",
    "check_unique_ids",
    "ensure_supported_schema_version",
    "find_forbidden_writer_fields",
    "find_secret_text",
    "has_blocking",
    "issues_from_pydantic_error",
    "json_pointer",
    "parse_artifact_document",
    "parse_chapter_metadata",
    "parse_co_create_document",
    "parse_model",
    "parse_project_config",
    "parse_relationship_document",
    "parse_timeline_document",
    "partition_by_effective_chapter",
    "payload_model_for",
    "result_from_issues",
    "summarize_errors",
    "valid_result",
    "validate_artifact_payload",
    "validate_auto_accept_scope",
    "validate_effective_selection",
    "validate_envelope_candidate",
    "validate_plan_does_not_mutate_state",
    "validate_relationship_pair",
    "validate_writer_projection",
]
