# Manual AI Novel

Ứng dụng local hỗ trợ viết tiểu thuyết theo workflow human-in-the-loop:

`Co-create → Base Idea → Architect → Long Plan → Short Plan → Skeleton → Writer Draft → Human Review → Finalize → Reconcile → Next Chapter`

Triết lý sản phẩm: **Potato, but effective.** AI chỉ đề xuất và tạo draft; người dùng quyết định canon.
Kiến trúc là Python + Streamlit + file JSON/Markdown, **không** có runtime agent, database, RAG,
workflow engine hay background worker.

Baseline sản phẩm: [novel_ai_spec_v0.2.md](novel_ai_spec_v0.2.md).
Contract triển khai: [docs/design/](docs/design/) ([workflow](docs/design/workflow.md),
[schemas](docs/design/schemas.md), [storage](docs/design/storage.md),
[context](docs/design/context.md), [architecture](docs/design/architecture.md)).

> **Trạng thái: đợt fix T26–T40 đã xong (offline + kiểm visual 3 độ rộng); live API chưa xác minh.**
> Toàn bộ luồng chính có backend + UI, được phủ bằng test offline (kể cả walkthrough full shell) và
> ảnh chụp app thật ở 1366×768/1600×900/1920×1080. Live API với provider thật và kiểm
> keyboard/focus bằng người dùng **chưa** được xác minh.
> Xem [Trạng thái và giới hạn](#trạng-thái-và-giới-hạn) và
> [báo cáo nghiệm thu đợt fix](docs/design/fix-acceptance-report.md).

## Mục lục

- [Yêu cầu môi trường](#yêu-cầu-môi-trường)
- [Cài đặt](#cài-đặt)
- [Cấu hình app](#cấu-hình-app)
- [Chạy app](#chạy-app)
- [Walkthrough: hoàn thành chương đầu](#walkthrough-hoàn-thành-chương-đầu)
- [Nơi lưu dữ liệu và backup](#nơi-lưu-dữ-liệu-và-backup)
- [Kiểm tra](#kiểm-tra)
- [Cấu trúc source](#cấu-trúc-source)
- [Trạng thái và giới hạn](#trạng-thái-và-giới-hạn)
- [Tài liệu liên quan](#tài-liệu-liên-quan)

## Yêu cầu môi trường

| Thành phần | Giá trị |
|---|---|
| OS / shell | Windows, PowerShell |
| Python | 3.12 (`requires-python = ">=3.12,<3.13"`); đã kiểm tra với 3.12.9 |
| Git | Không cần để chạy app; repo hiện **không** phải Git repository |
| Mạng | Chỉ cần khi tự cấu hình provider thật; mặc định dùng fake LLM offline |

Dependency trực tiếp được pin bằng `==` trong [pyproject.toml](pyproject.toml),
không thêm lock file ngoài:

- `streamlit==1.41.1`
- `pydantic==2.11.7`
- `python-dotenv==1.0.1`
- dev: `pytest==8.3.4`

## Cài đặt

Chạy từ thư mục repo:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Tạo cấu hình local:

```powershell
Copy-Item .env.example .env
notepad .env
```

Không lưu API key trong project truyện, snapshot, fixture hay log. `.env` và `projects/` đã nằm
trong [.gitignore](.gitignore).

## Cấu hình app

App config tách khỏi project truyện. Cấu hình **project** nằm trong
`projects/<slug>/project.json`; không nhồi endpoint/API key vào đó.

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `NOVEL_AI_USE_FAKE_LLM` | `true` | Dùng fake LLM offline, không gọi mạng. |
| `NOVEL_AI_API_BASE_URL` | — | Endpoint OpenAI-compatible; chỉ dùng khi tắt fake LLM. |
| `NOVEL_AI_API_KEY` | — | API key provider; không commit, không ghi vào project. |
| `NOVEL_AI_MODEL` | — | Tên model; không khóa cứng trong code. |
| `NOVEL_AI_LLM_TIMEOUT_SECONDS` | `120` | Timeout gọi API. |
| `NOVEL_AI_LLM_TEMPERATURE` | `0.7` | Tham số sinh. |
| `NOVEL_AI_LLM_MAX_TOKENS` | `4096` | Giới hạn token sinh. |
| `NOVEL_AI_LLM_NATIVE_STRUCTURED_OUTPUT` | `false` | Chỉ bật khi provider hỗ trợ `response_format=json_schema`. |
| `NOVEL_AI_PROJECTS_ROOT` | `projects` | Data root chứa project truyện. |
| `NOVEL_AI_PROMPT_ROOT` | `docs/prompts/v1` | Prompt root đã đăng ký. |

Đường dẫn tương đối được tính từ repo root, không phụ thuộc current working directory.
Biến môi trường thật thắng giá trị trong `.env`.

Kiểm tra cấu hình đang dùng mà không mở UI:

```powershell
python -m novel_ai.config
# hoặc console script sau khi cài editable:
novel-ai-config
```

Output chỉ in `set`/`not set` cho endpoint/model/API key, không bao giờ in giá trị secret:

```text
repo_root: C:\Games\Manual_AI_Novel
projects_root: C:\Games\Manual_AI_Novel\projects
prompt_root: C:\Games\Manual_AI_Novel\docs\prompts\v1
use_fake_llm: True
api_base_url: not set
api_key: not set
model: not set
```

## Chạy app

```powershell
.\.venv\Scripts\Activate.ps1
python -m streamlit run novel_ai\app.py
```

Streamlit mở URL local, thường là `http://localhost:8501`.

Shell theo D018 ([decisions.md](docs/design/decisions.md)) — refinement của spec mục 28:
giữ **vai trò** Project / Current Workspace / Arbiter nhưng **bỏ ba cột luôn mở**.

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ Co-create | Architect | Long Plan | Short Plan | Skeleton | Writer | ... │
│ Current workspace …                              [ Arbiter ▸ mở/đóng ]   │
├──────────────────────────────────────────────────────────────────────────┤
│                          CURRENT WORKSPACE (full width)                   │
│  nội dung workspace đang chọn (gọi service, form/stream/editor)           │
│                                                    ┌───────────────────┐ │
│                                                    │ ARBITER (tuỳ mở)  │ │
│                                                    │ trạng thái tầng,  │ │
│                                                    │ stale/recovery,   │ │
│                                                    │ bước tiếp theo    │ │
│                                                    └───────────────────┘ │
├──────────────────────────────────────────────────────────────────────────┤
│ API — … | OpenAI Compatible | Model: … | Context: … | Project: …         │
└──────────────────────────────────────────────────────────────────────────┘
   ▲ sidebar native (drawer) bên trái: chọn/tạo project, cây artifact/chapter,
     kiểm tra kết nối LLM
```

- **Nav workspace ở đỉnh**: 9 workspace (7 mục của spec cộng `Finalize / Reconcile` và
  `Revision / Retcon / Recovery`). Nav chỉ đổi workspace; nó không chạy action nào.
- **Sidebar `PROJECT` (drawer)**: chọn/tạo project, cây thật (group `Foundation`/`Plans`/`Chapters`,
  mỗi node có glyph `●` accepted, `◆` candidate, `▲` STALE, `○` chưa có) + selectbox chọn node để
  xem ngữ cảnh. Mở/đóng được bằng control sẵn có của Streamlit.
- **`CURRENT WORKSPACE`**: page của workspace đang chọn, chiếm full width khi Arbiter đóng.
- **`ARBITER` (compact, tuỳ mở)**: nút toggle ở toolbar; khi mở mới chiếm cột phải và hiển thị
  trạng thái từng tầng (Base Idea → Short Plan → Skeleton/Writer/Review của chapter đang xét →
  Timeline/Relationship), cảnh báo stale/recovery/pending và "Bước tiếp theo" kèm nút chuyển
  workspace. Arbiter **không** gọi LLM.
- **Cảnh báo recovery / read-only / stale / pending**: luôn hiển thị ở main **kể cả khi** drawer và
  Arbiter đang đóng — guard không phụ thuộc panel nào mở.
- **Status bar ở đáy**: trạng thái API thật (fake/đã cấu hình/đã kết nối/lỗi — không tự báo
  "Connected"), model, context state, project, chapter và prose revision.

`.streamlit/config.toml` tắt nav multipage tự động của Streamlit: `novel_ai/pages/` là workspace
module của router nội bộ (`render(ctx)`), không phải multipage app.

State bền nằm ở file project; `st.session_state` chỉ giữ UI working state (project đang mở,
workspace đang chọn, node đang chọn, kết quả probe LLM, kết quả action **theo từng workspace**,
transcript generation và working copy của editor theo project/artifact/revision).

## Walkthrough: hoàn thành chương đầu

Mặc định app dùng fake LLM nên **không** sinh được nội dung thật (FakeLLM chỉ trả response
được script trong test). Để đi hết luồng bạn cần cấu hình provider thật trong `.env`
(`NOVEL_AI_USE_FAKE_LLM=false` + endpoint/model/key). Với fake LLM ở app thật, các bước dưới
đây chạy tới chỗ gọi model và báo lỗi rõ ràng thay vì sinh nội dung.

1. **Tạo project** — sidebar → tab *Tạo project* → nhập tiêu đề, ngôn ngữ, genre id, style id →
   *Tạo project*. Slug lấy từ tiêu đề; `project_id` do backend cấp (`proj_0001`, …).
2. **Co-create** — trao đổi với AI cho tới khi đủ ý. AI trả `message` + `idea_state`
   (genre/tone/protagonist/core_concept/open_questions). Bấm **Finalize Idea** để tạo
   `idea/base_idea.md` + metadata accepted. Không có Base Idea accepted thì Architect bị chặn.
3. **Architect** — lần lượt Premise → Characters → World Rules → Foreshadow. Mỗi loại có tab
   **Form theo schema** và **Raw JSON (nâng cao)**; riêng Characters / World Rules / Foreshadow có
   **Append** entry mới kèm `effective_from_chapter`. Candidate là draft; chỉ **Accept** mới thành
   canon. Save form/raw **không** tự Accept dù Auto Accept bật.
4. **Long Plan** — nhập `planning_scope.start/end` (**toàn bộ horizon cần kiến trúc**, không phải
   số chương một arc) rồi generate Volume/Arc. Candidate xem được bằng tab **Editor (form)**:
   chọn Volume, mỗi Arc là card (`chapter_range` sửa được), kèm **Preview coverage** đúng luật
   validator; tab **JSON thô (sửa)** dùng cùng guard. Accept chỉ chạy khi coverage liên tục và phủ
   đúng hai đầu horizon. Accepted legacy thiếu `planning_scope` phải xác nhận horizon bằng action
   riêng — app không tự suy từ arc đang có.
5. **Short Plan** — chọn arc; đặt **default viết của project** (`default_pov`,
   `default_length_guidance`) hoặc override từng chương, rồi generate và Accept. Guard backend
   chặn **trước khi gọi LLM** nếu bất kỳ chapter nào còn thiếu language/pov/length. Accept tạo
   `chapter.json` cho các chapter trong arc với `short_plan_pin`. Candidate sửa được bằng form
   (ID selector cho character/world rule/foreshadow) mà không cần JSON.
6. **Skeleton** — chọn chapter, generate rồi Accept. Skeleton là **bắt buộc**: Writer không chạy nếu
   Skeleton chưa accepted và pin của chapter khớp. Candidate có form từng section (index/type/
   instruction/purpose/required beats/forbidden moves/foreshadow surface), thêm–bỏ section trên
   working copy, ID section do backend cấp. Accept set `skeleton_pin` và đưa chapter sang
   `skeleton_ready`.
7. **Writer** — *Generate* để sinh prose (stream hiện raw theo delta; structured output chỉ được
   parse/validate sau khi stream xong). Stream đứt ⇒ draft được giữ ở trạng thái **partial**
   (`is_complete=False`, chapter ở `draft`, *không* mở review) và **không** tự gọi request fallback;
   retry là action tường minh theo `operation_id`. Có thể *Continue*, *Save Draft*, *Regenerate*,
   *Discard*. Prose editor có tab **Edit | Preview** (preview chỉ hiển thị nội dung đang sửa, không
   ghi file) kèm số từ/ký tự. Draft chưa phải canon.
8. **Review** — chạy AI Review (báo cáo vấn đề, **không** tự sửa), sửa prose trực tiếp (cùng editor
   Edit/Preview), hoặc rewrite theo đoạn được chọn. Bấm **Human Review đã xong** cho revision hiện
   tại. Prose revision mới làm Human Review cũ hết hiệu lực.
9. **Finalize / Reconcile** — bấm **Finalize Chapter**: prose được đóng băng thành final candidate,
   chapter sang `finalizing`. Sau đó **Generate reconciliation** để AI trích timeline + relationship
   update; proposal sửa được bằng **form** (timeline group + relationship card theo cặp stable ID)
   hoặc raw JSON, rồi **Accept reconciliation**. Chỉ khi transaction commit xong chapter mới
   `final_reconciled` và Writer chương sau mới được unlock.
10. **Chương tiếp theo** — quay lại Short Plan/Skeleton cho chương N+1 rồi Writer. Hoặc dùng
    workspace **Revision / Retcon / Recovery** cho revise upstream, retcon và recovery.

Mọi action gọi LLM đều có generation surface dùng chung (connecting → streaming/non_streaming →
transport_complete → validating → saved/partial/invalid/error). Chỉ `saved` mới báo
"candidate/draft sẵn sàng"; `partial`/`invalid`/`error` không được Accept/Review/Finalize và không
làm hỏng accepted state. Inventory đầy đủ từng action ở
[docs/design/fix-acceptance-report.md](docs/design/fix-acceptance-report.md) mục 3.

### Auto Accept structured

`project.json` có `auto_accept_structured` (mặc định `false`). Khi bật:

- structured output (Premise, Characters, World Rules, Foreshadow, Long Plan, Short Plan, Skeleton,
  Rolling patch) được validate schema rồi merge tự động;
- **prose không bao giờ** được auto-finalize — Human Review vẫn là gate cứng cho từng chương;
- reconciliation proposal hợp lệ được auto-accept sau khi user đã review và bấm Finalize.

### Rolling Plan

`rolling_plan_every` (mặc định 3) chỉ tạo **lời nhắc** trong Arbiter khi đủ số chương final. Người
dùng bấm action *Review Plan Against Recent Chapters*; output chỉ được sửa **future plan** trong giới
hạn Long Plan. `allow_relationship_replan` quyết định Rolling có được đề xuất đổi hướng quan hệ không.

### Revise upstream, retcon, stale

- **Revise Base Idea / Premise** (workspace Revision) là action tường minh của user: tạo accepted
  revision mới, đánh dấu downstream liên quan `stale`, **không** rewrite Final Manuscript.
- **Retcon**: mở final chapter → *Bắt đầu retcon* tạo draft từ final (final cũ **vẫn là canon**) →
  sửa → review → finalize/reconcile lại. Retcon không tự rewrite chương sau; state downstream bị
  đánh dấu `stale` và cần `reconcile_downstream` để rebuild theo thứ tự.
- **Stale** nghĩa là "artifact từng hợp lệ nhưng upstream đã đổi" — cần review/reaccept hoặc
  regenerate. Không có auto regenerate.
- **Recovery**: nếu crash giữa transaction, banner cảnh báo xuất hiện; bấm *Chạy recovery (backend)*
  để hoàn tất commit đã staged. Nếu target bị sửa ngoài app, project chuyển **read-only** và cần xử
  lý tay (xem [docs/troubleshooting.md](docs/troubleshooting.md)).

## Nơi lưu dữ liệu và backup

Dữ liệu người dùng nằm ở `NOVEL_AI_PROJECTS_ROOT` (mặc định `projects/`), mỗi project một thư mục:

```text
projects/<slug>/
├── project.json                 # config project (không có secret)
├── co_create.json               # working state của Co-create + metadata Base Idea
├── idea/                        # base_idea.md + base_idea.meta.json
├── architect/                   # premise/characters/world_rules/foreshadow + envelope metadata
├── plans/                       # long_plan, short_plan, rolling/
├── chapters/ch_XXXX/
│   ├── chapter.json             # lifecycle + pins + draft/final/review metadata
│   ├── skeleton.json
│   ├── drafts/  final/  review_reports/  reconcile/  retcon/  operations/  rewrite/  impact/
├── state/                       # current_timeline.json, relationships.json, snapshots/
├── raw/                         # raw LLM output theo ngày (giữ khi schema sai/stream đứt)
├── history/<operation_id>/      # snapshot `before/` + manifest của mọi write action
├── .ops/pending/ .ops/done/     # transaction ledger: pending commit + kết quả đã commit
└── .locks/project.lock          # lock write action (multi-file)
```

**Backup an toàn**: chỉ copy thư mục project khi **không** có action ghi đang chạy và banner recovery
không hiện. Copy nguyên thư mục project (kể cả `history/` và `.ops/`) để giữ khả năng audit và
recovery. Không sửa file JSON bằng tay khi app đang mở: app đọc lại state từ disk mỗi lần render, và
fingerprint trong `.ops/` dùng để phát hiện sửa ngoài app (sửa tay có thể khiến project vào
`needs_manual_recovery`).

**Snapshot**: mỗi lần thay accepted revision, bản cũ được copy vào `history/<operation_id>/before/`.
Snapshot context (`state/snapshots/`) ghi lại context đã dùng để tạo candidate.

**Secret**: API key/endpoint chỉ nằm ở `.env`/biến môi trường. `project.json` không có field secret;
mọi thông báo lỗi provider đi qua `redact_secrets` trước khi hiển thị.

## Kiểm tra

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

Kết quả chạy mới nhất (T40, 2026-09-22; Windows, Python 3.12.9, streamlit 1.41.1,
pydantic 2.11.7, pytest 8.3.4):

| Lệnh | Kết quả |
|---|---|
| `python -m pytest -p no:randomly` | **660 passed, 1 skipped, 0 failed** (~85–140s) |
| `python docs\design\examples\check_t29_contracts.py` | PASS |
| `python docs\design\examples\check_planning_examples.py` | PASS |
| `python docs\design\examples\check_writing_examples.py` | PASS |
| `python -m novel_ai.config` | PASS, in cấu hình đã redact |
| `python -c "import novel_ai"` | PASS, import không side effect |
| `python docs\design\examples\check_keyboard_focus.py` | PASS — Tab/Enter/Arrow trên app thật (ảnh/ảnh log ở [docs/design/visual/](docs/design/visual/)) |
| App thật + Edge headless, 16 ảnh ở 900/1366/1600/1920 | PASS — ảnh ở [docs/design/visual/](docs/design/visual/) |

### Kiểm visual và keyboard (T40)

Đã chụp app thật (shell hiện tại) bằng Edge headless trên project demo trong thư mục tạm, ở
**1366×768 / 1600×900 / 1920×1080** với **Arbiter mở và đóng**, cộng ảnh editor từng workspace,
project mới, pending recovery và viewport hẹp 900×800:
[docs/design/visual/](docs/design/visual/). Kiểm visual này phát hiện 5 lỗi thật (2 cảnh báo widget
Streamlit, 1 lỗi không tới được candidate Short Plan, 1 nhãn blocker sai, 1 test flaky theo đồng hồ)
— đều đã sửa kèm test, chi tiết ở
[báo cáo nghiệm thu đợt fix](docs/design/fix-acceptance-report.md) mục 6.1.

Keyboard/focus cũng đã kiểm tự động trên browser thật bằng
[check_keyboard_focus.py](docs/design/examples/check_keyboard_focus.py) (WebSocket/CDP tự viết, không
thêm dependency): Tab phủ nav/drawer/Arbiter/form, Enter thu gọn sidebar, Enter mở Arbiter, Enter trên
"Chuyển tới …" đổi workspace, ArrowDown trên nav radio đổi workspace. Log:
[docs/design/visual/t40-keyboard-focus.txt](docs/design/visual/t40-keyboard-focus.txt).

1 test **skipped**: `tests/unit/test_layout_router.py` — case "page chưa nối" tự skip vì mọi
workspace đã được nối ở T20–T22. Đây là skip có điều kiện, không phải test bị bỏ quên.

Bố cục test:

- `tests/unit/`: config, models round-trip, validation, storage round-trip/transaction, lifecycle,
  context, prompts, LLM adapter, arbiter, layout router, generation event.
- `tests/integration/`: foundation services, planning services, skeleton/writer services,
  writer/review services, finalize/reconcile/revision, UI bằng `streamlit.testing.v1.AppTest`
  (gồm full shell `novel_ai/app.py`), `test_t26…test_t40` cho đợt fix, và
  `test_mvp_acceptance.py` (10 case bắt buộc của `IMPLEMENTATION_PLAN.md` mục 8).
- `tests/fixtures/` + `docs/design/examples/`: truyện giả lập ngắn có secret và lore hiệu lực muộn
  (chương 100) để phát hiện leak.

Toàn bộ chạy offline với `FakeLLMClient` và `tmp_path`; test cô lập cả biến môi trường
`NOVEL_AI_*`, dotenv trên disk và cache config nên **không** đọc `.env`/project thật.

Chi tiết nghiệm thu đợt fix (coverage matrix BUG/UI → task → test, inventory action LLM,
giới hạn còn lại): [docs/design/fix-acceptance-report.md](docs/design/fix-acceptance-report.md).
Mapping 32 invariant của MVP: [docs/design/acceptance-report.md](docs/design/acceptance-report.md).

## Cấu trúc source

```text
novel_ai/
├── app.py                     # entrypoint Streamlit (mỏng, gọi ui.layout.run)
├── config.py                  # app config + entrypoint `novel-ai-config`
├── core/
│   ├── models.py              # model Pydantic, enum, stable ID
│   ├── validation.py          # parse + cross-field validation, guard projection
│   ├── storage.py             # atomic write, transaction, lock, history, recovery
│   ├── project.py             # layout project, create/open/list
│   ├── lifecycle.py           # candidate/accept/stale + guard backend
│   ├── prompts.py             # prompt registry v1 + render
│   ├── context.py             # deterministic context builder + secret filtering
│   └── llm.py                 # LLMClient, OpenAICompatibleClient, FakeLLMClient
├── services/                  # co_create, architect, long_planner, short_planner,
│                              # skeleton, writer, reviewer, reconcile, revision
├── ui/                        # layout (shell/router), project_tree, arbiter, status_bar
└── pages/                     # 9 workspace + _common, _chapter_ui
docs/
├── design/                    # contract T01–T03 + acceptance report + review findings
├── prompts/v1/                # prompt mới (runtime) + manifest.json
├── prompts/*.md               # prompt cũ, chỉ reference, runtime không dùng
├── genres/ styles/ references/
└── tasks/                     # registry + task file
tests/                         # unit/ integration/ fixtures/
.streamlit/config.toml         # tắt nav multipage tự động (pages/ là workspace module)
```

## Trạng thái và giới hạn

Đã có và đã kiểm bằng test offline: tạo/mở project, Co-create → Base Idea, Architect (form schema +
append theo chương), Long Plan theo **complete horizon** do user nhập, Short Plan với default viết
theo project + override từng chương, Rolling Plan, Skeleton bắt buộc, Writer (stream, partial,
continue, regenerate, save, discard), prose editor Edit/Preview, Human + AI Review, Rewrite Section,
Finalize + Reconcile transaction (form + raw), auto accept structured, Current Timeline,
Relationship State, snapshot, revise upstream, stale, retcon + rebuild downstream, recovery sau
crash, Arbiter rule-based, drawer project + Arbiter compact, và 9 workspace UI.

**Chưa xác minh / chưa có:**

1. **Live API chưa chạy.** Không có endpoint/model/key trong môi trường build này. Adapter
   `OpenAICompatibleClient` chỉ được kiểm bằng transport giả (thành công, JSON sai, timeout, 401,
   5xx, stream đứt, `finish_reason=length`, `response_format` khi bật native structured output).
   **Không** có tuyên bố tương thích với provider thật.
2. **Screen reader/AT chưa kiểm.** Keyboard/focus đã kiểm tự động trên browser thật (Tab/Enter/Arrow,
   xem mục *Kiểm visual và keyboard*), nhưng mức hỗ trợ của trình đọc màn hình (ARIA announcement)
   chưa có phiên kiểm chuyên sâu.
3. **Độ rộng < ~1100px**: cột Arbiter bị bó hẹp và nav xuống nhiều dòng; app nhắm desktop ≥1366px,
   người dùng có thể đóng Arbiter để lấy full width.
4. **Chất lượng narrative chưa đánh giá.** BUG-004 sửa ở tầng contract (horizon/coverage/hai đầu);
   plan nhiều arc có *hay hơn* một arc hay không là đánh giá nội dung, cần provider thật + đọc bản
   thảo. App chỉ cảnh báo non-blocking khi plan chỉ có một arc, không đặt quota arc/chương.
5. **Add/Remove/Reorder chưa đủ mọi editor.** Skeleton và Reconciliation có thêm–bỏ trên working
   copy; Long Plan/Short Plan chưa có nút thêm–bỏ arc/chapter (phải sửa field/range hoặc raw JSON
   với `assign_ids`/`assign_chapter_ids`).
6. **Field enum trong editor** (`purpose_visibility`, `writer_context_policy`) chưa có widget nên
   form giữ nguyên giá trị; muốn đổi phải dùng Raw JSON (có guard).
7. **Write một file không acquire project lock** (chỉ multi-file transaction lấy lock) — rủi ro khi
   mở hai phiên Streamlit cùng lúc.
8. **Long Plan có candidate chưa accept ⇒ Short Plan báo "chưa có Long Plan accepted"** dù accepted
   revision vẫn là canon (guard đọc `status` của envelope). Cần quyết định contract riêng trước khi
   sửa; hiện tại hãy accept/reject candidate Long Plan trước khi sang Short Plan.
9. `discard_draft` chỉ bỏ được prose revision **cũ** (storage chặn lùi `current_draft_revision`).
10. `continue_draft` chưa nhận `user_instruction`.
11. `lifecycle.mark_downstream_stale` đánh dấu theo **family** (toàn bộ Short Plan/Skeleton) chứ không
    theo từng arc/chapter, nên có thể đánh dấu rộng hơn phạm vi bị ảnh hưởng.
12. Không có migration framework khi `schema_version` đổi: version chưa hỗ trợ bị **từ chối** kèm
    thông báo, không tự chuyển đổi. Accepted Long Plan legacy thiếu `planning_scope` cần user xác
    nhận horizon bằng action tường minh (không auto migrate).
13. Không có stop-generation, không rich-text editor, Rewrite Section chỉ resolve theo
    `selected_text`. `reconcile_downstream` (đường phục hồi sau retcon) hiển thị transcript nhưng
    chạy **non-stream**.
14. Sửa **nội dung** draft retcon qua Writer/Review bị `chapter_already_final` chặn; chưa có action
    service riêng để regenerate prose retcon.
15. Snapshot/write còn điểm chưa nguyên tử: `storage.save_snapshot` ghi đè khi trùng `snapshot_id`,
    `Project.update_config` không lock/history, `writer._run_generation` ghi ba write rời,
    `reviewer.run_ai_review` tự accept và chưa idempotent theo `operation_id`.
16. Ứng dụng là **local single-user**: không auth, không cloud sync, không multi-user, không
    background worker. Không có cơ chế chống hai process cùng ghi một project (ngoài lock của
    transaction nhiều file).

Đã **sửa ở T25** (trước đây là giới hạn): Short Plan provisional bị chặn ở cả đường accept thủ công;
`_context_basis` trừ state stale sau retcon; `accept_candidate` từ chối candidate chưa validate; kết
quả action được scope theo workspace nên không rò giữa workspace; generate của Architect/Long/Short
Plan đã idempotent theo `operation_id`; snapshot ghi `relationship_versions` thật.

Đã **sửa ở T26–T40**: test cô lập dotenv/env/cache; project tree một tầng expander; điều hướng
Arbiter dùng callback; Long Plan theo complete horizon lưu trên revision; guard contract viết chạy
trước LLM + default/override theo project; shell drawer + Arbiter compact; event generation dùng
chung cho mọi action LLM (kể cả `reconcile_downstream`); editor schema-aware cho foundation,
Long/Short Plan, Skeleton, Reconciliation và prose Edit/Preview.

Chi tiết từng phát hiện kèm `file:line` và cách tái hiện:
[docs/design/review-findings-t24.md](docs/design/review-findings-t24.md) (kèm cập nhật T25) —
các finding T24 **ngoài** phạm vi đợt fix vẫn giữ nguyên trạng thái, không được tuyên bố đã đóng.

## Tài liệu liên quan

- [Specification v0.2](novel_ai_spec_v0.2.md)
- [Kế hoạch triển khai MVP](IMPLEMENTATION_PLAN.md)
- [Kế hoạch đợt fix T26–T40](FIX_IMPLEMENTATION_PLAN.md)
- [Báo cáo nghiệm thu đợt fix T26–T40](docs/design/fix-acceptance-report.md)
- [Báo cáo debug BUG-001–004](DEBUG_REPORT.md) · [UI Review UI-01–04](UI_Review.md)
- [Hướng dẫn sử dụng chi tiết](docs/user-guide.md)
- [Xử lý sự cố](docs/troubleshooting.md)
- [Task registry](docs/tasks/README.md)
- [Contract thiết kế](docs/design/)
- [Báo cáo nghiệm thu T23](docs/design/acceptance-report.md)
- [Findings review T24](docs/design/review-findings-t24.md)
- [Bàn giao T40: nghiệm thu đợt fix](docs/tasks/T40-fix-acceptance-handoff.md)
- [Prompt registry v1](docs/prompts/v1/manifest.json)
- [Hướng dẫn cho coding agent](AGENTS.md)
