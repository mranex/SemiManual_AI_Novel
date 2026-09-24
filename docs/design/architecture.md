# Architecture contract

Phiên bản: 2026-09-19; phụ lục milestone A và B ngày 2026-09-23. Phụ thuộc: `workflow.md`, `schemas.md`, `storage.md`, `context.md`.

**Trạng thái hiện tại sau B08:** React/TypeScript gọi FastAPI JSON/SSE cùng origin; adapter gọi
`application` rồi `services/core`, lưu JSON/Markdown. `python -m novel_ai.web.main` là entrypoint;
adapter Streamlit đã gỡ. Các sơ đồ và trách nhiệm Streamlit bên dưới ghi lại thiết kế MVP/A lịch sử.

**Contract MVP lịch sử:** Python + Streamlit + JSON/Markdown; không FastAPI trong MVP. Quyết định milestone A: chuẩn bị Python application boundary cho hướng **FastAPI + React/TypeScript** ở milestone sau. A01 chốt [inventory/contract](application-boundary-a01.md); A02 tách read model/Arbiter/bootstrap, A03 thêm command/event boundary, A04 chuyển Streamlit sang adapter. A chưa viết HTTP server hoặc frontend. JSON/Markdown, không database/RAG/background worker/agent runtime tiếp tục có hiệu lực.

## 1. Ranh giới layer

```text
Streamlit UI/pages
        ↓ explicit user actions
services/*
        ↓ call deterministic core helpers + optional LLM client
core/*
        ↓ file I/O through storage only
project files
```

Đây là sơ đồ implementation MVP trước refactor. `pages/short_plan.py` hiện gọi command `save_writing_defaults` khi Save default. Service không phụ thuộc Streamlit. Core không gọi UI.

Ranh giới của A sau A04 (chi tiết ở [A01](application-boundary-a01.md)):

```text
Streamlit adapter (A04)          FastAPI adapter (milestone sau)
          \                           /
           application: query + command + GenerationEvent
                           ↓
                    services + core
                           ↓
                    JSON/Markdown files
```

`application` là API Python trong process. Query chỉ đọc; command tương ứng action user bấm. `application`/`services`/`core` không import `ui`/`pages`/Streamlit. HTTP transport, React, auth/CORS và nhiều writer đồng thời chưa được quyết định/triển khai trong A.

## 2. Core modules

| Module | Responsibility | I/O |
|---|---|---|
| `core.models` | Pydantic/dataclass models matching `schemas.md`. | Python objects; no file I/O. |
| `core.validation` | Schema and cross-field validation: IDs, effective chapter, projection leak, lifecycle guard. | Objects in, validation result/errors out. |
| `core.storage` | Resolve project root, read/write, atomic replace, operation manifest, recovery. | Chỉ I/O file system qua project root. |
| `core.lifecycle` | Artifact/chapter status transition checks from `workflow.md`. | Current state + requested action -> allowed/denied. |
| `core.context` | Deterministic context selection/projection from `context.md`. | Project read model -> context payload + pins/snapshot. |
| `core.prompts` | Load v1 prompt templates/manifest and render with context payload. | Prompt files -> messages. |
| `core.llm` | OpenAI-compatible client and fake client interface. | Messages/schema -> raw response/stream chunks. |
| `core.project` | Create/load project, high-level read model composition. | Project path -> project state object. |

Core helpers should be unit-testable without Streamlit and without live API. Fake LLM is default for tests.

## 3. Services

Services implement one explicit action each or a small family of related actions. They may call LLM only when the user action asks for generation/review/reconcile.

| Service | Examples | Writes |
|---|---|---|
| `services.co_create` | co-create turn, finalize base idea | `co_create.json`, `idea/*` |
| `services.architect` | generate/accept premise, characters, world rules, foreshadow append | `architect/*`, history/raw |
| `services.long_planner` | generate/accept long plan | `plans/long_plan.json` |
| `services.short_planner` | generate/accept short plan, rolling review/apply | `plans/short_plan.json`, `plans/rolling/*` |
| `services.skeleton` | generate/accept skeleton | `chapters/ch_xxxx/skeleton.json`, snapshots |
| `services.writer` | generate/continue/save draft | `chapters/ch_xxxx/drafts/*`, `chapter.json`, raw |
| `services.reviewer` | AI review, human review mark, rewrite section | `review_reports/*`, `chapter.json`, drafts |
| `services.reconcile` | finalize, reconcile, accept reconciliation | final prose, chapter metadata, state, snapshots |
| `services.revision` | upstream revise, retcon, impact report, stale/reaccept | artifact/state/history bị ảnh hưởng |

