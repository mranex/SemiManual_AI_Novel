# Nghiệm thu milestone B — WebUI cutover

Ngày: **2026-09-23**. Phạm vi: B01–B08 trên Windows/PowerShell, project tổng hợp trong thư mục
tạm, FakeLLM; không đọc hoặc sửa `projects/acc`, project thật hay `.env`.

## Kết quả chạy mới sau cutover

| Kiểm tra | Kết quả |
|---|---|
| `python -m pytest -ra` | **576 passed**, 0 failed, 0 skipped; gồm core/service/application/HTTP |
| `npm test` trong `frontend/` | **2 passed** |
| `npm run build` | TypeScript typecheck và Vite production build pass |
| `npm run test:e2e` | **1 Chrome E2E passed** trên FastAPI thật + FakeLLM |
| `test_architecture_dependency.py` | Subprocess chặn import Streamlit, vẫn tạo project, query và phục vụ FastAPI |

Chrome E2E dùng [workflow.spec.ts](../../frontend/e2e/workflow.spec.ts) và
[server tổng hợp](../../tests/web/e2e_server.py). Script đi qua recovery banner → reload → recovery
explicit, Writer guard chương 2, Writer/Review/Finalize/Reconcile hai chương, retcon chương 1,
sửa prose trước commit, kiểm final cũ vẫn là canon, accept reconciliation, reset consistency và
reconcile downstream. Prose final của chương 2 không bị rewrite. Keyboard Space/Enter dùng để
xác nhận Start Retcon; lỗi guard nhận focus. Chụp recovery/stale/revision tại **1366×768,
1600×900, 1920×1080** ở [b08-screenshots/](b08-screenshots/); đã xem ảnh và E2E kiểm không
tràn ngang ở từng độ rộng.

## Parity và failure path

- [Action matrix B07](b07-action-matrix.md) ánh xạ **12 query, 54 command** đến UI hoặc server;
  OpenAPI test kiểm toàn bộ command và input đóng.
- `tests/web/test_api.py` kiểm stale replay/409, write serialization, server restart không gửi lại
  generation, disconnect, partial/invalid/error không báo saved, pending recovery chặn write,
  secret redaction, read-only query, CORS dev allowlist và production static/API cùng origin.
- `tests/integration/test_webui_queries.py` kiểm Short Plan guard trước LLM, chapter projection,
  Writer khóa đến khi chương trước reconciled, partial stream không mở Review. Fixture legacy
  Long Plan thiếu horizon vẫn mở được với `planning_scope=None`, đòi xác nhận tường minh.
- `tests/integration/test_a05_file_compatibility.py` so command boundary với service trực tiếp:
  cùng canonical file set và Base Idea bytes/metadata sau action đại diện. Không có migration
  project tự động ở B.
- Adapter Streamlit (`novel_ai/app.py`, `ui/`, `pages/`, `.streamlit/`), dependency và test AppTest
  chuyên biệt đã gỡ. Python business tests được giữ. Test mới chặn import Streamlit ở subprocess.

## Chạy và giới hạn

Sau `npm ci` và `npm run build` trong `frontend/`, chạy `python -m novel_ai.web.main` ở repo root;
mở `http://127.0.0.1:8000`. Server chỉ bind loopback, một worker, phục vụ React build và API
cùng origin. Backup nguyên thư mục project khi không có write đang chạy và không có pending recovery.

**Chưa kiểm live provider** vì không có endpoint/model/key được cấp. Screen reader/AT và đánh giá
chất lượng văn xuôi chưa thực hiện. App vẫn là local single-user; các giới hạn storage/service
còn lại liệt kê trong [README](../../README.md#trạng-thái-và-giới-hạn). Không commit/push/deploy.
