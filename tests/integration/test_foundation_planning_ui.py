"""AppTest và test helper cho UI Co-create / Architect / Planning (T20).

Ba nhóm kiểm tra, tất cả offline (không server, không mạng):

1. **Helper thuần** của `novel_ai/pages/_common.py` và các page: dựng input từ
   session state, parse ID, format ID dễ đọc, view Rolling. Phần này không cần
   Streamlit nên chạy nhanh và không giòn.
2. **AppTest trên page thật**: mỗi test render đúng module page qua
   `novel_ai.ui.layout.render_workspace` với một harness nhỏ trong `tmp_path`.
   Harness truyền `FakeLLMClient` được script sẵn, nên không có API call thật.
3. **Luật state**: rerun không ghi file, Auto Accept on/off, JSON sai không làm
   mất accepted, append `effective_from_chapter` tương lai không làm quá khứ stale.

Fixture và seed dùng `tmp_path`; `tests/conftest.py` đã xoá mọi biến `NOVEL_AI_*`
trước mỗi test nên máy chạy test không ảnh hưởng kết quả.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from novel_ai.core import storage
from novel_ai.core.models import (
    ArtifactRevision,
    ArtifactStatus,
    ChapterStatus,
    CurrentTimelineDocument,
    DependencyPin,
    PayloadSource,
    RollingDeviation,
    RollingPatchPayload,
    ReviewedChapterRange,
    RollingStatus,
    ShortPlanChange,
    SourceType,
    ValidationResult,
    ValidationState,
)
from novel_ai.core.project import Project
from novel_ai.pages import _common, architect as architect_page, co_create as co_create_page
from novel_ai.pages import long_plan as long_plan_page
from novel_ai.pages import short_plan as short_plan_page
from novel_ai.services import GuardError, architect, short_planner
from novel_ai.ui import layout

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-09-19T10:00:00+07:00"


# ---------------------------------------------------------------------------
# Fixture và seed
# ---------------------------------------------------------------------------


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _valid() -> ValidationResult:
    return ValidationResult(state=ValidationState.valid, errors=[])


def _seed_artifact(
    project: Project,
    artifact_id: str,
    artifact_type: str,
    payload: Any,
    *,
    revision: int = 1,
) -> None:
    """Seed artifact accepted trực tiếp (không gọi LLM) cho test append/rolling."""
    from novel_ai.core import lifecycle

    envelope = lifecycle.new_artifact(artifact_type, artifact_id, now=NOW).model_copy(
        update={
            "status": ArtifactStatus.accepted,
            "accepted_revision": ArtifactRevision(
                revision=revision,
                payload=payload,
                payload_source=PayloadSource(source_type=SourceType.user),
                dependency_pins=[],
                validation=_valid(),
                created_at=NOW,
                accepted_at=NOW,
                accepted_by="user",
            ),
        }
    )
    storage.save_artifact(project, envelope, operation_id=f"op_seed_{artifact_id}")


def _seed_candidate(
    project: Project, artifact_id: str, artifact_type: str, payload: Any
) -> None:
    """Seed accepted r1 + candidate r2 để test editor candidate mà không gọi LLM."""
    from novel_ai.core import lifecycle

    accepted = lifecycle.new_artifact(artifact_type, artifact_id, now=NOW).model_copy(
        update={
            "status": ArtifactStatus.accepted,
            "accepted_revision": ArtifactRevision(
                revision=1,
                payload=payload,
                payload_source=PayloadSource(source_type=SourceType.user),
                dependency_pins=[],
                validation=_valid(),
                created_at=NOW,
                accepted_at=NOW,
                accepted_by="user",
            ),
        }
    )
    staged = lifecycle.set_candidate(
        accepted,
        payload,
        source=PayloadSource(source_type=SourceType.llm, operation_id="op_seed_candidate"),
        validation=_valid(),
        now=NOW,
    )
    storage.save_artifact(project, staged, operation_id=f"op_seed_candidate_{artifact_id}")


def _character(character_id: str, name: str, *, effective: int = 1) -> dict[str, Any]:
    return {
        "character_id": character_id,
        "display_name": name,
        "aliases": [],
        "role": "protagonist",
        "tier": "core",
        "effective_from_chapter": effective,
        "status": "accepted",
        "public_profile": {
            "description": "Bác sĩ cấp cứu tỉnh dậy ở thân xác lạ.",
            "traits": ["điềm tĩnh"],
            "voice": "mỉa mai ngắn",
            "known_history": "",
        },
        "writer_profile": {},
        "author_only": {},
        "future_direction": {},
    }


def _seed_base_idea(project: Project, *, markdown: str = "Base Idea test.\n") -> None:
    from novel_ai.services import co_create

    co_create.finalize_base_idea(project, markdown=markdown, now=NOW)


def _seed_foundation(project: Project) -> None:
    """Accepted base idea + premise + characters/world_rules/foreshadow tối thiểu.

    Kèm default viết của project (D016) để request Short Plan hợp lệ; test nào cần
    đường "thiếu default" thì xoá lại hai field này sau khi seed.
    """
    project.update_config(
        default_pov="ngôi ba giới hạn theo Sở Dương",
        default_length_guidance="1500–2000 từ",
    )
    _seed_base_idea(project)
    _seed_artifact(project, "premise", "premise", _premise_payload())
    _seed_reference_foundation(project)


def _seed_reference_foundation(project: Project) -> None:
    """Accepted characters/world_rules/foreshadow tối thiểu để FK của plan resolve."""
    _seed_artifact(
        project, "characters", "characters", {"characters": [_character("char_0001", "Sở Dương")]}
    )
    _seed_artifact(
        project,
        "world_rules",
        "world_rules",
        {
            "world_rules": [
                {
                    "world_rule_id": "rule_0001",
                    "category": "hệ thống",
                    "summary": "Cưỡng chế thân thể.",
                    "content": "Thân thể bị kéo đi theo lệnh thu thập.",
                    "boundary": "",
                    "effective_from_chapter": 1,
                    "visibility": "writer_safe",
                    "writer_projection": "Thân thể bị kéo đi.",
                    "author_only": {},
                }
            ]
        },
    )
    _seed_artifact(
        project,
        "foreshadow",
        "foreshadow",
        {
            "foreshadows": [
                {
                    "foreshadow_id": "fs_0001",
                    "label": "Nguồn gốc cái nồi",
                    "truth_author_only": "Bí mật chưa reveal.",
                    "planned_planting": [],
                    "planned_payoff": None,
                    "effective_from_chapter": 1,
                    "writer_visibility": "skeleton_only",
                    "status": "active",
                }
            ]
        },
    )


def _seed_long_plan(project: Project, *, chapters: int = 2) -> None:
    _seed_artifact(
        project,
        "long_plan",
        "long_plan",
        {
            "volumes": [
                {
                    "volume_id": "vol_0001",
                    "title": "Quyển 1",
                    "theme": "Giữ nguyên tắc",
                    "goal": "Sở Dương sống sót qua arc đầu.",
                    "arcs": [
                        {
                            "arc_id": "arc_0001",
                            "title": "Arc mở đầu",
                            "chapter_range": {"start": 1, "end": chapters},
                            "goal": "Định vị Sở Dương trong thân xác mới.",
                            "core_conflict": "Cưỡng chế thân thể và nguyên tắc cứu người.",
                            "start_state": "Tỉnh dậy, mất phương hướng.",
                            "end_state": "Chấp nhận luật chơi và giữ nguyên tắc.",
                            "major_reveals": [],
                            "character_ids": ["char_0001"],
                            "world_rule_ids": ["rule_0001"],
                            "foreshadow_ids": ["fs_0001"],
                            "relationship_directions": [],
                        }
                    ],
                }
            ],
            "global_threads": [],
        },
    )


def _short_plan_payload(chapters: list[dict[str, Any]]) -> dict[str, Any]:
    return {"arc_id": "arc_0001", "chapters": chapters}


def _chapter_plan(chapter_id: str, number: int, title: str) -> dict[str, Any]:
    return {
        "chapter_id": chapter_id,
        "chapter_number": number,
        "title": title,
        "summary": f"Tóm tắt chương {number}.",
        "hook": f"Hook chương {number}.",
        "outline": [f"Beat chương {number}"],
        "character_ids": ["char_0001"],
        "world_rule_ids": ["rule_0001"],
        "foreshadow_ids": ["fs_0001"],
        "threads": [],
        "relationship_changes": [],
        "chapter_goal": f"Mục tiêu chương {number}.",
        "planned_ending": f"Kết chương {number}.",
    }


def _seed_accepted_short_plan(project: Project) -> list[dict[str, Any]]:
    """Accepted Short Plan hai chương qua service (chapter metadata cũng được ghi)."""
    envelope = _common.envelope_or_none(project, "long_plan")
    assert envelope is not None and envelope.accepted_revision is not None
    arc = envelope.accepted_revision.payload.volumes[0].arcs[0]
    assigned = short_planner.resolve_assigned_chapters(project, arc)
    payload = _short_plan_payload(
        [
            _chapter_plan(str(item["chapter_id"]), int(item["chapter_number"]), f"Chương {item['chapter_number']}")
            for item in assigned
        ]
    )
    from novel_ai.core.llm import FakeLLMClient

    short_planner.generate(
        project,
        client=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
        arc_id="arc_0001",
        assigned_chapters=assigned,
        now=NOW,
    )
    short_planner.accept(project, now=NOW)
    return assigned


def _mark_chapter_final(project: Project, chapter_id: str) -> None:
    """Đưa một chapter lên `final_reconciled` + timeline, chỉ để test eligibility."""
    chapter = storage.load_chapter(project, chapter_id)
    assert chapter is not None
    storage.save_chapter(
        project,
        chapter.model_copy(update={"status": ChapterStatus.final_reconciled}),
        operation_id=f"op_final_{chapter_id}",
    )
    storage.save_timeline(
        project,
        CurrentTimelineDocument(
            latest_final_chapter=chapter.chapter_number,
            latest_consistent_chapter=chapter.chapter_number,
        ),
        operation_id=f"op_timeline_{chapter_id}",
    )


# ---------------------------------------------------------------------------
# Harness AppTest
# ---------------------------------------------------------------------------

_HARNESS = '''\
"""Harness tạm cho AppTest: render đúng page của T20 với client script sẵn.

