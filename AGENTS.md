# Manual AI Novel — hướng dẫn cho coding agent

Cập nhật: **2026-09-23**, sau WebUI B08 và đợt dọn tài liệu. Đây là file hướng dẫn duy nhất.

## Hiện trạng và đường đọc

App local một người dùng: Python 3.12 + FastAPI + React/TypeScript, state JSON và prose Markdown. WebUI có 9 workspace; Streamlit đã gỡ. T01–T40, A01–A05 và B01–B08 đã xong. Live provider chưa được xác minh. Chạy bằng `python -m novel_ai.web.main` sau `npm run build` trong `frontend/`.

Khi nhận việc, đọc **task được giao và phần contract liên quan**, rồi mới mở thêm nguồn theo nhu cầu:

- Tiến độ: `docs/tasks/README.md`. Task hoàn thành là lịch sử, không phải việc cần làm lại.
- Luật sản phẩm: `novel_ai_spec_v0.2.md`; quyết định sau spec: `docs/design/decisions.md`.
- Contract: `docs/design/{workflow,schemas,storage,context,architecture,web-api-v1}.md`.
- Prompt runtime: `docs/prompts/v1/manifest.json` và đúng template/genre/style mà manifest đăng ký.
- Chạy app, test và giới hạn: `README.md`; bằng chứng WebUI: `docs/design/milestone-b-acceptance.md`.

Chỉ dẫn rõ ràng mới nhất của người dùng > spec đã chấp thuận > quyết định/contract triển khai > task > tài liệu lịch sử. Không xem kế hoạch cũ hoặc báo cáo review như yêu cầu thay đổi spec.

## Luật sản phẩm

- Authority: `Base Idea > Premise/Architect > Long Plan > Short Plan > Skeleton > Writer`. Accepted foundation, Final Manuscript và accepted current state là canon; plan là ý định tương lai.
- AI output là draft. Validate structured output trước Accept/merge. Auto Accept theo config không tự finalize prose. Save candidate thủ công không tự Accept.
- Writer cần Skeleton accepted và còn hiệu lực. Chương N > 1 cần chương N−1 `final_reconciled` cùng state đầu vào hợp lệ. User review, bấm Finalize rồi Accept reconciliation; không bỏ qua gate này.
- Downstream không sửa upstream. Writer không tự lập plan, sửa state, tạo major plot/foreshadow; không nhận author truth hoặc future plot qua bất kỳ field context nào.
- Entity dùng stable ID. Entry append có `effective_from_chapter`; context chương quá khứ không thấy lore tương lai.
- Regenerate tạo candidate riêng. Accepted revision giữ nguyên tới khi candidate được Accept, có snapshot trước thay thế. Revise upstream/retcon là action rõ ràng và đánh dấu downstream `stale`, không tự rewrite.
- Lỗi API/schema/stream không làm hỏng accepted state. Giữ raw/partial để xử lý; partial/invalid/error không được Accept, Review hoặc Finalize. Retry là action explicit.
- File tồn tại không chứng minh artifact đã hoàn thành. Guard nằm ở backend. Arbiter chỉ gợi ý dựa trên state/rule, không gọi LLM.
- Long Plan phủ complete horizon do user chọn, không suy từ progress. Short Plan phải resolve language/POV/length của mọi chương trước LLM; default POV/length lưu theo project, override theo chương.

## Ranh giới kỹ thuật

Không thêm runtime agent, database, RAG, embedding, graph, workflow engine, queue, background worker, auth hoặc cloud sync nếu không có yêu cầu mới. Service/core độc lập frontend; UI gọi action tường minh, render không tự commit. Prompt lớn ở file, context chọn theo ID/rule. API key chỉ ở env/config, không trong project, fixture hay log.

Finalization nhiều file cần transaction/recovery và retry an toàn. Editor chỉ sửa working copy; Save qua service, không ghi accepted trực tiếp. Metadata/ID/pin do app quản lý; Raw JSON đi qua cùng guard. Recovery, read-only và stale phải hiện trong WebUI.

## Cách thực hiện task

1. Kiểm tra file hiện có và trạng thái repo. Repo hiện **không có Git**; không tự init/commit/push. Không sửa/xóa `projects/acc`, project thật hoặc `.env` để làm test pass.
2. Nếu user giao task cụ thể, đọc acceptance criteria/dependency rồi làm đúng phạm vi. Nếu yêu cầu “task tiếp theo”, tiếp tục task dở của mình; nếu không có, chọn `todo` đủ dependency với số nhỏ nhất trong registry. Không giành task người khác.
3. Với task trong registry, ghi `in_progress`, owner/ngày/file sửa; khi xong cập nhật trạng thái và bàn giao. Không đánh dấu `done` nếu acceptance criteria chưa đạt.
4. Test hành vi và failure path phù hợp bằng FakeLLM/transport stub, project tổng hợp trong thư mục tạm; cô lập env, dotenv và cache. Không gọi provider trả phí để test mặc định.
5. Đổi contract thì cập nhật schema, prompt, fixture và tài liệu liên quan. Giao tiếp/tài liệu dự án bằng tiếng Việt rõ ràng; field/API tiếng Anh nhất quán.
6. Kết thúc với thay đổi, kiểm tra **đã chạy**, giới hạn còn lại và bước tiếp theo. Không dùng số test lịch sử làm kết quả hiện tại.

UI cần kiểm qua WebUI thật ở độ rộng liên quan và Chrome E2E khi phù hợp; test Python không chứng minh pixel/usability. Test offline và live provider là hai bằng chứng riêng.
