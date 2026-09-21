# Architecture contract

Phiên bản: 2026-09-19. Phụ thuộc: `workflow.md`, `schemas.md`, `storage.md`, `context.md`.

MVP dùng Python + Streamlit + JSON/Markdown. Không FastAPI, không database, không RAG, không background worker, không agent runtime.

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

UI không tự mutate file. Service không phụ thuộc Streamlit. Core không gọi UI.

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
4. If pending op recoverable, run storage recovery before UI enables writes.
5. If not recoverable, load read-only with recovery report and block write services.

This keeps recovery in backend/storage, not in UI buttons alone.

## 8. Điểm bám kiểm thử

Future tests should target:

- `core.validation` for schema/cross-field rules.
- `core.context` for effective chapter and secret filtering.
- `core.storage` cho crash matrix và retry idempotent.
- `core.lifecycle` for Writer/Finalize unlock guards.
- Services with fake LLM and temp project roots.

Live API smoke, if any, is opt-in and never replaces deterministic fake tests.
