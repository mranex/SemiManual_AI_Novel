# B07 — Matrix HTTP/UI

Ngày 2026-09-23. Đối chiếu `ProjectQueries`, `ApplicationCommands`, `novel_ai/web/dto.py::FIELDS` và nút trong React. Cả 54 command HTTP đã có action tường minh trên UI; không có action server-only. Các lệnh generation dùng POST SSE; các lệnh còn lại POST JSON. Backend giữ toàn bộ guard.

| Nhóm | Query UI | Command UI |
|---|---|---|
| Shell | `list_projects`, `open_project`, `tree`, `arbiter`, `status` → `App.tsx` | `create_project`, `probe_llm` → `App.tsx` |
| Co-create/Architect | `foundation`, `artifact` → `foundation.tsx` | `co_create_turn`, `save_idea_state`, `finalize_base_idea`, `reserve_foundation_ids`, `generate_foundation`, `edit_foundation_candidate`, `accept_foundation`, `reject_foundation`, `append_foundation_entries` → `foundation.tsx` |
| Long/Short/Rolling | `planning`, `artifact` → `planning.tsx` | `save_writing_defaults`, `generate_long_plan`, `reserve_plan_ids`, `edit_long_plan_candidate`, `accept_long_plan`, `reject_long_plan`, `confirm_planning_scope`, `reserve_chapter_ids`, `generate_short_plan`, `edit_short_plan_candidate`, `accept_short_plan`, `reject_short_plan`, `generate_rolling`, `accept_rolling`, `reject_rolling` → `planning.tsx` |
| Chapter | `chapter`, `artifact`, `latest_writer_operation` → `chapters.tsx` | `reserve_section_ids`, `generate_skeleton`, `edit_skeleton_candidate`, `accept_skeleton`, `reject_skeleton`, `write_draft`, `save_draft`, `discard_draft`, `run_ai_review`, `rewrite_section`, `apply_rewrite`, `mark_reviewed`, `finalize_chapter`, `generate_reconciliation`, `retry_reconcile`, `edit_reconciliation_candidate`, `accept_reconciliation`, `reject_reconciliation`, `cancel_finalizing` → `chapters.tsx` |
| Revision/Recovery | `revision`, `recovery`, `foundation`, `artifact`, `chapter` → `revision.tsx` | `recover_project`, `reaccept_stale`, `start_retcon`, `edit_retcon_draft`, `reset_consistency`, `revise_base_idea`, `revise_premise`, `generate_impact_report`, `reconcile_downstream` → `revision.tsx` |

`finalize_chapter` và `generate_reconciliation` được gọi từ workspace Reconcile sau Start Retcon. Các nút Review/Reconcile theo blocker chỉ điều hướng, không gọi generation. Query GET và reload không mutate. Recovery là action duy nhất còn bật khi pending recoverable; manual read-only chỉ còn phần đối chiếu History.

Đối chiếu máy: tập tên 54 `FIELDS` đều xuất hiện trong `frontend/src/*.tsx`; `tests/web/test_api.py` kiểm mọi route OpenAPI/closed params, `tests/integration/test_reconcile_revision.py` kiểm transaction retcon. Browser E2E ở `frontend/e2e/workflow.spec.ts` kiểm recovery/reload, hai chương, edit/commit retcon, reset consistency và rebuild downstream.
