"""Kiểm tra prompt loader v1, reference policy và template rendering (T10).

Test offline, không gọi LLM, không cần API key. Phần lớn test dùng manifest thật
`docs/prompts/v1/manifest.json`; các case manifest sai dùng manifest tổng hợp
trong `tmp_path` để không phải sửa file contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from novel_ai.core.models import ProjectConfig
from novel_ai.core.prompts import (
    FORBIDDEN_PROTOCOL_MARKERS,
    PROMPT_ERROR_CODES,
    PromptError,
    PromptRegistry,
    find_forbidden_protocol_markers,
    load_reference,
    render_prompt,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_ROOT = REPO_ROOT / "docs" / "prompts" / "v1"
MANIFEST_PATH = PROMPT_ROOT / "manifest.json"
LEGACY_PROMPT = REPO_ROOT / "docs" / "prompts" / "writer.md"

#: registry -> attribute `ProjectConfig` cấp ID mặc định.
REFERENCE_ATTR = {"genres": "genre_prompt_id", "styles": "writing_style_id"}

#: Dấu hiệu giao thức agent/tool của bộ prompt cũ (D010).
_LEGACY_MARKERS = (
    "novel_context",
    "save_foundation",
    "dispatch",
    "audit_foundation",
    "tool_calls",
    "function_call",
)
_FILE_TOOL_PHRASES = re.compile(
    r"(đọc file|đọc tệp|mở file|gọi công cụ|gọi tool|dùng công cụ|sử dụng công cụ|thực thi công cụ)",
    re.IGNORECASE,
)
_NEGATIONS = ("không", "chẳng", "đừng", "chứ không")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry() -> PromptRegistry:
    return PromptRegistry.load(repo_root=REPO_ROOT)


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def project_config() -> ProjectConfig:
    stamp = "2026-09-19T09:00:00+07:00"
    return ProjectConfig(
        project_id="proj_test",
        title="Truyện test",
        default_language="vi",
        genre_prompt_id="suspense",
        writing_style_id="default",
        created_at=stamp,
        updated_at=stamp,
    )


def _placeholder(name: str) -> Any:
    """Giá trị giả theo tên biến; T10 chỉ kiểm tra hiện diện, T08 kiểm tra shape."""
    if name == "language":
        return "vi"
    if name == "action":
        return "generate"
    if name in {"chapter_number", "prose_revision", "latest_consistent_chapter"}:
        return 1
    if name == "allow_relationship_replan":
        return True
    if name == "chapter_id":
        return "ch_0001"
    if name == "title":
        return "Chương thử"
    if name == "user_message":
        return "Tin nhắn thử của người dùng."
    return {"fixture_field": name, "value": f"giá trị cho {name}"}


def _fixture_inputs(spec: Any, config: ProjectConfig) -> dict[str, Any]:
    """Input giả dựng từ chính `template_variables` của manifest."""
    inputs = {name: _placeholder(name) for name in spec.template_variables}
    for selection in spec.reference_selection:
        attribute = REFERENCE_ATTR[selection["registry"]]
        inputs[selection["input_field"]] = getattr(config, attribute)
    return inputs


def _entry(
    prompt_id: str = "test.v1",
    *,
    template: str = "t.md",
    variables: tuple[str, ...] = ("a",),
    output_kind: str = "json",
    reference_selection: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "prompt_id": prompt_id,
        "template": template,
        "template_variables": list(variables),
        "output_kind": output_kind,
        "schema_ref": "TestPayload",
        "schema_document": "docs/design/schemas.md",
        "schema_section": "1",
        "reference_selection": reference_selection or [],
    }


def _manifest(prompts: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "manifest_version": 1,
        "path_base": "repository_root",
        "input_mode": "json_message",
        "unknown_prompt_policy": "error",
        "reference_policy": "explicit_only",
        "prompts": prompts if prompts is not None else [_entry()],
        "genres": {},
        "styles": {},
    }
    data.update(overrides)
    return data


def _load_tmp(tmp_path: Path, data: dict[str, Any], *, template_text: str = "# template v1\n") -> PromptRegistry:
    root = tmp_path / "prompts"
    root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "t.md").write_text(template_text, encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return PromptRegistry.load(prompt_root=root, repo_root=tmp_path)


def _assert_no_agent_protocol(text: str, label: str) -> None:
    lowered = text.lower()
    for marker in _LEGACY_MARKERS:
        assert marker not in lowered, f"{label}: còn dấu hiệu giao thức cũ `{marker}`"
    for match in _FILE_TOOL_PHRASES.finditer(text):
        window = text[max(0, match.start() - 60) : match.start()].lower()
        assert any(negation in window for negation in _NEGATIONS), (
            f"{label}: yêu cầu đọc file/gọi tool không nằm trong câu phủ định: "
            f"{text[max(0, match.start() - 60) : match.end() + 20]!r}"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_loads_v1_manifest_and_exposes_registries(
    registry: PromptRegistry, manifest: dict[str, Any]
) -> None:
    assert registry.manifest_version == 1
    assert registry.prompt_ids() == sorted(entry["prompt_id"] for entry in manifest["prompts"])
    assert len(registry) == len(manifest["prompts"]) == 14
    assert "co_create.v1" in registry
    # `genres()`/`styles()` là method, không bị field dataclass che.
    assert registry.genres() == manifest["genres"]
    assert registry.styles() == manifest["styles"]
    assert registry.reference_registry("genres") == manifest["genres"]
    assert registry.reference_registry("styles") == manifest["styles"]
    assert registry.prompt_root == PROMPT_ROOT
    assert registry.manifest_path == MANIFEST_PATH


def test_registry_default_load_uses_config_prompt_root() -> None:
    loaded = PromptRegistry.load(repo_root=REPO_ROOT)
    assert loaded.prompt_root == PROMPT_ROOT
    assert loaded.prompt_ids() == PromptRegistry.load(PROMPT_ROOT, repo_root=REPO_ROOT).prompt_ids()


def test_every_registered_prompt_spec_is_complete(registry: PromptRegistry) -> None:
    for spec in registry.prompt_specs():
        assert spec.template_path.is_file()
        assert spec.template_path.is_relative_to(REPO_ROOT)
        assert spec.template_text.strip()
        assert len(spec.template_hash) == 64
        assert spec.output_kind in {"json", "markdown"}
        assert spec.template_variables
        assert len(set(spec.template_variables)) == len(spec.template_variables)


# ---------------------------------------------------------------------------
# Render toàn manifest
# ---------------------------------------------------------------------------


def test_every_manifest_prompt_renders_with_manifest_derived_inputs(
    registry: PromptRegistry, project_config: ProjectConfig
) -> None:
    for spec in registry.prompt_specs():
        inputs = _fixture_inputs(spec, project_config)
        frozen = dict(inputs)

        rendered = render_prompt(
            registry,
            spec.prompt_id,
            inputs=inputs,
            repo_root=REPO_ROOT,
            config=project_config,
        )

        assert rendered.prompt_id == spec.prompt_id
        assert [message.role for message in rendered.messages] == ["system", "user"]
        assert rendered.system_text == spec.template_text
        assert rendered.template_hash == spec.template_hash
        assert rendered.prompt_version == f"{spec.prompt_id}+{spec.template_hash[:8]}"
        assert len(spec.template_hash[:8]) == 8

        payload = json.loads(rendered.user_text)
        assert sorted(payload) == sorted(spec.template_variables)
        assert rendered.input_payload == payload

        # `inputs` của caller không bị mutate, kể cả field reference.
        assert inputs == frozen

        for selection in spec.reference_selection:
            field = selection["input_field"]
            ref_id = inputs[field]
            assert rendered.references[field] == ref_id
            assert payload[field] == load_reference(registry, selection["registry"], ref_id)
            assert payload[field] != ref_id


def test_prompt_version_and_hash_are_stable_between_renders(
    registry: PromptRegistry, project_config: ProjectConfig
) -> None:
    spec = registry.get("writer.v1")
    inputs = _fixture_inputs(spec, project_config)
    first = render_prompt(registry, spec.prompt_id, inputs=inputs, repo_root=REPO_ROOT)
    second = render_prompt(registry, spec.prompt_id, inputs=inputs, repo_root=REPO_ROOT)
    assert first.template_hash == second.template_hash
    assert first.prompt_version == second.prompt_version
    assert first.user_text == second.user_text
    # Hash phải phụ thuộc nội dung template, không phải thời điểm render.
    other = registry.get("review.v1")
    assert other.template_hash != spec.template_hash


def test_reference_content_is_injected_not_the_registry_id(
    registry: PromptRegistry, project_config: ProjectConfig
) -> None:
    spec = registry.get("co_create.v1")
    inputs = _fixture_inputs(spec, project_config)
    rendered = render_prompt(
        registry, "co_create.v1", inputs=inputs, repo_root=REPO_ROOT, config=project_config
    )
    suspense_text = (REPO_ROOT / registry.genres()["suspense"]).read_text(encoding="utf-8")
    assert rendered.input_payload["genre_prompt"] == suspense_text
    assert rendered.input_payload["genre_prompt"] != "suspense"


def test_payload_json_is_utf8_and_sorted(registry: PromptRegistry, project_config: ProjectConfig) -> None:
    spec = registry.get("co_create.v1")
    inputs = _fixture_inputs(spec, project_config)
    rendered = render_prompt(registry, "co_create.v1", inputs=inputs, repo_root=REPO_ROOT)
    payload = json.loads(rendered.user_text)
    assert payload["user_message"] == inputs["user_message"]
    # `ensure_ascii=False`: tiếng Việt phải còn nguyên trong user message.
    assert "Tin nhắn thử của người dùng." in rendered.user_text


def test_reference_can_come_from_config_when_field_is_absent(
    registry: PromptRegistry, project_config: ProjectConfig
) -> None:
    spec = registry.get("writer.v1")
    reference_fields = {item["input_field"] for item in spec.reference_selection}
    inputs = {
        name: _placeholder(name)
        for name in spec.template_variables
        if name not in reference_fields
    }
    rendered = render_prompt(
        registry, "writer.v1", inputs=inputs, repo_root=REPO_ROOT, config=project_config
    )
    assert rendered.references["style"] == "default"
    assert rendered.input_payload["style"] != "default"


def test_inline_reference_content_from_caller_is_kept(
    registry: PromptRegistry,
) -> None:
    """Caller cấp sẵn nội dung (không phải ID) thì loader không đọc registry."""
    spec = registry.get("co_create.v1")
    inputs = {name: _placeholder(name) for name in spec.template_variables}
    inline = "Genre Prompt: nội dung do caller cấp sẵn, không phải ID."
    inputs["genre_prompt"] = inline

    rendered = render_prompt(registry, "co_create.v1", inputs=inputs, repo_root=REPO_ROOT)

    assert rendered.input_payload["genre_prompt"] == inline
    assert rendered.references == {}


def test_render_accepts_real_context_bundle_payload(tmp_path: Path) -> None:
    """Tích hợp với T12: context bundle đặt ID registry, loader thay bằng nội dung.

    Đây là hợp đồng tích hợp chính: `build_*_context(...).payload` được truyền
    thẳng vào `render_prompt(inputs=bundle.payload)`.
    """
    from novel_ai.core.context import build_co_create_context
    from novel_ai.core.project import Project

    project = Project.create(
        tmp_path / "projects",
        "Truyện tích hợp",
        genre_prompt_id="suspense",
        writing_style_id="default",
    )
    bundle = build_co_create_context(project, user_message="Một nữ pháp y điều tra án mạng.")
    loaded = PromptRegistry.load(repo_root=REPO_ROOT)
    caller_payload = dict(bundle.payload)

    rendered = render_prompt(
        loaded,
        "co_create.v1",
        inputs=bundle.payload,
        repo_root=REPO_ROOT,
        config=project.config,
    )

    payload = json.loads(rendered.user_text)
    assert sorted(payload) == sorted(loaded.get("co_create.v1").template_variables)
    assert rendered.references["genre_prompt"] == "suspense"
    assert payload["genre_prompt"] == load_reference(loaded, "genres", "suspense")
    assert payload["genre_prompt"] != "suspense"
    assert payload["user_message"] == "Một nữ pháp y điều tra án mạng."
    # Mapping của context bundle không bị mutate.
    assert bundle.payload == caller_payload
    assert bundle.payload["genre_prompt"] == "suspense"


def test_failed_render_never_reaches_llm_client(
    registry: PromptRegistry, project_config: ProjectConfig
) -> None:
    """Render fail thì chưa có request nào được gửi."""
    from novel_ai.core.llm import FakeLLMClient

    client = FakeLLMClient(responses=["không bao giờ được gọi"])
    spec = registry.get("co_create.v1")
    inputs = _fixture_inputs(spec, project_config)
    del inputs["user_message"]

    with pytest.raises(PromptError) as excinfo:
        render_prompt(registry, "co_create.v1", inputs=inputs, repo_root=REPO_ROOT)

    assert excinfo.value.code == "missing_variable"
    assert client.calls == []


# ---------------------------------------------------------------------------
# Lỗi render
# ---------------------------------------------------------------------------


def test_missing_variable_names_prompt_and_variable(
    registry: PromptRegistry, project_config: ProjectConfig
) -> None:
    spec = registry.get("co_create.v1")
    inputs = _fixture_inputs(spec, project_config)
    del inputs["language"]

    with pytest.raises(PromptError) as excinfo:
        render_prompt(registry, "co_create.v1", inputs=inputs, repo_root=REPO_ROOT)

    error = excinfo.value
    assert isinstance(error, ValueError)
    assert error.code == "missing_variable"
    assert error.prompt_id == "co_create.v1"
    assert error.variable == "language"
    assert "co_create.v1" in str(error)
    assert "language" in str(error)


def test_missing_reference_field_without_config_is_missing_variable(
    registry: PromptRegistry,
) -> None:
    spec = registry.get("co_create.v1")
    inputs = {name: _placeholder(name) for name in spec.template_variables}
    del inputs["genre_prompt"]

    with pytest.raises(PromptError) as excinfo:
        render_prompt(registry, "co_create.v1", inputs=inputs, repo_root=REPO_ROOT)

    assert excinfo.value.code == "missing_variable"
    assert excinfo.value.variable == "genre_prompt"


def test_unknown_variable_is_rejected(registry: PromptRegistry, project_config: ProjectConfig) -> None:
    spec = registry.get("co_create.v1")
    inputs = _fixture_inputs(spec, project_config)
    inputs["bien_khong_khai_bao"] = "x"

    with pytest.raises(PromptError) as excinfo:
        render_prompt(registry, "co_create.v1", inputs=inputs, repo_root=REPO_ROOT)

    assert excinfo.value.code == "unknown_variable"
    assert excinfo.value.variable == "bien_khong_khai_bao"
    assert "bien_khong_khai_bao" in str(excinfo.value)


@pytest.mark.parametrize(
    ("prompt_id", "field", "bad_id", "kind"),
    [
        ("co_create.v1", "genre_prompt", "khong_ton_tai", "genres"),
        ("writer.v1", "style", "khong_ton_tai", "styles"),
    ],
)
def test_unknown_reference_id_from_input_is_rejected(
    registry: PromptRegistry,
    project_config: ProjectConfig,
    prompt_id: str,
    field: str,
    bad_id: str,
    kind: str,
) -> None:
    spec = registry.get(prompt_id)
    inputs = _fixture_inputs(spec, project_config)
    inputs[field] = bad_id

    with pytest.raises(PromptError) as excinfo:
        render_prompt(registry, prompt_id, inputs=inputs, repo_root=REPO_ROOT)

    error = excinfo.value
    assert error.code == "unknown_reference"
    assert error.variable == field
    assert bad_id in str(error)
    assert kind in str(error)


def test_unknown_reference_id_from_config_is_rejected(project_config: ProjectConfig) -> None:
    registry = PromptRegistry.load(repo_root=REPO_ROOT)
    broken = project_config.model_copy(update={"genre_prompt_id": "khong_ton_tai"})
    spec = registry.get("co_create.v1")
    inputs = {
        name: _placeholder(name)
        for name in spec.template_variables
        if name != "genre_prompt"
    }

    with pytest.raises(PromptError) as excinfo:
        render_prompt(registry, "co_create.v1", inputs=inputs, repo_root=REPO_ROOT, config=broken)

    assert excinfo.value.code == "unknown_reference"
    assert "khong_ton_tai" in str(excinfo.value)


def test_load_reference_rejects_unknown_kind_and_id(registry: PromptRegistry) -> None:
    with pytest.raises(PromptError) as bad_kind:
        load_reference(registry, "khong-co-registry", "custom")
    assert bad_kind.value.code == "unknown_reference"

    with pytest.raises(PromptError) as bad_id:
        load_reference(registry, "genres", "khong_ton_tai")
    assert bad_id.value.code == "unknown_reference"
    assert "khong_ton_tai" in str(bad_id.value)

    assert load_reference(registry, "genres", "custom").strip()
    assert load_reference(registry, "styles", "default").strip()


def test_unknown_prompt_id_is_rejected(registry: PromptRegistry) -> None:
    with pytest.raises(PromptError) as excinfo:
        registry.get("khong_ton_tai.v1")
    assert excinfo.value.code == "unknown_prompt"
    assert "khong_ton_tai.v1" in str(excinfo.value)

    with pytest.raises(PromptError) as render_error:
        render_prompt(
            registry,
            "khong_ton_tai.v1",
            inputs={},
            repo_root=REPO_ROOT,
        )
    assert render_error.value.code == "unknown_prompt"


def test_render_from_other_repo_root_is_rejected(
    registry: PromptRegistry, tmp_path: Path, project_config: ProjectConfig
) -> None:
    spec = registry.get("co_create.v1")
    inputs = _fixture_inputs(spec, project_config)
    with pytest.raises(PromptError) as excinfo:
        render_prompt(registry, "co_create.v1", inputs=inputs, repo_root=tmp_path)
    assert excinfo.value.code == "invalid_manifest"


def test_non_mapping_inputs_rejected(registry: PromptRegistry) -> None:
    with pytest.raises(PromptError) as excinfo:
        render_prompt(registry, "co_create.v1", inputs=["a"], repo_root=REPO_ROOT)  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_inputs"


# ---------------------------------------------------------------------------
# Manifest sai (tmp_path, manifest tổng hợp)
# ---------------------------------------------------------------------------


def test_missing_template_raises_missing_template(tmp_path: Path) -> None:
    data = _manifest([_entry(template="khong_ton_tai.md")])
    root = tmp_path / "prompts"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PromptError) as excinfo:
        PromptRegistry.load(prompt_root=root, repo_root=tmp_path)

    assert excinfo.value.code == "missing_template"
    assert "test.v1" in str(excinfo.value)
    assert "khong_ton_tai.md" in str(excinfo.value)


def test_missing_manifest_raises_invalid_manifest(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    root.mkdir()
    with pytest.raises(PromptError) as excinfo:
        PromptRegistry.load(prompt_root=root, repo_root=tmp_path)
    assert excinfo.value.code == "invalid_manifest"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("manifest_version", 2),
        ("manifest_version", "1"),
        ("path_base", "docs"),
        ("input_mode", "text_message"),
        ("unknown_prompt_policy", "warn"),
        ("reference_policy", "glob_all"),
    ],
)
def test_manifest_header_must_match_contract(tmp_path: Path, key: str, value: Any) -> None:
    data = _manifest(**{key: value})
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(tmp_path, data)
    assert excinfo.value.code == "invalid_manifest"
    assert key in str(excinfo.value)


def test_duplicate_prompt_id_is_rejected(tmp_path: Path) -> None:
    data = _manifest([_entry("dup.v1"), _entry("dup.v1")])
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(tmp_path, data)
    assert excinfo.value.code == "invalid_manifest"
    assert "dup.v1" in str(excinfo.value)


@pytest.mark.parametrize("template", ["../outside.md", "..\\outside.md"])
def test_template_path_escape_is_rejected(tmp_path: Path, template: str) -> None:
    (tmp_path / "outside.md").write_text("# ngoài repo\n", encoding="utf-8")
    data = _manifest([_entry(template=template)])
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(tmp_path, data)
    assert excinfo.value.code == "invalid_manifest"
    assert "template" in str(excinfo.value)


def test_absolute_template_path_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# ngoài repo\n", encoding="utf-8")
    data = _manifest([_entry(template=str(outside))])
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(tmp_path, data)
    assert excinfo.value.code == "invalid_manifest"


def test_reference_path_escape_is_rejected(tmp_path: Path) -> None:
    data = _manifest(genres={"custom": "../outside.md"})
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(tmp_path, data)
    assert excinfo.value.code == "invalid_manifest"


def test_unknown_output_kind_is_rejected(tmp_path: Path) -> None:
    data = _manifest([_entry(output_kind="xml")])
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(tmp_path, data)
    assert excinfo.value.code == "invalid_manifest"
    assert "output_kind" in str(excinfo.value)


def test_reference_selection_for_undeclared_field_is_rejected(tmp_path: Path) -> None:
    data = _manifest(
        [_entry(variables=("a",), reference_selection=[
            {"input_field": "genre_prompt", "registry": "genres", "selection": "exactly_one"}
        ])],
        genres={},
    )
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(tmp_path, data)
    assert excinfo.value.code == "invalid_manifest"
    assert "genre_prompt" in str(excinfo.value)


@pytest.mark.parametrize("registry_name", ["plugins", "docs"])
def test_unknown_reference_registry_in_manifest_is_rejected(
    tmp_path: Path, registry_name: str
) -> None:
    data = _manifest(
        [_entry(variables=("a",), reference_selection=[
            {"input_field": "a", "registry": registry_name, "selection": "exactly_one"}
        ])]
    )
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(tmp_path, data)
    assert excinfo.value.code == "invalid_manifest"
    assert registry_name in str(excinfo.value)


def test_reference_selection_must_be_exactly_one(tmp_path: Path) -> None:
    data = _manifest(
        [_entry(variables=("a",), reference_selection=[
            {"input_field": "a", "registry": "genres", "selection": "all"}
        ])]
    )
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(tmp_path, data)
    assert excinfo.value.code == "invalid_manifest"


def test_missing_reference_file_raises_missing_reference(tmp_path: Path) -> None:
    data = _manifest(
        [_entry(variables=("genre_prompt",), reference_selection=[
            {"input_field": "genre_prompt", "registry": "genres", "selection": "exactly_one"}
        ])],
        genres={"custom": "genres/khong_ton_tai.md"},
    )
    registry = _load_tmp(tmp_path, data)

    with pytest.raises(PromptError) as excinfo:
        render_prompt(
            registry,
            "test.v1",
            inputs={"genre_prompt": "custom"},
            repo_root=tmp_path,
        )
    assert excinfo.value.code == "missing_reference"
    assert "custom" in str(excinfo.value)


def test_legacy_protocol_template_is_refused(tmp_path: Path) -> None:
    data = _manifest([_entry()])
    with pytest.raises(PromptError) as excinfo:
        _load_tmp(
            tmp_path,
            data,
            template_text="# cũ\nBạn hãy gọi novel_context để lấy state.\n",
        )
    assert excinfo.value.code == "legacy_protocol"
    assert "novel_context" in str(excinfo.value)


def test_forbidden_protocol_marker_helper() -> None:
    assert FORBIDDEN_PROTOCOL_MARKERS == frozenset(_LEGACY_MARKERS)
    assert find_forbidden_protocol_markers("Bạn hãy dispatch và audit_foundation") == [
        "audit_foundation",
        "dispatch",
    ]
    assert find_forbidden_protocol_markers("Prompt v1 bình thường") == []


def test_documented_error_codes_cover_contract() -> None:
    """Các code bắt buộc theo contract T10 phải nằm trong tập code công bố."""
    assert {
        "missing_variable",
        "unknown_variable",
        "missing_template",
        "unknown_prompt",
        "unknown_reference",
        "invalid_manifest",
    } <= PROMPT_ERROR_CODES


# ---------------------------------------------------------------------------
# Không đọc prompt legacy
# ---------------------------------------------------------------------------


def test_loader_reads_only_manifest_registered_files(
    monkeypatch: pytest.MonkeyPatch, project_config: ProjectConfig
) -> None:
    opened: list[Path] = []
    original = Path.read_text

    def recording_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(self)
        assert self != LEGACY_PROMPT, f"loader đọc prompt legacy {self}"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read_text)

    loaded = PromptRegistry.load(repo_root=REPO_ROOT)
    for spec in loaded.prompt_specs():
        render_prompt(
            loaded,
            spec.prompt_id,
            inputs=_fixture_inputs(spec, project_config),
            repo_root=REPO_ROOT,
            config=project_config,
        )

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    allowed = {
        MANIFEST_PATH.resolve(),
        *((REPO_ROOT / entry["template"]).resolve() for entry in manifest["prompts"]),
        *((REPO_ROOT / path).resolve() for path in manifest["genres"].values()),
        *((REPO_ROOT / path).resolve() for path in manifest["styles"].values()),
    }
    assert opened, "loader phải đọc manifest"
    assert LEGACY_PROMPT.exists()
    assert set(opened) <= allowed, f"loader đọc file ngoài manifest: {set(opened) - allowed}"
    # Không glob cả thư mục docs: các prompt legacy không được mở.
    assert not any("prompts" in path.parts and "v1" not in path.parts for path in opened)


def test_no_legacy_fallback_when_template_is_missing(tmp_path: Path, project_config: ProjectConfig) -> None:
    """Thiếu template đã đăng ký thì fail rõ, không rơi về prompt cũ cùng tên."""
    data = _manifest([_entry("writer.v1", template="khong_ton_tai/writer.md", variables=("a",))])
    root = tmp_path / "prompts"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PromptError) as excinfo:
        PromptRegistry.load(prompt_root=root, repo_root=REPO_ROOT)

    assert excinfo.value.code == "missing_template"
    assert excinfo.value.prompt_id == "writer.v1"


# ---------------------------------------------------------------------------
# Nội dung render không mang giao thức agent cũ
# ---------------------------------------------------------------------------


def test_rendered_prompts_contain_no_agent_protocol(
    registry: PromptRegistry, project_config: ProjectConfig
) -> None:
    for spec in registry.prompt_specs():
        rendered = render_prompt(
            registry,
            spec.prompt_id,
            inputs=_fixture_inputs(spec, project_config),
            repo_root=REPO_ROOT,
            config=project_config,
        )
        for message in rendered.messages:
            _assert_no_agent_protocol(message.content, f"{spec.prompt_id}/{message.role}")


def test_genre_and_style_references_contain_no_agent_protocol(registry: PromptRegistry) -> None:
    for kind in ("genres", "styles"):
        for ref_id in registry.reference_ids(kind):
            text = load_reference(registry, kind, ref_id)
            lowered = text.lower()
            for marker in _LEGACY_MARKERS:
                assert marker not in lowered, f"{kind}.{ref_id} chứa `{marker}`"
