# Workflow decisions

Phiên bản: 2026-09-19. Đi cùng `workflow.md`.

Tài liệu này ghi các quyết định triển khai của T01. Các quyết định ở đây không sửa spec gốc; chúng làm rõ interpretation để các task sau triển khai nhất quán.

## D001 - AI Review là action hỗ trợ, không phải gate MVP

Quyết định: AI Review mặc định là report hỗ trợ người dùng đọc chapter draft. Human Review và Finalize mới là gate cứng.

Lý do: Spec yêu cầu review bắt buộc sau mỗi chapter và mô tả AI Review là kiểm tra vấn đề, không phải validator tuyệt đối. Nếu bắt AI Review là gate cứng ngay ở MVP, lỗi LLM/review có thể chặn người dùng dù họ đã review thủ công.

Hệ quả: Finalize không cần AI Review report tồn tại. UI nên khuyến khích chạy AI Review, nhưng backend chỉ bắt Human Review gắn với prose revision hiện tại.

## D002 - Human Review gắn với prose revision

Quyết định: Human Review hợp lệ chỉ cho đúng `prose_revision` đã review. Sửa draft sau review làm review cũ mất hiệu lực.

Lý do: Nếu review chỉ gắn với chapter, người dùng có thể sửa nội dung sau khi review mà vẫn finalize nhầm bản chưa duyệt.

Hệ quả: Rewrite Section hoặc edit draft tạo prose revision mới và phải review lại trước Finalize.

## D003 - Finalizing dùng final candidate, chưa unlock chương sau

Quyết định: Khi user bấm Finalize, backend đóng băng prose thành `final_candidate` và chuyển chapter sang `finalizing`. Bản này chỉ trở thành Final Manuscript canon để unlock chương sau khi reconciliation commit thành công.

Lý do: Spec yêu cầu Finalize Chapter đi kèm Reconcile State và chapter sau chỉ unlock khi chương trước `final_reconciled`. Cách này tránh trạng thái prose đã final nhưng timeline/relationship chưa đồng bộ.

Hệ quả: Nếu reconciliation lỗi, chapter vẫn `finalizing`; user retry/edit/cancel. Accepted timeline/relationship chưa đổi.

## D004 - Retcon giữ final cũ tới khi commit mới thành công

Quyết định: Trong retcon, final cũ vẫn là canon cho tới khi replacement final candidate và reconciliation commit thành công.

Lý do: Tránh làm hỏng canon đã có nếu retcon bị lỗi giữa chừng.

Hệ quả: Retcon không rewrite future chapters. Sau commit retcon, downstream derived state/artifacts bị stale theo phạm vi dependency.

## D005 - Auto Accept chỉ áp dụng cho structured output hợp lệ

Quyết định: Auto Accept chỉ merge structured candidate sau parse/schema/ID/scope/freshness validation. Không auto-finalize prose, không mark Human Review, không accept AI Review thành canon.

Lý do: Đây là assumption đã khóa trong spec v0.2.

Hệ quả: Các service phải biết output kind của action. Markdown prose và review confirmation luôn cần user action.

## D006 - Stale là guard yêu cầu quyết định rõ ràng, không phải xóa dữ liệu

Quyết định: Artifact stale được giữ nguyên để xem/audit, nhưng không được dùng cho downstream action cho tới khi user reaccept hoặc regenerate.

Lý do: Stale nghĩa là dependency đổi, không nhất thiết nội dung sai. Người dùng vẫn là authority cuối cùng.

Hệ quả: Backend cần action `Review/Reaccept stale artifact`. Reaccept phải validate lại schema/ID/scope và cập nhật dependency pins.

## D007 - Chapter 1 và chapter N+1 có unlock guard khác nhau

Quyết định: Chapter 1 Writer không cần previous chapter. Chapter N với N > 1 cần chapter N-1 `final_reconciled`.

Lý do: Spec có gate "chương N > 1 phải có chương N-1 final_reconciled"; chương đầu là ngoại lệ tự nhiên.

Hệ quả: T02/T08 nên biểu diễn chapter number rõ ràng để backend guard không dựa vào tên file.

## D008 - Rolling Plan là user-triggered action hoặc Arbiter reminder

Quyết định: `rolling_plan_every` chỉ làm Arbiter nhắc hoặc UI đề xuất action. Nó không tự gọi LLM và không tự apply plan.

Lý do: Spec cấm autonomous loop và mô tả Rolling Plan là action của Short Plan.

Hệ quả: Scheduler/background worker không thuộc MVP. Applying rolling proposal đi qua structured accept như các artifact khác.

## D009 - Semantic correctness không được nâng thành deterministic guarantee

Quyết định: Backend validation kiểm tra schema, ID, scope, dependency và freshness. Nó không tuyên bố bảo đảm chất lượng ngữ nghĩa, nghệ thuật hoặc mọi conflict văn chương.

Lý do: Spec coi AI Review là hỗ trợ, không phải validator tuyệt đối. Deterministic code không nên giả vờ hiểu toàn bộ ý nghĩa truyện.

Hệ quả: Test tập trung vào guard/lifecycle/context leak. Semantic conflict được report tốt nhất có thể bởi review prompt và người dùng quyết định.

