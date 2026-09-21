"""Prompt loader v1 và template rendering (T10).

Module này là **cổng duy nhất** để runtime lấy prompt. Nó đọc manifest explicit
`docs/prompts/v1/manifest.json`, chỉ load template/reference đã đăng ký trong
manifest đó và render thành `messages` cho LLM adapter.

Luật đã chốt (không được nới trong module này):

- Chỉ prompt v1 có trong manifest mới được load. **Không** glob thư mục docs,
  **không** fallback sang prompt legacy trong `docs/prompts/*.md`, **không** nội
  suy template bằng string replace. Biến của template là **field input JSON**,
  không phải marker thay chuỗi trong Markdown.
- Prompt runtime không chứa giao thức agent/tool (D010): loader từ chối template
  mang dấu hiệu protocol cũ thay vì load rồi hy vọng model bỏ qua.
- Mọi lỗi load/render raise `PromptError` có `code` ổn định và **không** gửi
  request: render fail thì service chưa được gọi LLM.
- Template path và reference path phải nằm trong repository root (không escape
  bằng `..`, absolute path hay symlink).
- API key/secret không bao giờ đi qua module này: prompt payload chỉ chứa dữ
  liệu truyện do service/context cấp.

Đóng gói reference (`reference_selection`): backend chọn **đúng một** entry từ
registry `genres`/`styles` theo `input_field`, rồi **thay giá trị ID bằng nội
dung file** trước khi serialize payload. Caller (T12 context) đặt sẵn ID
registry vào `inputs[input_field]`; loader không mutate mapping của caller.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from novel_ai import config as app_config
from novel_ai.core.llm import ChatMessage
from novel_ai.core.models import ProjectConfig

__all__ = [
    "FORBIDDEN_PROTOCOL_MARKERS",
    "MANIFEST_FILENAME",
    "PROMPT_ERROR_CODES",
    "PromptError",
    "PromptRegistry",
    "PromptSpec",
    "REFERENCE_REGISTRIES",
    "RenderedPrompt",
    "find_forbidden_protocol_markers",
    "load_reference",
    "render_prompt",
]

MANIFEST_FILENAME = "manifest.json"
MANIFEST_VERSION = 1
PATH_BASE_REPOSITORY_ROOT = "repository_root"
INPUT_MODE_JSON_MESSAGE = "json_message"
UNKNOWN_PROMPT_POLICY_ERROR = "error"
REFERENCE_POLICY_EXPLICIT_ONLY = "explicit_only"
EXACTLY_ONE_SELECTION = "exactly_one"

OUTPUT_KINDS = ("json", "markdown")

#: Registry reference mà manifest được phép trỏ tới.
REFERENCE_REGISTRIES: tuple[str, ...] = ("genres", "styles")

#: `input_field` -> attribute `ProjectConfig` cấp ID mặc định cho reference.
REFERENCE_ID_FIELD_BY_REGISTRY: dict[str, str] = {
    "genres": "genre_prompt_id",
    "styles": "writing_style_id",
}

#: Mã lỗi ổn định để service/UI phân loại mà không parse message.
PROMPT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_manifest",
        "missing_template",
        "missing_reference",
        "unknown_prompt",
        "unknown_reference",
        "missing_variable",
        "unknown_variable",
        "invalid_inputs",
        "legacy_protocol",
    }
)

#: Dấu hiệu giao thức agent/tool của bộ prompt cũ. Prompt v1 không được chứa.
FORBIDDEN_PROTOCOL_MARKERS: frozenset[str] = frozenset(
    {
        "novel_context",
        "save_foundation",
        "dispatch",
        "audit_foundation",
        "tool_calls",
        "function_call",
    }
)

#: ID registry chỉ gồm chữ thường/số/gạch dưới; dùng để nhận diện "trông như ID".
_REFERENCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class PromptError(ValueError):
    """Lỗi load/render prompt.

    `code` ổn định (`missing_variable`, `unknown_reference`, `unknown_prompt`,
    `missing_template`, `invalid_manifest`, ...) để caller xử lý mà không phải
    đọc message. `prompt_id`/`variable` giúp error nêu đúng chỗ sai.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "prompt_error",
        prompt_id: str | None = None,
        variable: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.prompt_id = prompt_id
        self.variable = variable
        self.details: dict[str, Any] = dict(details or {})