`FakeLLMClient` được giữ trong `st.session_state` để mọi rerun của cùng một
AppTest dùng **đúng một** client; nhờ vậy test đếm được số lần gọi LLM. Harness
chỉ dùng session state làm chỗ giữ client fake của test, không phải state bền.
"""

import json
import os

import streamlit as st

from novel_ai.config import load_config
from novel_ai.core.llm import FakeLLMClient
from novel_ai.core.project import Project
from novel_ai.ui import layout

config = load_config(
    env={{"NOVEL_AI_PROJECTS_ROOT": os.environ["NOVEL_AI_TEST_PROJECTS_ROOT"]}},
    repo_root={repo_root!r},
)
_project = Project.open(config.projects_root, os.environ["NOVEL_AI_TEST_SLUG"])
CLIENT = st.session_state.get("__t20_client")
if CLIENT is None:
    CLIENT = FakeLLMClient(json.loads(os.environ.get("NOVEL_AI_TEST_RESPONSES") or "[]"))
    _stream = json.loads(os.environ.get("NOVEL_AI_TEST_STREAM") or "[]")
    if _stream:
        from novel_ai.core.llm import STREAM_INTERRUPTED

        CLIENT.queue_stream(
            *[STREAM_INTERRUPTED if item == "interrupted" else item for item in _stream]
        )
    st.session_state["__t20_client"] = CLIENT
ctx = layout.AppContext(
    app_config=config,
    project=_project,
    registry=layout.load_prompt_registry(config)[0],
    workspace=os.environ["NOVEL_AI_TEST_WORKSPACE"],
    llm_client=CLIENT,
    llm_error=None,
)
layout.render_workspace(ctx)
'''


@pytest.fixture
def apptest_factory(tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch):
    """Factory dựng AppTest cho một workspace, truyền response cho `FakeLLMClient`."""
    harness = tmp_path / "t20_harness.py"
    harness.write_text(_HARNESS.format(repo_root=str(REPO_ROOT)), encoding="utf-8")
    monkeypatch.setenv("NOVEL_AI_TEST_PROJECTS_ROOT", str(projects_root))

    def build(
        project: Project,
        workspace: str,
        responses: list[Any] | None = None,
        *,
        stream: list[Any] | None = None,
    ) -> AppTest:
        monkeypatch.setenv("NOVEL_AI_TEST_SLUG", project.slug)
        monkeypatch.setenv("NOVEL_AI_TEST_WORKSPACE", workspace)
        monkeypatch.setenv(
            "NOVEL_AI_TEST_RESPONSES", json.dumps(responses or [], ensure_ascii=False)
        )
        monkeypatch.setenv(
            "NOVEL_AI_TEST_STREAM", json.dumps(stream or [], ensure_ascii=False)
        )
        at = AppTest.from_file(str(harness))
        at.session_state["novel_ai_open_project"] = project.slug
        at.session_state["novel_ai_workspace_nav"] = layout.label_for_workspace(workspace)
        at.run(timeout=120)
        return at

    return build


def _client(at: AppTest) -> Any:
    """`FakeLLMClient` của lần run gần nhất (harness gán vào `__main__.CLIENT`)."""
    import sys

    module = sys.modules["__main__"]
    return getattr(module, "CLIENT")


def _click(at: AppTest, key: str) -> None:
    at.button(key=key).click()
    at.run(timeout=120)


def _submit(at: AppTest, key: str) -> None:
    at.button(key=key).click()
    at.run(timeout=120)


def _rendered(at: AppTest) -> str:
    parts: list[str] = []
    for elements in (at.markdown, at.caption, at.info, at.warning, at.error, at.success):
        parts.extend(str(element.value) for element in elements)
    return "\n".join(parts)


def _fingerprint_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): storage.file_fingerprint(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _artifact(project: Project, artifact_id: str) -> Any:
    return storage.load_artifact(project, artifact_id)


# ---------------------------------------------------------------------------
# 1. Helper thuần
# ---------------------------------------------------------------------------


def test_working_state_inputs_split_lines_and_trim() -> None:
    payload = co_create_page.build_working_state_inputs(
        {
            "genre": "  tiên hiệp  ",
            "core_concept": "Bác sĩ tỉnh dậy.",
            "tone": "",
            "protagonist": "Sở Dương",
            "setting": "",
            "conflict": "",
            "constraints": "Không reveal cái nồi.\n\n  Giữ POV ngôi ba.  \n",
            "open_questions": "Vì sao?\n",
        }
    )

    assert payload["genre"] == "tiên hiệp"
    assert payload["constraints"] == ["Không reveal cái nồi.", "Giữ POV ngôi ba."]
    assert payload["open_questions"] == ["Vì sao?"]
    assert payload["tone"] == ""


def test_parse_assigned_ids_accepts_lists_and_rejects_wrong_prefix() -> None:
    assert architect_page.parse_assigned_ids("char_0001, char_0002", "characters") == [
        "char_0001",
        "char_0002",
    ]
    assert architect_page.parse_assigned_ids("", "characters") == []
    with pytest.raises(GuardError):
        architect_page.parse_assigned_ids("rule_0001", "characters")


def test_build_architect_action_passes_none_for_empty_pool() -> None:
    params = architect_page.build_architect_action(
        artifact_type="characters",
        action="regenerate",
        chapter_number=4,
        assigned_ids=[],
        user_instruction="  thêm phản diện  ",
    )

    assert params["assigned_ids"] is None
    assert params["chapter_number"] == 4
    assert params["user_instruction"] == "thêm phản diện"


def test_build_planning_scope_validates_range() -> None:
    assert long_plan_page.build_planning_scope(1, 5) == {"start": 1, "end": 5}
    with pytest.raises(GuardError):
        long_plan_page.build_planning_scope(5, 2)


def test_build_chapter_constraint_values_defaults_language(projects_root: Path) -> None:
    assigned = [
        {"chapter_id": "ch_0001", "chapter_number": 1},
        {"chapter_id": "ch_0002", "chapter_number": 2},
    ]
    session = {
        "novel_ai_short_plan_constraint_ch_0001_pov": "  ngôi ba  ",
        "novel_ai_short_plan_constraint_ch_0002_language": "en",
        "novel_ai_short_plan_constraint_ch_0002_length_guidance": "800 từ",
    }

    values = short_plan_page.build_chapter_constraint_values(
        assigned, session, default_language="vi"
    )
    drafts = _common.build_chapter_constraints(assigned, values, default_language="vi")

    assert values["ch_0001"]["language"] == "vi"
    assert values["ch_0001"]["pov"] == "ngôi ba"
    payload = _common.chapter_constraints_payload(drafts)
    assert [item["chapter_id"] for item in payload] == ["ch_0001", "ch_0002"]
    assert payload[1]["language"] == "en"
    assert "Chương 2" in _common.chapter_constraints_summary(drafts)[1]


def test_chapter_constraints_payload_skips_empty_entries() -> None:
    drafts = _common.build_chapter_constraints(
        [{"chapter_id": "ch_0001", "chapter_number": 1}], {}, default_language=""
    )

    assert drafts[0].is_empty
    assert _common.chapter_constraints_payload(drafts) == []


def test_format_id_uses_index_labels() -> None:
    index = {"char_0001": "Sở Dương", "fs_0001": "Nguồn gốc cái nồi"}

    assert _common.format_id("char_0001", index) == "char_0001 (nhân vật · Sở Dương)"
    assert "Sở Dương" not in _common.format_id("char_9999", index)
    assert "nhân vật" in _common.format_id("char_9999", index)
    assert "foreshadow" in _common.format_id_list(["fs_0001"], index)
    assert "không tham chiếu" in _common.format_id_list([], index)


def test_parse_json_payload_reports_invalid_json_and_non_object() -> None:
    payload, error = _common.parse_json_payload('{"title": "ok"}')
    assert error is None and payload == {"title": "ok"}

    payload, error = _common.parse_json_payload("{not json}")
    assert payload is None and error is not None and "không hợp lệ" in error

    payload, error = _common.parse_json_payload("[1, 2]")
    assert payload is None and error is not None and "object" in error

    payload, error = _common.parse_json_payload("")
    assert payload is None and error is not None


def test_artifact_caption_and_stale_notes_are_honest() -> None:
    assert "missing" in _common.artifact_caption(None, label="premise")
    assert _common.artifact_stale_notes(None) == []


def test_build_rolling_view_flags_out_of_scope_targets() -> None:
    patch = RollingPatchPayload(
        status=RollingStatus.adjust,
        reviewed_chapter_range=ReviewedChapterRange(start=1, end=1),
        deviations=[
            RollingDeviation(
                chapter_id="ch_0001",
                planned="Đi thẳng",
                actual="Rẽ sang chợ",
                evidence="Draft chương 1.",
            )
        ],
        short_plan_changes=[
            ShortPlanChange(chapter_id="ch_0002", changes={"summary": "Đổi"}, reason="Deviation")
        ],
    )

    in_scope = _common.build_rolling_view(
        patch,
        eligible_chapters=[{"chapter_id": "ch_0002", "chapter_number": 2}],
        allow_relationship_replan=False,
    )
    out_of_scope = _common.build_rolling_view(
        patch, eligible_chapters=[{"chapter_id": "ch_0001", "chapter_number": 1}]
    )

    assert in_scope.touches_only_future is True
    assert in_scope.allow_relationship_replan is False
    assert in_scope.deviations[0]["chapter_id"] == "ch_0001"
    assert "summary" in in_scope.short_plan_changes[0].fields
    assert out_of_scope.touches_only_future is False
    assert out_of_scope.out_of_scope_targets == ("ch_0002",)


def test_build_id_index_reads_accepted_foundation(projects_root: Path) -> None:
    project = Project.create(projects_root, "Truyện Index")
    _seed_foundation(project)

    index = _common.build_id_index(project)

    assert index["char_0001"] == "Sở Dương"
    assert index["rule_0001"] == "Cưỡng chế thân thể."
    assert index["fs_0001"] == "Nguồn gốc cái nồi"


def test_arc_options_only_use_accepted_long_plan(projects_root: Path) -> None:
    project = Project.create(projects_root, "Truyện Arc")
    _seed_foundation(project)
    assert _common.arc_options(project) == []

    _seed_long_plan(project)
    options = _common.arc_options(project)

    assert len(options) == 1
    assert options[0]["arc_id"] == "arc_0001"
    assert options[0]["chapter_start"] == 1 and options[0]["chapter_end"] == 2
    assert "Arc mở đầu" in options[0]["label"]


def test_eligible_chapters_for_rolling_excludes_final(projects_root: Path) -> None:
    project = Project.create(projects_root, "Truyện Rolling")
    _seed_foundation(project)
    _seed_long_plan(project)
    assigned = _seed_accepted_short_plan(project)

    # Chưa có actual: mọi chapter chưa final đều nằm trong eligible scope.
    assert [
        item["chapter_number"] for item in short_planner.eligible_chapters_for_rolling(project)
    ] == [1, 2]

    _mark_chapter_final(project, str(assigned[0]["chapter_id"]))

    # Sau khi chương 1 final: chỉ future Short Plan (chương 2) còn eligible.
    eligible_after = short_planner.eligible_chapters_for_rolling(project)
    assert [item["chapter_number"] for item in eligible_after] == [2]
    assert eligible_after[0]["chapter_id"] == assigned[1]["chapter_id"]
    view = _common.build_rolling_view(
        RollingPatchPayload(
            status=RollingStatus.ok,
            reviewed_chapter_range=ReviewedChapterRange(start=1, end=1),
            short_plan_changes=[
                ShortPlanChange(
                    chapter_id=str(assigned[1]["chapter_id"]),
                    changes={"summary": "Đổi hướng theo actual"},
                    reason="Actual lệch plan.",
                )
            ],
        ),
        eligible_chapters=eligible_after,
        allow_relationship_replan=project.config.allow_relationship_replan,
    )
    assert view.touches_only_future is True
    assert view.allow_relationship_replan is True


# ---------------------------------------------------------------------------
# 4. T34 — structured stream trên page planning
# ---------------------------------------------------------------------------


def test_long_plan_page_streams_json_and_reports_saved(
    projects_root: Path, apptest_factory
) -> None:
    """Structured stream: delta hiện dần, chỉ `saved` mới báo validate/lưu."""
    from novel_ai.ui import generation

    project = _project_with_long_plan(projects_root, "Truyện Stream JSON")
    payload = json.dumps(_long_plan_payload(end=40), ensure_ascii=False)
    half = max(1, len(payload) // 2)
    # Project đã có accepted Long Plan ⇒ generate lại là thao tác tường minh với
    # horizon rộng hơn; candidate mới không tự thay accepted cũ.
    at = apptest_factory(project, "long_plan", stream=[payload[:half], payload[half:], ""])

    assert not at.exception
    at.number_input(key="novel_ai_long_plan_scope_start").set_value(1)
    at.number_input(key="novel_ai_long_plan_scope_end").set_value(40)
    _submit(at, "FormSubmitter:novel_ai_long_plan_generate-Chạy generate/regenerate")

    assert not at.exception
    rendered_text = _rendered(at)
    assert "đã validate và lưu" in rendered_text
    assert "Raw preview" in rendered_text
    scoped = generation.scope_key(
        project_id=project.config.project_id, workspace="long_plan", artifact_id="long_plan"
    )
    transcript = at.session_state[generation.KEY_TRANSCRIPT][scoped]
    assert transcript["state"] == "saved"
    assert transcript["transport"] == "streaming"
    assert [event["status"] for event in transcript["events"]][-1] == "saved"
    envelope = _artifact(project, "long_plan")
    assert envelope.candidate_revision is not None
    # Accepted cũ giữ nguyên (candidate mới chưa accept).
    assert envelope.accepted_revision.revision == 1


def test_long_plan_page_partial_stream_does_not_create_candidate(
    projects_root: Path, apptest_factory
) -> None:
    project = _project_with_long_plan(projects_root, "Truyện Stream Đứt")
    payload = json.dumps(_long_plan_payload(end=40), ensure_ascii=False)
    half = max(1, len(payload) // 2)
    at = apptest_factory(project, "long_plan", stream=[payload[:half], "interrupted"])

    assert not at.exception
    at.number_input(key="novel_ai_long_plan_scope_start").set_value(1)
    at.number_input(key="novel_ai_long_plan_scope_end").set_value(40)
    _submit(at, "FormSubmitter:novel_ai_long_plan_generate-Chạy generate/regenerate")

    assert not at.exception
    rendered_text = _rendered(at)
    assert "partial — chưa dùng được" in rendered_text
    assert "không" in rendered_text and "auto accept" in rendered_text
    envelope = _artifact(project, "long_plan")
    assert envelope.candidate_revision is None
    assert envelope.accepted_revision.revision == 1


# ---------------------------------------------------------------------------
# 2. AppTest: luồng chính
# ---------------------------------------------------------------------------


def _single_arc_payload(*, start: int, end: int) -> dict[str, Any]:
    return {
        "volumes": [
            {
                "volume_id": "vol_0001",
                "title": "Quyển 1",
                "theme": "t",
                "goal": "g",
                "arcs": [
                    {
                        "arc_id": "arc_0001",
                        "title": "Arc duy nhất",
                        "chapter_range": {"start": start, "end": end},
                        "goal": "g",
                        "core_conflict": "c",
                        "start_state": "s",
                        "end_state": "e",
                        "major_reveals": [],
                        "character_ids": ["char_0001"],
                        "world_rule_ids": ["rule_0001"],
                        "foreshadow_ids": ["fs_0001"],
                        "relationship_directions": [],
                    }
                ],
            }
        ],
        "global_threads": [],
    }


def test_long_plan_page_requires_horizon_before_calling_llm(
    projects_root: Path, apptest_factory
) -> None:
    """T30/BUG-004: page không còn prefill horizon từ tiến độ; thiếu input ⇒ zero call."""
    project = Project.create(projects_root, "Truyện Horizon")
    _seed_foundation(project)
    root = project.root

    at = apptest_factory(
        project, "long_plan", responses=[json.dumps(_single_arc_payload(start=1, end=40))]
    )

    assert not at.exception
    # Không có horizon đã lưu ⇒ hai ô nhập trống, không suy từ current_chapter.
    assert at.number_input(key="novel_ai_long_plan_scope_start").value is None
    assert at.number_input(key="novel_ai_long_plan_scope_end").value is None
    assert "Project chưa có horizon nào được lưu" in _rendered(at)
    before = _fingerprint_tree(root)

    _submit(at, "FormSubmitter:novel_ai_long_plan_generate-Chạy generate/regenerate")

    assert not at.exception
    assert "Cần nhập đủ `planning_scope.start`" in _rendered(at)
    assert _client(at).calls == []
    assert _artifact(project, "long_plan") is None
    assert _fingerprint_tree(root) == before


def test_long_plan_page_previews_coverage_and_single_arc_warning(
    projects_root: Path, apptest_factory
) -> None:
    project = Project.create(projects_root, "Truyện Preview")
    _seed_foundation(project)

    at = apptest_factory(
        project, "long_plan", responses=[json.dumps(_single_arc_payload(start=1, end=40))]
    )
    at.number_input(key="novel_ai_long_plan_scope_start").set_value(1)
    at.number_input(key="novel_ai_long_plan_scope_end").set_value(40)
    _submit(at, "FormSubmitter:novel_ai_long_plan_generate-Chạy generate/regenerate")

    assert not at.exception
    rendered = _rendered(at)
    assert "Preview coverage" in rendered
    assert "Horizon 1–40 (40 chương) · 1 volume · 1 arc · 40 chương được phủ" in rendered
    assert "Coverage liên tục và phủ đúng hai đầu horizon." in rendered
    assert "chỉ có một arc" in rendered
    assert "`planning_scope` của candidate: 1–40" in rendered
    # Accepted vẫn trống: candidate chưa được accept.
    assert _artifact(project, "long_plan").accepted_revision is None


def test_long_plan_page_legacy_banner_and_confirm_action(
    projects_root: Path, apptest_factory
) -> None:
    """Accepted revision legacy: page cảnh báo, chỉ migrate bằng action tường minh."""
    project = Project.create(projects_root, "Truyện Legacy")
    _seed_foundation(project)
    _seed_artifact(
        project, "long_plan", "long_plan", _single_arc_payload(start=1, end=40)
    )
    root = project.root
    before = _fingerprint_tree(root)

    at = apptest_factory(project, "long_plan")

    assert not at.exception
    assert "legacy" in _rendered(at)
    assert "novel_ai_long_plan_confirm_start" in [
        widget.key for widget in at.number_input
    ]
    # Chỉ render: chưa ghi file nào.
    assert _fingerprint_tree(root) == before

    at.number_input(key="novel_ai_long_plan_confirm_start").set_value(1)
    at.number_input(key="novel_ai_long_plan_confirm_end").set_value(40)
    _submit(
        at, "FormSubmitter:novel_ai_long_plan_confirm_scope-Xác nhận horizon cho accepted revision"
    )

    assert not at.exception
    envelope = _artifact(project, "long_plan")
    assert envelope.accepted_revision.planning_scope.start == 1
    assert envelope.accepted_revision.planning_scope.end == 40
    # Payload không bị sửa bởi migration.
    assert [len(volume.arcs) for volume in envelope.accepted_revision.payload.volumes] == [1]


def test_long_plan_page_rejects_horizon_not_covering_legacy_payload(
    projects_root: Path, apptest_factory
) -> None:
    project = Project.create(projects_root, "Truyện Legacy Lệch")
    _seed_foundation(project)
    _seed_artifact(
        project, "long_plan", "long_plan", _single_arc_payload(start=1, end=40)
    )

    at = apptest_factory(project, "long_plan")
    at.number_input(key="novel_ai_long_plan_confirm_start").set_value(1)
    at.number_input(key="novel_ai_long_plan_confirm_end").set_value(10)
    _submit(
        at, "FormSubmitter:novel_ai_long_plan_confirm_scope-Xác nhận horizon cho accepted revision"
    )

    assert not at.exception
    assert "scope_does_not_cover_payload" in _rendered(at) or "không khớp coverage" in _rendered(at)
    assert _artifact(project, "long_plan").accepted_revision.planning_scope is None


def test_main_flow_create_foundation_and_two_chapter_plan(
    projects_root: Path, apptest_factory
) -> None:
    """Finalize Idea -> accept Premise -> Long Plan -> Short Plan hai chương."""
    project = Project.create(projects_root, "Nồi Canh Bên Đường")
    root = project.root

    # Co-create: Finalize Base Idea bằng markdown user nhập (không gọi LLM).
    at = apptest_factory(project, "co_create")
    assert not at.exception
    at.text_area(key=co_create_page.KEY_FINALIZE_MARKDOWN).set_value(
        "# Base Idea\n\n- Thể loại: tiên hiệp hài\n- Hạt nhân: bác sĩ tỉnh dậy trong thân xác lạ.\n"
    )
    _submit(at, "FormSubmitter:novel_ai_co_create_finalize-Finalize Idea")
    assert not at.exception
    assert (root / "idea" / "base_idea.md").is_file()

    # Architect: accept premise (một LLM call cho generate).
    at = apptest_factory(
        project,
        "architect",
        responses=[json.dumps(_premise_payload(), ensure_ascii=False)],
    )
    assert not at.exception
    assert _common.envelope_or_none(project, "premise") is None
    _submit(at, "FormSubmitter:novel_ai_architect_generate_premise-Chạy generate/regenerate/edit")
    assert not at.exception
    assert "Đã tạo candidate `premise` r1" in _rendered(at)
    assert len(_client(at).calls) == 1
    _click(at, "novel_ai_architect_accept_premise")
    assert not at.exception
    premise = _artifact(project, "premise")
    assert premise is not None
    assert premise.status is ArtifactStatus.accepted
    assert premise.accepted_revision is not None
    assert premise.candidate_revision is None

    # Characters/World Rules/Foreshadow: user accept thêm ba artifact foundation
    # (seed qua storage như hành động rời rạc của user, không gọi LLM) để plan
    # tham chiếu được ID đã accepted.
    _seed_reference_foundation(project)
    assert _artifact(project, "characters").status is ArtifactStatus.accepted

    # Long Plan: user nhập horizon tường minh 1-2 (không còn default suy từ tiến độ)
    # rồi generate + accept.
    at = apptest_factory(
        project,
        "long_plan",
        responses=[json.dumps(_long_plan_payload(end=2), ensure_ascii=False)],
    )
    assert not at.exception
    at.number_input(key="novel_ai_long_plan_scope_start").set_value(1)
    at.number_input(key="novel_ai_long_plan_scope_end").set_value(2)
    _submit(at, "FormSubmitter:novel_ai_long_plan_generate-Chạy generate/regenerate")
    assert not at.exception
    _click(at, "novel_ai_long_plan_accept")
    assert not at.exception
    long_plan = _artifact(project, "long_plan")
    assert long_plan is not None and long_plan.status is ArtifactStatus.accepted

    # Short Plan: hai chapter trong arc, mỗi chapter phải đủ yêu cầu viết (D016).
    at = apptest_factory(
        project,
        "short_plan",
        responses=[json.dumps(_short_plan_two_chapters(), ensure_ascii=False)],
    )
    assert not at.exception
    # Cache `assigned_chapters` mang revision Long Plan accepted (C2 của review T24).
    long_plan_revision = _artifact(project, "long_plan").accepted_revision.revision
    assigned_key = short_plan_page.assigned_cache_key("arc_0001", long_plan_revision)
    rows = at.session_state[assigned_key]
    assert [row["chapter_number"] for row in rows] == [1, 2]
    for row in rows:
        chapter_id = row["chapter_id"]
        at.text_input(
            key=f"novel_ai_short_plan_constraint_{chapter_id}_pov"
        ).set_value("ngôi ba giới hạn")
        at.text_input(
            key=f"novel_ai_short_plan_constraint_{chapter_id}_length_guidance"
        ).set_value("1500–2000 từ")
    _submit(at, "FormSubmitter:novel_ai_short_plan_generate_arc_0001-Chạy generate/regenerate")
    assert not at.exception
    assert len(_client(at).calls) == 1
    _click(at, "novel_ai_short_plan_accept")
    assert not at.exception
    assert not at.exception

    # Assert file thật trên disk.
    assert (root / "idea" / "base_idea.md").is_file()
    premise_document = json.loads((root / "architect" / "premise.json").read_text(encoding="utf-8"))
    assert premise_document["status"] == "accepted"
    assert premise_document["accepted_revision"] is not None
    short_plan_document = json.loads((root / "plans" / "short_plan.json").read_text(encoding="utf-8"))
    assert short_plan_document["status"] == "accepted"
    assert short_plan_document["accepted_revision"]["payload"]["arc_id"] == "arc_0001"
    chapter_ids = [chapter["chapter_id"] for chapter in short_plan_document["accepted_revision"]["payload"]["chapters"]]
    assert len(chapter_ids) == 2
    for chapter_id in chapter_ids:
        metadata_path = root / "chapters" / chapter_id / "chapter.json"
        assert metadata_path.is_file(), f"thiếu metadata cho {chapter_id}"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["status"] == "planned"


def _premise_payload() -> dict[str, Any]:
    return {
        "title": "Nồi Canh Bên Đường",
        "logline": "Sở Dương tỉnh dậy ở Thương Ngô giới với cái nồi sắt đen.",
        "dramatic_question": "Anh có giữ được nguyên tắc cứu người không?",
        "themes": ["quyền lựa chọn"],
        "tone_contract": ["Tiếng Việt tự nhiên."],
        "hard_constraints": ["Không reveal nguồn gốc cái nồi trong chương 1."],
        "non_goals": [],
    }


def _long_plan_payload(*, end: int = 2) -> dict[str, Any]:
    return {
        "volumes": [
            {
                "volume_id": "vol_0001",
                "title": "Quyển 1",
                "theme": "Giữ nguyên tắc",
                "goal": "Sở Dương sống sót qua arc đầu.",
                "arcs": [
                    {
                        "arc_id": "arc_0001",
                        "title": "Arc mở đầu",
                        "chapter_range": {"start": 1, "end": end},
                        "goal": "Định vị Sở Dương.",
                        "core_conflict": "Cưỡng chế thân thể.",
                        "start_state": "Mất phương hướng.",
                        "end_state": "Giữ nguyên tắc.",
                        "major_reveals": [],
                        "character_ids": ["char_0001"],
                        "world_rule_ids": ["rule_0001"],
                        "foreshadow_ids": ["fs_0001"],
                        "relationship_directions": [],
                    }
                ],
            }
        ],
        "global_threads": [],
    }


def _short_plan_two_chapters() -> dict[str, Any]:
    return _short_plan_payload(
        [_chapter_plan("ch_0001", 1, "Chương 1"), _chapter_plan("ch_0002", 2, "Chương 2")]
    )


def _characters_payload() -> dict[str, Any]:
    return {"characters": [_character("char_0001", "Sở Dương")]}


# ---------------------------------------------------------------------------
# 3. Rerun không ghi, Auto Accept, lỗi JSON, append tương lai
# ---------------------------------------------------------------------------


def test_pure_rerun_does_not_write_or_call_llm(projects_root: Path, apptest_factory) -> None:
    project = Project.create(projects_root, "Truyện Rerun")
    _seed_foundation(project)
    _seed_long_plan(project)
    _seed_accepted_short_plan(project)
    before = _fingerprint_tree(project.root)
    assert before

    at = apptest_factory(project, "short_plan")
    assert not at.exception
    calls_after_first_render = len(_client(at).calls)
    at.run(timeout=120)
    at.run(timeout=120)

    assert not at.exception
    assert _fingerprint_tree(project.root) == before
    assert len(_client(at).calls) == calls_after_first_render == 0


def test_architect_generate_repeat_click_does_not_call_llm_twice(
    projects_root: Path, apptest_factory
) -> None:
    """C3: generate của Architect truyền `operation_id` tất định ⇒ bấm lặp là replay."""
    project = Project.create(projects_root, "Truyện Bấm Lặp")
    _seed_base_idea(project)
    at = apptest_factory(
        project, "architect", responses=[json.dumps(_premise_payload(), ensure_ascii=False)]
    )
    assert not at.exception

    button = "FormSubmitter:novel_ai_architect_generate_premise-Chạy generate/regenerate/edit"
    _submit(at, button)
    assert not at.exception
    assert len(_client(at).calls) == 1
    first = _artifact(project, "premise").candidate_revision.revision

    _submit(at, button)
    assert not at.exception
    # Không gọi LLM lần hai và không tạo revision thứ hai.
    assert len(_client(at).calls) == 1
    assert _artifact(project, "premise").candidate_revision.revision == first


def test_auto_accept_on_accepts_structured_artifact(
    projects_root: Path, apptest_factory
) -> None:
    project = Project.create(projects_root, "Truyện AutoAccept On")
    _seed_base_idea(project)
    project.update_config(auto_accept_structured=True)
    project.reload()

    at = apptest_factory(
        project, "architect", responses=[json.dumps(_premise_payload(), ensure_ascii=False)]
    )
    assert not at.exception
    assert "BẬT" in _rendered(at)
    _submit(at, "FormSubmitter:novel_ai_architect_generate_premise-Chạy generate/regenerate/edit")
    assert not at.exception

    premise = _artifact(project, "premise")
    assert premise is not None
    assert premise.status is ArtifactStatus.accepted
    assert premise.candidate_revision is None
    assert premise.accepted_revision is not None
    assert premise.accepted_revision.accepted_by == "auto_accept"


def test_auto_accept_off_keeps_candidate_and_old_accepted(
    projects_root: Path, apptest_factory
) -> None:
    project = Project.create(projects_root, "Truyện AutoAccept Off")
    _seed_base_idea(project)
    _seed_artifact(project, "premise", "premise", _premise_payload())

    at = apptest_factory(
        project,
        "architect",
        responses=[
            json.dumps({**_premise_payload(), "title": "Bản regenerate"}, ensure_ascii=False)
        ],
    )
    assert not at.exception
    assert "TẮT" in _rendered(at)
    _submit(at, "FormSubmitter:novel_ai_architect_generate_premise-Chạy generate/regenerate/edit")
    assert not at.exception

    premise = _artifact(project, "premise")
    assert premise is not None
    assert premise.status is ArtifactStatus.draft
    assert premise.candidate_revision is not None
    assert premise.accepted_revision is not None
    assert premise.accepted_revision.payload.title == "Nồi Canh Bên Đường"
    assert premise.candidate_revision.payload.title == "Bản regenerate"


def test_invalid_json_keeps_accepted_and_reports_field_errors(
    projects_root: Path, apptest_factory
) -> None:
    """LLM trả JSON sai schema: UI báo lỗi, accepted và candidate cũ giữ nguyên."""
    project = Project.create(projects_root, "Truyện JSON Sai")
    _seed_foundation(project)
    accepted_before = {
        item.character_id: item.display_name
        for item in _artifact(project, "characters").accepted_revision.payload.characters
    }
    broken = {
        "characters": [
            {
                "character_id": "char_0001",
                "display_name": 7,  # sai kiểu: phải là string
                "role": "protagonist",
                "public_profile": {"description": ""},
            }
        ]
    }
    at = apptest_factory(
        project, "architect", responses=[json.dumps(broken, ensure_ascii=False)]
    )
    assert not at.exception
    at.radio(key="novel_ai_architect_artifact").set_value("characters")
    at.run(timeout=120)
    _submit(
        at,
        "FormSubmitter:novel_ai_architect_generate_characters-Chạy generate/regenerate/edit",
    )

    assert not at.exception
    rendered = _rendered(at)
    assert "không parse/không khớp schema" in rendered or "không qua validation" in rendered
    characters = _artifact(project, "characters")
    assert characters is not None
    assert characters.status is ArtifactStatus.accepted
    assert characters.candidate_revision is None
    assert {
        item.character_id: item.display_name
        for item in characters.accepted_revision.payload.characters
    } == accepted_before


def test_candidate_editor_keeps_user_input_when_validation_fails(
    projects_root: Path, apptest_factory
) -> None:
    """Sửa candidate bằng JSON sai: lỗi hiển thị theo field và nội dung đang sửa còn nguyên."""
    project = Project.create(projects_root, "Truyện Sửa Candidate")
    _seed_foundation(project)
    _seed_candidate(project, "characters", "characters", _characters_payload())
    at = apptest_factory(project, "architect")
    assert not at.exception
    at.radio(key="novel_ai_architect_artifact").set_value("characters")
    at.run(timeout=120)
    assert not at.exception

    editor_key = _common.editor_key("characters", workspace="architect")
    assert editor_key in [widget.key for widget in at.text_area]
    broken_text = json.dumps({"characters": [{"character_id": "char_0009"}]}, ensure_ascii=False)
    at.text_area(key=editor_key).set_value(broken_text)
    _click(at, "novel_ai_architect_edit_characters")

    assert not at.exception
    assert "không đúng contract" in _rendered(at) or "không qua validation" in _rendered(at)
    assert at.text_area(key=editor_key).value == broken_text
    characters = _artifact(project, "characters")
    assert characters is not None
    # Candidate cũ giữ nguyên (không bị xoá) và accepted không đổi.
    assert characters.status is ArtifactStatus.draft
    assert characters.candidate_revision is not None
    assert characters.candidate_revision.payload.characters[0].display_name == "Sở Dương"
    assert characters.accepted_revision is not None


def test_candidate_editor_reload_button_refetches_candidate(
    projects_root: Path, apptest_factory
) -> None:
    """Nút `Nạp lại editor từ candidate` là đường reset tường minh cho user."""
    project = Project.create(projects_root, "Truyện Nạp Lại Editor")
    _seed_foundation(project)
    _seed_candidate(project, "characters", "characters", _characters_payload())
    at = apptest_factory(project, "architect")
    assert not at.exception
    at.radio(key="novel_ai_architect_artifact").set_value("characters")
    at.run(timeout=120)

    editor_key = _common.editor_key("characters", workspace="architect")
    original = at.text_area(key=editor_key).value
    at.text_area(key=editor_key).set_value('{"characters": []}')
    _click(at, "novel_ai_architect_reload_characters")

    assert not at.exception
    assert at.text_area(key=editor_key).value == original


def test_future_append_entry_is_visible_and_keeps_past_chapters_fresh(
    projects_root: Path, apptest_factory
) -> None:
    project = Project.create(projects_root, "Truyện Append Tương Lai")
    _seed_foundation(project)
    _seed_long_plan(project)
    _seed_accepted_short_plan(project)
    chapter = storage.load_chapter(project, "ch_0001")
    assert chapter is not None
    chapter_pin_before = chapter.short_plan_pin.revision
    plan_before = _artifact(project, "long_plan")
    plan_status_before = plan_before.status if plan_before is not None else None

    at = apptest_factory(project, "architect")
    assert not at.exception
    at.radio(key="novel_ai_architect_artifact").set_value("characters")
    at.run(timeout=120)
    assert not at.exception

    editor_key = _common.editor_key("characters", workspace="architect", variant="append")
    # Editor append chỉ chứa entry mới, không chứa entry đã accepted.
    assert "char_0001" not in at.text_area(key=editor_key).value
    at.text_area(key=editor_key).set_value(
        json.dumps(
            {"characters": [_character("char_0002", "Lâm Thanh", effective=1)]},
            ensure_ascii=False,
        )
    )
    at.run(timeout=120)
    assert not at.exception
    assert "char_0002" in at.text_area(key=editor_key).value
    at.number_input(key="novel_ai_architect_append_chapter_characters").set_value(5)
    at.run(timeout=120)
    assert not at.exception
    assert "char_0002" in at.text_area(key=editor_key).value
    _click(at, "novel_ai_architect_append_characters")

    assert not at.exception
    characters = _artifact(project, "characters")
    assert characters is not None and characters.accepted_revision is not None
    assert characters.status is ArtifactStatus.accepted
    assert len(characters.accepted_revision.payload.characters) == 2
    appended = {
        item.character_id: item.effective_from_chapter
        for item in characters.accepted_revision.payload.characters
    }
    assert appended["char_0002"] == 5
    # Entry tương lai không làm chapter quá khứ/plan hiện tại stale.
    chapter_after = storage.load_chapter(project, "ch_0001")
    assert chapter_after is not None
    assert chapter_after.status is ChapterStatus.planned
    assert chapter_after.short_plan_pin.revision == chapter_pin_before
    plan_after = _artifact(project, "long_plan")
    assert plan_after is not None and plan_after.status is plan_status_before
    # Entry mới hiển thị được trong UI (caption ID đã có trong accepted).
    assert "char_0002" in _rendered(at)


# ---------------------------------------------------------------------------
# 3. T31 — default viết của project và guard trước LLM (BUG-003)
# ---------------------------------------------------------------------------


def _project_with_long_plan(projects_root: Path, title: str) -> Project:
    project = Project.create(projects_root, title)
    _seed_foundation(project)
    _seed_long_plan(project, chapters=2)
    return project


def test_build_chapter_constraint_values_uses_project_defaults() -> None:
    assigned = [{"chapter_id": "ch_0001", "chapter_number": 1}]

    values = short_plan_page.build_chapter_constraint_values(
        assigned,
        {},
        default_language="vi",
        default_pov="ngôi ba giới hạn",
        default_length_guidance="1500 từ",
    )

    assert values["ch_0001"] == {
        "language": "vi",
        "pov": "ngôi ba giới hạn",
        "length_guidance": "1500 từ",
    }
    # Default rỗng vẫn để rỗng: app không bịa POV/độ dài.
    empty = short_plan_page.build_chapter_constraint_values(
        assigned, {}, default_language="vi", default_pov="", default_length_guidance=""
    )
    assert empty["ch_0001"]["pov"] == ""
    assert empty["ch_0001"]["length_guidance"] == ""


def test_short_plan_page_blocks_generate_when_defaults_missing(
    projects_root: Path, apptest_factory
) -> None:
    """BUG-003: thiếu default ⇒ UI báo rõ và service không được gọi."""
    project = _project_with_long_plan(projects_root, "Truyện Thiếu Default")
    project.update_config(default_pov="", default_length_guidance="")
    project.reload()

    at = apptest_factory(
        project, "short_plan", responses=[json.dumps(_short_plan_two_chapters())]
    )

    assert not at.exception
    rendered = _rendered(at)
    assert "Project chưa thiết lập default cho" in rendered
    assert "`pov`" in rendered and "`length_guidance`" in rendered

    _submit(at, "FormSubmitter:novel_ai_short_plan_generate_arc_0001-Chạy generate/regenerate")

    assert not at.exception
    assert _client(at).calls == []
    assert "missing_writing_contract" in _rendered(at)
    assert "không gọi LLM" in _rendered(at)
    envelope = _artifact(project, "short_plan")
    assert envelope is None or envelope.candidate_revision is None


def test_short_plan_page_saves_writing_defaults_without_llm(
    projects_root: Path, apptest_factory
) -> None:
    project = _project_with_long_plan(projects_root, "Truyện Lưu Default")
    project.update_config(default_pov="", default_length_guidance="")
    project.reload()
    root = project.root

    at = apptest_factory(project, "short_plan")

    assert not at.exception
    at.text_input(key="novel_ai_writing_default_pov").set_value("ngôi ba giới hạn")
    at.text_input(key="novel_ai_writing_default_length").set_value("1500–2000 từ")
    _submit(at, "FormSubmitter:novel_ai_writing_defaults-Lưu default viết")

    assert not at.exception
    reloaded = Project.open(root.parent, project.slug)
    assert reloaded.config.default_pov == "ngôi ba giới hạn"
    assert reloaded.config.default_length_guidance == "1500–2000 từ"
    # Save default không gọi LLM và không tạo candidate.
    assert _client(at).calls == []
    envelope = _artifact(reloaded, "short_plan")
    assert envelope is None or envelope.candidate_revision is None

    # Sau khi lưu default, request generate hợp lệ và gọi LLM đúng một lần.
    at2 = apptest_factory(
        reloaded, "short_plan", responses=[json.dumps(_short_plan_two_chapters())]
    )
    assert not at2.exception
    assert "Project chưa thiết lập default" not in _rendered(at2)
    _submit(at2, "FormSubmitter:novel_ai_short_plan_generate_arc_0001-Chạy generate/regenerate")
    assert not at2.exception
    assert len(_client(at2).calls) == 1
    candidate = _artifact(reloaded, "short_plan")
    assert candidate is not None and candidate.candidate_revision is not None
