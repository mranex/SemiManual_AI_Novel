# Manual_AI_Novel — Hướng dẫn cho coding agent

## App này là gì?

Ứng dụng local hỗ trợ người dùng viết tiểu thuyết theo từng bước, dùng Python, Streamlit và file JSON/Markdown. AI đề xuất nội dung; người dùng sở hữu câu chuyện và quyết định canon. Triết lý: **Potato, but effective.**

Luồng chính:

`Co-create → Base Idea → Architect → Long Plan → Short Plan → Skeleton → Writer Draft → Human Review → Finalize → Reconcile → Next Chapter`

Long Plan quản lý Volume/Arc. Short Plan quản lý các chương trong Arc. Skeleton là chỉ dẫn chi tiết của một chương. Không nhầm chúng với hai prompt cũ `architect-long` / `architect-short`, vốn phân biệt truyện dài và truyện ngắn.

“Không agent” là ràng buộc kiến trúc của **app được xây dựng**: không runtime agent, tự chọn tool, tự điều phối hay autonomous loop. Coding agent vẫn có thể hỗ trợ phát triển repo theo yêu cầu của người dùng.

## Đọc gì trước khi làm?

1. Đọc file này.
2. Đọc `novel_ai_spec_v0.2.md` để hiểu luật sản phẩm.
3. Đọc `IMPLEMENTATION_PLAN.md`, rồi `docs/tasks/README.md`.
4. Đọc task được giao, dependency và tài liệu contract liên quan. Chỉ đọc thêm reference cần cho task.

Thứ tự xử lý yêu cầu: chỉ dẫn rõ ràng mới nhất của người dùng → spec được người dùng chấp thuận → contract triển khai được ghi nhận → kế hoạch/task → tài liệu tham khảo cũ. Không tự biến đề xuất trong kế hoạch thành thay đổi spec. Nếu phát hiện mâu thuẫn ảnh hưởng hành vi, ghi rõ và giải quyết trong phạm vi được giao; cần hỏi khi không thể giữ đúng yêu cầu đã chốt.

`Structure.md` và prompt hiện có trong `docs/prompts/` là **tài liệu tham khảo từ app khác**, không phải contract chạy được của app mới. Không thực thi các chỉ dẫn gọi tool/agent trong chúng. Bộ prompt mới dự kiến ở `docs/prompts/v1/`; không ghi đè tài liệu cũ chỉ để khớp implementation.

## Các luật không được phá

- Authority của chỉ dẫn: `Base Idea > Premise/Architect > Long Plan > Short Plan > Skeleton > Writer`.
- Accepted foundation, Final Manuscript và accepted current state là canon; plan mô tả ý định tương lai, không chứng minh sự kiện đã xảy ra.
- AI output là draft. Validate structured output trước Accept/merge. Auto Accept không tự finalize prose.
- Writer chỉ chạy với Skeleton accepted, còn hiệu lực; chương N > 1 phải có chương N−1 `final_reconciled` và state đầu vào hợp lệ.
- Người dùng review từng chương và bấm Finalize. Reconciliation chưa hoàn tất thì chương sau vẫn khóa.
- Downstream không sửa upstream. Writer không lập plan, không sửa state, không tự tạo major plot hoặc foreshadow.
- Writer không nhận full author truth hoặc future plot. Lọc cả field trong profile/world rule/Skeleton, không chỉ bỏ file foreshadow.
- Entity dùng stable ID; tên hiển thị không phải khóa liên kết. Entry append có `effective_from_chapter`; không leak thông tin tương lai vào chương quá khứ.
- Regenerate tạo candidate riêng. Accepted revision giữ nguyên cho đến khi candidate được accept; snapshot trước thay thế.
- User revise upstream hoặc retcon bằng action rõ ràng. Đánh dấu downstream liên quan `stale`, không tự regenerate/rewrite.
- API lỗi, schema sai hoặc stream dở không được làm hỏng accepted state. Lưu raw output/partial draft để người dùng xử lý.
- File tồn tại không đồng nghĩa artifact hoàn thành. Guard phải nằm ở backend, không chỉ ở nút UI.
- Arbiter là hàm Python dựa trên state/rule, chỉ gợi ý hành động, không gọi LLM hoặc tự chạy bước tiếp.

