"""Rà fixture và cross-reference cho contract T29 (D016-D019). Document check only.

Script này:

1. cài đặt lại **luật đã ghi trong tài liệu** (coverage horizon ở `schemas.md` mục 3.1,
   bảng legacy ở mục 3.4, default viết ở D016, state machine ở mục 11.2) rồi kiểm fixture
   `t29_contract_cases.json` khớp kỳ vọng;
2. đối chiếu decision ID D016-D019 và các mục contract có mặt trong `decisions.md`,
   `schemas.md`, `workflow.md`, `prompt-catalog.md`;
3. kiểm mọi link nội bộ trong các tài liệu đó vẫn resolve.

Nó **không** thay validator runtime, không import `novel_ai`, và không chứng minh backend đã
implement D016-D019.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DESIGN = ROOT / "docs" / "design"
EXAMPLES = DESIGN / "examples"

data = json.loads((EXAMPLES / "t29_contract_cases.json").read_text(encoding="utf-8"))
decisions = (DESIGN / "decisions.md").read_text(encoding="utf-8")
schemas = (DESIGN / "schemas.md").read_text(encoding="utf-8")
workflow = (DESIGN / "workflow.md").read_text(encoding="utf-8")
catalog = (DESIGN / "prompt-catalog.md").read_text(encoding="utf-8")
storage = (DESIGN / "storage.md").read_text(encoding="utf-8")

ARC_KEYS = {
    "arc_id",
    "title",
    "chapter_range",
    "goal",
    "core_conflict",
    "start_state",
    "end_state",
    "major_reveals",
    "character_ids",
    "world_rule_ids",
    "foreshadow_ids",
    "relationship_directions",
}


def coverage_issues(payload: dict, scope: dict) -> list[str]:
    """Luật coverage của schemas.md mục 3.1 (thứ tự volume -> arc)."""
    issues: list[str] = []
    volumes = payload.get("volumes") or []
    if not volumes:
        return ["empty_long_plan"]
    arcs = []
    for volume in volumes:
        volume_arcs = volume.get("arcs") or []
        if not volume_arcs:
            issues.append("empty_volume")
            continue
        arcs.extend(volume_arcs)
    if not arcs:
        return issues or ["empty_long_plan"]

    seen: set[str] = set()
    for arc in arcs:
        if set(arc) != ARC_KEYS:
            issues.append(f"arc_fields:{arc.get('arc_id')}")
        arc_id = arc["arc_id"]
        if arc_id in seen:
            issues.append(f"duplicate_arc:{arc_id}")
        seen.add(arc_id)
        start = arc["chapter_range"]["start"]
        end = arc["chapter_range"]["end"]
        if start > end:
            issues.append(f"invalid_range:{arc_id}")
        if start < scope["start"] or end > scope["end"]:
            issues.append("out_of_scope_arc")

    if issues:
        return issues

    if arcs[0]["chapter_range"]["start"] != scope["start"]:
        issues.append("uncovered_scope_start")
    if arcs[-1]["chapter_range"]["end"] != scope["end"]:
        issues.append("uncovered_scope_end")
    for previous, current in zip(arcs, arcs[1:]):
        previous_end = previous["chapter_range"]["end"]
        current_start = current["chapter_range"]["start"]
        if current_start == previous_end + 1:
            continue
        issues.append("overlap" if current_start <= previous_end else "gap_in_scope")
    return issues


def check_long_plan_cases() -> int:
    checked = 0
    for case in data["long_plan_cases"]:
        issues = coverage_issues(case["payload"], case["planning_scope"])
        expected = case["expect"]
        if expected == "valid":
            assert not issues, (case["case_id"], issues)
            arcs = [a for v in case["payload"]["volumes"] for a in v["arcs"]]
            single_arc = len(arcs) == 1
            assert single_arc is case["single_arc_warning"], case["case_id"]
        else:
            assert issues, f"{case['case_id']} phải bị từ chối nhưng lại hợp lệ"
            assert case["code"] in issues, (case["case_id"], case["code"], issues)
        checked += 1
    # Negative probe của chính checker: sửa một case hợp lệ thành có gap phải bị bắt.
    probe = json.loads(json.dumps(data["long_plan_cases"][0]))
    probe["payload"]["volumes"][0]["arcs"][0]["chapter_range"]["end"] = 19
    assert "gap_in_scope" in coverage_issues(probe["payload"], probe["planning_scope"])
    return checked


def check_legacy_cases() -> int:
    for case in data["legacy_cases"]:
        scope = case["planning_scope"]
        if scope is None:
            assert case["read"].startswith("allowed"), case["case_id"]
            assert case["generate"] == "blocked", case["case_id"]
            assert case["accept"] == "blocked", case["case_id"]
            assert case["auto_accept"] == "blocked", case["case_id"]
            assert case["code"] == "missing_planning_scope", case["case_id"]
        else:
            assert case["read"] == "allowed", case["case_id"]
            assert case["generate"] == "allowed" and case["accept"] == "allowed"
            assert case["auto_accept"] == "per_config", case["case_id"]
        # Không nhánh nào được ghi file khi chỉ mở project.
        assert case["writes_on_open"] is False, case["case_id"]
    # Bảng legacy trong schemas.md phải có đủ cột/nhãn đã dùng ở fixture.
    assert "missing_planning_scope" in schemas
    assert "legacy" in schemas
    assert "confirm_planning_scope" in storage
    return len(data["legacy_cases"])


def resolve_constraints(case: dict) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Resolve default project + override chương, trả `(effective, issues)`."""
    project = case["project"]
    language = project.get("default_language", "")
    pov = project.get("default_pov", "")
    length = project.get("default_length_guidance", "")
    overrides = case["overrides"]
    issues: list[str] = []
    assigned = list(case["assigned_chapters"])
    for chapter_id in case.get("duplicate_chapters", []):
        issues.append("duplicate_chapter_constraint")
    for chapter_id in overrides:
        if chapter_id not in assigned:
            issues.append("unknown_chapter_constraint")
    effective: dict[str, dict[str, str]] = {}
    for chapter_id in assigned:
        override = overrides.get(chapter_id, {})
        values = {
            "language": str(override.get("language") or language).strip(),
            "pov": str(override.get("pov") or pov).strip(),
            "length_guidance": str(override.get("length_guidance") or length).strip(),
        }
        if not all(values.values()):
            issues.append("missing_writing_contract")
        effective[chapter_id] = values
    # Giữ thứ tự issue ổn định để case `unknown` không lẫn `missing`.
    deduped = list(dict.fromkeys(issues))
    return effective, deduped


