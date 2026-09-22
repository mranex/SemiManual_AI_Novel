# Manual_AI_Novel — Kế hoạch triển khai MVP

Ngày lập: 2026-09-19. Baseline sản phẩm: [Specification v0.2](novel_ai_spec_v0.2.md).

**Ghi chú 2026-09-22:** Đây là kế hoạch MVP lịch sử T01–T25, hiện registry ghi đã done. Đợt sửa prototype sau user test nằm ở [FIX_IMPLEMENTATION_PLAN.md](FIX_IMPLEMENTATION_PLAN.md), task T26–T40 trong [registry](docs/tasks/README.md#registry-fix). Các mô tả “chưa có app” bên dưới phản ánh thời điểm lập kế hoạch, không phải hiện trạng.

## 1. Mục tiêu và hiện trạng

Xây một ứng dụng local cho người dùng điều khiển toàn bộ quá trình viết tiểu thuyết bằng AI, từ ý tưởng đến chương được duyệt và đồng bộ state. Ưu tiên workflow đúng, state đáng tin cậy, dữ liệu đọc được và dễ debug.

Hiện có spec, [mẫu dữ liệu tham khảo](Structure.md) và thư viện prompt/style/reference. Chưa có app, schema triển khai, dependency manifest hoặc test suite. Tại thời điểm khảo sát, thư mục chưa là Git repository. Không tính bộ tài liệu kế hoạch là implementation đã hoàn thành.

Tài liệu vận hành:

- [AGENTS.md](AGENTS.md): hướng dẫn cho coding agent.
- [Task registry](docs/tasks/README.md): trạng thái và thứ tự thực hiện.
- `docs/tasks/Txx-*.md`: phạm vi, đầu ra, acceptance criteria và bàn giao của từng task.
- `docs/design/`: contract triển khai sẽ được tạo trong T01–T03.
- `docs/prompts/v1/`: prompt mới sẽ được tạo trong T04–T06.

## 2. Kết quả MVP phải đạt

Người dùng tạo/mở project, chốt Base Idea, xây foundation, duyệt Long/Short Plan, duyệt Skeleton, sinh và sửa prose, xem AI review, bấm Finalize, duyệt reconciliation rồi viết chương tiếp. Có thể đóng/mở lại app mà không mất trạng thái đã lưu.

MVP còn phải xử lý regenerate candidate, auto accept structured, append lore theo chương, rolling plan cho tương lai, revise upstream, retcon, snapshot và API failure. Backend thực thi các gate ngay cả khi gọi trực tiếp mà không qua UI.

Không nằm trong MVP: agent runtime, multi-agent orchestration, RAG, database, auto rewrite toàn truyện, rich-text editor, selected-text magic, import tiểu thuyết cũ, style imitation pipeline, EPUB/DOCX export, nhiều người dùng, cloud sync. Các prompt import/simulation hiện có chỉ được giữ tham khảo.

## 3. Baseline và các quyết định triển khai cần ghi rõ

Các mục sau là mặc định đề xuất cho kế hoạch. T01–T03 phải đối chiếu spec và ghi contract cụ thể trước khi code; nếu cần đổi luật sản phẩm thì phải đưa ra rõ ràng.

| Chủ đề | Hướng triển khai |
|---|---|
| Công nghệ | Python, Streamlit, Pydantic cho validation, pytest cho test; một client OpenAI-compatible. Chọn phiên bản cụ thể và khóa dependency tại T07. |
| Kiến trúc | UI gọi service Python trực tiếp; không thêm HTTP backend riêng. Domain, storage, context và service test được ngoài Streamlit. |
| Nơi lưu | Prompt và reference đi cùng repo; project truyện nằm ở data root có cấu hình, mặc định `projects/`. Không phụ thuộc current working directory. |
| API config | Cấu hình app riêng/môi trường; endpoint, model, timeout và tham số sinh. Không lưu API key trong project. Không khóa cứng tên model. |
| Artifact | Tách accepted revision khỏi candidate; metadata chứa schema version, revision, status và dependency revision cần thiết. |
| Markdown | Base Idea/prose là Markdown, metadata lưu ở JSON tương ứng. Xác định nơi lưu trong storage contract. |
| Auto Accept | Chỉ apply structured candidate sau kiểm tra schema, ID, phạm vi và freshness. Không diễn giải schema valid thành đúng ngữ nghĩa truyện. |
| AI review | Là báo cáo hỗ trợ; không được tự sửa hoặc finalize. Human Review là gate cứng; việc AI review có bắt buộc chạy mỗi revision phải được ghi rõ ở T01. |
| Canon khi finalize | Bản prose người dùng chọn được đóng băng trong bước `finalizing`; chưa được dùng để unlock chương sau trước khi reconciliation commit hoàn tất. T01 xác định rõ cách hiển thị final candidate và bản final cũ khi retcon. |
| Temporal state | Context chương N dùng state trước N; không lấy relationship/timeline mới nhất của chương xa hơn khi retcon hoặc lập lại Skeleton cũ. Lưu snapshot theo chương/revision đủ để dựng lại. |
| Transaction | Có bản ghi pending commit tối thiểu và recovery cho thay đổi nhiều file. Không xây workflow engine. Retry không nhân đôi timeline/relationship. |
| Stale | Theo dependency revision, chương hiệu lực và phạm vi plan; không xây graph framework. Artifact stale chặn action phụ thuộc cho đến khi được review/reaccept hoặc thay thế hợp lệ. |
| Rolling Plan | `rolling_plan_every` tạo lời nhắc trong Arbiter. Người dùng bấm action; chỉ đề xuất/apply future Short Plan trong giới hạn Long Plan. |
| Secret boundary | Tách author-only fields khỏi dữ liệu an toàn cho Writer; `effective_from_chapter` không đồng nghĩa được reveal mọi secret. |
| Genre và style | Genre prompt phục vụ Architect; writing style phục vụ prose. Lựa chọn rõ ràng theo project, không ghép mọi reference thành một prompt khổng lồ. |
| Prompt cũ | Giữ nguyên làm reference. Runtime chỉ load prompt mới đã được đăng ký, không fallback âm thầm sang prompt có tool calling. |

## 4. Structure đích

Đây là cấu trúc dự kiến; T02–T03 chốt field và storage layout. Không tạo file rỗng để giả hoàn thành module.

```text
Manual_AI_Novel/
├── AGENTS.md
├── IMPLEMENTATION_PLAN.md
├── README.md                         # tạo hướng dẫn tối thiểu ở T07, hoàn thiện T24
├── pyproject.toml
├── .env.example
├── .gitignore
├── novel_ai_spec_v0.2.md
├── Structure.md                      # reference cũ
├── novel_ai/
│   ├── app.py
│   ├── config.py
│   ├── core/
│   │   ├── models.py                 # tách nhỏ theo domain nếu cần
│   │   ├── validation.py
│   │   ├── storage.py
│   │   ├── project.py
│   │   ├── lifecycle.py
│   │   ├── prompts.py
│   │   ├── context.py
│   │   └── llm.py
│   ├── services/
│   │   ├── co_create.py
│   │   ├── architect.py
│   │   ├── long_planner.py
│   │   ├── short_planner.py
│   │   ├── skeleton.py
│   │   ├── writer.py
│   │   ├── reviewer.py
│   │   ├── reconcile.py
│   │   └── revision.py
│   ├── ui/                          # layout, project tree, Arbiter, status
│   └── pages/                       # các workspace theo spec
├── docs/
│   ├── design/                      # architecture, schemas, storage, workflow
│   ├── prompts/
│   │   ├── *.md                     # reference cũ
│   │   └── v1/                      # prompt mới, runtime dùng explicit manifest
│   ├── genres/                      # genre prompt mới
│   ├── styles/                      # style được rà soát trước khi đăng ký
│   ├── references/
│   └── tasks/
├── tests/
│   ├── fixtures/
│   ├── unit/
│   └── integration/
└── projects/                        # dữ liệu người dùng, không đưa vào source control
```

Layout một project giữ các nhóm của spec: `project.json`, `idea/`, `architect/`, `plans/`, `chapters/`, `state/`, `history/`. T03 bổ sung vị trí candidate, raw response, reconciliation pending và snapshot theo chương; không trộn chúng với accepted artifact.

## 5. Contract dữ liệu cần có

T02 phải định nghĩa schema, ví dụ hợp lệ/không hợp lệ và quan hệ giữa:

- Project config và trạng thái Co-create; Base Idea đã chốt.
- Artifact metadata, candidate, accepted revision và stale reason/dependency.
- Premise; Character, World Rule, Foreshadow với stable ID, hiệu lực và author-only fields.
- Long Plan gồm Volume/Arc; Short Plan gồm chapter và ID context selector.
- Skeleton có section/instruction, mục đích, beats/constraints và projection an toàn cho Writer.
- Chapter metadata; draft hoàn chỉnh/partial; review gắn đúng prose revision.
- Current Timeline, Current Relationship, snapshot và reconciliation proposal.
- Rolling Plan proposal; retcon/impact report; lỗi validation có đường dẫn field rõ ràng.

Các field hữu ích từ `Structure.md` được cân nhắc cho chapter contract; không bê nguyên JSONL timeline, liên kết nhân vật theo tên hoặc layered outline gộp mọi tầng sang app mới.

## 6. Chuyển bộ prompt cũ thành bộ prompt mới

Prompt mới nhận input do app đóng gói, trả một output theo contract; không gọi tool, đọc file, tự tiếp tục hay tự lưu state.

| Nhóm | Output | Nguồn tham khảo |
|---|---|---|
| Co-create | message + idea_state | Spec mục 32, differentiation |
| Architect | Premise / Characters / World Rules / Foreshadow candidate | architect-long/short, character-building, genre references |
| Long Plan | Volume/Arc candidate | longform-planning, plot structures, arc templates |
| Short Plan | Chapter plans trong arc | outline-template, chapter contract mẫu |
| Rolling Plan | deviations + future changes | Spec mục 22, revision analysis |
| Skeleton | Section-level instructions | chapter-guide, hook/dialogue techniques |
| Writer | Markdown prose | voice, style, anti-ai-tone; bỏ toàn bộ execution protocol cũ |
| Review | Issues có bằng chứng và nguồn ràng buộc | editor, quality-checklist; không tự accept/rewrite |
| Rewrite Section | Prose cho đoạn được chọn | Writer constraints và yêu cầu user |
| Reconcile | Timeline + relationship updates | Spec mục 20, nguyên tắc trích xuất fact của import/revision |
| Retcon impact | Báo cáo downstream conflicts | revision-analyze; không mutation |

Mỗi prompt phải ghi input/output, phạm vi quyền hạn, cách xử lý thiếu dữ liệu/xung đột và ví dụ. Manifest chỉ định template, biến đầu vào, output kind/schema và reference được dùng. Không nhét toàn bộ reference vào mọi request.

## 7. Các giai đoạn và cổng nghiệm thu

| Giai đoạn | Task | Kết quả để sang bước tiếp |
|---|---|---|
| A. Chốt contract | T01–T03 | Luật workflow, schema và persistence đủ cụ thể để code không tự đoán |
| B. Bộ prompt mới | T04–T06 | Đủ prompt cho MVP, không còn giao thức agent, schema nhất quán |
| C. Nền backend | T07–T12 | Project round-trip, lifecycle, LLM fake/real adapter và context có guard |
| D. Workflow backend | T13–T18 | Chạy được luồng 2 chương bằng fake LLM, có review/finalize/recovery/retcon |
| E. Frontend | T19–T22 | Người dùng hoàn thành cùng luồng qua Streamlit và thấy rõ draft/accepted/stale |
| F. Nghiệm thu và bàn giao | T23–T24 | Regression offline đạt, hướng dẫn chạy và hạn chế được ghi trung thực |

Làm tuần tự theo dependency trong registry; mỗi phiên chỉ nhận một hoặc vài task. Không cần hoàn thành mọi backend trước khi xem UI: T19 có thể làm ngay sau các dependency của nó, nhưng UI không được thay thế service bằng logic tạm khó gỡ.

## 8. Chiến lược kiểm tra

Unit test cho schema, ID/effective filtering, lifecycle, stale rules, context projection và Arbiter. Integration test cho storage/revision, finalize nhiều file, retry sau lỗi, retcon và hai chương nối tiếp. Dùng thư mục tạm; test không đụng project người dùng.

Fake LLM phải có kịch bản: response hợp lệ, JSON sai, ID không tồn tại, timeout, stream đứt, output từ dependency revision cũ. Test offline là mặc định. Live API smoke chỉ chạy khi được cho phép, có endpoint/model/key hợp lệ, không thay thế test deterministic; nếu chưa chạy phải ghi rõ.

Các case bắt buộc ở T23:

1. Chương 1 hoàn tất Human Review → Finalize → Reconcile thì Writer chương 2 mới unlock.
2. Auto Accept bật vẫn không tự finalize prose.
3. Accepted state không đổi khi regenerate thất bại hoặc candidate chưa được accept.
4. Lore hiệu lực chương 100 không xuất hiện trong context chương 20; author-only secret không lọt qua profile hay Skeleton purpose.
5. Retry Finalize/reconcile và Streamlit rerun không merge hoặc sinh request trùng.
6. Crash giữa commit nhiều file được recovery về state nhất quán; không unlock sớm.
7. Sửa Premise đánh dấu planning liên quan stale, giữ nguyên Final Manuscript.
8. Retcon chương cũ dùng state đúng thời điểm, làm stale downstream cần thiết, không âm thầm ghi lùi current state đang ở chương mới.
9. Rolling Plan không sửa chương final, foundation hoặc Long Plan; config quan hệ được tôn trọng.
10. Đóng/mở lại app vẫn thấy accepted revision, candidate, partial draft và pending reconcile đúng trạng thái.

## 9. Quy tắc chia phiên và bàn giao

Task registry là nguồn tiến độ duy nhất; master plan mô tả phạm vi và dependency, không duy trì một bảng trạng thái thứ hai. Mỗi task có phần bàn giao ban đầu trống để người thực hiện cập nhật.

Mẫu yêu cầu sử dụng:

> Đọc AGENTS.md, IMPLEMENTATION_PLAN.md và docs/tasks/README.md. Thực hiện T01, chỉ trong phạm vi task. Cập nhật trạng thái và bàn giao, chạy các kiểm tra phù hợp; chưa commit/push.

> Tiếp tục task đang in_progress. Nếu không có, làm task todo có dependency đã done, ưu tiên số nhỏ nhất. Chỉ làm một task rồi bàn giao.

Một task lớn có thể kéo dài qua nhiều phiên. Dừng ở trạng thái có thể tiếp tục, ghi rõ những gì đã làm và còn thiếu; không đánh dấu done để khớp thời lượng phiên. Nếu tách thêm task, cập nhật registry và dependency, giữ truy vết task gốc.

## 10. Definition of Done cho toàn MVP

- Đủ hành vi thuộc mục 41 của spec, với streaming nếu provider hỗ trợ và failure path rõ ràng.
- 32 invariants của spec có mapping tới guard, test hoặc manual acceptance cụ thể; không tuyên bố code tự chứng minh được chất lượng ngữ nghĩa của truyện.
- Prompt và reference runtime thống nhất với schema; không còn agent/tool instructions trong prompt mới.
- UI thực hiện được toàn bộ luồng chính, sửa draft, retry reconcile, regenerate, append, rolling plan và retcon rõ ràng.
- Accepted data và snapshot được bảo vệ; secret cấu hình không lọt vào dữ liệu truyện/log.
- Test offline và manual UI checklist có kết quả ghi lại. Live API chưa thử thì phải ghi là chưa xác minh.
- README có cài đặt, cấu hình, lệnh chạy/test, workflow, backup/recovery và giới hạn thực tế.
- Tất cả task done hoặc phạm vi thay đổi đã được người dùng chấp thuận rõ ràng.
