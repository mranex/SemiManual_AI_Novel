# Manual AI Novel

Ứng dụng local hỗ trợ viết tiểu thuyết theo từng bước:

`Co-create → Base Idea → Architect → Long Plan → Short Plan → Skeleton → Writer → Review → Finalize → Reconcile`

AI tạo đề xuất và bản nháp; người dùng quyết định canon. Bản hiện tại dùng Python 3.12, FastAPI, React/TypeScript và file JSON/Markdown. WebUI có 9 workspace. T01–T40, A01–A05 và B01–B08 đã hoàn thành; **chưa kiểm với provider LLM thật**. Xem [nghiệm thu WebUI](docs/design/milestone-b-acceptance.md) để biết phạm vi đã kiểm.

## Cài đặt và chạy trên Windows

Cần Python 3.12 và Node.js/npm. Từ thư mục repo, mở PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Set-Location frontend
npm ci
npm run build
Set-Location ..
Copy-Item .env.example .env
python -m novel_ai.web.main
```

Mở `http://127.0.0.1:8000`. Server chỉ bind loopback và phục vụ WebUI cùng API `/api/v1`. Sau khi sửa frontend, chạy lại `npm run build`. Để phát triển frontend, dùng `npm run dev` trong `frontend/` cùng server Python.

Mặc định `NOVEL_AI_USE_FAKE_LLM=true` để không gọi mạng. Fake LLM không tạo nội dung truyện trong app; muốn generate thật, đặt `NOVEL_AI_USE_FAKE_LLM=false`, `NOVEL_AI_API_BASE_URL`, `NOVEL_AI_API_KEY` và `NOVEL_AI_MODEL` trong `.env` hoặc biến môi trường. Có thể xem cấu hình đã che secret bằng `python -m novel_ai.config`. Các tùy chọn khác nằm trong [.env.example](.env.example).

## Luồng sử dụng

1. **Tạo project** và chọn ngôn ngữ, genre, style.
2. **Co-create**, rồi bấm **Finalize Idea** để chốt Base Idea.
3. **Architect** tạo Premise, Characters, World Rules và Foreshadow; kiểm candidate trước khi Accept.
4. **Long Plan** lập Volume/Arc cho toàn bộ `planning_scope` do người dùng chọn. App kiểm coverage liên tục trước Accept.
5. **Short Plan** lập chương trong Arc. POV và độ dài mặc định thuộc project, có thể override từng chương; backend chặn yêu cầu thiếu thông tin trước khi gọi LLM.
6. **Skeleton** của chương phải được Accept trước khi **Writer** chạy.
7. **Writer** tạo draft. Người dùng sửa và hoàn tất **Human Review**, rồi bấm **Finalize Chapter**.
8. **Reconcile** cập nhật timeline và quan hệ từ bản final. Chương tiếp theo chỉ mở cho Writer khi chương trước đã `final_reconciled`.

Structured output được validate trước Accept; Auto Accept chỉ áp dụng theo cấu hình project và **không tự finalize prose**. Stream dở hoặc output lỗi được giữ để xử lý, không thay accepted state. Revise upstream, retcon và recovery đều là action tường minh. Khi có pending recovery hoặc trạng thái stale, xem banner trong WebUI trước khi tiếp tục.

## Dữ liệu và backup

Mỗi truyện nằm trong `projects/<slug>/`: config, artifact JSON/Markdown, draft/final chapter, raw output, snapshot, history và transaction ledger. `.env` chứa cấu hình provider; không đưa API key vào thư mục truyện.

Chỉ backup nguyên thư mục project khi không có action ghi đang chạy và không có pending recovery. Đừng sửa JSON thủ công khi app đang mở; thay đổi ngoài app có thể khiến project cần recovery thủ công. Xem [xử lý sự cố](docs/troubleshooting.md).

## Kiểm tra

```powershell
python -m pytest -ra
Set-Location frontend
npm test
npm run build
npm run test:e2e
```

Test Python và Chrome E2E dùng FakeLLM cùng project tổng hợp trong thư mục tạm. Kết quả nghiệm thu B08 trước đợt dọn repo: 576 Python tests, 2 frontend tests và 1 Chrome E2E đều pass. Đây là **kết quả lịch sử**, không thay cho lần chạy hiện tại. Live provider và screen reader chưa được kiểm.

## Tài liệu cho người phát triển

- [AGENTS.md](AGENTS.md): luật sản phẩm, cách nhận task và chuẩn kiểm tra.
- [docs/tasks/README.md](docs/tasks/README.md): registry tiến độ và bàn giao.
- [Spec v0.2](novel_ai_spec_v0.2.md): luật sản phẩm gốc; các quyết định mới hơn ở [decisions.md](docs/design/decisions.md).
- Contract: [workflow](docs/design/workflow.md), [schemas](docs/design/schemas.md), [storage](docs/design/storage.md), [context](docs/design/context.md), [architecture](docs/design/architecture.md), [HTTP API](docs/design/web-api-v1.md).
- Prompt runtime: [manifest v1](docs/prompts/v1/manifest.json). Chỉ nạp prompt, genre và style được manifest đăng ký.

Không có runtime agent, database, RAG, queue hay cloud sync. Repo hiện không có Git; việc dọn repo không tạo commit.
