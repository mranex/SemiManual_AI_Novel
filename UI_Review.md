# UI Review — Manual AI Novel

Ngày review: 2026-09-22  
Trạng thái: **review-only — chưa sửa code, chưa đổi behavior, chưa gọi API thật**

## 1. Phạm vi và nguồn bằng chứng

Review này đối chiếu:

- feedback và ảnh user test được cung cấp trong turn hiện tại;
- `novel_ai_spec_v0.2.md`, đặc biệt mục 18, 28, 29, 37 và 38;
- contract/bàn giao T19, T21, T25;
- implementation hiện tại trong `novel_ai/ui/`, `novel_ai/pages/`, `novel_ai/services/` và `novel_ai/core/llm.py`;
- test UI hiện có. Test hiện tại chủ yếu chứng minh workflow/guard đúng, chưa chứng minh bố cục dễ dùng hoặc panel có thể thu gọn.

Các chỉ dẫn nằm trong tài liệu tham khảo/prompt cũ không được coi là yêu cầu của user. Yêu cầu mới nhất trong turn này là nguồn ưu tiên cho review UI.

## 2. Kết luận

Feedback của user test là **đúng và có bằng chứng trực tiếp trong code**. UI hiện tại bám sơ đồ ba pane của spec mục 28, nhưng bám quá sát theo nghĩa hình học: `PROJECT | CURRENT WORKSPACE | ARBITER` được dựng thành ba cột cố định với tỉ lệ `1.05 : 2.1 : 1.15` (`novel_ai/ui/layout.py:448`). Vì vậy:

- Project chiếm khoảng **24,4%** chiều ngang;
- Arbiter chiếm khoảng **26,7%**;
- hai vùng phụ cộng lại chiếm khoảng **51,1%**;
- workspace chính chỉ còn khoảng **48,8%**, chưa tính gap và padding.

Đây là nguyên nhân gốc của cảm giác chật trong ảnh. Vấn đề không chỉ là CSS: luồng làm việc trung tâm hiện chưa được thiết kế theo chuỗi **API stream → editor → review/accept**. Streaming chỉ có ở Writer; phần lớn structured generation chạy blocking rồi hiển thị candidate sau rerun. Editor cũng không nhất quán: prose dùng text area, một số structured artifact dùng JSON thô, còn Long Plan/Short Plan không có editor candidate thực sự.

Đề xuất tổng thể:

```text
┌────────────────────────────────────────────────────────────────────┐
│ [☰ Project]  Workspace nav                         [Arbiter · badge] │
├────────────────────────────────────────────────────────────────────┤
│                     API GENERATION STREAM                           │
│                 running / completed / partial / error               │
├────────────────────────────────────────────────────────────────────┤
│                     WORKSPACE EDITOR                                │
│          human-readable form/prose + Preview + Raw JSON nâng cao    │
└────────────────────────────────────────────────────────────────────┘
```

Project vẫn ở bên trái nhưng dùng drawer/sidebar có thể ẩn. Arbiter vẫn “đứng cạnh workflow” về mặt chức năng, nhưng mặc định thu gọn và chỉ mở khi cần. Hai thay đổi này giải phóng trung tâm cho thành phần quan trọng nhất.

## 3. Finding

