# Findings review code T24

Ngày: 2026-09-21. Phạm vi: review **chỉ đọc** toàn bộ `novel_ai/` (core, services, ui, pages)
đối chiếu `novel_ai_spec_v0.2.md`, `docs/design/{workflow,schemas,storage,context,architecture}.md`,
`docs/prompts/v1/manifest.json` và `AGENTS.md`.

Cách làm: bốn review độc lập theo lớp (core storage/lifecycle/project; validation/context/prompts;
services; ui/pages), mỗi phát hiện phải kèm `file:line` và cách tái hiện. Các phát hiện được
kiểm chứng lại bằng script probe chạy thật trên `tmp_path` trước khi sửa.

Trạng thái cột **Xử lý**: `fixed` = đã sửa kèm regression test; `documented` = ghi nhận, chưa sửa
(có lý do).

> **Cập nhật T25** (`docs/tasks/T25-ui-layout-spec28.md`): các mục ở mục B/C dưới đây đã được sửa
> tiếp — **F-B1, F-B2, F-B6, C1, C2, C3** và một phần **F-B7** (`ContextBundle.to_snapshot` điền
> `relationship_versions`). Kiểm lại F-B3 thấy **allowlist đã được enforce** trong
> `validate_auto_accept_scope`, nên không cần sửa. F-A4 và F-A9 (đã sửa ở T24 nhưng thiếu test) nay
> có test riêng. Các mục còn `documented`: **F-B5**, phần còn lại của **F-B7**, **F-B8**, **C4**
> (kiểm lại thấy đã an toàn). Chi tiết cách sửa và test nằm trong
> [T25](T25-ui-layout-spec28.md#2-finding-t24-đã-xử-lý-trong-phiên-này).

---

## A. Bug đã sửa trong phiên này

### F-A1 — Leak secret: `WorldRule.visibility` không được enforce khi build Writer context

- **Mức độ**: nghiêm trọng (leak author truth vào prompt Writer).
- **Vị trí**: `novel_ai/core/context.py` → `_world_rule_writer_projection`.
- **Sai thế nào**: hàm fallback thẳng `writer_projection if not None else content` và **không đọc**
  `rule.visibility`. Một rule `author_only` (hoặc `planner_only`/`skeleton_only`) có hiệu lực từ
  chương đang viết và `writer_projection: null` sẽ đẩy nguyên `content` cho Writer.
  `validate_writer_projection` không bắt được vì guard chỉ soi **tên key**, không phân loại giá trị string.
- **Tái hiện (đã chạy)**: `WorldRule(world_rule_id="rule_0100", content="CO-QUAN-BI-MAT…",
  effective_from_chapter=1, visibility="author_only", writer_projection=None)` →
  projection chứa secret, `validate_writer_projection(...).is_valid == True`.
- **Vì sao test cũ không bắt**: fixture chỉ có một rule `author_only` và nó có
  `effective_from_chapter: 100`, nên bị lọc theo hiệu lực chương — test pass vì lý do sai.
- **Đã sửa**: chỉ gửi `content` khi `visibility == writer_safe`; rule khác chỉ gửi `writer_projection`
  nếu được khai tường minh, ngược lại bỏ hẳn field (không gửi `null`).
- **Test**: `tests/unit/test_context.py::test_writer_projection_never_sends_non_writer_safe_world_rule_content`
  (parametrize 3 visibility) và `..._uses_explicit_writer_projection_for_author_only_rule`.
- **Xử lý**: `fixed`.

### F-A2 — `revision._abort_quietly` đệ quy chính nó (không bao giờ abort)

- **Mức độ**: nghiêm trọng.
- **Vị trí**: `novel_ai/services/revision.py::_abort_quietly` gọi `_abort_quietly(handle)` thay vì
  `handle.abort()`.
- **Sai thế nào**: mọi lần gọi đều `RecursionError`; `except Exception` của hàm nuốt lỗi đó nên
  transaction không được abort. `_save_error_record` vì vậy vẫn `return relpath` và caller báo
  "error record đã lưu" trong khi file không tồn tại.
- **Vì sao test cũ không bắt**: ở nhánh replace-failed, `commit_operation` **đã tự** abort và release
  lock trước khi raise, nên test chỉ kiểm lock/pending vẫn pass dù `_abort_quietly` hỏng.
- **Đã sửa**: `handle.abort()` (giống `reconcile._abort_quietly`).
- **Test**: `tests/integration/test_reconcile_revision.py::test_failed_revision_commit_aborts_cleanly_and_releases_lock`
  và `::test_revise_base_idea_crash_between_targets_recovers_without_duplication`.
- **Xử lý**: `fixed`.

### F-A3 — `_state_chain_guard` dùng `max()` nên guard consistency vô hiệu

- **Mức độ**: nghiêm trọng.
- **Vị trí**: `novel_ai/core/lifecycle.py::_state_chain_guard` (timeline và relationship).
- **Sai thế nào**: `consistent = max(latest_consistent_chapter, latest_final_chapter)`. Sau retcon,
  `latest_consistent_chapter` bị **hạ** xuống chương retcon nhưng `latest_final_chapter` giữ nguyên
  (đúng luật: không ghi lùi state). Lấy `max` khiến `consistent` luôn bằng `latest_final_chapter`,
  nên `guard_writer` cho Writer chạy trên state chain cũ.
- **Tái hiện (đã chạy)**: timeline `latest_final_chapter=3`, `latest_consistent_chapter=1`; chương 2
  và 3 `final_reconciled`; `guard_writer(ch_0004)` trả `allowed=True` (phải là `False`).
- **Đã sửa**: dùng `latest_consistent_chapter or latest_final_chapter` (fallback khi chưa set),
  khớp `_State.latest_consistent_chapter()` của context builder.
- **Test**: `tests/unit/test_lifecycle.py::test_state_chain_guard_blocks_after_retcon_lowered_consistency`.
- **Xử lý**: `fixed`.

### F-A4 — Accept Short Plan hạ chapter đang có prose về `planned`

- **Mức độ**: nghiêm trọng (metadata nói ngược dữ liệu thật trên disk).
- **Vị trí**: `novel_ai/services/short_planner.py::_chapter_metadata_updates`, nhánh `existing is not None`.
- **Sai thế nào**: luôn set `status=ChapterStatus.planned` cho mọi chapter trong payload không locked.
  Chương đang `draft`/`review_required`/`skeleton_ready` bị hạ về `planned` dù `drafts` vẫn còn;
  Arbiter/UI coi như chưa viết, và `guard_review` vẫn cho qua vì nó chỉ đọc `current_draft`.
- **Đã sửa**: giữ nguyên `status` của chapter đã tồn tại; chỉ cập nhật `title`,
  `previous_chapter_id`, `short_plan_pin`.
- **Xử lý**: `fixed` (được phủ bởi suite hiện có; chưa thêm test riêng — xem mục D).

### F-A5 — Retry cùng `operation_id` với nội dung khác ghi đè accepted revision

- **Mức độ**: nghiêm trọng (vi phạm idempotency + silent overwrite).
- **Vị trí**: `novel_ai/core/storage.py::{_is_already_applied, save_artifact, _write_bytes_with_history}`.
- **Sai thế nào**: hai lỗ hổng cộng lại:
  1. `_is_already_applied` chỉ trả `True` khi target **đã bằng** nội dung mới; nó không kiểm
     "operation này đã commit target này chưa".
  2. Write một file chỉ ghi ledger khi target **đã tồn tại** (`had_previous`), nên thao tác tạo file
     mới không để lại dấu vết nào trong `.ops/done`.
  Hệ quả: gọi `save_artifact(env r1 "A", op_1)` rồi `save_artifact(env r2 "B", op_1)` **thành công**,
  revision 1 bị thay bằng revision 2.
- **Tái hiện (đã chạy)**: probe in ra `sau lan 2 (cung operation_id, noi dung khac): 2 B`.
- **Đã sửa**: thêm `_committed_hash_for` + `_check_operation_idempotency` (raise `stale_candidate`
  khi cùng `operation_id` đã commit target đó với hash khác) và ghi `.ops/done` cho **mọi** write
  một file (kể cả tạo file mới) để ledger đủ dữ liệu.
- **Hệ quả lên test cũ**: `tests/unit/test_lifecycle.py::test_repeated_accept_same_operation_does_not_duplicate_revision`
  trước đây dùng **một** `operation_id` cho hai write khác nhau (ghi candidate, rồi accept). Đã
  tách thành `op_premise_candidate` / `op_premise_accept` — đúng cách tầng service làm (mỗi bước một
  `operation_id` riêng, ví dụ `f"{op_id}.proposal"` rồi `f"{op_id}.accept"`).
- **Test**: `tests/unit/test_storage_transaction.py::test_same_operation_id_with_different_content_is_rejected`.
- **Xử lý**: `fixed`.

### F-A6 — `history/<op>/before/**` bị ghi đè bằng nội dung **sau** commit

- **Mức độ**: bug nhỏ nhưng phá audit/recovery.
- **Vị trí**: `novel_ai/core/storage.py::{_record_history_for_transaction, commit_operation, recover_pending}`.
- **Sai thế nào**: `_record_history_for_transaction` copy từ `project.root/<target>` (state **hiện tại**)
  và được gọi **lần thứ hai sau khi đã replace xong** mọi target (trong `commit_operation` và trong
  `recover_pending`), nên bản `before` bị copy đè bằng nội dung sau commit.
- **Tái hiện (đã chạy)**: crash giữa commit reconciliation rồi `recover_pending` →
  `history/<op>/before/chapters/ch_0001/chapter.json` có `status = final_reconciled` thay vì
  `finalizing`.
- **Đã sửa**: guard "chỉ ghi `before` nếu chưa tồn tại" trong `_record_history_for_transaction`, và
  bỏ hai lần gọi sau commit.
- **Test**: `tests/integration/test_reconcile_revision.py::test_history_before_snapshot_survives_crash_and_recovery`.
- **Xử lý**: `fixed`.

### F-A7 — `_merge_with_accepted` bỏ toàn bộ chapter của arc cũ

- **Mức độ**: bug nhỏ (mất dữ liệu plan).
- **Vị trí**: `novel_ai/services/short_planner.py::_merge_with_accepted` + `accept`.
- **Sai thế nào**: khi `candidate.arc_id != previous.arc_id` hàm `return candidate`, ném bỏ mọi chapter
  của arc cũ khỏi Short Plan trong khi metadata chapter vẫn còn và vẫn pin plan cũ.
- **Đã sửa**: `_guard_arc_replacement` từ chối accept khi candidate nhắm arc khác mà chapter của arc
  cũ vẫn đang dùng Short Plan (chưa final/finalizing), kèm `orphaned_chapter_ids` trong `details`.
  Từ chối rõ ràng thay vì âm thầm bỏ dữ liệu.
- **Xử lý**: `fixed`.

### F-A8 — `assert ctx.llm_client is not None` làm page Reconcile crash

- **Mức độ**: nghiêm trọng ở tầng UI (vi phạm luật "guard phải ở backend").
- **Vị trí**: `novel_ai/pages/reconcile.py::_render_actions_panel`.
- **Sai thế nào**: `disabled=not llm_ready` chỉ chặn chuột; bàn phím/AppTest vẫn kích hoạt được nút,
  và khi đó `assert` raise `AssertionError` không được bắt → page crash traceback thay vì báo lỗi
  cấu hình. Các page khác (Writer/Review/Skeleton) đã xử lý đúng bằng nhánh báo lỗi.
- **Đã sửa**: thay `assert` bằng nhánh `st.error(...)` + `return` trước khi vào action.
- **Xử lý**: `fixed`.

### F-A9 — Skeleton: `foreshadow_surfaces[].foreshadow_id` không kiểm hiệu lực chương

- **Mức độ**: bug nhỏ (đường leak lore tương lai qua surface).
- **Vị trí**: `novel_ai/core/validation.py::_validate_skeleton`.
- **Sai thế nào**: `section.foreshadow_ids` có truyền `chapter_number` cho `check_reference` nhưng
  `foreshadow_surfaces` thì không, nên surface trỏ foreshadow hiệu lực chương 100 vẫn `valid` ở
  chương 1 và `surface_instruction` của nó đi thẳng vào Writer projection.
- **Tái hiện (đã chạy)**: Skeleton chương 1 có `foreshadow_surfaces=[{foreshadow_id: fs_0100
  (effective 100)}]` và không khai `foreshadow_ids` → `validate_artifact_payload("skeleton", ...)`
  trả `valid`; nếu khai cùng ID trong `foreshadow_ids` thì bị bắt (`effective_from_future`).
- **Đã sửa**: truyền `chapter_number=chapter_number` cho `check_reference` của surface.
- **Xử lý**: `fixed` (chưa thêm test riêng — xem mục D).

---

## B. Phát hiện đã kiểm chứng nhưng **chưa** sửa

### F-B1 — `_context_basis` bỏ qua chuỗi consistency sau retcon

- **Mức độ**: nghiêm trọng về mặt lý thuyết (plan/Skeleton dựng trên state thiếu).
- **Vị trí**: `novel_ai/core/context.py::_context_basis`.
- **Sai thế nào**: dùng `latest_final_reconciled_number()` (đọc status `chapter.json`) và không trừ
  phần state đã bị đánh dấu stale sau retcon. Sau retcon, chapter sau vẫn `final_reconciled` nên
  basis khai `actual` cho một chương mà `timeline_as_of` thực tế chỉ còn tới chương retcon.
  Vì `preparation_context=None`, `skeleton.accept` không chặn candidate đó.
- **Đã thử sửa và revert**: đổi sang `state.latest_consistent_chapter()` làm hỏng hai test đang mô
  tả luồng provisional (`test_skeleton_context_uses_provisional_when_previous_chapter_not_final`,
  `test_skeleton_maps_temporary_ids_and_refuses_provisional_accept`). Sửa đúng cần đụng tới cả
  luồng provisional của Short Plan/Skeleton (F-B2) nên **không** vá nửa vời trong phiên này.
- **Xử lý**: `fixed` (T25). `models.consistent_chapter_number` hạ trần giá trị dẫn xuất của
  timeline/relationship xuống chương `final_reconciled` **thật sự** theo metadata, và
  `_context_basis` dùng hàm đó; `lifecycle._state_chain_guard` cùng `short_planner` dùng chung để
  không lệch luật. Test: `test_context.py` (provisional case cập nhật) +
  `test_skeleton_writer_services.py::test_skeleton_maps_temporary_ids_and_refuses_provisional_accept`.

### F-B2 — Short Plan thiếu guard provisional và không ghi `preparation_context`

- **Vị trí**: `novel_ai/services/short_planner.py` (`generate` không set
  `ArtifactRevision.preparation_context`; `accept` không kiểm mode provisional).
- Skeleton đã có guard tương ứng (`novel_ai/services/skeleton.py::accept`). Short Plan thì chưa,
  nên candidate dựng từ actual thiếu vẫn accept được qua đường thủ công.
- **Xử lý**: `fixed` (T25). `generate` ghi `preparation_context` (qua tham số mới
  `preparation_context` của `lifecycle.set_candidate`); `accept` gọi `_guard_provisional_candidate`.
  Guard chỉ chặn khi bridge mô tả chương **đã có prose** — chặn mọi candidate provisional sẽ phá
  luồng bình thường của MVP (lập plan trước khi viết). Đồng thời basis của Short Plan tính theo
  chapter lớn nhất trong range; `generate` lọc chapter đã `finalizing`/`final_reconciled` khỏi
  `assigned_chapters`. Test: `test_planning_services.py::{test_short_plan_provisional_candidate_carries_preparation_context,
  test_short_plan_provisional_candidate_is_not_acceptable,
  test_short_plan_actual_candidate_is_acceptable_after_previous_chapter_final}`.

### F-B3 — `validate_auto_accept_scope` đã có allowlist nhưng chưa enforce

- **Vị trí**: `novel_ai/core/validation.py`. `AUTO_ACCEPT_ALLOWED_OUTPUT_KINDS` được khai báo và
  export nhưng hàm chỉ loại trừ `AUTO_ACCEPT_FORBIDDEN_OUTPUT_KINDS`; `output_kind` ngoài allowlist
  (ví dụ `review_report`, hoặc typo) vẫn được coi là hợp lệ.
- **Ghi chú**: đã thử enforce trong phiên này và revert cùng lúc với các thay đổi khác để giữ diff
  gọn; hành vi hiện tại không bị lộ qua caller thật (mọi caller truyền kind hợp lệ).
- **Xử lý**: `documented`. **Kiểm lại ở T25**: hàm **đã** enforce allowlist
  (`if output_kind not in AUTO_ACCEPT_ALLOWED_OUTPUT_KINDS: ...`), ghi chú cũ không còn đúng; không
  cần sửa. Vẫn nên có test trực tiếp cho nhánh này khi làm task validation.

### F-B4 — trùng F-B3 (cùng một phát hiện, ghi hai lần trong bản nháp review)

- **Xử lý**: gộp vào F-B3. Giữ mục này để số ID không đổi khi trích dẫn.
### F-B5 — Single-file write không acquire `project.lock`

- **Vị trí**: `novel_ai/core/storage.py::_check_write_lock` chỉ **đọc** lock; chỉ `begin_operation`
  mới `acquire_lock`. Hai write một file chạy song song (hai phiên Streamlit) đều thấy "chưa có lock".
- **Đối chiếu contract**: `storage.md` mục 4 nói mọi write action first-class tạo lock.
- **Vì sao chưa sửa**: đổi sang acquire/release cho từng write một file là thay đổi rộng, ảnh hưởng
  mọi service và test lock hiện có; cần một task riêng có review.
- **Xử lý**: `documented`.

### F-B6 — `accept_candidate` chấp nhận candidate `not_checked`

- **Vị trí**: `novel_ai/core/lifecycle.py::accept_candidate`. Chỉ raise khi
  `validation.state is invalid`; `ValidationResult()` mặc định là `not_checked`, nên backend không
  tự enforce "phải validate trước khi merge". Mọi caller service hiện tại đều truyền `validation=`
  cụ thể, nên đây là guard lỏng chứ chưa phải đường leak đã kiểm chứng.
- **Xử lý**: `fixed` (T25). `accept_candidate` raise `LifecycleError(code="validation_required")` khi
  state là `not_checked`. Test:
  `tests/unit/test_lifecycle.py::test_accept_candidate_refuses_unchecked_validation`.

### F-B7 — Snapshot/UI Khác

- `save_snapshot` tái dùng đúng relpath khi `snapshot_id` đã tồn tại ⇒ cùng loại rủi ro ghi đè như F-A5
  nhưng cho snapshot (chưa dựng được probe riêng).
- `Project.update_config` ghi `project.json` không qua lock và không snapshot history.
- `reconcile._next_chapter_snapshot` chỉ điền `effective_character_ids`; `effective_world_rule_ids`,
  `effective_foreshadow_ids`, excluded tương ứng và `writer_projection_hash` để rỗng.
- `reconcile.finalize_chapter` không kiểm previous-chapter guard.
- `context.build_review_context` bịa `revision=1` cho constraint source khi artifact chưa accepted.
- `context.to_snapshot` luôn ghi `relationship_versions: []` vì `included_ids` không có key
  `"relationships"`.
- `validate_plan_does_not_mutate_state` chỉ bắt key `timeline` khi node anh em có `chapter_number`.
- `prompts.py`: `writing_style_id`/`genre_prompt_id` sai định dạng bị coi là nội dung reference thay
  vì báo `unknown_reference`.
- **Xử lý**: `documented`, **trừ** `context.to_snapshot` đã `fixed` ở T25
  (`ContextBundle._relationship_version_refs` suy từ payload `relationships_as_of`).
  Các mục còn lại vẫn là việc của task riêng vì mỗi mục là một thay đổi rời ở tầng storage/service.

### F-B8 — Nit và dead code

- `context._State._accepted`/`skeleton` so sánh enum bằng string (`== "stale"`) thay vì `is ArtifactStatus.*`.
- `_PLAN_MUTATION_KEYS` trong `validation.py` là dead code.
- `storage.commit_operation` đọc `staged.read_bytes()` rồi verify bằng `file_fingerprint` (đúng kết
  quả, chỉ kém rõ ràng).
- **Xử lý**: `documented`.

---

## C. UI (đã kiểm chứng; C1–C3 sửa ở T25, C4 kiểm lại thấy an toàn)

- **C1 — Kết quả action rò giữa workspace**: `novel_ai/ui/__init__.py` dùng **một** `ACTION_RESULT_KEY`
  toàn cục, không gắn workspace. Đã tái hiện bằng AppTest trên `novel_ai/app.py` thật: thông báo +
  warning của Writer hiện trong workspace Review (page Review render trước sẽ hiển thị rồi xoá key,
  nên quay lại Writer không còn thấy kết quả của chính action đó).
  `_chapter_ui.set_note/show_note` làm đúng vì key có `{workspace}` — nên sửa theo cùng pattern.
  → `fixed` (T25): `ACTION_RESULT_KEY_TEMPLATE` + `action_result_key(workspace)`, có tham số
  `workspace` tường minh cho caller không đi qua page. Test:
  `test_app_entrypoint.py::test_action_result_does_not_leak_between_workspaces`.
- **C2 — `short_plan.py` ghi `st.session_state` trong lúc render** và cache `assigned_chapters`
  không mang revision Long Plan, nên sau khi revise Long Plan UI vẫn gửi `assigned_chapters` cũ vào
  `short_planner.generate`.
  → `fixed` (T25): `assigned_cache_key(arc_id, long_plan_revision)`.
- **C3 — architect/long_plan/short_plan generate không truyền `operation_id`**, nên bấm lặp sinh
  thêm LLM call và thay candidate đang xem (Writer/Skeleton/Review/Reconcile đã truyền
  `stable_operation_id`).
  → `fixed` (T25): cả ba page dùng `stable_operation_id` + `run_action` + input "Lần chạy". Test:
  `test_foundation_planning_ui.py::test_architect_generate_repeat_click_does_not_call_llm_twice`.
- **C4 — editor proposal reconcile** dùng key không có `chapter_id` (hiện an toàn nhờ `version` có
  `chapter_id`). → `documented`; kiểm lại ở T25 thấy `version` đã mang `chapter_id` nên không đổi.
- **Xử lý**: `fixed` cho C1–C3, `documented` cho C4.

---

## D. Việc nên làm tiếp (đề xuất thứ tự)

> Cập nhật T25: mục 1–4 và 6–7 dưới đây **đã xong** (xem
> [T25](T25-ui-layout-spec28.md)); thứ tự còn lại bắt đầu từ mục 5.

1. ~~F-B1 + F-B2 cùng nhau~~ → xong ở T25.
2. ~~C1: scope `ACTION_RESULT_KEY` theo workspace~~ → xong ở T25.
3. ~~C3: đưa architect/long_plan/short_plan generate qua `run_action` + `stable_operation_id`~~ → xong ở T25.
4. ~~F-B3: enforce `AUTO_ACCEPT_ALLOWED_OUTPUT_KINDS`~~ → kiểm lại: đã enforce; chỉ còn thiếu test trực tiếp.
5. F-B5: quyết định acquire lock cho single-file write hay ghi rõ trong `storage.md` rằng lock chỉ
   áp cho multi-file commit.
6. ~~F-B6: `accept_candidate` tự chạy `validate_artifact_payload` khi `not_checked`~~ → xong ở T25
   (từ chối `not_checked` bằng `validation_required`).
7. ~~Thêm test riêng cho F-A4 và F-B4~~ → xong ở T25 (F-A4 và F-A9; F-B4 gộp vào F-B3).
8. Phần còn lại của F-B7 (snapshot/writer/reviewer/reconcile) và F-B8 (nit, dead code).
9. Bổ sung test trực tiếp cho `validate_auto_accept_scope` (allowlist + forbidden).

---

## E. Ghi chú về phạm vi

- Review này **không** kết luận gì về chất lượng ngữ nghĩa/văn chương của output LLM.
- Live API vẫn **chưa** xác minh; mọi kiểm tra ở trên chạy offline với `FakeLLMClient`/`tmp_path`.
- Test tự động không chứng minh được invariant 30 (không subsystem nào thành agent) và 31 (không
  thêm công nghệ ngoài rule + JSON); hai mục đó vẫn dựa trên code review.