## D010 - Prompt mới không được kế thừa giao thức agent từ prompt cũ

Quyết định: Prompt runtime v1 chỉ nhận input app đóng gói và trả output theo schema/kind. Không prompt nào được yêu cầu đọc file, gọi tool, tự lưu hoặc tự chạy bước tiếp.

Lý do: Spec và AGENTS.md phân biệt rõ app mới không có runtime agent; prompt cũ chỉ là reference.

Hệ quả: T04-T06 phải tạo prompt mới trong `docs/prompts/v1/` và manifest rõ ràng; không fallback âm thầm sang prompt cũ.

## D011 - Backend là nơi enforce gate

Quyết định: Mọi guard quan trọng phải nằm trong backend service/domain. UI chỉ là lớp hiển thị và nhập liệu.

Lý do: Spec nói file tồn tại không đồng nghĩa artifact hoàn thành và guard không chỉ nằm ở nút UI.

Hệ quả: Unit/integration test phải gọi service trực tiếp để kiểm tra guard.

## D012 - Ranh giới trách nhiệm sang T02/T03

Các chi tiết sau thuộc phạm vi T02/T03 ở mức tên field và layout cụ thể; hành vi ràng buộc đã được T01 chốt trong `workflow.md`:

- T02 chốt schema cụ thể cho metadata, revision, dependency pins, candidate và review record.
- T03 chốt layout storage cho raw output, partial draft, history snapshot, pending transaction và recovery.
- T03 chốt cách snapshot state theo chapter để context retcon không lấy nhầm "latest" của chương xa hơn.

Các điểm này không chặn T01 vì `workflow.md` đã định nghĩa rule hành vi mà schema/storage phải phục vụ.

## D013 — Cụ thể hóa payload planning ở T05 (2026-09-20)

Giữ top-level schema T02. Dùng một object `{language, pov, length_guidance}` trong ChapterPlan.outline để truyền yêu cầu viết xuống Skeleton.global_constraints; không thêm project config. Dùng preallocated IDs theo T04. Cụ thể hóa nested Rolling patch tại schemas.md mục 6.5: allowlist field ChapterPlan, tách relationship changes, chỉ target eligible future chapters. Config false cấm cả thay đổi quan hệ gián tiếp qua narrative fields.

Lý do: schema trước đã cho outline string/object và Rolling collections nhưng chưa chốt object con; prompt/backend cần cùng một cách đóng gói. Đây là chi tiết contract trong phạm vi T05, không thêm action hay đổi authority sản phẩm.

Hệ quả: T08 dùng shape chi tiết; T10 dùng input registry trong prompt-catalog; T12/T15 truyền contract viết qua projection; T14 validate candidate sau ghép patch và freshness/config/scope trước apply. Fixture T05 độc lập, không sửa fixture T02 hoặc coi plan là actual. Kiểm tra tĩnh không thay backend guard hoặc review ngữ nghĩa.

## D014 — Hiệu chỉnh T05 theo quyền chuẩn bị trước và vai trò Skeleton

Spec mục 4 cho phép chuẩn bị Skeleton tương lai; gate final_reconciled thuộc Writer. ContextBasis actual/provisional và preparation_context app-owned được định nghĩa tại schemas mục 4.1 để giữ giả định riêng với actual. Provisional được lưu candidate, chỉ accept sau user review trên actual. Không tự regenerate/reaccept.

Spec mục 14–16 giao Skeleton thiết kế chi tiết thi hành và surface author truth. Short Plan không phải viết sẵn từng hành động/surface. Stable ID không quyết định số section/arc; Long Plan/Skeleton dùng pool và mapping ID cục bộ trước accept (schemas mục 4.1). Quyết định này thay phần preallocation-only/đòi actual sát target cho mọi generation của T05 cũ; foundation T04 không đổi. Cập nhật contract, fixture và kiểm tra tài liệu trong T05; backend thực thi thuộc T08–T15.

## D015 — Manifest và ranh giới prose/report T06 (2026-09-20)

T06 tạo manifest explicit cho cả 14 prompt, 6 genre, style default độc lập; giữ JSON-message input T04/T05, không dùng nội suy template hoặc fallback reference cũ. Schema refs là alias tài liệu chờ models T08, không khai đã có executable schema. Runtime loader vẫn thuộc T10.

Rewrite theo schema T02: JSON chứa replacement_markdown, notes, changed_intent; chỉ phần prose thay thế trong field đó, không trả toàn chương, không auto-apply khi auto_accept_structured bật. Review/Impact là report; Reconcile chỉ actual từ final candidate và prior N−1. Cụ thể hóa nested types tại schemas mục 10, input/guard tại catalog mục 8; không đổi spec hay top-level payload.

Writer thiếu dữ liệu không được nhét lỗi vào manuscript: backend chặn trước gọi; fallback output rỗng phải bị coi incomplete, không review_required. Kiểm tra tĩnh không chứng minh tuân thủ ngữ nghĩa. T02 relationship fixture chương 1 tham chiếu char hiệu lực 2 được ghi là hạn chế ví dụ lịch sử, không dùng để nới contract effective filtering.