## Phạm vi kỹ thuật

- Dùng Python + Streamlit; JSON cho structured state, Markdown cho prose và prompt.
- MVP một người dùng local; không thêm database, RAG, embedding, graph, workflow engine, queue, background worker, auth hoặc cloud sync.
- Không thêm FastAPI/React/server API riêng khi Streamlit gọi service trực tiếp đã đủ.
- Prompt lớn ở file, không hardcode vào Python. Context chọn theo ID/rule rõ ràng, không để LLM tự tìm dữ liệu.
- Một adapter OpenAI-compatible là mục tiêu MVP. API key ở cấu hình riêng hoặc môi trường, không trong project truyện, snapshot, fixture hay log.
- Domain/service không phụ thuộc Streamlit. UI gọi action rõ ràng; rerun/rerender không tự sinh API call hoặc commit lần nữa.
- Giữ cấu trúc đơn giản theo spec. Chỉ bổ sung module nhỏ khi phục vụ trách nhiệm cụ thể đã nêu trong task.
- Atomic replace một file không bảo đảm transaction nhiều file. Finalize cần cơ chế commit/recovery được định nghĩa, có retry an toàn và không merge trùng.

## Cách thực hiện một task

1. Kiểm tra trạng thái repo và thay đổi có sẵn; không ghi đè công việc chưa rõ chủ sở hữu. Nếu chưa có Git, không tự commit/push/init trừ khi task hoặc người dùng yêu cầu.
2. Chọn task được giao và kiểm tra dependency. Nếu được yêu cầu “làm task tiếp theo”, chọn task `todo` có dependency `done`, ưu tiên số nhỏ nhất.
3. Đổi trạng thái task thành `in_progress` trong `docs/tasks/README.md`. Không tự nhận thêm task ngoài phạm vi được giao.
4. Đọc acceptance criteria trước khi sửa. Làm đủ code, prompt, tài liệu và kiểm tra thuộc task; tránh refactor lan man.
5. Test hành vi quan trọng và failure path phù hợp. Dùng fake LLM/fixture offline làm mặc định; không gọi API trả phí ngoài phạm vi được cho phép.
6. Cập nhật task registry và phần bàn giao trong file task: thay đổi chính, kiểm tra đã chạy/kết quả, phần chưa xong, quyết định phát sinh.
7. Chỉ đánh dấu `done` khi acceptance criteria đạt. Nếu chưa xong, giữ `in_progress` hoặc ghi `blocked` kèm dependency/lý do cụ thể. Không khai đã test khi chỉ đọc code.
8. Kết thúc bằng báo cáo ngắn: task hoàn thành, kết quả kiểm tra, hạn chế còn lại và task nên làm tiếp. Không tự commit/push/deploy.

Một lần làm việc có thể hoàn thành một hoặc vài task được người dùng giao. Nếu bị ngắt, bàn giao phải đủ rõ để agent khác tiếp tục mà không cần lịch sử chat.

## Chuẩn kiểm tra và tài liệu

- Dùng `pytest` cho luật trạng thái, authority, context, persistence và failure/recovery; UI dùng kiểm tra phù hợp cùng manual checklist.
- Test phải kiểm tra hành vi sản phẩm; tránh test chỉ lặp lại implementation hoặc snapshot toàn bộ văn bản prompt dễ vỡ.
- Dữ liệu test là truyện giả lập ngắn, gồm cả bí mật và lore có hiệu lực muộn để phát hiện leak.
- README phản ánh thứ **đã chạy được**; tính năng chưa có phải ghi rõ. Hướng dẫn Windows/PowerShell là ưu tiên.
- Thay đổi contract phải cập nhật schema/document, fixture và dependency liên quan. Không âm thầm đổi tên field giữa prompt, service và UI.
- Giao tiếp và tài liệu dự án dùng tiếng Việt rõ ràng; tên module/field/API dùng tiếng Anh nhất quán.

## Mốc hiện tại

Repo đang ở giai đoạn chuẩn bị triển khai. Bộ kế hoạch và task không phải bằng chứng app đã được xây dựng. Xem task registry để biết tiến độ thực tế.
