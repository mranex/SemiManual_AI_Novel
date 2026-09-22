"""Editor schema-aware nhỏ dùng chung (T37; `UI_FIX_03`).

Nguyên tắc:

- Không rich text, không framework form lớn: form dựng trực tiếp từ **model schema
  thật** của payload (`model_fields`), nên field nào schema có thì form có.
- Roundtrip: hàm trả về dict **copy từ payload gốc** rồi chỉ ghi đè field có widget.
  Field không có widget (nested/optional/lạ) được giữ nguyên, không bị nuốt.
- Metadata do app sở hữu (`*_id`, `status`, revision, timestamp, pin) render
  read-only — form không được đổi.
- Field author-only (`author_only`, `truth_author_only`, `future_direction`) có nhãn
  cảnh báo rõ và không lẫn với projection writer-safe.
- Save vẫn là action tường minh của page; module này **không** gọi service.

Validation chính thức vẫn do service/model; `field_issues` chỉ ánh xạ lỗi validation
sang field/card để hiển thị cạnh chỗ sai.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

__all__ = [
    "AUTHOR_ONLY_FIELDS",
    "READONLY_FIELDS",
    "field_issues",
    "is_author_only",
    "is_readonly_field",
    "render_artifact_form",
    "render_model_form",
    "scalar_kind",
    "string_list_kind",
]

#: Field do app sở hữu: stable ID, lifecycle, revision/timestamp.
#: `chapter_number` nằm ở đây vì số chương do `assigned_chapters`/chapter metadata
#: cấp (T36 chặn `chapter_number_mismatch`); user sửa số chương là đổi scope.
READONLY_FIELDS: frozenset[str] = frozenset(
    {
        "chapter_number",
        "status",
        "revision",
        "created_at",
        "updated_at",
        "accepted_at",
        "accepted_by",
        "dependency_pins",
        "schema_version",
        "short_plan_pin",
        "previous_chapter_id",
        "source_final_candidate",
    }
)

#: Field chỉ dành cho tác giả (không được gửi Writer).
AUTHOR_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "author_only",
        "truth_author_only",
        "future_direction",
        "author_only_notes",
        "planned_payoff",
    }
)

_STRING_LIST = ("str",)
_MAX_DEPTH = 3


def is_readonly_field(name: str) -> bool:
    """True nếu field do app sở hữu (stable ID/lifecycle/revision)."""
    return name in READONLY_FIELDS or name.endswith("_id")


def is_author_only(name: str) -> bool:
    """True nếu field là author-only (hiển thị nhãn cảnh báo, không gửi Writer)."""
    return name in AUTHOR_ONLY_FIELDS


def _unwrap(annotation: Any) -> Any:
    """Bỏ `Optional[...]`/`X | None` để lấy kiểu thật."""
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if len(args) == 1 and origin in (Union, UnionType):
        return args[0]
    return annotation


def scalar_kind(annotation: Any) -> str | None:
    """`"str" | "int" | "float" | "bool"` cho widget scalar; `None` nếu không phải."""
    annotation = _unwrap(annotation)
    if annotation is str:
        return "str"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is bool:
        return "bool"
    return None


def string_list_kind(annotation: Any) -> bool:
    """True nếu field là `list[str]` (render một dòng một mục)."""
    annotation = _unwrap(annotation)
    if get_origin(annotation) is not list:
        return False
    args = get_args(annotation)
    return bool(args) and _unwrap(args[0]) is str


def _list_model(annotation: Any) -> type[BaseModel] | None:
    """Model class nếu field là `list[SomeModel]`."""
    annotation = _unwrap(annotation)
    if get_origin(annotation) is not list:
        return None
    args = get_args(annotation)
    if not args:
        return None
    inner = _unwrap(args[0])
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return inner
    return None


def _model_kind(annotation: Any) -> type[BaseModel] | None:
    """Model class nếu field là **một** nested model (ví dụ `chapter_range`)."""
    annotation = _unwrap(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _freeform_kind(annotation: Any) -> bool:
    """True nếu field là dict/JSON tự do (render JSON text area)."""
    annotation = _unwrap(annotation)
    origin = get_origin(annotation)
    if origin is dict:
        return True
    return annotation in (dict, Any)


def _label(name: str, *, author_only: bool = False) -> str:
    suffix = "  ·  ⚠ author-only (không gửi Writer)" if author_only else ""
    return f"{name}{suffix}"


def _lines_to_list(text: Any) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _list_to_lines(values: Sequence[Any]) -> str:
    return "\n".join(str(item) for item in values)


def render_model_form(
    model_cls: type[BaseModel],
    data: Mapping[str, Any],
    *,
    key_prefix: str,
    depth: int = 0,
    selectors: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Render form cho một model; trả dict đã sửa (giữ nguyên field không có widget).

    `data` được copy trước khi ghi đè, nên field nested/optional không có widget
    hoặc field lạ vẫn round-trip nguyên vẹn.

    `selectors` (T38): `{field_name: [id_hợp_lệ]}` ⇒ field đó render bằng multiselect
    thay vì text area. ID đang có nhưng không nằm trong danh sách hợp lệ vẫn được giữ
    trong options để không âm thầm mất dữ liệu (validator sẽ báo nếu thật sự sai).
    Từ T39 selector áp cho cả field cùng tên ở model lồng (section Skeleton,
    relationship update) vì đó cũng là tham chiếu stable ID.
    """
    import streamlit as st

    selectors = selectors or {}
    edited: dict[str, Any] = dict(data)
    for name, field in model_cls.model_fields.items():
        value = edited.get(name, field.get_default(call_default_factory=True))
        widget_key = f"{key_prefix}__{name}"
        author_only = is_author_only(name)
        if is_readonly_field(name):
            st.caption(f"`{name}` (app quản lý, read-only): `{value}`")
            continue
        if name in selectors:
            allowed = [str(item) for item in selectors[name]]
            current = [str(item) for item in (value or [])]
            options = [*allowed, *[item for item in current if item not in allowed]]
            edited[name] = st.multiselect(
                _label(name, author_only=author_only),
                options,
                default=current,
                key=widget_key,
            )
            continue
        kind = scalar_kind(field.annotation)
        if kind == "bool":
            edited[name] = st.checkbox(_label(name, author_only=author_only), value=bool(value), key=widget_key)
            continue
        if kind in ("int", "float"):
            step = 1 if kind == "int" else 0.1
            number = st.number_input(
                _label(name, author_only=author_only),
                value=None if value is None else (int(value) if kind == "int" else float(value)),
                step=step,
                key=widget_key,
            )
            edited[name] = number
            continue
        if kind == "str":
            current = "" if value is None else str(value)
            if len(current) > 120 or "\n" in current:
                edited[name] = st.text_area(
                    _label(name, author_only=author_only), value=current, height=100, key=widget_key
                )
            else:
                edited[name] = st.text_input(
                    _label(name, author_only=author_only), value=current, key=widget_key
                )
            continue
        if string_list_kind(field.annotation):
            edited[name] = _lines_to_list(
                st.text_area(
                    f"{_label(name, author_only=author_only)} (mỗi dòng một mục)",
                    value=_list_to_lines(value or []),
                    height=100,
                    key=widget_key,
                )
            )
            continue
        nested = _list_model(field.annotation)
        if nested is not None and depth < _MAX_DEPTH:
            rows = list(value or [])
            st.markdown(f"**{_label(name, author_only=author_only)}** ({len(rows)})")
            rendered_rows: list[Any] = []
            for index, row in enumerate(rows):
                row_data = row if isinstance(row, Mapping) else row.model_dump(mode="json")
                title = str(row_data.get("display_name") or row_data.get("label") or row_data.get("title") or f"{name}[{index}]")
                st.markdown(f"*{index + 1}. {title}*")
                rendered_rows.append(
                    render_model_form(
                        nested,
                        row_data,
                        key_prefix=f"{widget_key}__{index}",
                        depth=depth + 1,
                        selectors=selectors,
                    )
                )
            edited[name] = rendered_rows
            continue
        nested_single = _model_kind(field.annotation)
        if nested_single is not None and depth < _MAX_DEPTH:
            if isinstance(value, BaseModel):
                nested_data = value.model_dump(mode="json")
            elif isinstance(value, Mapping):
                nested_data = dict(value)
            else:
                nested_data = {}
            st.markdown(f"**{_label(name, author_only=author_only)}**")
            edited[name] = render_model_form(
                nested_single,
                nested_data,
                key_prefix=widget_key,
                depth=depth + 1,
                selectors=selectors,
            )
            continue
        if _freeform_kind(field.annotation):
            raw = st.text_area(
                f"{_label(name, author_only=author_only)} (JSON)",
                value=json.dumps(value if value is not None else {}, ensure_ascii=False, indent=2),
                height=120,
                key=widget_key,
            )
            try:
                edited[name] = json.loads(raw) if str(raw).strip() else {}
            except json.JSONDecodeError:
                st.warning(f"`{name}` không phải JSON hợp lệ; giữ nguyên giá trị cũ cho tới khi sửa.")
                edited[name] = value
            continue
        # Kiểu chưa hỗ trợ: giữ nguyên và nói thật (không bịa widget).
        st.caption(f"`{name}` (kiểu chưa có widget): giữ nguyên giá trị hiện tại.")
    return edited


