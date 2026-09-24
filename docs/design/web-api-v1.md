# Contract HTTP v1 cho WebUI

Ngày 2026-09-23. B01 khóa contract, B02/B03 hiện thực trong `novel_ai/web/`; application/service/core vẫn là nơi quyết định luật truyện. Không đổi schema project hay luồng Accept/Finalize.

## Runtime và bảo vệ local

- `python -m novel_ai.web.main` hoặc `novel-ai-web` bind **127.0.0.1:8000**, một worker. Frontend build ở `frontend/dist` được FastAPI phục vụ cùng origin. Dev Vite bind 127.0.0.1:5173 và proxy `/api` tới 127.0.0.1:8000. CORS chỉ cho hai origin dev loopback; CORS không phải authentication. Không dùng cấu hình này cho LAN/remote nếu chưa thiết kế auth, CSRF và phân quyền.
- Một `AppConfig`, `ApplicationCommands`, `ProjectQueries`, LLM client và prompt registry sống trong server process. GET không dựng request provider. Browser chỉ truyền stable ID; `ProjectQueries.resolve` tra dưới configured projects root. Root/path/key/prompt/context và Python dependency không có trong DTO. Error HTTP loại raw `input` và traceback.
- Mọi POST mutation giữ lock theo project trong **một process**, bao gồm toàn bộ vòng generation; `create_project` dùng lock global. Lock application chỉ ngăn hai request trong process tranh ghi, còn guard revision/transaction thuộc service/core. Không cam kết đa process, multi-user, hoặc an toàn khi code khác ghi cùng thư mục ngoài server.
- Python 3.12; pin FastAPI 0.116.1, Uvicorn 0.35.0, Pydantic 2.11.7, httpx 0.28.1 (test) trong `pyproject.toml`. Node 24.14.1 và npm 11.11.0 đã kiểm tại máy; frontend pin React/ReactDOM 19.1.1, TypeScript 5.9.2, Vite 7.3.6, Vitest 4.1.11 và `package-lock.json`. Vite dev version đã nâng sau `npm audit` để không giữ advisory của pin thử ban đầu. [Vite yêu cầu Node 20.19+ hoặc 22.12+](https://vite.dev/guide/); [Node 24 là LTS](https://nodejs.org/en/about/previous-releases).

## Endpoint, DTO và mã lỗi

Prefix `/api/v1`. Tất cả query dùng GET và trả `QueryBody {kind, project_id, data, needs_recovery, read_only, write_blocked}`. Query map trực tiếp tới method cùng tên của `ProjectQueries`:

| GET | Method | Kiểm |
|---|---|---|
| `/projects` | `list_projects()` | `tests/web/test_api.py` |
| `/projects/{id}` | `open_project(id)` | `tests/web/test_api.py` |
| `/projects/{id}/foundation` | `foundation(id)` — Co-create working state và Base Idea accepted, không trả markdown ref | `tests/web/test_api.py` |
| `/projects/{id}/planning` | `planning(id)` — horizon đã lưu, arc/assigned chapter, default viết và Rolling eligibility; không ghi ID | `tests/integration/test_webui_queries.py` |
| `/projects/{id}/tree` | `tree(id)` | `tests/web/test_api.py` |
| `/projects/{id}/arbiter` | `arbiter(id)` | `tests/web/test_api.py` |
| `/projects/{id}/status?workspace=&chapter_id=` | `status(id, ...)` | `tests/web/test_api.py` |
| `/projects/{id}/artifacts/{artifact_id}?audience=` | `artifact(id, ...)` | `tests/web/test_api.py` |
| `/projects/{id}/chapters/{chapter_id}?audience=` | `chapter(id, ...)` | `tests/web/test_api.py` |
| `/projects/{id}/recovery` | `recovery(id)` | `tests/web/test_api.py` |
| `/projects/{id}/revision` | `revision(id)` — blocker, version, retcon, snapshot/history đã lọc đường dẫn | `tests/web/test_api.py`, browser E2E |
| `/projects/{id}/chapters/{chapter_id}/latest-writer-operation` | `latest_writer_operation(id, chapter_id)` | `tests/web/test_api.py` |

B04–B07 bổ sung projection cho browser: `foundation` trả Co-create/Markdown Base Idea đã lưu; `planning` trả lựa chọn arc, assigned chapter chưa lập và default viết; `chapter` với `audience=author` trả prose working revision, Human Review và trạng thái final/reconcile cùng cờ `retcon_open`. `revision` trả summary chỉ đọc; marker retcon và history không chứa file ref. `audience=writer` vẫn loại prose, pin và author-only fields. Các query không trả `markdown_ref` hay đường dẫn file; server đọc text qua storage trong configured root. Editor gửi fingerprint/revision từ query cho Save để backend chặn form stale.

POST thường là `/commands/{name}`, POST generation là `/generations/{name}`. Mỗi route có `Request` riêng trong OpenAPI: common envelope `operation_id` (bắt buộc), `project_id`, `base_ref`, `expected_revision`, `expected_fingerprint`, `audience`, `attempt`, `stream`, `version=1` và `params` **đóng theo action** tại `novel_ai/web/dto.py::FIELDS`. Field lạ bị HTTP 422 trước service. `stream` của generation chọn transport provider thực; `stream=false` phát `non_streaming`. `params` dạng payload JSON chỉ xuất hiện ở field đã khai, còn service validate schema/domain. `base_ref` và fingerprint bắt buộc cho editor/save theo application boundary. Các DTO response là `ResultBody`, `EventBody`, `ErrorBody` trong cùng module; OpenAPI ở `/openapi.json`.

Mapping đủ 53 command của handoff A→B và `edit_retcon_draft` bổ sung ở B07, tổng 54 (mỗi tên ánh xạ `ApplicationCommands.execute(Command(name,...))`; cột kiểm là `tests/web/test_api.py` cho HTTP matrix/validation và integration application/service cho luật nghiệp vụ):

| Nhóm | POST thường (`/commands/`) | POST SSE (`/generations/`) |
|---|---|---|
| Project | `create_project`, `probe_llm`, `save_writing_defaults`, `recover_project` | — |
| Co-create/Architect | `save_idea_state`, `finalize_base_idea`, `reserve_foundation_ids`, `edit_foundation_candidate`, `accept_foundation`, `reject_foundation`, `append_foundation_entries` | `co_create_turn`, `generate_foundation` |
| Long Plan | `reserve_plan_ids`, `edit_long_plan_candidate`, `accept_long_plan`, `reject_long_plan`, `confirm_planning_scope` | `generate_long_plan` |
| Short/Rolling | `reserve_chapter_ids`, `edit_short_plan_candidate`, `accept_short_plan`, `reject_short_plan`, `accept_rolling`, `reject_rolling` | `generate_short_plan`, `generate_rolling` |
| Skeleton/Writer | `reserve_section_ids`, `edit_skeleton_candidate`, `accept_skeleton`, `reject_skeleton`, `save_draft`, `discard_draft` | `generate_skeleton`, `write_draft` |
| Review/Reconcile | `apply_rewrite`, `mark_reviewed`, `finalize_chapter`, `edit_reconciliation_candidate`, `accept_reconciliation`, `reject_reconciliation`, `cancel_finalizing` | `run_ai_review`, `rewrite_section`, `generate_reconciliation`, `retry_reconcile` |
| Revision | `reaccept_stale`, `start_retcon`, `edit_retcon_draft`, `reset_consistency`, `revise_base_idea`, `revise_premise` | `generate_impact_report`, `reconcile_downstream` |

`edit_retcon_draft` yêu cầu `base_ref=chapter:<id>`, `expected_revision` và `expected_fingerprint` từ chapter query. Service ghi prose revision, chapter metadata và marker retcon trong một transaction; final cũ giữ nguyên cho tới Accept Reconciliation. Đây là action user explicit để draft retcon thực sự sửa được, thay cho giới hạn editor của MVP Streamlit.

Các code lỗi ổn định: HTTP 422 `invalid_input`/ID/base_ref sai; 428 `missing_base_revision`; 404 `project_not_found`/`unknown_command`; 409 `stale_candidate`, `stale_dependency`, `operation_conflict`, `retry_result_unavailable`; 423 `recovery_required`; 503 `llm_unavailable`/`configuration_error`; guard/schema domain khác HTTP 400 với `code` riêng. Body luôn có `code`, `message`, `operation_id`, `issues`, `retryable`, `recovery_required`, `current_revision`. Stream đã gửi HTTP 200 thì lỗi sau đó đi bằng frame `error` cùng body, không đổi status giữa chừng.

## SSE và request lifecycle

`fetch` POST JSON nhận `text/event-stream`; framing `event: generation|result|error` + `data: <JSON>` + dòng trống. Event generation là `GenerationEvent.as_dict`, cùng `operation_id`/`attempt`; `saved` chỉ phát sau validate và persist. Một frame `result` theo sau terminal `saved`; lỗi/partial/invalid kết thúc bằng frame `error` hoặc result status partial/invalid tùy service. Client chỉ mở candidate sau **`saved` + `result`**, không sau `transport_complete` hoặc text preview. Không giả token stream cho provider non-streaming. Preview mỗi event giới hạn 4096 ký tự, detail 512; raw ref path tuyệt đối bị bỏ.

Producer synchronous chạy trong thread của đúng HTTP request, không block event loop. Queue tối đa 32 frame áp backpressure; request disconnect set cancel flag, callback tiếp theo dừng producer. Không có queue bền, worker nền hay tác vụ generation tách request. Nếu provider đang block I/O khi client rời đi, Python không thể cắt syscall đồng bộ ngay; callback kế tiếp kiểm cờ trước mutation tiếp theo. Đây là giới hạn của adapter/LLM hiện tại và cần kiểm live trước khi mở remote. Timeout provider do `AppConfig.llm_timeout_seconds` quản lý; transport HTTP không tự request fallback.

Reload/reconnect chỉ GET `open_project`, artifact/chapter, recovery và `latest_writer_operation`; không re-POST generation. Retry do user bấm tạo `operation_id`/`attempt` mới. Replay cùng ID/intent trong process trả cache; ID cũ đã có raw nhưng không replay được sau restart trả `retry_result_unavailable`. Không cam kết exactly-once provider khi crash ở khoảng không rõ response.

## Ví dụ wire

Query: `GET /api/v1/projects` → `200 {"kind":"list_projects","project_id":null,"data":{"projects":[],"skipped_count":0},"needs_recovery":false,"read_only":false,"write_blocked":false}`.

Command: `POST /api/v1/commands/create_project` body `{"operation_id":"create_1","params":{"title":"Truyện A"}}` → `200 {"action":"create_project","operation_id":"create_1","project_id":"<stable-id>","data":{"project_id":"<stable-id>","title":"Truyện A","slug":"truyen-a"},...}`.

Generation success: `POST /api/v1/generations/co_create_turn` body `{"operation_id":"turn_1","project_id":"<stable-id>","stream":true,"params":{"user_message":"Một ý tưởng"}}` → `generation connecting`, `streaming` (có `text_delta`), `transport_complete`, `validating`, `saved`, rồi `result` có `status=saved`. Mỗi frame mang `operation_id=turn_1`.

Partial stream: cùng endpoint với `operation_id=turn_2`, transport đứt → `generation partial` hoặc `error`, không có `result status=saved`; raw/partial do service lưu nếu có. Validation sai: body chứa `params.client` → HTTP 422 `{"code":"invalid_input","issues":[{"path":"body.params.client","code":"extra_forbidden","message":"Input không hợp lệ."}],...}`; input thô không echo.

Stale form: `save_writing_defaults` có `base_ref=project`, `expected_fingerprint` cũ → HTTP 409 `{"code":"stale_candidate","current_revision":null,...}`. Pending recovery: POST ghi khác `recover_project` → HTTP 423 `{"code":"recovery_required","recovery_required":true,...}`. Reconnect: chỉ `GET /api/v1/projects/{id}/recovery` và `GET /api/v1/projects/{id}/chapters/{chapter_id}/latest-writer-operation`; GET không gọi provider/commit.

## Bằng chứng B01 spike

`tests/web/test_api.py` chạy FakeLLM qua callback đồng bộ và HTTP POST SSE: xác nhận thứ tự, terminal, replay, partial, guard trước LLM, query không ghi, hai POST ghi cạnh tranh. Test ASGI trực tiếp gửi `http.disconnect` sau frame đầu và xác nhận producer dừng ở callback kế tiếp; test khác giữ provider đồng bộ trong request và xác nhận GET vẫn chạy. Frontend parser/correlation ở `frontend/src/generation.ts` và `generation.test.ts`; `npm test` + `npm run build`. Disconnect trên browser/live provider vẫn là gate riêng trước khi tuyên bố production WebUI hoàn chỉnh.