@dataclass(frozen=True)
class PromptSpec:
    """Một prompt v1 đã đăng ký trong manifest.

    `template_variables` là **field bắt buộc** của input JSON (không phải marker
    nội suy). `reference_selection` là tuple các selection đã chuẩn hóa
    (`input_field`, `registry`, `selection`). Chỉ ghi, không sửa.
    """

    prompt_id: str
    template_path: Path
    template_variables: tuple[str, ...]
    output_kind: str
    schema_ref: str
    schema_document: str
    schema_section: str
    reference_selection: tuple[dict[str, str], ...]
    template_text: str
    template_hash: str


@dataclass(frozen=True)
class RenderedPrompt:
    """Kết quả render, đủ để request LLM và để debug mà không cần đọc lại file.

    - `messages`: một system message chứa template text + một user message chứa
      payload JSON của action.
    - `template_hash`: sha256 của template text + entry manifest.
    - `prompt_version`: `"<prompt_id>+<8 ký tự hash>"`, đưa vào metadata debug.
    - `input_payload`: input **sau khi** reference đã được thay bằng nội dung.
    - `references`: `input_field` -> ID registry đã chọn. Không chứa nội dung
      file (nội dung đã nằm trong `input_payload`) và không chứa secret.
    """

    prompt_id: str
    messages: tuple[ChatMessage, ...]
    template_hash: str
    prompt_version: str
    input_payload: dict[str, Any]
    references: dict[str, str]

    @property
    def system_text(self) -> str:
        return self.messages[0].content

    @property
    def user_text(self) -> str:
        return self.messages[1].content