def check_writing_default_cases() -> int:
    for case in data["writing_default_cases"]:
        effective, issues = resolve_constraints(case)
        if case["expect"] == "valid":
            assert not issues, (case["case_id"], issues)
            for chapter_id in case["assigned_chapters"]:
                assert all(effective[chapter_id].values()), case["case_id"]
            # Override phải thắng default ở đúng chapter được override.
            for chapter_id, override in case["overrides"].items():
                for field, value in override.items():
                    assert effective[chapter_id][field] == value, (case["case_id"], field)
        else:
            assert case["code"] in issues, (case["case_id"], case["code"], issues)
            assert case["llm_calls"] == 0, case["case_id"]
            for field in case.get("fields", []):
                # Field thiếu phải là field thật của contract viết.
                assert field in {"language", "pov", "length_guidance"}
    # Override rỗng không được coi là giá trị hợp lệ (không bịa default).
    empty_override = {
        "case_id": "probe_empty_override",
        "project": {
            "default_language": "vi",
            "default_pov": "Ngôi thứ ba",
            "default_length_guidance": "1000 từ",
        },
        "assigned_chapters": ["ch_0001"],
        "overrides": {"ch_0001": {"pov": ""}},
        "expected": "invalid",
    }
    _effective, issues = resolve_constraints(empty_override)
    assert "missing_writing_contract" not in issues, (
        "override rỗng phải rơi về default project, không phải lỗi"
    )
    strict = {
        **empty_override,
        "project": {
            "default_language": "vi",
            "default_pov": "",
            "default_length_guidance": "",
        },
    }
    _effective, issues = resolve_constraints(strict)
    assert "missing_writing_contract" in issues
    return len(data["writing_default_cases"])


def check_generation_states() -> None:
    documented = re.findall(r"^\| `([a-z_]+)` \|", schemas, re.M)
    states = data["generation_event_states"]
    for state in states:
        assert state in documented, state
        assert f"`{state}`" in schemas
    assert set(data["generation_terminal_states"]) == {
        "saved",
        "partial",
        "invalid",
        "error",
    }
    assert data["candidate_ready_states"] == ["saved"]
    assert "transport_complete" in workflow
    assert "`saved`" in workflow
    assert "không" in workflow  # tài liệu tiếng Việt, không phải placeholder tiếng Anh


def check_cross_references() -> None:
    for decision_id in ("D016", "D017", "D018", "D019"):
        assert f"## {decision_id} " in decisions, decision_id
    # Mỗi tài liệu phải tham chiếu đúng nhóm decision mà nó chịu trách nhiệm.
    required = {
        "schemas.md": ("D016", "D017", "D018", "D019"),
        "workflow.md": ("D016", "D017", "D018", "D019"),
        "prompt-catalog.md": ("D016", "D017", "D018", "D019"),
        "storage.md": ("D017", "D019"),
    }
    for doc_name, text in (
        ("schemas.md", schemas),
        ("workflow.md", workflow),
        ("prompt-catalog.md", catalog),
        ("storage.md", storage),
    ):
        for decision_id in required[doc_name]:
            assert decision_id in text, (doc_name, decision_id)
    for anchor in (
        "schemas.md mục 11.2",
        "schemas.md mục 3.1",
        "schemas.md mục 3.4",
        "storage.md mục 11",
    ):
        doc, _sep, section = anchor.partition(" mục ")
        assert section.split(".")[0] in (DESIGN / doc).read_text(encoding="utf-8")
    # Link nội bộ trong 5 tài liệu contract phải resolve.
    checked = 0
    for text in (decisions, schemas, workflow, catalog, storage):
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if "://" in target:
                continue
            path = target.split("#")[0]
            if not path:
                continue
            assert (DESIGN / path).exists(), target
            checked += 1
    assert checked > 0


if __name__ == "__main__":
    long_plan = check_long_plan_cases()
    legacy = check_legacy_cases()
    writing = check_writing_default_cases()
    check_generation_states()
    check_cross_references()
    print(
        "PASS: "
        f"{long_plan} long-plan coverage cases (multi-volume/arc, gap, uncovered edges, "
        f"empty payload/volume, overlap, out-of-scope, single-arc valid), "
        f"{legacy} legacy matrix cases, {writing} writing-default cases, "
        f"{len(data['generation_event_states'])} generation states, "
        "editor working-copy rules, D016-D019 cross-references, internal links. "
        "Document checks only, no runtime validation."
    )