No service calls another service to continue the workflow automatically. A service can call shared core helpers, then return result/next suggested action for UI/Arbiter.

## 4. UI and pages

Streamlit responsibilities:

- Hiển thị project tree, artifact status, stale reasons và gợi ý Arbiter.
- Let user trigger explicit actions.
- Show candidates vs accepted revisions clearly.
- Show pending recovery/read-only state if storage needs manual recovery.
- Never hide backend errors by changing state locally.

UI pages may keep ephemeral widget state, but accepted story state lives only in project files through services.
Các action ghi/LLM của page đi qua `ui/action_adapter.py` tới `ApplicationCommands`; helper đọc thuần nằm ở `application/chapter_state.py`, `planning_views.py`, `workspace_state.py` và `queries.py`. Nút editor giữ fingerprint lúc mở form; chỉ Save hoặc Nạp lại explicit mới thay base token.

## 5. I/O contracts by action kind

| Action kind | Input | Output | Mutation boundary |
|---|---|---|---|
| Generate structured | Project path, artifact kind, context selection | Candidate envelope + raw output | Candidate only; accepted unchanged. |
| Accept structured | Candidate id/revision, base fingerprints | Accepted revision + history snapshot | One artifact or scoped patch. |
| Generate prose | Chapter id, skeleton revision | Draft markdown/prose revision | Draft only. |
| Human review | Chapter id, prose revision | Review record | Chapter metadata only. |
| Finalize/Reconcile | Chapter id, final candidate, reconciliation proposal | Final manuscript + state commit | Multi-file transaction manifest. |
| Retcon | Chapter id, retcon draft/proposal | Replacement final + stale marks | Multi-file transaction; không rewrite future prose. |

## 6. Config and secrets

App config may come from environment or local app config outside project:

- API base URL.
- API key.
- model name.
- timeout/generation parameters.
- projects root.

Project files must not contain API key, bearer token or provider account secret. Raw outputs should not include request headers.

## 7. Recovery khi app khởi động

1. Resolve project root.
2. Kiểm tra `.ops/pending` và `.locks/project.lock`.
3. If no pending op, load project normally.
4. MVP design ban đầu dự kiến tự recovery nếu recoverable. Implementation hiện tại hiển thị pending/recovery và nút `reconcile.recover` tường minh; A chốt query mở project chỉ đọc, `recover_project` là command explicit.
5. Nếu không recoverable, hiển thị read-only với recovery report và chặn write services.

This keeps recovery in backend/storage, not in UI buttons alone.

## 8. Điểm bám kiểm thử

Future tests should target:

- `core.validation` for schema/cross-field rules.
- `core.context` for effective chapter and secret filtering.
- `core.storage` cho crash matrix và retry idempotent.
- `core.lifecycle` for Writer/Finalize unlock guards.
- Services with fake LLM and temp project roots.

Live API smoke, if any, is opt-in and never replaces deterministic fake tests.

## 9. Trạng thái sau milestone A (2026-09-23)

`novel_ai/application` cung cấp `ProjectQueries`, `ApplicationCommands` và `GenerationEvent` thuần Python. Streamlit ở `novel_ai/app.py` là adapter hiện tại; action ghi/LLM đi qua command boundary, các read model shell và helper chung nằm ở application. Test kiến trúc xác nhận `application/core/services` không import ngược `ui/pages/streamlit`, và subprocess gọi query + command headless. Trang Streamlit còn một số đường đọc service/storage để dựng form; việc thêm FastAPI không được coi là tự động xóa các đường đọc đó.

Kết quả chạy, matrix luồng và giới hạn: [milestone-a-acceptance.md](milestone-a-acceptance.md). Contract chuyển giao cho adapter kế tiếp: [milestone-b-handoff.md](milestone-b-handoff.md). Chưa có HTTP API hoặc React frontend trong milestone A.

## 10. Trạng thái sau milestone B (2026-09-23)

```text
React/TypeScript → FastAPI JSON/SSE → application query/command/event
                                           ↓
                                      services + core
                                           ↓
                                   JSON/Markdown project files
```

Backend giữ guard ở `application/services/core`; frontend chỉ gửi action user chọn và hiển thị
read model. FastAPI phục vụ `frontend/dist` cùng origin trên loopback, một worker, không auth và
không multi-user. Revision/retcon/recovery nằm trong workspace thứ chín, có confirm riêng cho
action thay canon. Project legacy mở trực tiếp, không migration ngầm. Xem
[HTTP contract](web-api-v1.md), [action matrix](b07-action-matrix.md) và
[nghiệm thu B](milestone-b-acceptance.md).
