# Bàn giao Python application boundary cho milestone B

Ngày 2026-09-23. Milestone A hoàn thành interface Python trong process; Streamlit vẫn chạy qua `novel_ai/app.py`. Đây là contract để thiết kế adapter FastAPI và React/TypeScript sau này, chưa phải HTTP API.

## Điểm vào và chiều phụ thuộc

```text
Streamlit hiện tại ──┐
                    ├── application queries / commands / events ── services / core ── JSON, Markdown
FastAPI tương lai ──┘
```

- `AppConfig` chọn projects root, prompt root và provider; adapter giữ config/client ở server, không nhận secret hoặc đường dẫn project từ browser.
- `ProjectQueries(config)` chỉ đọc. Mọi phương thức nhận stable `project_id`, không nhận slug/path do client đưa; `list_projects()` trả ID để client chọn. `QueryResult` có `kind`, `project_id`, `data`, `needs_recovery`, `read_only`, `write_blocked`. `QueryError` có `code`.
- `ApplicationCommands(config, client=..., registry=...).execute(Command(...), on_event=...)` nhận một intent user và trả `CommandResult`. Không tự điều phối bước kế tiếp. `ApplicationError.as_dict()` là lỗi redacted; adapter nên map `code` thành HTTP status và giữ cấu trúc lỗi, không parse thông báo tiếng Việt.
- `GenerationEvent` là event Python không phụ thuộc Streamlit; callback `on_event` nhận từng event. UI giữ transcript/working copy tức thời; backend giữ candidate/raw/partial/history/operation manifest.

## Query ổn định

| `ProjectQueries` | Dữ liệu chính |
|---|---|
| `list_projects()` | Danh sách `project_id`, title, slug, current chapter; số project lỗi bị bỏ qua. |
| `open_project(project_id)` | Config project an toàn, defaults viết, fingerprint, pending operations. |
| `tree(project_id)` | Cây artifact/chapter và status. |
| `arbiter(project_id)` | Báo cáo rule-based, summary, status rows; không gọi LLM. |
| `status(project_id, workspace=..., chapter_id=...)` | Status bar hiện tại. |
| `artifact(project_id, artifact_id, audience=...)` | Accepted/candidate revision, status, validation, scope, fingerprint. Payload chỉ cho author. |
| `chapter(project_id, chapter_id, audience=...)` | Status, draft/review/final revision và fingerprint; pin chỉ cho author. |
| `recovery(project_id)` | Pending IDs, needs_recovery, read_only, write_blocked. |
| `latest_writer_operation(project_id, chapter_id)` | Metadata operation Writer để phục hồi UI; raw ref được lọc. |

`audience="writer"` là projection chặt hơn, không phải cơ chế đăng nhập. Client author vẫn không được dùng dữ liệu full author truth để ghép Writer context; service/context builder làm việc đó ở server.

## Command ổn định

`Command` version 1 có `name`, `project_id`, `operation_id`, `params`, `base_ref`, `expected_revision`, `expected_fingerprint`, `audience`, `stream`, `attempt`. `base_ref` là `project`, `artifact:<id>` hoặc `chapter:<id>`; Save/edit yêu cầu fingerprint từ query để phát hiện form stale. Không đưa dependency như `project`, `client`, `registry`, `on_event`, path hoặc key vào `params`.

Các tên `name` hiện được dispatcher chấp nhận:

| Nhóm | Command |
|---|---|
| Project/config | `create_project`, `probe_llm`, `save_writing_defaults`, `recover_project` |
| Co-create/Architect | `co_create_turn`, `save_idea_state`, `finalize_base_idea`, `reserve_foundation_ids`, `generate_foundation`, `edit_foundation_candidate`, `accept_foundation`, `reject_foundation`, `append_foundation_entries` |
| Long Plan | `generate_long_plan`, `reserve_plan_ids`, `edit_long_plan_candidate`, `accept_long_plan`, `reject_long_plan`, `confirm_planning_scope` |
| Short Plan/Rolling | `reserve_chapter_ids`, `generate_short_plan`, `edit_short_plan_candidate`, `accept_short_plan`, `reject_short_plan`, `generate_rolling`, `accept_rolling`, `reject_rolling` |
| Skeleton/Writer | `reserve_section_ids`, `generate_skeleton`, `edit_skeleton_candidate`, `accept_skeleton`, `reject_skeleton`, `write_draft` (`mode=generate/regenerate/continue`), `save_draft`, `discard_draft` |
| Review/Finalize/Reconcile | `run_ai_review`, `rewrite_section`, `apply_rewrite`, `mark_reviewed`, `finalize_chapter`, `generate_reconciliation`, `retry_reconcile`, `edit_reconciliation_candidate`, `accept_reconciliation`, `reject_reconciliation`, `cancel_finalizing` |
| Revision/retcon | `reaccept_stale`, `start_retcon`, `reset_consistency`, `revise_base_idea`, `revise_premise`, `generate_impact_report`, `reconcile_downstream` |

`CommandResult` trả `action`, `operation_id`, `project_id`, optional artifact/chapter/revision/status, `message`, `data`, `warnings`, `validation_issues`, `recovery_required`. `ApplicationError.as_dict()` trả `code`, `message`, `operation_id`, `issues`, `retryable`, `recovery_required`, `current_revision`. Payload và lỗi được lọc secret/path; writer audience bị giới hạn mạnh hơn. Adapter phải giữ nguyên `operation_id` để correlate response/event/retry.

## Generation, retry và recovery

`GenerationEvent` chứa `status`, `operation_id`, `action`, `attempt`, `transport`, optional prompt/artifact/chapter ID, `text_delta`, `detail`, `raw_ref`. State: `idle → connecting → streaming|non_streaming → transport_complete → validating → saved|partial|invalid|error`; `streaming` có thể lặp. `saved`, `partial`, `invalid`, `error` là terminal. Chỉ `saved` cho phép UI coi candidate/draft hoàn chỉnh; `text_delta` chỉ là preview raw và không được Accept. `transport` phản ánh cách gọi thật, không giả stream.

Replay cùng `operation_id` và cùng intent trong một instance trả kết quả cache; khác intent trả `operation_conflict`. Nếu raw generation cũ đã lưu trên disk mà kết quả không replay được sau restart, boundary trả `retry_result_unavailable`, không âm thầm gọi provider lần hai. Retry mới là action explicit với operation/attempt mới. Không cam kết provider at-most-once khi crash xảy ra ở khoảng không biết request đã được xử lý hay chưa.

Query mở project có pending operation trả cờ recovery/read-only mà không mutate. `recover_project` là command explicit; khi project cần recovery, các command ghi khác bị chặn. Backend storage/transaction giữ guard thực tế, adapter chỉ hiển thị cờ và lỗi. Không suy artifact hoàn thành từ sự tồn tại của file.

## Việc milestone B cần quyết định

1. Thiết kế endpoint/HTTP DTO và OpenAPI từ `QueryResult`, `Command`, `CommandResult`, `ApplicationError`; giữ version và ID, không expose file path/secret. Xác định cách serialize các trường `params` theo từng command, thay vì một endpoint nhận object tùy ý nếu cần validation ở biên HTTP.
2. Chọn SSE hoặc WebSocket cho `GenerationEvent`, quy tắc reconnect/transcript sau reload, timeout/cancel và cách báo terminal khi kết nối mất. Không biến mất kết nối thành retry tự động.
3. Chọn cơ chế chạy Python server cùng frontend, CORS/origin và auth nếu phạm vi sản phẩm cần. MVP hiện local single-user; file lock/write một file chưa bảo đảm nhiều writer đồng thời. Không mở multi-user chỉ bằng cách thêm endpoint.
4. Chọn React build/dev setup và mapping workspace/editor từ Streamlit, giữ Save explicit, stale fingerprint, author/writer projection và recovery banner. Không chuyển guard backend sang UI.
5. Kiểm live provider, failure/restart và visual/keyboard của frontend mới bằng bằng chứng mới. Báo cáo A05 chỉ chứng minh boundary và prototype Streamlit offline.

Chi tiết inventory gốc: [application-boundary-a01.md](application-boundary-a01.md). Nghiệm thu A05: [milestone-a-acceptance.md](milestone-a-acceptance.md).