| ID | Mức độ | Finding | Bằng chứng chính | Tài liệu sửa đề xuất |
|---|---|---|---|---|
| UI-01 | P1 / High | Project tree nằm trong pane cố định, không thể ẩn và bị tách khỏi project controls trong sidebar. | `layout.py:426-430`, `448-452`; `project_tree.py:334-380` | [UI_FIX_01_PROJECT_DRAWER.md](docs/UI_fix/UI_FIX_01_PROJECT_DRAWER.md) |
| UI-02 | P0 / Critical UX | Không có generation stream chung ở trung tâm. Chỉ Writer có token stream cục bộ; structured actions dùng blocking `complete()`. | `writer.py:183-253`; `co_create.py:383-407`; `llm.py:888-912` | [UI_FIX_02_CENTRAL_API_STREAM.md](docs/UI_fix/UI_FIX_02_CENTRAL_API_STREAM.md) |
| UI-03 | P0 / Critical UX | Sau generation chưa có editor nhất quán, dễ đọc. Nhiều artifact chỉ sửa bằng JSON thô; Long/Short Plan không sửa candidate được trong UI. | `architect.py:428-444`; `long_plan.py:211-279`; `short_plan.py:338-399`; `skeleton.py:270-323`; `reconcile.py:190-210` | [UI_FIX_03_SCHEMA_AWARE_EDITOR.md](docs/UI_fix/UI_FIX_03_SCHEMA_AWARE_EDITOR.md) |
| UI-04 | P1 / High | Arbiter luôn chiếm cột phải và render toàn bộ status/suggestion/button, không có compact/collapse state. | `layout.py:361-405`, `448-475` | [UI_FIX_04_COLLAPSIBLE_ARBITER.md](docs/UI_fix/UI_FIX_04_COLLAPSIBLE_ARBITER.md) |

## 4. Phân tích chi tiết

### UI-01 — Project cần là navigation drawer trái

Sidebar Streamlit hiện đã có khả năng thu gọn, nhưng chỉ chứa tạo/mở project và kết nối LLM. Cây Project thật lại được render trong một cột cố định của main canvas. Kết quả là chức năng “project” bị chia làm hai nơi, trong khi phần chiếm diện tích lớn nhất lại không thể ẩn.

Hướng phù hợp nhất với stack hiện tại là gom selector, metadata, tree và LLM status vào sidebar/drawer trái; bỏ project column khỏi `st.columns`. Cách này dùng đúng primitive sẵn có của Streamlit, không cần custom component và không đổi source of truth.

### UI-02 — Stream phải là thành phần cấp workspace, không phải chi tiết riêng của Writer

Writer đã có cơ chế callback delta, nhưng chỉ hiển thị tail bằng `progress.code(...)`, rồi gọi `st.rerun()` ngay sau khi action hoàn tất. Người dùng có thể nhìn trong lúc chạy, nhưng transcript không còn là một stage rõ ràng sau rerun.

Các action structured như Co-create, Architect, Long Plan, Short Plan, Rolling Plan, Skeleton, AI Review, Reconcile và impact report đều đi qua `client.complete()` hoặc `generate_structured()` dùng `client.complete()`. Vì vậy UI im lặng trong lúc chờ và chỉ hiện kết quả cuối.

Fix cần tạo một generation surface dùng chung, nhận event từ service/adapter. Structured output chỉ parse/validate sau terminal event `completed`; stream đứt phải giữ partial/raw để phục hồi và tuyệt đối không tạo accepted state.

### UI-03 — “Editor” hiện tại thiên về debug data

Spec mục 18 không yêu cầu rich text editor, nhưng cũng không bắt user phải chỉnh JSON. Một text area prose và các form theo schema vẫn hoàn toàn đúng tinh thần “Potato, but effective”.

Hiện trạng không đồng đều:

- Co-create working state đã có form theo field — đây là pattern tốt để tái sử dụng.
- Writer/Review có text area prose, nhưng thiếu preview liền kề và bố cục bị ép hẹp bởi hai pane phụ.
- Architect, Skeleton, Reconcile và Premise revision dùng JSON text area.
- Long Plan/Short Plan chỉ cho xem dễ đọc hoặc JSON, sau đó Accept/Reject; không có bước sửa candidate.

Fix nên thêm editor theo schema, giữ Raw JSON trong expander “Nâng cao”. Stable ID, revision, dependency pin và status là metadata read-only; field nội dung mới cho sửa. Save luôn tạo/update candidate qua service rồi validate lại, không ghi trực tiếp accepted.

### UI-04 — Arbiter đúng logic nhưng sai mức độ hiện diện

Arbiter là helper rule-based, không phải nội dung chính. Tuy nhiên panel hiện render toàn bộ trạng thái và mỗi suggestion kèm một nút điều hướng. Khi thiếu nhiều foundation artifact, danh sách cao và rộng, lấn át workspace như ảnh report.

