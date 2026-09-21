# Xử lý sự cố — Manual AI Novel

Tài liệu này liệt kê lỗi thực tế của app, nguyên nhân và cách xử lý. Mọi mục đều bám vào hành vi
đã có trong code (mã lỗi ổn định trong `novel_ai`), không phải suy đoán.

- [Nguyên tắc chung](#nguyên-tắc-chung)
- [Nhóm 1 — Lỗi cấu hình và khởi động](#nhóm-1--lỗi-cấu-hình-và-khởi-động)
- [Nhóm 2 — Lỗi LLM và output](#nhóm-2--lỗi-llm-và-output)
- [Nhóm 3 — Lỗi schema và dữ liệu](#nhóm-3--lỗi-schema-và-dữ-liệu)
- [Nhóm 4 — Guard chặn action](#nhóm-4--guard-chặn-action)
- [Nhóm 5 — Transaction, pending và recovery](#nhóm-5--transaction-pending-và-recovery)
- [Nhóm 6 — Stale và retcon](#nhóm-6--stale-và-retcon)
- [Nhóm 7 — UI và Streamlit](#nhóm-7--ui-và-streamlit)
- [Nhóm 8 — Test](#nhóm-8--test)
- [Khi cần xem trực tiếp dữ liệu](#khi-cần-xem-trực-tiếp-dữ-liệu)

## Nguyên tắc chung

Ba câu quyết định cách xử lý mọi lỗi:

1. **Accepted state không bị lỗi làm hỏng.** API lỗi, schema sai hay stream đứt chỉ để lại raw
   output / partial draft / error record; canon giữ nguyên.
2. **App không tự sửa dữ liệu của bạn.** Không tự reset JSON hỏng, không tự chuyển `schema_version`,
   không tự regenerate artifact `stale`.
3. **Đừng sửa file JSON khi app đang mở.** App fingerprint nội dung file; sửa tay có thể làm project
   vào trạng thái cần recovery thủ công.

Trước khi làm gì, hãy xem panel **Arbiter** và banner recovery: chúng nói trạng thái thật và bước
gợi ý. Nếu cần, bấm *Đọc lại state từ file* để render lại từ disk.

## Nhóm 1 — Lỗi cấu hình và khởi động

### `Chưa dựng được LLM client` / sidebar báo lỗi cấu hình

- **Nguyên nhân**: `NOVEL_AI_USE_FAKE_LLM=false` nhưng thiếu `NOVEL_AI_API_BASE_URL` hoặc
  `NOVEL_AI_MODEL`, hoặc giá trị không hợp lệ.
- **Xử lý**: chạy `python -m novel_ai.config` để xem cấu hình đang dùng (giá trị secret chỉ hiện
  `set`/`not set`), sửa `.env`, rồi **khởi động lại** app. App config được cache một lần cho UI.
- Lưu ý: nút action vẫn có thể bị kích hoạt bằng bàn phím dù đang `disabled`; page sẽ báo lỗi cấu
  hình thay vì crash.

### `Không load được prompt registry v1`

- **Nguyên nhân**: thiếu/không đọc được `docs/prompts/v1/manifest.json`, hoặc `NOVEL_AI_PROMPT_ROOT`
  trỏ sai.
- **Xử lý**: kiểm tra `NOVEL_AI_PROMPT_ROOT` (mặc định `docs/prompts/v1`) và file `manifest.json`.
  Runtime **không** fallback sang prompt cũ trong `docs/prompts/*.md`; thiếu manifest là lỗi cứng.

### `Không tạo được project` / `project_exists`

- **Nguyên nhân**: slug lấy từ tiêu đề đã tồn tại trong `projects/`.
- **Xử lý**: đổi tiêu đề, hoặc mở project cũ ở tab *Mở project*. App **không** ghi đè dữ liệu người
  dùng, kể cả khi bạn cố tạo lại cùng slug.

### `Định danh project không hợp lệ` / `Đường dẫn thoát project root`

- **Nguyên nhân**: tên project/slug hoặc `chapter_id` chứa dấu phân cách, `..`, ký tự Windows không
  hợp lệ, hoặc absolute path.
- **Xử lý**: dùng tên đơn giản. Đây là guard bảo vệ, không phải bug.

## Nhóm 2 — Lỗi LLM và output

### Timeout / 401 / 5xx / network error

- **Hành vi**: exception được map thành lỗi service có `code` ổn định, thông báo đã redact secret.
  Accepted state không đổi.
- **Xử lý**: kiểm tra endpoint/model/key và `NOVEL_AI_LLM_TIMEOUT_SECONDS`, rồi chạy lại action.
  Retry cùng `operation_id` là idempotent nên không tạo revision trùng.

### `Writer không trả prose (output rỗng hoặc chỉ là thông báo lỗi)`

- **Nguyên nhân**: model trả text rỗng, hoặc một câu từ chối/không đủ dữ liệu (heuristic: output
  ngắn, một đoạn, chứa dấu hiệu từ chối).
- **Hành vi**: app **không** tạo prose revision và **không** nhét thông báo lỗi vào manuscript; chỉ
  ghi operation record `reason="no_prose_output"` và lưu raw output.
- **Xử lý**: xem raw output trong `raw/YYYY-MM-DD/` để hiểu model trả gì, rồi Regenerate (có thể thêm
  `user_instruction`) hoặc kiểm tra Skeleton/context.

### Stream đứt giữa chừng

- **Hành vi**: draft được giữ ở trạng thái **partial** (`is_complete=False`), chapter ở `draft`.
  **Không** mở Review/Finalize. Raw output được lưu.
- **Xử lý**: bấm *Continue* để viết tiếp từ tail của draft hiện tại, hoặc *Regenerate*, hoặc
  *Discard*. Continue tạo prose revision mới nối vào bản cũ.

### Draft hiển thị không phải revision tôi vừa sửa

- **Nguyên nhân**: mỗi lần Save/Regenerate/Continue tạo **revision mới**; Human Review cũ mất hiệu lực.
- **Xử lý**: xem danh sách revision trong workspace Review/Writer và chọn đúng revision trước khi
  xác nhận Human Review.

## Nhóm 3 — Lỗi schema và dữ liệu

### `missing_field` / `unknown_field` / `invalid_enum` / `invalid_stable_id`

- **Nguyên nhân**: LLM trả thiếu field, thêm field lạ, sai enum, hoặc dùng tên hiển thị làm ID.
  Model dùng `extra="forbid"` nên field lạ fail rõ ràng thay vì bị bỏ qua âm thầm.
- **Hành vi**: candidate **không** được merge; raw output + error record (có `path` JSON Pointer)
  được giữ. Accepted state không đổi.
- **Xử lý**: xem lỗi theo `path`, sửa JSON tay nếu action cho phép (ví dụ reconciliation), hoặc
  Regenerate. Với action auto-accept, candidate sai schema đơn giản là không được merge.

### `schema_version` không được hỗ trợ

- **Nguyên nhân**: file có `schema_version` khác bản build hỗ trợ, hoặc thiếu hẳn.
- **Hành vi**: app **từ chối** đọc kèm thông báo; **không** tự chuyển đổi và **không** ghi đè.
- **Xử lý**: khôi phục file từ backup/`history/`, hoặc dùng đúng bản app đã tạo ra dữ liệu đó.
  Không có migration framework trong MVP.

### `JSON hỏng ở dòng N`

- **Nguyên nhân**: file JSON bị sửa tay hoặc ghi dở (do công cụ ngoài).
- **Xử lý**: khôi phục bản cũ từ `history/<operation_id>/before/<path>` (bản này được ghi **trước**
  khi thay accepted revision), hoặc từ backup. App sẽ không tự reset.

### `artifact_id_mismatch` / `artifact_type_mismatch`

- **Nguyên nhân**: file nằm sai vị trí so với `artifact_id`/`artifact_type` bên trong (thường do
  copy/rename tay).
- **Xử lý**: đặt file về đúng đường dẫn theo `docs/design/storage.md` mục 2. App không tự di chuyển
  hay sửa dữ liệu.

## Nhóm 4 — Guard chặn action

Các thông báo dưới đây là **guard backend** — chúng chặn cả khi bạn gọi service trực tiếp, không chỉ
ở UI. Đây là hành vi đúng, không phải bug.

| Mã lỗi | Nghĩa | Cách xử lý |
|---|---|---|
| `chapter_missing` | Chưa có `chapter.json` cho chapter đó. | Accept Short Plan để tạo chapter metadata. |
| `skeleton_missing` | Chưa có Skeleton accepted cho chapter. | Generate + Accept Skeleton. |
| `skeleton_pin_missing` | Chapter chưa được gắn Skeleton (chưa accept Skeleton cho chapter này). | Accept Skeleton. |
| `skeleton_pin_mismatch` | Skeleton đã lên revision mới nhưng pin của chapter chưa cập nhật. | Accept lại Skeleton (hoặc reaccept) để cập nhật pin. |
| `skeleton_stale` | Skeleton bị đánh dấu `stale` do upstream đổi. | Review rồi reaccept, hoặc regenerate Skeleton. |
| `previous_chapter_not_finalized` | Chương trước chưa `final_reconciled`. | Finalize + Accept reconciliation cho chương trước. |
| `timeline_not_consistent` / `relationship_not_consistent` | State chain chưa nhất quán tới chương trước (thường sau retcon). | Chạy `reconcile_downstream` cho các chương sau theo thứ tự. |
| `timeline_entry_stale` / `relationship_stale` | Entry state thuộc chain cũ sau retcon. | Rebuild bằng `reconcile_downstream`. |
| `draft_missing` / `draft_incomplete` | Chưa có prose revision, hoặc draft là partial. | Generate/Continue tới khi draft `complete`. |
| `human_review_missing` / `human_review_stale` | Chưa review revision hiện tại. | Review rồi xác nhận Human Review cho đúng revision. |
| `chapter_already_final` | Chapter đã `final_reconciled`. | Sửa bản final phải đi qua action **retcon**. |
| `chapter_finalizing` | Chapter đang trong transaction finalize. | Hoàn tất reconciliation, hoặc Cancel finalizing. |
| `stale_dependency` | Dependency pin lệch revision hiện tại. | Review/reaccept artifact stale hoặc regenerate. |
| `provisional_not_writer_ready` | Candidate dựng từ context `provisional` (chưa đủ actual). | Chờ chương trước `final_reconciled` rồi regenerate. |
| `provisional_candidate_not_writer_ready` | Short Plan candidate dựa trên `planned_bridge` của chương **đã có prose** (ý định cũ đã bị actual vượt qua). | Regenerate/review Short Plan trên context `actual` rồi accept lại. |
| `validation_required` | Candidate chưa có kết quả validation (`not_checked`); backend không merge. | Để action của app chạy lại (nó validate trước khi accept), hoặc validate payload trước khi gọi `accept`. |
| `arc_replacement_not_allowed` | Candidate Short Plan nhắm arc khác trong khi chapter của arc cũ vẫn dùng plan đó. | Hoàn tất/replace các chapter cũ trước, hoặc generate Short Plan cho đúng arc. |
| `unknown_arc` / `unknown_reference` | ID không resolve trong accepted artifact. | Kiểm tra ID có thật và đã accepted chưa. |
| `effective_from_future` | Entity chỉ hiệu lực từ chương sau. | Sửa `effective_from_chapter` hoặc chọn entity khác. |
| `missing_dependency` | Chưa có upstream bắt buộc (Base Idea/Premise/Long Plan/Short Plan). | Hoàn tất và accept tầng trên trước. |
| `path_outside_project` | Đường dẫn thoát project root. | Dùng ID/tên hợp lệ. |

## Nhóm 5 — Transaction, pending và recovery

### Banner *"Có transaction dở cần recovery trước khi ghi tiếp"*

- **Nguyên nhân**: app bị đóng/crash giữa lúc commit nhiều file (ví dụ đang finalize).
- **Xử lý**: bấm *Chạy recovery (backend)*. Recovery hoàn tất commit đã staged, release lock, và
  gọi lần hai là idempotent (không nhân đôi timeline/relationship).
- **Kiểm tra**: sau recovery, `needs_recovery` phải về false; nếu không, xem mục dưới.

### Banner *"Project đang read-only"* / cần recovery thủ công

- **Nguyên nhân**: target bị sửa ngoài app, thiếu staged file, hoặc manifest không đọc được. App
  **không** đoán và **không** ghi đè.
- **Xử lý**:
  1. Xem `.ops/pending/<operation_id>/manifest.json` để biết `write_set`, `expected_old_hash`,
     `staged_hash`, `applied_paths`.
  2. So với file thật trong project và bản `history/<operation_id>/before/...`.
  3. Khôi phục file về đúng một trong hai trạng thái (trước hoặc sau commit) bằng tay.
  4. Xoá thư mục pending khi đã nhất quán, rồi bấm *Đọc lại state từ file*.
- Sao lưu **cả** thư mục project (gồm `.ops/` và `history/`) trước khi sửa tay.

### `Project đang bị khóa bởi op_xxx`

- **Nguyên nhân**: còn `.locks/project.lock` của một write action.
- **Hành vi**: lock quá `LOCK_STALE_SECONDS` (900s) **không** kèm pending manifest được xoá an toàn
  khi acquire; nếu còn pending thì phải recovery trước.
- **Xử lý**: chạy recovery. Chỉ xoá `.locks/project.lock` bằng tay khi chắc chắn không còn tiến trình
  nào đang ghi và `.ops/pending/` trống.

### `stale_candidate` khi lưu artifact

- **Nguyên nhân**: bạn đang ghi dựa trên revision cũ hơn bản trên disk, hoặc retry cùng
  `operation_id` nhưng nội dung khác.
- **Xử lý**: đọc lại state (bấm *Đọc lại state từ file*), rồi regenerate/accept lại với
  `operation_id` **mới**. Không cố ghi đè.

## Nhóm 6 — Stale và retcon

### Artifact hiển thị `STALE`

- **Nghĩa**: artifact từng hợp lệ nhưng upstream (Base Idea/Premise/Long Plan/…) đã thay đổi.
  Không phải "nội dung sai" và không mất dữ liệu.
- **Xử lý**: đọc `stale_reasons` (trong file envelope) để biết nguồn, rồi hoặc **reaccept** (nếu nội
  dung cũ vẫn đúng) hoặc **regenerate**. App không tự làm.

### Sau retcon, Writer các chương sau bị khóa

- **Nguyên nhân**: retcon hạ `latest_consistent_chapter` và đánh dấu state các chương sau `stale`.
- **Xử lý**: dùng *reconcile_downstream* cho từng chương sau **theo thứ tự tăng dần**. Mỗi lần rebuild
  một chương sẽ đưa consistency tiến lên; Writer mở lại khi chain đủ.

### Skeleton/Short Plan bị đánh dấu stale quá rộng

- **Nguyên nhân đã biết**: `mark_downstream_stale` đánh dấu theo **family** (toàn bộ Short Plan /
  Skeleton) chứ không theo từng arc/chapter.
- **Xử lý**: review/reaccept những artifact thực sự không bị ảnh hưởng. Đây là hạn chế đã ghi nhận,
  không phải dữ liệu sai.

## Nhóm 7 — UI và Streamlit

### App không thấy thay đổi tôi vừa ghi bằng tay

- Cây project có selectbox **Node đang xem** và app đọc lại state từ file mỗi lần render; chỉ cần
  đổi workspace hoặc bấm bất kỳ widget nào để rerun. State bền nằm ở file project;
  `st.session_state` chỉ giữ UI working state và không tự theo dõi thay đổi ngoài app.

### Sửa `.env` nhưng app không đổi hành vi

- App config được cache một lần cho mỗi phiên UI. **Khởi động lại** app.

### Thông báo kết quả action hiện ở workspace khác

- Từ T25 kết quả action được scope **theo workspace** (`action_result_key(workspace)`), nên không còn
  rò sang workspace khác. Kết quả vẫn chỉ hiển thị **một lần**: page đọc rồi xóa khỏi session state.
- Nếu bạn đổi workspace ngay sau khi bấm action, quay lại workspace đó để xem thông báo; state thật
  vẫn kiểm được trong cây project. Bấm lại action là idempotent nên không tạo revision trùng.

### Bố cục màn hình khác mô tả trong spec mục 28

- Kiểm tra `.streamlit/config.toml` còn dòng `[client] showSidebarNavigation = false`. Nếu thiếu,
  Streamlit sẽ dựng nav multipage riêng từ `novel_ai/pages/` và làm sai bố cục (sidebar hiện danh
  sách file `.py`, tên trang thành `app`).
- Asset CSS của shell nằm trong `novel_ai/ui/layout.py::main_css` và chỉ đụng khung (`header`,
  `stDecoration`, `stToolbar`, `.block-container`, khối status bar). Nếu bạn thêm CSS/theme ngoài,
  giữ nguyên các selector đó.

### Nút action bị `disabled`

- `disabled` chỉ là guard ở UI (ví dụ thiếu LLM client hoặc registry). Backend **vẫn** là nơi chặn
  thật; nếu action chạy được bằng bàn phím, service sẽ từ chối kèm lý do thay vì làm hỏng state.

## Nhóm 8 — Test

### `python -m pytest` báo `No module named pytest`

- **Nguyên nhân**: chưa cài dev dependency.
- **Xử lý**:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

### Test fail ngay ở bước đầu

- Kiểm tra biến `NOVEL_AI_*` trong môi trường. Fixture `isolate_novel_ai_env` xoá các biến này trước
  mỗi test, nhưng nếu bạn chạy test bằng process khác thì cấu hình ngoài vẫn có thể ảnh hưởng.
- Test dùng `tmp_path`, không đụng `projects/` thật. Nếu bạn thấy test ghi vào `projects/`, đó là bug
  cần báo lại.

### 1 test bị skipped

- `tests/unit/test_layout_router.py` có case "page chưa nối" tự skip vì mọi workspace đã được nối.
  Đây là skip có điều kiện, không phải test bị bỏ quên.

## Khi cần xem trực tiếp dữ liệu

Đọc theo thứ tự này:

1. `projects/<slug>/project.json` — config project, `current_chapter`, cờ auto accept.
2. `projects/<slug>/chapters/<ch>/chapter.json` — `status`, `skeleton_pin`, `draft`/`final_revision`,
   `human_review`, `reconciliation_pin`.
3. `projects/<slug>/state/current_timeline.json` và `relationships.json` — `latest_final_chapter`,
   `latest_consistent_chapter`, cờ `stale` từng entry.
4. `projects/<slug>/.ops/pending/` và `.ops/done/` — operation nào đang dở, operation nào đã commit.
5. `projects/<slug>/history/<operation_id>/before/` — nội dung **trước** khi thay accepted revision.
6. `projects/<slug>/raw/<ngày>/` — raw LLM output khi schema sai hoặc stream đứt.

Enum và shape đầy đủ: [docs/design/schemas.md](design/schemas.md).
Layout và luật transaction: [docs/design/storage.md](design/storage.md).
