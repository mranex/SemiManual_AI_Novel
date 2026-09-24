# Nghiệm thu milestone A — 2026-09-23

Phạm vi: A01–A05 tách ranh giới Python application cho prototype Streamlit. Kiểm tra trên Windows/Python 3.12, FakeLLM và project tổng hợp trong thư mục tạm. Không dùng `projects/acc`, không thay `.env`, không gọi provider trả phí.

## Kết quả chạy

Chạy từ repo root trong PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:randomly -ra
```

Kết quả cuối của A05: **679 passed, 1 skipped, 0 failed**. Skip là case router lịch sử trong `tests/unit/test_layout_router.py` dành cho workspace chưa nối; cả 9 workspace đã có page. Lần chạy này gồm 2 test kiến trúc/headless, test tương thích file A05 và regression nhãn project khi chuyển sidebar. Dữ liệu test được tạo qua `tmp_path`; fixture chặn cấu hình môi trường/dotenv và dùng FakeLLM/transport stub.

## Ma trận luồng và bằng chứng

| Luồng/rủi ro | Bằng chứng chạy trong suite | Kết quả |
|---|---|---|
| Tạo/mở project, query cây/Arbiter/status | `test_application_queries.py`, `test_app_entrypoint.py` | Pass; query chỉ đọc, mở project phản ánh recovery và nhãn sidebar theo project hiện tại. |
| Co-create → foundation → Long/Short Plan → Skeleton → Writer → Human Review → Finalize/Reconcile | `test_mvp_acceptance.py`, `test_application_commands.py`, các integration service và AppTest entrypoint | Pass với FakeLLM và project tạm; guard ở backend vẫn chặn bước sai thứ tự. |
| Contract command, stale revision/fingerprint, replay operation, retry | `test_application_commands.py`, test service/revision tương ứng | Pass; Save stale bị từ chối, replay không gọi lại action đã hoàn tất trong cùng instance; raw generation cũ không tự gửi lại sau restart. |
| Context Writer, secret và lore có hiệu lực muộn | `test_context.py`, `test_application_queries.py`, `test_application_commands.py`, fixture truyện giả lập | Pass; writer projection không trả payload author-only, context lọc theo chapter hiệu lực. |
| Partial/invalid/error stream và accepted state | `test_generation_events.py`, test Writer/Review/Finalize và application command | Pass; chỉ `saved` sau validate/persist là candidate ready. |
| Pending/recovery/read-only và retcon/stale | `test_revision_recovery_ui.py`, test storage/reconcile/revision, `test_application_queries.py` | Pass; mở project là read-only query, recovery là command explicit. |
| Tương thích file trước/sau boundary | `test_a05_file_compatibility.py` | Pass; direct service và command tạo cùng tập file, cùng bytes Base Idea, status/revision/co-create state. Không phát sinh migration. |
| Chiều phụ thuộc và chạy headless | `test_architecture_dependency.py` | Pass; AST chặn import ngược từ application/core/services sang UI/pages/Streamlit; subprocess query + command không nạp Streamlit/UI/pages. |

Walkthrough entrypoint dùng `streamlit.testing.v1.AppTest` qua `novel_ai/app.py`, gồm trigger Accept Short Plan → rerun shell → mở lại Arbiter/nav. Test tương tác không thay thế việc nhìn app thật.

## Kiểm visual/manual app thật

Chạy Streamlit ở `localhost:8508` với `NOVEL_AI_PROJECTS_ROOT` trỏ tới thư mục tạm và `NOVEL_AI_USE_FAKE_LLM=true`. Ba project tổng hợp: mới trống, chapter đã có accepted plan/chapter, và project có pending operation cần recovery. Kiểm bằng browser thật ở **1366×768, 1600×900, 1920×1080**.

| Màn hình | 1366×768 | 1600×900 | 1920×1080 |
|---|---|---|---|
| Project mới / Co-create | Đã xem | Đã xem | Đã xem |
| Chapter / Long Plan candidate | Đã xem | Đã xem | Đã xem |
| Chapter / Skeleton candidate | Đã xem | Đã xem | Đã xem |
| Chapter / Writer prose editor | Đã xem | Đã xem | Đã xem |
| Project recovery / Revision workspace | Đã xem | Đã xem | Đã xem |

Ở 1366×768 đã xem thêm Short Plan defaults/assigned chapter và Arbiter trên project mới. Các màn hình trên render được, không hiện exception hoặc tràn ngang tại các độ rộng đã kiểm. Long Plan editor có accepted r1 và candidate r2; Skeleton editor có candidate chương 2; Writer editor đã nạp prose chương 1. Recovery hiện banner read-only và pending operation. Khi chuyển project, phát hiện caption “Đang mở” của sidebar chậm một render; đã sửa ở `ui/layout.py`, kiểm lại trên app và thêm AppTest regression.

## Giới hạn bằng chứng

- Không chạy live API/provider thật hoặc đánh giá chất lượng narrative. FakeLLM và stub chỉ chứng minh contract offline.
- Visual là kiểm thủ công giao diện đang chạy; không đo focus/screen reader trong A05. Bằng chứng keyboard T40 có trong báo cáo đợt fix, không coi là lần chạy A05.
- Short Plan candidate form không được xem lại ở ba độ rộng trong A05; suite có AppTest/editor coverage từ các task trước. Không kiểm mọi tổ hợp candidate/stale/recovery bằng mắt.
- Parity file A05 so sánh một action đại diện (Finalize Base Idea); các luồng còn lại dựa vào integration/regression suite, không phải so bytes toàn bộ lifecycle.
- `ApplicationCommands` có replay cache trong instance và guard raw tồn tại trên disk; chưa có cam kết exactly-once với provider sau crash không rõ kết quả. File storage hiện thiết kế local một người dùng; không chứng minh an toàn nhiều writer đồng thời.
- Chưa có FastAPI, HTTP DTO, React UI hay kiểm kết nối browser với API. Quyết định adapter thuộc milestone B.

Hợp đồng bàn giao chi tiết: [milestone-b-handoff.md](milestone-b-handoff.md). Registry: [A05](../tasks/A05-refactor-acceptance.md).