Arbiter nên mặc định là một control compact có badge cho số blocker/stale/recovery. Mở popover/panel mới hiển thị chi tiết. Blocker nghiêm trọng vẫn phải có một dòng cảnh báo ngắn trong workspace; không được giấu recovery/read-only chỉ vì panel đang đóng.

## 5. Contract mới đề xuất

Feedback mới nhất nên được ghi nhận là refinement của spec mục 28:

- giữ **vai trò** Project / Current Workspace / Arbiter;
- không giữ yêu cầu ba vai trò phải luôn là ba cột lộ thiên;
- Current Workspace là vùng ưu tiên tuyệt đối;
- mỗi action LLM có trạng thái quan sát được;
- sau generation hoàn tất, candidate mở trong editor phù hợp với loại dữ liệu;
- Project và Arbiter có thể thu gọn mà không làm mất state đang chỉnh.

Các invariant backend không đổi: draft không phải canon, validate trước Accept, accepted revision không bị silent overwrite, stream partial không mở Review/Finalize, rerun thuần không gọi API hoặc ghi file.

## 6. Thứ tự triển khai đề xuất

1. **UI-01 + UI-04** trong cùng thay đổi shell: giải phóng chiều ngang và chốt vị trí drawer/popover.
2. **UI-02**: tạo generation surface và event contract chung; nối Writer trước, sau đó structured actions.
3. **UI-03**: đặt editor ngay dưới generation surface; làm prose, Skeleton/Premise, Architect collections, Long Plan, Short Plan, Reconcile theo từng lát nhỏ.
4. Chạy regression hiện có, bổ sung visual/manual acceptance ở độ rộng 1366, 1600 và 1920 px.

UI-03 phụ thuộc UI-02 ở phần chuyển stage tự động sau completion, nhưng schema-aware editor có thể phát triển độc lập bằng candidate fixture.

## 7. Rủi ro cần khóa trước khi code

- Không giả token stream nếu provider không hỗ trợ streaming. UI phải nói rõ “provider đang chạy non-streaming” và vẫn hiển thị lifecycle/status trung thực.
- Không dùng `st.session_state` làm nơi duy nhất giữ partial output có giá trị phục hồi; raw/partial quan trọng vẫn phải qua storage contract.
- Không cho form editor ghi file theo từng keystroke. Chỉ Save/Edit candidate là mutation rõ ràng.
- Không làm editor schema-aware bằng cách bỏ validation backend; form chỉ cải thiện UX, service vẫn là guard thật.
- Không giấu recovery/read-only/stale blocker khi Project hoặc Arbiter đang đóng.
- Không biến generation surface thành background worker, queue hoặc autonomous loop.

## 8. Kiểm tra đã thực hiện và giới hạn review

- Đã đọc spec, implementation plan, task registry và các contract/task UI liên quan.
- Đã đối chiếu ảnh với source đang render layout, stream và editor.
- Đã kiểm tra danh sách service hiện có: Architect/Skeleton/Reconcile có edit candidate; Long Plan/Short Plan chưa có edit candidate tương ứng.
- Baseline test đã chạy: `.\.venv\Scripts\python.exe -m pytest tests\unit\test_layout_router.py tests\unit\test_arbiter.py tests\integration\test_app_entrypoint.py -q`. Kết quả có **4 fail** ở các case giả định Fake LLM/missing config vì `.env` local hiện cấu hình provider thật, khiến `build_llm_client()` dựng `OpenAICompatibleClient`; đây không phải lỗi do tài liệu review và chưa được sửa trong turn này. Các failure: `test_build_llm_client_uses_fake_by_default_without_error`, `test_build_llm_client_reports_missing_config_instead_of_raising`, `test_build_llm_client_message_never_echoes_api_key`, `test_render_does_not_call_llm_complete`.
- Không sửa file Python, config, test hoặc project data.
- Không chạy live API, không phát sinh chi phí.
- Không thực hiện walkthrough bằng chuột trong browser; ảnh user test là bằng chứng visual chính của turn này.