def render_artifact_form(
    artifact_type: str,
    data: Mapping[str, Any],
    *,
    key_prefix: str,
    selectors: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Form cho một artifact type theo model registry của app."""
    from novel_ai.core.models import ARTIFACT_PAYLOAD_MODELS

    model_cls = ARTIFACT_PAYLOAD_MODELS[artifact_type]
    return render_model_form(model_cls, data, key_prefix=key_prefix, selectors=selectors)


def field_issues(result: Any, *, root: str = "/payload") -> dict[str, list[str]]:
    """Ánh xạ `ValidationResult.errors` sang `field path -> [message]`.

    Dùng để hiển thị lỗi cạnh field/card. Path JSON Pointer
    (`/payload/characters/0/public_profile/description`) được rút gọn thành đường dẫn
    tương đối trong payload; lỗi ở gốc payload gom vào key `""`.
    """
    mapped: dict[str, list[str]] = {}
    for issue in getattr(result, "errors", []) or []:
        path = str(getattr(issue, "path", "") or "")
        relative = path[len(root) :].lstrip("/") if path.startswith(root) else path.lstrip("/")
        message = str(getattr(issue, "message", "") or "")
        code = str(getattr(issue, "code", "") or "")
        entry = f"{message} _(code `{code}`)_" if code else message
        mapped.setdefault(relative, []).append(entry)
    return mapped