def _hash_text_parts(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _canonical_entry(entry: Mapping[str, Any]) -> str:
    """JSON canonical của entry manifest để hash ổn định giữa các lần load."""
    return json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def find_forbidden_protocol_markers(text: str) -> list[str]:
    """Trả các marker giao thức agent cũ (đã sắp xếp) xuất hiện trong `text`.

    So khớp không phân biệt hoa thường vì prompt cũ viết lẫn lộn. Dùng cho guard
    lúc load và cho test chống tái nhập giao thức cũ vào prompt v1.
    """
    lowered = text.lower()
    return sorted(marker for marker in FORBIDDEN_PROTOCOL_MARKERS if marker in lowered)


# ---------------------------------------------------------------------------
# Validate manifest
# ---------------------------------------------------------------------------


def _invalid(message: str, *, prompt_id: str | None = None, details: Mapping[str, Any] | None = None) -> PromptError:
    return PromptError(
        message,
        code="invalid_manifest",
        prompt_id=prompt_id,
        details=details,
    )


def _require_str(
    value: Any,
    *,
    label: str,
    prompt_id: str | None = None,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise _invalid(f"Manifest v1: `{label}` phải là chuỗi không rỗng.", prompt_id=prompt_id)
    return value


def _resolve_registered_path(
    root: Path,
    relative: Any,
    *,
    label: str,
    prompt_id: str | None = None,
) -> Path:
    """Resolve path tương đối theo repo root và từ chối escape/absolute path."""
    raw = _require_str(relative, label=label, prompt_id=prompt_id)
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.drive:
        raise _invalid(
            f"Manifest v1: `{label}` phải là đường dẫn tương đối tính từ repository root, không nhận `{raw}`.",
            prompt_id=prompt_id,
        )
    resolved = (root / candidate).resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise _invalid(
            f"Manifest v1: `{label}` trỏ ra ngoài repository root ({resolved} không nằm trong {root}).",
            prompt_id=prompt_id,
        )
    return resolved


def _parse_reference_registry(
    raw: Any,
    *,
    kind: str,
    root: Path,
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise _invalid(f"Manifest v1: `{kind}` phải là object ID -> path.")
    registry: dict[str, str] = {}
    for ref_id, path in raw.items():
        if not isinstance(ref_id, str) or not _REFERENCE_ID_RE.match(ref_id):
            raise _invalid(
                f"Manifest v1: ID trong `{kind}` phải là chuỗi chữ thường/số/gạch dưới, thấy {ref_id!r}."
            )
        if ref_id in registry:
            raise _invalid(f"Manifest v1: ID `{ref_id}` bị lặp trong `{kind}`.")
        _resolve_registered_path(root, path, label=f"{kind}.{ref_id}")
        registry[ref_id] = str(path)
    return registry


def _parse_reference_selection(
    raw: Any,
    *,
    template_variables: tuple[str, ...],
    prompt_id: str,
) -> tuple[dict[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _invalid("Manifest v1: `reference_selection` phải là array.", prompt_id=prompt_id)
    selections: list[dict[str, str]] = []
    seen_fields: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise _invalid("Manifest v1: mỗi reference selection phải là object.", prompt_id=prompt_id)
        input_field = item.get("input_field")
        registry = item.get("registry")
        selection = item.get("selection", EXACTLY_ONE_SELECTION)
        if not isinstance(input_field, str) or not input_field:
            raise _invalid("Manifest v1: reference selection thiếu `input_field`.", prompt_id=prompt_id)
        if registry not in REFERENCE_REGISTRIES:
            raise _invalid(
                f"Manifest v1: reference registry `{registry}` không được hỗ trợ; chỉ có {list(REFERENCE_REGISTRIES)}.",
                prompt_id=prompt_id,
            )
        if selection != EXACTLY_ONE_SELECTION:
            raise _invalid(
                f"Manifest v1: selection `{selection}` không được hỗ trợ; reference phải là `{EXACTLY_ONE_SELECTION}`.",
                prompt_id=prompt_id,
            )
        if input_field not in template_variables:
            raise _invalid(
                f"Manifest v1: reference input_field `{input_field}` không nằm trong template_variables của `{prompt_id}`.",
                prompt_id=prompt_id,
            )
        if input_field in seen_fields:
            raise _invalid(
                f"Manifest v1: reference input_field `{input_field}` bị khai báo hai lần trong `{prompt_id}`.",
                prompt_id=prompt_id,
            )
        seen_fields.add(input_field)
        selections.append(
            {"input_field": input_field, "registry": str(registry), "selection": selection}
        )
    return tuple(selections)


def _parse_prompt_entry(
    entry: Any,
    *,
    root: Path,
    seen_ids: set[str],
) -> PromptSpec:
    if not isinstance(entry, Mapping):
        raise _invalid("Manifest v1: mỗi prompt phải là object.")
    prompt_id = entry.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id.strip():
        raise _invalid("Manifest v1: prompt thiếu `prompt_id`.")
    if prompt_id in seen_ids:
        raise _invalid(f"Manifest v1: prompt_id `{prompt_id}` bị lặp.", prompt_id=prompt_id)

    raw_variables = entry.get("template_variables")
    if not isinstance(raw_variables, list) or not raw_variables:
        raise _invalid("Manifest v1: `template_variables` phải là array không rỗng.", prompt_id=prompt_id)
    variables: list[str] = []
    for item in raw_variables:
        if not isinstance(item, str) or not item.strip():
            raise _invalid("Manifest v1: template variable phải là chuỗi không rỗng.", prompt_id=prompt_id)
        if item in variables:
            raise _invalid(f"Manifest v1: template variable `{item}` bị lặp.", prompt_id=prompt_id)
        variables.append(item)
    template_variables = tuple(variables)

    output_kind = entry.get("output_kind")
    if output_kind not in OUTPUT_KINDS:
        raise _invalid(
            f"Manifest v1: output_kind `{output_kind}` không hợp lệ; chỉ có {list(OUTPUT_KINDS)}.",
            prompt_id=prompt_id,
        )
    schema_ref = _require_str(entry.get("schema_ref"), label="schema_ref", prompt_id=prompt_id)
    schema_document = _require_str(
        entry.get("schema_document"), label="schema_document", prompt_id=prompt_id
    )
    schema_section = entry.get("schema_section") or ""
    if not isinstance(schema_section, str):
        raise _invalid("Manifest v1: `schema_section` phải là chuỗi.", prompt_id=prompt_id)

    template_path = _resolve_registered_path(
        root, entry.get("template"), label="template", prompt_id=prompt_id
    )
    if not template_path.is_file():
        raise PromptError(
            f"Prompt `{prompt_id}` thiếu template đã đăng ký: {template_path}. "
            "Runtime không fallback sang prompt legacy.",
            code="missing_template",
            prompt_id=prompt_id,
        )
    template_text = template_path.read_text(encoding="utf-8")
    markers = find_forbidden_protocol_markers(template_text)
    if markers:
        raise PromptError(
            f"Template `{prompt_id}` chứa dấu hiệu giao thức agent/tool cũ: {', '.join(markers)}. "
            "Prompt runtime chỉ nhận input đóng gói và trả output theo schema (D010).",
            code="legacy_protocol",
            prompt_id=prompt_id,
            details={"markers": markers},
        )

    reference_selection = _parse_reference_selection(
        entry.get("reference_selection", []),
        template_variables=template_variables,
        prompt_id=prompt_id,
    )

    seen_ids.add(prompt_id)
    return PromptSpec(
        prompt_id=prompt_id,
        template_path=template_path,
        template_variables=template_variables,
        output_kind=str(output_kind),
        schema_ref=schema_ref,
        schema_document=schema_document,
        schema_section=schema_section,
        reference_selection=reference_selection,
        template_text=template_text,
        template_hash=_hash_text_parts(template_text, _canonical_entry(entry)),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class PromptRegistry:
    """Manifest v1 đã validate + prompt/reference đã load.

    Khởi tạo qua `PromptRegistry.load`. Registry bất biến sau khi load: chỉ đọc
    lại file khi caller gọi `load` lần nữa (service không tự reload giữa action).
    """

    repo_root: Path
    prompt_root: Path
    manifest_path: Path
    manifest_version: int
    specs: dict[str, PromptSpec] = field(default_factory=dict)
    #: Field private (không tên `genres`/`styles`) để không che method truy vấn.
    genre_paths: dict[str, str] = field(default_factory=dict)
    style_paths: dict[str, str] = field(default_factory=dict)

    # -- load ---------------------------------------------------------------
    @classmethod
    def load(
        cls,
        prompt_root: Path | None = None,
        *,
        repo_root: Path | None = None,
    ) -> PromptRegistry:
        """Load manifest v1.

        `repo_root=None` dùng `novel_ai.config.REPO_ROOT`, `prompt_root=None`
        dùng prompt root từ app config (mặc định `docs/prompts/v1`), nên loader
        không phụ thuộc current working directory.
        """
        root = Path(repo_root).resolve() if repo_root is not None else Path(app_config.REPO_ROOT).resolve()

        if prompt_root is None:
            prompt_root_path = app_config.load_config(repo_root=root).prompt_root
        else:
            candidate = Path(prompt_root).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            prompt_root_path = candidate.resolve()

        manifest_path = prompt_root_path / MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise PromptError(
                f"Không thấy manifest prompt v1 tại {manifest_path}. "
                "Runtime chỉ load prompt đã đăng ký trong manifest explicit.",
                code="invalid_manifest",
            )
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PromptError(
                f"manifest.json tại {manifest_path} không đọc/parse được: {exc}",
                code="invalid_manifest",
            ) from exc

        return cls._from_manifest(
            raw,
            repo_root=root,
            prompt_root=prompt_root_path,
            manifest_path=manifest_path,
        )

    @classmethod
    def _from_manifest(
        cls,
        raw: Any,
        *,
        repo_root: Path,
        prompt_root: Path,
        manifest_path: Path,
    ) -> PromptRegistry:
        if not isinstance(raw, Mapping):
            raise _invalid("manifest.json phải là object JSON.")

        version = raw.get("manifest_version")
        if isinstance(version, bool) or not isinstance(version, int) or version != MANIFEST_VERSION:
            raise _invalid(
                f"manifest_version {version!r} không được hỗ trợ; build này chỉ hỗ trợ {MANIFEST_VERSION}."
            )
        for key, expected in (
            ("path_base", PATH_BASE_REPOSITORY_ROOT),
            ("input_mode", INPUT_MODE_JSON_MESSAGE),
            ("unknown_prompt_policy", UNKNOWN_PROMPT_POLICY_ERROR),
            ("reference_policy", REFERENCE_POLICY_EXPLICIT_ONLY),
        ):
            actual = raw.get(key)
            if actual != expected:
                raise _invalid(
                    f"Manifest v1: `{key}` phải là `{expected}`, thấy {actual!r}. "
                    "Không hỗ trợ chế độ khác để tránh load nhầm prompt ngoài allowlist."
                )

        raw_prompts = raw.get("prompts")
        if not isinstance(raw_prompts, list) or not raw_prompts:
            raise _invalid("Manifest v1: `prompts` phải là array không rỗng.")

        specs: dict[str, PromptSpec] = {}
        seen_ids: set[str] = set()
        for entry in raw_prompts:
            spec = _parse_prompt_entry(entry, root=repo_root, seen_ids=seen_ids)
            specs[spec.prompt_id] = spec

        genres = _parse_reference_registry(raw.get("genres", {}), kind="genres", root=repo_root)
        styles = _parse_reference_registry(raw.get("styles", {}), kind="styles", root=repo_root)

        return cls(
            repo_root=repo_root,
            prompt_root=prompt_root,
            manifest_path=manifest_path,
            manifest_version=MANIFEST_VERSION,
            specs=specs,
            genre_paths=genres,
            style_paths=styles,
        )

    # -- truy vấn -----------------------------------------------------------
    def get(self, prompt_id: str) -> PromptSpec:
        """PromptSpec theo ID; ID lạ raise `unknown_prompt` (không fallback)."""
        try:
            return self.specs[prompt_id]
        except KeyError as exc:
            raise PromptError(
                f"Prompt `{prompt_id}` không có trong manifest v1 "
                f"(unknown_prompt_policy={UNKNOWN_PROMPT_POLICY_ERROR}, không fallback sang prompt cũ). "
                f"Prompt đã đăng ký: {', '.join(self.prompt_ids())}.",
                code="unknown_prompt",
                prompt_id=prompt_id,
            ) from exc

    def prompt_ids(self) -> list[str]:
        return sorted(self.specs)

    def prompt_specs(self) -> list[PromptSpec]:
        return [self.specs[prompt_id] for prompt_id in self.prompt_ids()]

    def genres(self) -> dict[str, str]:
        """Bản copy `ID genre -> path` từ manifest (không phải nội dung prompt)."""
        return dict(self.genre_paths)

    def styles(self) -> dict[str, str]:
        """Bản copy `ID style -> path` từ manifest (không phải nội dung prompt)."""
        return dict(self.style_paths)

    def reference_registry(self, kind: str) -> dict[str, str]:
        """Bản copy registry reference theo `kind`; kind lạ raise `unknown_reference`."""
        if kind == "genres":
            return dict(self.genre_paths)
        if kind == "styles":
            return dict(self.style_paths)
        raise PromptError(
            f"Reference registry `{kind}` không được hỗ trợ; chỉ có {list(REFERENCE_REGISTRIES)}.",
            code="unknown_reference",
            details={"kind": kind},
        )

    def reference_ids(self, kind: str) -> list[str]:
        return sorted(self.reference_registry(kind))

    def reference_path(self, kind: str, ref_id: str) -> Path:
        """Path tuyệt đối của reference đã đăng ký; ID lạ raise `unknown_reference`."""
        registry = self.reference_registry(kind)
        relative = registry.get(ref_id)
        if relative is None:
            raise PromptError(
                f"Reference `{ref_id}` không có trong registry `{kind}` đã đăng ký: "
                f"{', '.join(sorted(registry)) or '(rỗng)'}. Không fallback âm thầm.",
                code="unknown_reference",
                details={"kind": kind, "reference_id": ref_id},
            )
        resolved = (self.repo_root / relative).resolve()
        if resolved != self.repo_root and not resolved.is_relative_to(self.repo_root):
            raise _invalid(
                f"Reference `{kind}.{ref_id}` trỏ ra ngoài repository root ({resolved}).",
            )
        return resolved

    def __contains__(self, prompt_id: object) -> bool:
        return prompt_id in self.specs

    def __len__(self) -> int:
        return len(self.specs)


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------


def load_reference(registry: PromptRegistry, kind: str, ref_id: str) -> str:
    """Nội dung file của reference đã đăng ký.

    `kind` là `genres` hoặc `styles`. ID lạ hoặc registry lạ raise
    `PromptError(code="unknown_reference")`; file thiếu raise
    `PromptError(code="missing_reference")`. Không bao giờ fallback sang file
    khác hoặc sang prompt cũ.
    """
    path = registry.reference_path(kind, ref_id)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(
            f"Reference `{kind}.{ref_id}` thiếu file đã đăng ký: {path}.",
            code="missing_reference",
            details={"kind": kind, "reference_id": ref_id, "path": str(path)},
        ) from exc


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"kiểu {type(value).__name__} không serialize được sang JSON")


def _configured_reference_id(kind: str, config: ProjectConfig | None) -> str | None:
    if config is None:
        return None
    attribute = REFERENCE_ID_FIELD_BY_REGISTRY.get(kind)
    if attribute is None:
        return None
    value = getattr(config, attribute, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _select_reference_id(
    registry: PromptRegistry,
    payload: Mapping[str, Any],
    *,
    input_field: str,
    kind: str,
    prompt_id: str,
    config: ProjectConfig | None,
) -> str | None:
    """Chọn reference cho một `input_field`. `None` = giữ nguyên nội dung caller cấp.

    Thứ tự ưu tiên (không bao giờ fallback sang file/prompt khác):

    1. `payload[input_field]` là **ID đã đăng ký** -> dùng entry đó (caller T12
       đặt sẵn `genre_prompt_id`/`writing_style_id`).
    2. `payload[input_field]` **trông như ID** (`[a-z][a-z0-9_]*`) nhưng không có
       trong registry -> `unknown_reference`. ID lạ không bao giờ được đoán.
    3. `payload[input_field]` là chuỗi **không phải dạng ID** (có khoảng trắng,
       dấu câu, nội dung dài) -> coi là nội dung reference caller đã resolve sẵn,
       giữ nguyên, không đọc registry.
    4. Field thiếu/rỗng -> lấy ID từ `config`; vẫn không resolve được thì
       `missing_variable` (không tự chọn default âm thầm).

    Giá trị không phải chuỗi (dict/list/số) là lỗi caller -> `unknown_reference`.
    """
    explicit = payload.get(input_field)
    if isinstance(explicit, str):
        value = explicit.strip()
        if value:
            if value in registry.reference_registry(kind):
                return value
            if _REFERENCE_ID_RE.match(value):
                raise PromptError(
                    f"Input cho `{prompt_id}`: `{input_field}` = `{value}` không có trong registry `{kind}` "
                    f"({', '.join(registry.reference_ids(kind)) or '(rỗng)'}). Không fallback âm thầm.",
                    code="unknown_reference",
                    prompt_id=prompt_id,
                    variable=input_field,
                    details={"kind": kind, "reference_id": value},
                )
            return None
    elif explicit is not None:
        raise PromptError(
            f"Input cho `{prompt_id}`: field `{input_field}` phải là ID registry hoặc nội dung chuỗi, "
            f"không nhận {type(explicit).__name__}.",
            code="unknown_reference",
            prompt_id=prompt_id,
            variable=input_field,
            details={"kind": kind},
        )

    configured = _configured_reference_id(kind, config)
    if configured is not None:
        if configured not in registry.reference_registry(kind):
            raise PromptError(
                f"Config của `{prompt_id}` trỏ tới `{kind}.{configured}` không có trong manifest "
                f"({', '.join(registry.reference_ids(kind)) or '(rỗng)'}).",
                code="unknown_reference",
                prompt_id=prompt_id,
                variable=input_field,
                details={"kind": kind, "reference_id": configured},
            )
        return configured

    raise PromptError(
        f"Prompt `{prompt_id}` thiếu biến bắt buộc: {input_field} (reference `{kind}`, chọn đúng một entry). "
        "Không gửi request khi render fail.",
        code="missing_variable",
        prompt_id=prompt_id,
        variable=input_field,
        details={"kind": kind},
    )


def _dump_payload(payload: Mapping[str, Any], *, prompt_id: str) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
    except TypeError as exc:
        raise PromptError(
            f"Input của `{prompt_id}` không serialize được sang JSON: {exc}",
            code="invalid_inputs",
            prompt_id=prompt_id,
        ) from exc


def render_prompt(
    registry: PromptRegistry,
    prompt_id: str,
    *,
    inputs: Mapping[str, Any],
    repo_root: Path,
    config: ProjectConfig | None = None,
) -> RenderedPrompt:
    """Render prompt v1 thành `messages` + metadata debug.

    Caller (T12 context) đặt sẵn ID registry vào `inputs[input_field]`; loader
    **thay giá trị đó bằng nội dung file** đã đăng ký. Chuỗi không phải dạng ID
    được coi là nội dung caller đã resolve và giữ nguyên. `inputs` của caller
    **không** bị mutate: hàm copy nông trước khi thay field reference (chỉ ghi
    đè ở tầng top-level nên copy nông là đủ).

    Không gọi LLM, không ghi file, không tự chạy bước tiếp.
    """
    if not isinstance(inputs, Mapping):
        raise PromptError(
            "`inputs` phải là mapping field -> giá trị đã kiểm tra.",
            code="invalid_inputs",
            prompt_id=prompt_id,
        )

    spec = registry.get(prompt_id)

    root = Path(repo_root).resolve()
    if not spec.template_path.is_relative_to(root):
        raise PromptError(
            f"Template của `{prompt_id}` ({spec.template_path}) nằm ngoài repository root {root}.",
            code="invalid_manifest",
            prompt_id=prompt_id,
        )

    payload: dict[str, Any] = dict(inputs)
    references: dict[str, str] = {}
    for selection in spec.reference_selection:
        input_field = selection["input_field"]
        kind = selection["registry"]
        ref_id = _select_reference_id(
            registry,
            payload,
            input_field=input_field,
            kind=kind,
            prompt_id=prompt_id,
            config=config,
        )
        if ref_id is None:
            # Caller đã cấp nội dung reference; không đọc registry, không ghi `references`.
            continue
        payload[input_field] = load_reference(registry, kind, ref_id)
        references[input_field] = ref_id

    declared = set(spec.template_variables)
    unknown = sorted(str(name) for name in set(payload) - declared)
    if unknown:
        raise PromptError(
            f"Input cho `{prompt_id}` chứa biến không khai báo trong manifest: {', '.join(unknown)}.",
            code="unknown_variable",
            prompt_id=prompt_id,
            variable=unknown[0],
        )
    missing = [name for name in spec.template_variables if name not in payload]
    if missing:
        raise PromptError(
            f"Prompt `{prompt_id}` thiếu biến bắt buộc: {', '.join(missing)}. "
            "Không gửi request khi render fail.",
            code="missing_variable",
            prompt_id=prompt_id,
            variable=missing[0],
        )

    text = _dump_payload(payload, prompt_id=prompt_id)
    messages = (
        ChatMessage(role="system", content=spec.template_text),
        ChatMessage(role="user", content=text),
    )
    return RenderedPrompt(
        prompt_id=prompt_id,
        messages=messages,
        template_hash=spec.template_hash,
        prompt_version=f"{prompt_id}+{spec.template_hash[:8]}",
        input_payload=payload,
        references=references,
    )
