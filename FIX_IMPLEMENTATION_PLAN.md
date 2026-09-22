# Manual_AI_Novel — Kế hoạch sửa prototype sau user test

Ngày lập: 2026-09-22. Phạm vi: lập kế hoạch từ [DEBUG_REPORT.md](DEBUG_REPORT.md) và [UI_Review.md](UI_Review.md). **Tài liệu này không có nghĩa các fix đã triển khai.**

## 1. Mục tiêu và baseline

Khôi phục luồng đang crash sau Accept Short Plan; sửa điều hướng và input guard; làm Long Plan đúng horizon cấp truyện; dành vùng trung tâm cho generation và editor dễ đọc.

T01–T25 được registry ghi `done`. Source Python/Streamlit và test hiện tồn tại; không còn ở giai đoạn chuẩn bị xây app. Thư mục hiện không phải Git repository. [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) giữ làm lịch sử triển khai MVP; [registry](docs/tasks/README.md#registry-fix) là nguồn trạng thái duy nhất cho T26–T40.

Bằng chứng lịch sử cần phân biệt:

- T25 ghi `494 passed, 1 skipped`.
- Debug 2026-09-22 ghi `493 passed, 4 failed, 1 skipped`; bốn fail liên quan test đọc dotenv local. Chưa chạy lại suite trong phiên lập kế hoạch này.
- BUG-001–004 và UI-01–04 đều chưa sửa theo báo cáo. Số dòng/file trong báo cáo là điểm bắt đầu điều tra, phải kiểm code thực tế khi nhận task.
- Không sửa `projects/acc`, accepted state, source, prompt runtime, .env hay test trong phiên lập kế hoạch.

## 2. Phạm vi và truy vết

| Nguồn | Vấn đề / kết quả cần đạt | Task sở hữu | Nghiệm thu |
|---|---|---|---|
| Baseline của hai report | Test offline không đọc config/key thật | T26 | Test cô lập dotenv + full suite |
| BUG-001 | Cây có chapter không dùng nested expander | T27 | Accept → full shell rerun → reopen |
| BUG-002 | Arbiter click đồng bộ navbar/workspace | T28 | Click nút thật + rerun tiếp |
| BUG-004 | Full horizon được lưu/revalidate, coverage đúng | T29, T30 | Multi-volume/arc, gaps, legacy, reload/Accept |
| BUG-003 | Default theo project và guard trước API | T29, T31 | Missing/duplicate/out-of-scope → zero calls |
| UI-01, UI-04 | Drawer trái + Arbiter compact | T29, T32 | Toggle không mất input + ảnh app thật |
| UI-02 | Generation surface và event cho mọi action | T29, T33–T35 | Complete/partial/error/nonstream, no rerun call |
| UI-03 | Editor theo schema, Save candidate riêng Accept | T29, T36–T39 | Roundtrip + edit/Save/Accept qua service |
| Toàn đợt | Hồi quy và hướng dẫn đúng build | T40 | Coverage matrix + 2 chương + visual |

Không tự nhận các finding T24 còn lại trong [review-findings-t24.md](docs/design/review-findings-t24.md), refactor toàn storage, nâng dependency, tự chạy batch nhiều arc, thiết kế queue hay đổi stack. Nếu phát hiện blocker liên quan trực tiếp thì ghi rõ trong task và registry; không giấu nó bằng fixture dễ hơn.

## 3. Contract và quyết định cho agent triển khai

Đây là hướng đích của đợt fix được giao; **T29 phải ghi chi tiết vào design/schema/decision và amendment liên quan trước khi code phụ thuộc**. Không coi mọi ví dụ widget/field trong UI report là executable contract.

### 3.1. Default viết — user đã chốt

Ngày 2026-09-22 user chọn: **“Lưu theo project, cho override từng chương”** cho POV và độ dài mặc định.

- Quyết định này thay riêng phần “không thêm project config” của D013; giữ contract cuối `{language, pov, length_guidance}` và authority của Short Plan.
- T29 chọn tên field project, format version và compatibility cụ thể. Ngôn ngữ dùng default hiện có; POV/length do user nhập, không bịa số từ/POV.
- Default là input cho request mới, không sửa ngược accepted chapter contract. Mỗi chương resolve default + override trước gọi LLM; hiển thị giá trị hiệu lực.
- Project cũ thiếu field vẫn mở được. Chặn generation thiếu dữ liệu và hướng dẫn Save default rõ ràng; không tự điền accepted data khi render.
- Guard backend kiểm đủ từng assigned chapter và không để model thay contract đã cấp. Save default là explicit action, không gọi LLM.

### 3.2. Long Plan horizon

Theo yêu cầu được ghi trong BUG-004, `planning_scope={start,end}` là toàn horizon cần kiến trúc, có thể nhiều Volume/Arc hoặc toàn truyện. Không phải số chương mỗi arc hay edit window.

- Initial horizon phải được user chọn rõ; không default ngầm `1..3` từ current chapter/Short Plan. Regenerate dùng scope được lưu trừ khi user chủ động đổi.
- Candidate phải giữ horizon theo revision qua save/reload/Accept/snapshot. T29 chốt vị trí lưu, owner là app, schema version và metadata không cho LLM/form sửa.
- Payload không rỗng; Volume có Arc; Arc ranges phủ đúng start..end, liên tục, không overlap/gap/out-of-scope. Edit/regenerate trả full payload của horizon; giữ entity không đổi theo stable ID.
- Không quota “ít nhất 2 volume” hoặc “mỗi arc 8 chương”. One-arc hợp lệ về cấu trúc không chứng minh phân rã truyện tốt. Prompt hướng dẫn narrative phases, fixture đầy đủ multi-volume/arc, UI preview và cảnh báo mọi plan chỉ có một arc (không tự đặt ngưỡng “horizon lớn”).
- Warning không đổi Auto Accept thành Human Accept bắt buộc. Auto Accept vẫn theo config sau full validation; không tuyên bố backend đánh giá được chất lượng narrative.
- Legacy accepted/candidate thiếu scope: đọc để xem được, không coi min/max arc hiện có là horizon gốc đã xác nhận. T29 ghi rõ action xác nhận/migration và guard nào chặn; giữ bản cũ/snapshot, không auto migrate khi mở. Migration phải retry an toàn và không tự sửa manuscript.
- T30 kiểm output truncation. Không tự thiết kế chunking/autonomous nhiều request; Short Plan hiển thị phạm vi/số chương thực sự sẽ gửi, không tự cắt nhỏ hay mở rộng scope.

### 3.3. UI sau user test

Refinement mục 28: giữ vai trò Project / Workspace / Arbiter, nav trên đỉnh và status thông tin; **không bắt ba cột luôn mở**.

- Project vào sidebar trái native; Arbiter compact mở detail; workspace lấy chiều rộng được giải phóng.
- Recovery/read-only/blocking stale vẫn hiện ở main khi panel đóng.
- Mọi generation có surface trên editor. Không fake token stream; non-streaming phải nói rõ.
- Không dùng nested expander ở project tree **hoặc editor**. Ví dụ “expander lồng” trong UI_FIX_03 không phù hợp bug và bản dependency hiện tại; dùng tab/select/card.
- Working input trong phiên phải sống qua toggle/rerun/chuyển workspace quay lại, scope theo project/artifact/revision. Chưa Save không hứa tồn tại sau đóng app; raw/partial đáng phục hồi phải lưu disk.

### 3.4. Stream, retry và bảo vệ state

- Callback/event thuần Python; UI render bằng primitive Streamlit. Không service import Streamlit, không worker/queue/concurrent generation.
- T29 chốt một event contract dùng chung. Phải phân biệt response nhận xong với payload đã parse/validate/lưu thành công. `completed` ở UI không được báo sớm.
- Structured JSON đang stream chỉ là raw preview; partial, timeout, finish_reason truncation hoặc invalid schema không tạo complete candidate, không auto accept, không mở Review/Finalize.
- Preserve accepted/candidate hợp lệ cũ khi lỗi. Lưu raw/error/partial qua storage contract; không log API key/full prompt/context secrets.
- Author workspace có thể hiển thị output author-only theo quyền hiện hành; Writer transcript/editor không nhận full author truth. Không dùng chung cache transcript giữa workspace/project.
- Rerun/replay operation thành công phải không gọi/merge trùng. Retry sau lỗi là action explicit; T29 chốt reuse operation/attempt ID theo storage hiện có. Không hứa at-most-once request từ provider khi crash sau gửi request nhưng trước nhận phản hồi.
- Nếu provider không hỗ trợ streaming, chọn đường non-streaming rõ từ capability/config. Không tự gửi request fallback thứ hai khi đã nhận partial rồi lỗi.

### 3.5. Editor và Auto Accept

- Form đúng schema thực tế; Raw JSON nâng cao dùng cùng service/validation. Không generic form framework lớn.
- Save thủ công chỉ candidate/draft, **không tự Accept dù Auto Accept bật**. Auto Accept của output AI vẫn hoạt động như contract hiện có; khi AI output đã auto accept, view phải ghi accepted và cho action Revise explicit, không giả candidate chờ Accept.
- Stable IDs, status, revision, dependency pins và horizon metadata do app sở hữu; selectors từ ID hợp lệ. Add/remove/reorder trong working copy phải qua ID allocator, scope/FK/freshness guards.
- Long/Short Plan cần service edit candidate trước UI; không ghi accepted file từ widget. Prose Save vô hiệu review cũ; Finalize/Retcon giữ action và gate riêng.
- Roundtrip phải giữ field optional/nested không có widget; stale working copy không được overwrite revision mới. Error giữ input và chỉ field/card lỗi.

## 4. Task và dependency

Mọi task mới ban đầu `todo`; trạng thái, owner khi nhận và kết quả chỉ ở registry/bàn giao task.

| Task | Phạm vi | Dependency |
|---|---|---|
| [T26](docs/tasks/T26-offline-test-isolation.md) | Cô lập cấu hình và dựng baseline test offline | T25 |
| [T27](docs/tasks/T27-project-tree-crash.md) | Sửa crash project tree sau Accept Short Plan | T26 |
| [T28](docs/tasks/T28-arbiter-navigation.md) | Sửa điều hướng Arbiter và đồng bộ navbar | T27 |
| [T29](docs/tasks/T29-remediation-contracts.md) | Chốt contract horizon, default viết và UI sau user test | T26 |
| [T30](docs/tasks/T30-long-plan-horizon.md) | Sửa Long Plan theo complete horizon và lưu theo revision | T29 |
| [T31](docs/tasks/T31-short-plan-writing-defaults.md) | Default viết theo project và guard trước Short Plan API | T30 |
| [T32](docs/tasks/T32-workspace-shell.md) | Project drawer và Arbiter thu gọn | T28, T29 |
| [T33](docs/tasks/T33-generation-events-writer.md) | Event generation dùng chung và tích hợp Writer | T31, T32 |
| [T34](docs/tasks/T34-planning-structured-stream.md) | Streaming Co-create, Architect và Planning | T33 |
| [T35](docs/tasks/T35-chapter-review-stream.md) | Streaming Skeleton, Review, Rewrite, Reconcile và Impact | T34 |
| [T36](docs/tasks/T36-planning-candidate-edit-services.md) | Service sửa candidate Long Plan và Short Plan | T35 |
| [T37](docs/tasks/T37-foundation-editors.md) | Editor nền và form Base Idea, Premise, Architect | T36 |
| [T38](docs/tasks/T38-planning-editors.md) | Editor Volume/Arc và Chapter Plan | T37 |
| [T39](docs/tasks/T39-chapter-reconcile-editors.md) | Editor Skeleton, prose và Reconciliation | T38 |
| [T40](docs/tasks/T40-fix-acceptance-handoff.md) | Nghiệm thu đợt fix và cập nhật hướng dẫn sử dụng | T39 |

Ưu tiên thực hiện: T26 là bước làm test an toàn, sau đó T27/T28 khôi phục shell; T29 có thể làm đồng thời với nhánh đó vì chỉ chốt tài liệu. T30/T31 sửa planning trước mở rộng streaming/editor. T32 có thể làm song song T30/T31 sau khi đủ dependency, nhưng phải khóa vùng file chung.

T33 trở đi cố ý tích hợp theo thứ tự: callbacks/helper/LLM client, services và page chung có nhiều chỗ trùng. T36 chờ stream integration xong để tránh hai agent cùng sửa long_planner/short_planner. T37 tạo pattern editor, T38/T39 áp dụng từng nhóm dễ review. Không cần mỗi task nằm trong một cuộc hội thoại mới; đây là đơn vị công việc trong repo.

## 5. Giao việc và phối hợp

- Khi chưa có task in_progress, chọn todo đủ dependency số nhỏ nhất. Tiếp tục task đang nhận chỉ khi thuộc agent/phiên mình hoặc user giao bàn giao; không giành task của agent khác.
- Khi giao nhiều agent, ghi owner/ngày/file đang sửa trong mục bàn giao **và** ghi chú cạnh trạng thái registry. Owner là ghi chú, không đổi tên trạng thái chuẩn.
- Hai nhánh độc lập sớm: T27 → T28 và T29 → T30 → T31; T32 sau T28 + T29. T26 phải xong trước mọi nhánh.
- Các file dễ đụng: layout.py, core/models.py/validation.py, core/llm.py, services/co_create.py, services/long_planner.py/short_planner.py, pages/_common.py/_chapter_ui.py và tests integration chung. Không chạy đồng thời task cùng sửa những file đó.
- Registry là file dùng chung: chỉ sửa hàng task mình nhận và phần bàn giao; đọc lại trước ghi để tránh mất update. Thư mục chưa Git nên không dựa vào branch/merge để cứu overwrite.
- Khi conflict contract cần quyết định sản phẩm chưa được ghi, tiếp tục phần độc lập và hỏi cụ thể; không tự áp dụng suy đoán. Không hỏi lại việc user đã chốt default theo project.
- Không sửa thẳng báo cáo review lịch sử để biến “chưa sửa” thành bằng chứng đã pass. Sau fix thêm follow-up với task/test/date, T40 tổng hợp resolution.

Mẫu giao việc:

> Đọc AGENTS.md, FIX_IMPLEMENTATION_PLAN.md và docs/tasks/README.md. Thực hiện T26, chỉ phạm vi task. Ghi nhận task, dùng test offline, cập nhật bàn giao và registry; không sửa .env/project thật, không commit/push.

> Thực hiện T30 sau khi T29 done. Đọc decision IDs và compatibility matrix trong bàn giao T29 trước. Không tự đổi horizon contract hoặc đặt quota arc.

## 6. Nghiệm thu và điểm dừng

Từng task có acceptance/test riêng; không dồn mọi test đến T40. Không đánh dấu done nếu chỉ đổi widget/fixture mà chưa đi qua trigger báo lỗi. Lệnh test ưu tiên PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Chỉ chạy suite sau khi T26 cô lập dotenv/provider; không đổi .env thật để suite pass. Những document checker chỉ xác nhận ví dụ/tài liệu, không thay runtime tests.

T40 phải có matrix BUG/UI → task → runtime test → screenshot/manual result; full-shell Accept/reopen và Arbiter click; project default/override và legacy; horizon revalidation; generation đủ action; hai chương qua review/finalize/reconcile; context secret/temporal, stale, retcon, retry/atomic recovery.

Visual ở 1366×768, 1600×900, 1920×1080, panel đóng/mở, project mới/có chapter/recovery, stream và editor. AppTest pass không đủ để khai visual pass. Nếu môi trường không cho visual hoặc live provider chưa thử, ghi giới hạn chính xác; thiếu visual bắt buộc thì task tương ứng chưa done. Live API chỉ khi user cho phép, không bắt buộc để nghiệm thu offline; không tuyên bố chất lượng văn chương hay mọi provider đã được xác minh.

Đợt fix hoàn tất khi T26–T40 đạt acceptance và report mới + hướng dẫn đã cập nhật. Không tự commit/push/deploy.

