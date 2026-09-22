# Báo cáo nghiệm thu đợt fix T26–T40

Ngày chạy: **2026-09-22** (Windows 11, PowerShell, Python 3.12.9, streamlit 1.41.1,
pydantic 2.11.7, pytest 8.3.4). Người thực hiện: DSH agent.

Phạm vi: đợt sửa sau review prototype — BUG-001–004 ([DEBUG_REPORT.md](../../DEBUG_REPORT.md)),
UI-01–04 ([UI_Review.md](../../UI_Review.md)), theo [FIX_IMPLEMENTATION_PLAN.md](../../FIX_IMPLEMENTATION_PLAN.md).
Mọi bằng chứng dưới đây là **kết quả chạy mới trong đợt này**, không dùng số liệu lịch sử.
Chi tiết bàn giao từng task ở `docs/tasks/T26…T40`.

## 1. Lệnh đã chạy và kết quả

| Lệnh | Kết quả |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -p no:randomly` | **660 passed, 1 skipped, 0 failed, 0 error** (junit-xml: 661 test, 82–136s) |
| `.\.venv\Scripts\python.exe -m pytest tests\integration\test_t40_acceptance.py -p no:randomly` | 5 passed (walkthrough full shell, BUG-003/004, UI-02 surface, blocker labeling) |
| `.\.venv\Scripts\python.exe docs\design\examples\check_t29_contracts.py` | PASS — 9 long-plan coverage case, 4 legacy matrix, 6 writing-default, 10 generation state, editor working-copy rules, D016–D019 cross-reference, internal link |
| `.\.venv\Scripts\python.exe docs\design\examples\check_planning_examples.py` | PASS — 9 request/response case, 33 JSON block, complete-horizon coverage, 9 negative probe |
| `.\.venv\Scripts\python.exe docs\design\examples\check_writing_examples.py` | PASS — 14 prompt, 6 genre, 10 authored case, 2 Skeleton projection case, 10 negative probe |
| `.\.venv\Scripts\python.exe -m pytest tests\integration\test_app_entrypoint.py -p no:randomly` | PASS — shell/tree/Arbiter/full-shell BUG-001 + BUG-002 trigger |
| App thật + Edge headless (`--virtual-time-budget`), 16 ảnh ở 900/1366/1600/1920 | **PASS** — xem mục 6; project demo trong `%TEMP%`, không đụng `projects/` thật |

**1 test skipped** (có lý do ghi trong test): `tests/unit/test_layout_router.py::test_render_workspace_prints_not_wired_message_when_page_missing`
tự `pytest.skip` vì `novel_ai.pages.skeleton` đã được nối từ T20 — case "page chưa nối"
không còn ý nghĩa. Không phải test bị bỏ quên hay fail.

Baseline lịch sử `493 passed, 4 failed, 1 skipped` trong `DEBUG_REPORT.md` là của
trước đợt fix (4 fail do dotenv local); T26 cô lập dotenv/env/cache nên suite hiện
không đọc `.env` thật của repo.

## 2. Coverage matrix — BUG/UI finding → task → test → kết quả

| Finding | Task | Bằng chứng runtime (test/trigger) | Kết quả |
|---|---|---|---|
| BUG-001 Project tree tạo expander lồng nhau sau Accept Short Plan | T27 | `test_app_entrypoint.py::test_accept_short_plan_in_full_shell_reruns_and_reopens_without_crash` (Accept Short Plan trong **shell thật** → rerun → reopen), `::test_project_tree_renders_one_chapter_without_nested_expander`, `::test_project_tree_renders_many_chapters_without_nested_expander`, `::test_project_tree_selection_still_reaches_skeleton_and_reconciliation` | PASS — chỉ còn một tầng expander; renderer một tầng trong `ui/project_tree.py` |
| BUG-002 Nút Arbiter không chuyển workspace | T28 | `test_app_entrypoint.py::test_arbiter_button_switches_workspace_navbar_and_page`, `::test_arbiter_navigation_works_without_llm_client`, `::test_arbiter_navigation_reaches_recovery_workspace` | PASS — điều hướng dùng `on_click` callback, không sửa widget state sau instantiate |
| BUG-003 Thiếu `length_guidance` vẫn gọi Short Plan API | T31 | `test_t40_acceptance.py::test_shell_blocks_short_plan_before_llm_when_writing_contract_missing` (0 LLM call khi thiếu contract, rồi lưu default và chạy được), `test_foundation_planning_ui.py` (nhóm default/override), `test_planning_services.py` (guard trước LLM ở tầng service) | PASS — guard nằm ở service, chạy trước khi gọi LLM; default theo project + override từng chương |
| BUG-004 Long Plan gộp horizon thành một arc | T30 | `test_t40_acceptance.py::test_full_shell_walkthrough_long_plan_to_chapter_two` + `::test_shell_keeps_user_horizon_and_does_not_infer_legacy_scope` (horizon 1–4, hai arc, sống qua Accept/reopen), `test_planning_services.py` (coverage/legacy), `test_validation.py::test_horizon_issues_*` | PASS — `planning_scope` app-owned trên revision; validator chặn gap/overlap/out-of-scope/thiếu hai đầu |
| UI-01 Project tree nằm pane cố định, tách khỏi project controls | T32 | `test_app_entrypoint.py::test_shell_layout_has_top_nav_project_drawer_and_compact_arbiter`, `::test_blocking_notices_visible_when_drawer_and_arbiter_closed`, ảnh [1366](visual/t40-long-plan-arbiter-open-1366x768.png)/[1920](visual/t40-long-plan-arbiter-open-1920x1080.png) | PASS — tree nằm trong **drawer sidebar**, đóng được; blocking notice vẫn hiển thị khi đóng (ảnh pending recovery) |
| UI-02 Structured action dùng blocking `complete()`, thiếu stream chung | T33/T34/T35 + T40 | `test_generation_events.py` (state/transition), `test_t35_stream_coverage.py`, `test_t35_ui_surface.py`, `test_t40_acceptance.py::test_reconcile_downstream_action_has_generation_surface` | PASS — xem inventory mục 3; action phục hồi cuối cùng (`reconcile_downstream`) đã có surface/transcript |
| UI-03 Sau generation chưa có editor nhất quán; Long/Short/… chỉ sửa JSON | T37/T38/T39 (+ V2/V3 ở T40) | `test_t37_foundation_editor.py`, `test_t38_planning_editor.py`, `test_t39_chapter_editors.py`, ảnh editor [Long Plan](visual/t40-long-plan-candidate-editor-1366x2600.png)/[Short Plan](visual/t40-short-plan-candidate-editor-1366x2400.png)/[Skeleton](visual/t40-skeleton-candidate-editor-1366x2400.png)/[prose](visual/t40-writer-prose-editor-1366x2600.png) | PASS — happy path không cần JSON; raw JSON vẫn ở tab nâng cao; T40 sửa cảnh báo widget (V2) và lỗi không tới được candidate Short Plan (V3) |
| UI-04 Arbiter luôn chiếm cột phải, không compact/collapse | T32 | `test_app_entrypoint.py::test_shell_layout_has_top_nav_project_drawer_and_compact_arbiter`, `::test_arbiter_panel_toggle_keeps_state_and_writes_nothing`, `test_arbiter.py::test_badge_summary_*`, ảnh Arbiter mở ([1366](visual/t40-long-plan-arbiter-open-1366x768.png)) và đóng ([1366](visual/t40-writer-arbiter-closed-1366x768.png)) | PASS — Arbiter compact + toggle, không còn 3 cột luôn mở; cảnh báo recovery vẫn thấy khi panel đóng |

Ghi chú: D018 cho phép giữ vai trò Project/Arbiter nhưng **bỏ ba cột luôn mở**;
T32 thực thi, T40 chỉ nghiệm thu phần chạy được offline. Ảnh/visual thuộc mục 6
(chưa chạy — xem giới hạn).

## 3. Inventory toàn bộ action gọi LLM (UI-02)

| Workspace / action | Service | Surface generation | Stream | Partial/retry |
|---|---|---|---|---|
| Co-create — `Gửi tin nhắn` | `co_create.run_turn` | `generation.start_surface` + `recorder_for` | có (structured stream) | partial không tạo candidate; retry theo `operation_id` |
| Architect — Generate/Regenerate (premise/characters/world_rules/foreshadow) | `architect.generate` | có | có | có |
| Long Plan — Generate/Regenerate | `long_planner.generate` | có | có | có |
| Short Plan — Generate/Regenerate | `short_planner.generate` | có | có | có |
| Short Plan — Rolling proposal | `short_planner.generate_rolling` | có | không (structured non-stream) | proposal không apply khi lỗi |
| Skeleton — Generate/Regenerate | `skeleton.generate` | có | có | có |
| Writer — Generate/Regenerate/Continue | `writer.generate_draft` / `regenerate_draft` / `continue_draft` | có | có (token stream) | draft partial giữ lại, `Continue` để viết tiếp |
| Review — AI Review | `reviewer.run_ai_review` | có (`review.<chapter>`) | có | report-only, lỗi schema ⇒ invalid |
| Review — Rewrite Section | `reviewer.rewrite_section` | có (`rewrite_<chapter>`) | có | replacement chỉ áp khi bấm Apply |
| Reconcile — Generate/Retry | `reconcile.generate_reconciliation` / `retry_reconcile` | có | có | chapter vẫn `finalizing`, retry không nhân đôi timeline |
| Revision — Impact report (revise upstream) | `revision.generate_impact_report` | có | có | report-only |
| Revision — Reconcile downstream (rebuild sau retcon) | `revision.reconcile_downstream` | có (**bổ sung ở T40**) | không (structured non-stream) | chapter giữ nguyên nếu proposal lỗi; rebuild theo thứ tự chương |

Mọi action ở bảng trên: (a) guard backend chạy **trước** LLM (Base Idea/premise/Short
Plan/pin/contract viết/chapter locked), (b) lỗi/schema sai/stream đứt **không** đổi
accepted state, (c) chỉ mốc `saved` mới báo candidate/draft sẵn sàng.

## 4. Trigger gốc đã kiểm lại

- **BUG-001**: Accept Short Plan → `st.rerun()` → shell render cây có chapter (không
  `StreamlitAPIException`), rerun thuần và "restart" (AppTest mới) không ghi thêm file.
- **BUG-002**: click nút Arbiter sau khi navbar đã render → workspace/navbar/page đổi
  cùng nhau, giữ nguyên project, 0 LLM call, không ghi file.
- **BUG-003**: constraint viết rỗng ⇒ **0 LLM call** + không tạo artifact Short Plan;
  sau khi lưu default viết ⇒ 1 call và có candidate. Guard cũng chạy khi gọi service
  trực tiếp (không chỉ qua UI).
- **BUG-004**: horizon nhập tay 1–4 giữ nguyên qua Accept, reload và reopen; hai arc
  `1–2`/`3–4` không bị gộp; accepted legacy (không có `planning_scope`) hiển thị cảnh
  báo riêng và không bị app tự suy horizon.
- **Legacy/default migration**: accepted Long Plan legacy chỉ được xác nhận horizon
  bằng action tường minh (`confirm_planning_scope`); mở project **không** ghi file;
  đổi default viết không rewrite Short Plan đã accepted.
- **Stream states**: `idle/connecting/non_streaming/streaming/transport_complete/validating/saved/partial/invalid/error`
  có test chuyển trạng thái (`test_generation_events.py`); partial/truncated ⇒ không
  candidate, không auto accept, không mở Review/Finalize; retry là action tường minh
  theo `operation_id` (không replay mù sau crash).
- **Full shell navigation/collapse**: nav 9 workspace, drawer project đóng/mở được,
  Arbiter compact + toggle, blocking notice vẫn thấy khi panel đóng (AppTest).
- **Fingerprint accepted**: nhiều test so `fingerprint_tree`/`fingerprint_canon` trước
  và sau rerun (T40, T22, T32) — rerun/preview/điều hướng không ghi file.

## 5. Walkthrough offline trên entrypoint thật

`test_t40_acceptance.py::test_full_shell_walkthrough_long_plan_to_chapter_two` chạy trên
`novel_ai/app.py` (không phải harness từng workspace) với dữ liệu giả và `FakeLLMClient`
scripted:

Co-create/Base Idea + foundation (seed bằng service) → **Long Plan multi-arc 1–4** →
Accept → **Short Plan** (arc `1–2`) → Accept (tạo `chapter.json`) → **Skeleton ch.1** →
Accept → **Writer ch.1** → **Human Review** → **Finalize** (`finalizing`) → **Reconcile
Generate + Accept** (`final_reconciled`, timeline 1 entry) → **Skeleton ch.2** → Accept →
**Writer ch.2** (draft complete). Tổng **7 LLM call** đúng bằng số action generate;
Human Review/Finalize/Accept không gọi LLM.

Kèm theo: hai test trigger BUG-003/004 ở trên và test surface cho
`reconcile_downstream` (transcript `saved`, chương 2 hết stale, chương 3 là bước kế tiếp).

## 6. Visual/manual matrix — đã chạy (ảnh có đường dẫn)

Chạy app thật (shell `novel_ai/ui/layout.run`, cùng code `novel_ai/app.py`) với project demo
trong `%TEMP%` (3 chapter, Long/Short candidate, Skeleton accepted + candidate, prose r1, một
project có pending recovery) và chụp bằng **Edge headless** ở các độ rộng yêu cầu. Ảnh nằm ở
[docs/design/visual/](visual/).

| Ảnh | Kích thước | Nội dung đã quan sát |
|---|---|---|
| [long-plan-arbiter-open-1366x768](visual/t40-long-plan-arbiter-open-1366x768.png) | 1366×768 | Nav 9 workspace xuống 2 dòng; drawer project ~315px; workspace + Arbiter mở (`st.columns([3.5, 1])`); status bar ở đáy; **không** còn cảnh báo Streamlit |
| [long-plan-arbiter-open-1600x900](visual/t40-long-plan-arbiter-open-1600x900.png) | 1600×900 | Như trên, thoáng hơn; bảng "Trạng thái" của Arbiter đủ chỗ |
| [long-plan-arbiter-open-1920x1080](visual/t40-long-plan-arbiter-open-1920x1080.png) | 1920×1080 | Nav 1 dòng; drawer hiện cây PROJECT (Foundation/Plans/Chapters) + Kết nối LLM; Arbiter có nút "Chuyển tới Review" |
| [writer-arbiter-closed-1366x768](visual/t40-writer-arbiter-closed-1366x768.png) | 1366×768 | Arbiter **đóng**: workspace full width, chỉ còn toggle `Arbiter · <badge>`; không cột rỗng |
| [writer-arbiter-closed-1600x900](visual/t40-writer-arbiter-closed-1600x900.png) | 1600×900 | Writer full width: chapter select, gate, draft, form Generate/Continue |
| [writer-arbiter-closed-1920x1080](visual/t40-writer-arbiter-closed-1920x1080.png) | 1920×1080 | Như trên |
| [long-plan-candidate-editor-1366x2600](visual/t40-long-plan-candidate-editor-1366x2600.png) | 1366×2600 (viewport cao để thấy hết trong một ảnh) | Candidate r2: **Preview coverage** + warning single-arc non-blocking; tabs Editor/Candidate/Accepted/JSON; card Arc với `chapter_range` sửa được; `volume_id`/`arc_id` read-only; cây drawer hiện `◆ candidate` |
| [short-plan-candidate-editor-1366x2400](visual/t40-short-plan-candidate-editor-1366x2400.png) | 1366×2400 | **Sau fix T40**: trạng thái "arc đã kín plan" vẫn hiện panel candidate; form có caption "Contract viết **hiệu lực**" (default + override), `chapter_id`/`chapter_number` read-only, `character_ids`/`world_rule_ids` multiselect |
| [skeleton-candidate-editor-1366x2400](visual/t40-skeleton-candidate-editor-1366x2400.png) | 1366×2400 | Section card: `section_id`/`chapter_id`/`chapter_number` read-only, `index`/`type`/`instruction`/`purpose` sửa được, `purpose_visibility` giữ nguyên (chưa có widget) |
| [writer-prose-editor-1366x2600](visual/t40-writer-prose-editor-1366x2600.png) | 1366×2600 | Prose **Edit | Preview**, text area 480px, "23 từ · 100 ký tự · chưa có thay đổi", Save Draft/Discard |
| [review-prose-editor-1366x2600](visual/t40-review-prose-editor-1366x2600.png) | 1366×2600 | Prose editor + "AI Generation — AI Review"/"— Rewrite Section" + panel AI Review/Rewrite |
| [architect-candidate-editor-1366x2400](visual/t40-architect-candidate-editor-1366x2400.png) | 1366×2400 | Panel candidate Architect (form schema + raw JSON); không có cảnh báo Streamlit |
| [revision-retcon-panels-1366x2400](visual/t40-revision-retcon-panels-1366x2400.png) | 1366×2400 | **Sau fix T40**: Recovery, Stale chain ("Chapter chưa `final_reconciled`" đúng nhãn), Retcon, Revise Base Idea (Edit/Preview + đếm), Revise Premise (Form/Raw) |
| [empty-state-1366x768](visual/t40-empty-state-1366x768.png) | 1366×768 | Project mới: drawer có form Tạo/Mở project; main báo "Chưa có project nào đang mở" |
| [pending-recovery-banner-1366x768](visual/t40-pending-recovery-banner-1366x768.png) | 1366×768 | Pending transaction: banner vàng + nút "Chạy recovery (backend)" hiện **dù Arbiter đóng**; toggle hiện `Arbiter · cần recovery · 1 blocker` |
| [narrow-900x800-sidebar-auto](visual/t40-narrow-900x800-sidebar-auto.png) | 900×800 | Ngoài matrix: dưới ~1100px cột Arbiter bị bó hẹp (chữ xuống dòng 2–3 từ/dòng) và nav xuống 5 dòng — xem giới hạn ở mục 7 |

**Keyboard/focus (browser thật, tự động)**: `docs/design/examples/check_keyboard_focus.py` điều
khiển Edge headless qua CDP (WebSocket client tự viết bằng stdlib, không thêm dependency) và **PASS**:

- Tab phủ **45 bước**: control thu gọn sidebar → tabs Tạo/Mở project → form tạo project → cây
  Foundation/Plans/Chapters → probe LLM → **nav radio** → **nút toggle Arbiter** → form workspace →
  action buttons, rồi wrap về đầu (không kẹt focus, control nào cũng tới được bằng Tab);
- Tab đầu tiên + **Enter** ⇒ sidebar thu gọn (bề rộng 336px → 0);
- Tab tới `▸ Arbiter · …` + **Enter** ⇒ panel Arbiter mở (`.dsh-arbiter-row` 0 → 12);
- Tab tới `Chuyển tới …` + **Enter** ⇒ đổi workspace (`long_plan` → `skeleton`);
- focus nav radio + **ArrowDown** ⇒ đổi workspace (`long_plan` → `short_plan`).

Log đầy đủ: [t40-keyboard-focus.txt](visual/t40-keyboard-focus.txt). Vì vậy tiêu chí 4 của **T32**
("keyboard/focus dùng được") đã đạt bằng bằng chứng chạy thật, không cần phiên bấm tay.

### 6.1 Lỗi tìm thấy **khi** kiểm visual và đã sửa trong T40

| # | Hiện tượng quan sát | Nguyên nhân | Sửa | Bằng chứng |
|---|---|---|---|---|
| V1 | Hộp vàng "The widget with key `novel_ai_workspace_nav` was created with a default value but also had its value set via the Session State API" ở đầu workspace | `_render_navbar` truyền `index=` cho radio trong khi `KEY_WORKSPACE_NAV` đã được gán trước đó (state có sẵn hoặc callback điều hướng Arbiter) | `novel_ai/ui/layout.py`: bỏ `index=`, chỉ để session state quyết định | ảnh 1366 trước/sau; test T28/T32 vẫn pass |
| V2 | Cùng cảnh báo vàng ở editor prose (Writer/Review) và mọi editor raw JSON | `sync_text_editor` gán `session_state[key]` rồi widget vẫn được truyền `value=` (9 chỗ: architect ×2, long_plan, short_plan, skeleton, reconcile, review rewrite, revision premise, prose) | bỏ `value=` ở cả 9 chỗ (session state đã bảo đảm giá trị) | [ảnh writer](visual/t40-writer-prose-editor-1366x2600.png) không còn cảnh báo |
| V3 | Arc đã kín plan + có candidate Short Plan ⇒ panel candidate **không render**, không sửa/accept được (trái acceptance UI-03) | `pages/short_plan.py` `return` sớm khi `rows` rỗng | render `_render_candidate` trong nhánh rỗng + thông báo rõ; thêm test `test_short_plan_candidate_still_editable_when_arc_has_no_new_chapter` | [ảnh short plan](visual/t40-short-plan-candidate-editor-1366x2400.png) |
| V4 | Revision gắn nhãn "Chapter đang `finalizing` … → workspace **Reconcile**" cho chương mới chỉ `planned`/`review_required` (sai việc cần làm) | panel gộp mọi blocker `kind == "chapter"` dù service phát hai lý do khác nhau | tách theo `suggested_action`: `finalizing` vs "Chapter chưa `final_reconciled`" (→ Review → Finalize → Reconcile); test `test_revision_blockers_distinguish_finalizing_from_locked_chapters` | [ảnh revision](visual/t40-revision-retcon-panels-1366x2400.png) |
| V5 | Test storage idempotency fail **1 lần** giữa các lần chạy full suite (junit: `test_same_operation_id_with_different_content_is_rejected`) | fixture `_accepted_premise` lấy `created_at` theo **đồng hồ thật**; hai lần gọi "cùng nội dung" lệch giây ⇒ hash khác ⇒ guard (đúng) từ chối | `tests/unit/test_storage_transaction.py`: truyền `now=_STAMP` cho `new_artifact`/`set_candidate`; đã chứng minh payload ổn định qua `sleep(1.2s)` | 20 lần chạy đơn + 6 lần chạy cả file + 3 lần full suite đều pass |

## 7. Giới hạn và việc chưa xác minh

1. **Live API**: chưa có endpoint/model/key để chạy thật; adapter chỉ được kiểm bằng transport giả
   (thành công, JSON sai, timeout, 401, 5xx, stream đứt, `finish_reason=length`). Không có tuyên bố
   tương thích provider thật.
2. **Keyboard/focus**: đã kiểm bằng browser thật (Tab/Enter/Arrow qua CDP — xem mục 6). Còn lại chỉ
   là mức độ hỗ trợ của **trình đọc màn hình** (screen reader/ARIA announcement) — chưa có phiên AT
   thật; đây là giới hạn của mọi bản build chưa test a11y chuyên sâu, không phải tiêu chí của T32/T40.
3. **Độ rộng < ~1100px**: cột Arbiter bị bó hẹp (chữ xuống dòng 2–3 từ/dòng) và nav xuống nhiều
   dòng; app nhắm desktop ≥1366px và người dùng có thể đóng Arbiter để lấy full width (D018). Ghi
   nhận, chưa xử lý responsive.
4. **Chất lượng narrative một arc**: BUG-004 sửa ở tầng contract (horizon/coverage); plan nhiều arc
   có *hay hơn* một arc hay không là đánh giá nội dung, cần provider thật + đọc bản thảo. App chỉ
   cảnh báo non-blocking khi plan chỉ có một arc, không đặt quota.
5. **Add/Remove/Reorder chưa đủ mọi editor**: Skeleton và Reconciliation có thêm–bỏ trên working
   copy; Long Plan/Short Plan chưa có nút thêm–bỏ arc/chapter (phải sửa field/range hoặc raw JSON +
   `assign_ids`/`assign_chapter_ids`).
6. **Field enum trong editor** (`purpose_visibility`, `writer_context_policy`) chưa có widget nên
   form giữ nguyên giá trị; muốn đổi phải dùng Raw JSON (có guard). Thấy rõ trong ảnh Skeleton.
7. **Chương downstream bị chặn bởi candidate Long Plan**: khi Long Plan có candidate chưa accept
   (envelope `draft`), trang Short Plan báo "chưa có Long Plan accepted" dù accepted revision vẫn là
   canon (`_common.accepted_long_plan` và `require_accepted_artifact` kiểm `status`). Đây là hành vi
   cũ, **không** đổi trong đợt fix vì ảnh hưởng guard nhiều tầng; cần một quyết định contract riêng
   (đọc theo `accepted_revision` + scope thay vì `status`) trước khi sửa.
8. **Finding T24 ngoài phạm vi** không được tuyên bố đã đóng:
   [docs/design/review-findings-t24.md](review-findings-t24.md).
9. `reconcile_downstream` hiển thị transcript nhưng chạy **non-stream** (structured); retry sau crash
   vẫn theo `operation_id`, không hứa at-most-once từ provider.

## 8. Kết luận

- BUG-001–004 và UI-01–04 đều có bằng chứng runtime đúng tầng (AppTest trên shell thật, service
  test, fixture âm) và **đã có ảnh thật** ở 1366×768 / 1600×900 / 1920×1080 với Arbiter mở và đóng,
  cùng ảnh cho editor Long/Short/Skeleton/Writer/Review/Architect/Revision, project mới và pending
  recovery (mục 6).
- Kiểm visual còn phát hiện **5 lỗi thật** (V1–V5); 4 lỗi sản phẩm đã sửa kèm test và ảnh xác nhận,
  1 lỗi test flaky đã sửa tận gốc (mục 6.1).
- Keyboard/focus đã kiểm tự động bằng browser thật (Tab/Enter/Arrow) và **PASS** (mục 6), nên tiêu
  chí 4 của T32 đã đạt.
- Suite offline hiện tại: **660 passed, 1 skipped (có lý do), 0 failed**; document check PASS.
- **T26–T40 đều `done`**: T32 đóng nốt bằng ảnh 3 độ rộng + keyboard/focus; T40 đủ điều kiện `done`
  (walkthrough, trigger gốc, coverage matrix có ảnh, inventory LLM, README + user guide, giới hạn
  được nêu).
- Chưa xác minh được (ghi rõ ở mục 7): live API, screen reader/AT chuyên sâu, cột Arbiter dưới
  ~1100px, chất lượng narrative một arc, Add/Remove arc–chapter, field enum trong editor, và việc
  Short Plan bị chặn khi Long Plan có candidate chưa accept (cần quyết định contract).
