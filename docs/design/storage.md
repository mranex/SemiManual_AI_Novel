# Storage, transaction và recovery contract

Phiên bản: 2026-09-19. Phụ thuộc: `workflow.md`, `schemas.md`.

Mục tiêu của storage MVP là rõ, đọc được bằng mắt, recover được sau lỗi, và không cần database. Mọi đường dẫn trong project đều tương đối từ project root; không phụ thuộc current working directory.

## 1. Source layout

Source repo giữ code, prompt và tài liệu. Dữ liệu truyện người dùng nằm trong `projects/` theo mặc định và không đưa vào source control.

```text
Manual_AI_Novel/
├── novel_ai/
│   ├── app.py
│   ├── config.py
│   ├── core/
│   │   ├── models.py
│   │   ├── validation.py
│   │   ├── storage.py
│   │   ├── lifecycle.py
│   │   ├── prompts.py
│   │   ├── context.py
│   │   └── llm.py
│   ├── services/
│   ├── ui/
│   └── pages/
├── docs/
│   ├── design/
│   ├── prompts/v1/
│   ├── genres/
│   └── styles/
├── tests/
└── projects/
```

Runtime prompt loader chỉ dùng manifest/prompt v1 được đăng ký. Prompt cũ trong `docs/prompts/` là reference, không fallback runtime.

## 2. Project layout

Layout dưới đây là contract mặc định cho T09/T12. T03 chốt tên thư mục để schema refs trong T02 có nơi lưu cụ thể.

```text
projects/
└── nồi-canh-bên-đường/
    ├── project.json
    ├── co_create.json
    ├── idea/
    │   ├── base_idea.md
    │   └── base_idea.meta.json
    ├── architect/
    │   ├── premise.json
    │   ├── characters.json
    │   ├── world_rules.json
    │   └── foreshadow.json
    ├── plans/
    │   ├── long_plan.json
    │   ├── short_plan.json
    │   └── rolling/
    │       └── rolling_patch_arc_0001.json
    ├── chapters/
    │   └── ch_0001/
    │       ├── chapter.json
    │       ├── skeleton.json
    │       ├── drafts/
    │       │   ├── draft_r0001.md
    │       │   └── draft_r0002.md
    │       ├── final/
    │       │   └── final_r0001.md
    │       ├── review_reports/
    │       │   └── review_report_r0001.json
    │       ├── reconcile/
    │       │   └── reconciliation_r0001.json
    │       └── retcon/
    │           └── draft_r0003.md
    ├── state/
    │   ├── current_timeline.json
    │   ├── relationships.json
    │   └── snapshots/
    │       └── snapshot_ch_0001_skeleton_r0001.json
    ├── raw/
    │   └── 2026-09-19/
    │       └── op_01j..._response.json
    ├── history/
    │   └── op_01j.../
    │       ├── manifest.json
    │       └── before/
    ├── .ops/
    │   ├── pending/
    │   └── done/
    └── .locks/
        └── project.lock
```

Các file JSON artifact (`premise.json`, `characters.json`, `skeleton.json`, `reconciliation_r0001.json`) lưu envelope theo `schemas.md`. Markdown prose/base idea có metadata JSON cạnh bên hoặc trong `chapter.json`; nội dung prose không chứa lifecycle metadata.

## 3. Accepted, candidate, history và raw output

- `accepted_revision` và `candidate_revision` cùng nằm trong envelope artifact tương ứng. Candidate không được ghi đè accepted.
- Raw LLM output lưu trong `raw/YYYY-MM-DD/` và được trỏ bởi `payload_source.raw_output_ref` hoặc `StructuredOutputError.raw_output_ref`.
- Partial writer stream lưu như draft markdown trong `chapters/ch_xxxx/drafts/` với `is_complete: false` trong `chapter.json`.
- Snapshot trước khi thay accepted revision lưu dưới `history/op_<operation_id>/before/` kèm manifest. Snapshot này là bản sao byte-for-byte của file target trước khi replace.
- `history_refs` trong artifact/chapter trỏ tới manifest history, không cần copy vào mỗi artifact nếu manifest đủ rõ.

Không dùng "file exists" để suy luận hoàn thành. Backend luôn đọc metadata status/revision/dependency pins.

## 4. Operation ID, fingerprint và lock

Mọi write action first-class có `operation_id` dạng UUID/ULID do backend tạo hoặc nhận lại khi retry cùng request. Ví dụ: `op_01j8manualainovel0001`.

`OperationManifest` tối thiểu:

| Field | Ý nghĩa |
|---|---|
| `operation_id` | Idempotency key của action. |
| `operation_type` | `accept_artifact`, `save_draft`, `finalize_reconcile`, `retcon_commit`, `mark_stale`, ... |
| `status` | `preparing`, `staged`, `committing`, `committed`, `aborted`, `needs_manual_recovery`. |
| `created_at`, `updated_at` | Thời điểm backend tạo/cập nhật. |
| `base_fingerprints` | Hash/revision của mọi file/revision phải còn nguyên trước khi commit. |
| `write_set` | Danh sách target path, staged path, before path, expected old hash, staged hash. |
| `applied_paths` | Target đã replace thành công. |
| `result` | Tóm tắt khi committed/aborted. |

Fingerprint là hash nội dung file cộng với revision metadata liên quan. Accept candidate phải so base fingerprint hiện tại với manifest/candidate. Nếu lệch, backend từ chối candidate cũ bằng lỗi `stale_candidate` thay vì merge.

`project.lock`:

- Tạo bằng atomic create khi bắt đầu write action.
- Chứa `operation_id`, `operation_type`, `created_at`, `heartbeat_at`.
- Nếu lock tồn tại và operation chưa recover xong, write action khác bị từ chối.
- MVP local một người dùng nên chỉ cần project-level lock, không cần lock từng file.
- Nếu app khởi động thấy lock kèm pending manifest, phải recovery trước khi cho ghi tiếp.

## 5. Atomic replace một file

Quy tắc ghi một file JSON/Markdown:

1. Serialize nội dung mới vào file temp trong cùng thư mục target: `<name>.tmp.<operation_id>`.
2. Parse/validate lại file temp nếu là JSON.
3. Replace target bằng primitive atomic replace của hệ điều hành.
4. Đọc lại target và so staged hash.
5. Xóa temp còn dư nếu có.

Nếu lỗi trước bước replace, target cũ vẫn nguyên. Nếu lỗi sau replace, recovery dùng manifest/hash để biết target đang ở old hay staged version.

## 6. Multi-file commit cho Finalize/Reconcile

Finalize/Reconcile là transaction nhiều file tối thiểu, không phải workflow engine. Một commit thành công có thể ghi:

- `chapters/ch_0001/final/final_r0001.md`
- `chapters/ch_0001/chapter.json`
- `chapters/ch_0001/reconcile/reconciliation_r0001.json`
- `state/current_timeline.json`
- `state/relationships.json`
- `state/snapshots/snapshot_ch_0002_writer_r0001.json` nếu cần chuẩn bị chapter sau
- stale metadata của downstream artifact nếu retcon/upstream revise

Thứ tự commit:

1. Acquire `project.lock`.
2. Build manifest `status = preparing`, ghi vào `.ops/pending/<operation_id>/manifest.json`.
3. Validate base fingerprints và dependency pins.
4. Ghi toàn bộ staged files vào `.ops/pending/<operation_id>/staged/...`.
5. Validate staged JSON/Markdown refs; so staged hash.
6. Copy mọi target hiện có vào `history/<operation_id>/before/...`; ghi `history/<operation_id>/manifest.json`.
7. Update manifest `status = staged`.
8. Update manifest `status = committing`.
9. Atomic replace từng target theo `write_set`; sau mỗi target, update `applied_paths`.
10. Đọc lại mọi target, validate cross-file tối thiểu.
11. Move/copy manifest sang `.ops/done/<operation_id>.json` với `status = committed`.
12. Xóa pending manifest/staged tạm đã an toàn; release lock.

Đọc project khi có pending manifest:

- Loader kiểm tra `.ops/pending/` trước khi đọc state bình thường.
- Nếu có manifest recoverable, tự recovery trước.
- Nếu recovery không xác định được old/staged hash, project mở read-only và UI hiển thị yêu cầu recovery thủ công.
- Không coi chapter là `final_reconciled` chỉ vì một vài target đã được replace; manifest phải committed hoặc recovery phải hoàn tất.

## 7. Crash matrix cho finalize/reconcile

| Điểm lỗi | Trạng thái trên disk | Khi load/retry | Kết quả mong muốn |
|---|---|---|---|
| Trước khi tạo manifest | Không có pending op | Retry tạo operation mới hoặc cùng id | Accepted state không đổi. |
| Sau lock, trước manifest | Lock có thể còn, không pending | Lock stale được xóa sau kiểm tra an toàn | Accepted state không đổi. |
| Manifest `preparing`, chưa staged | `.ops/pending` có manifest, không target đổi | Abort pending, release lock | Accepted state không đổi. |
| Staged đã ghi, chưa copy history | Target chưa đổi | Abort hoặc restage | Accepted state không đổi. |
| History copy một phần | Target chưa đổi; history incomplete | Xóa pending/history op incomplete, retry | Accepted state không đổi. |
| Manifest `staged`, chưa replace target | Staged + history đủ; target old | Có thể abort hoặc continue theo retry cùng operation_id | Nếu continue, commit đủ; nếu abort, target old. |
| Đang `committing`, replace một phần | Một số target staged, một số old; `applied_paths` ghi subset | Recovery tiếp tục replace target còn old bằng staged nếu hash khớp | Commit hoàn tất idempotent. |
| Sau replace hết, trước done manifest | Target đều staged; pending vẫn còn | Recovery validate target hash, ghi done manifest | Commit hoàn tất. |
| Done manifest ghi, lock còn | Target committed; `.ops/done` có result | Release lock, cleanup pending | Commit đã thành công. |
| Retry cùng `operation_id` sau committed | `.ops/done/<id>.json` tồn tại | Return result cũ | Không merge/trích xuất trùng. |
| Retry khác `operation_id` với candidate cũ | Base fingerprint lệch | Reject `stale_candidate` | Không overwrite silent. |
| Target không khớp old hay staged | Có sửa ngoài app hoặc corruption | `needs_manual_recovery` | Không đoán; project read-only. |

## 8. Temporal state và retcon

State thực tế lưu theo entry/chapter, không chỉ "latest blob".

`current_timeline.json` cần thêm metadata triển khai:

- `latest_final_chapter`: chương final cao nhất.
- `latest_consistent_chapter`: chương cao nhất có state chain không stale sau retcon/rebuild.
- `entries[]`: mỗi entry có `chapter_id`, `chapter_number`, `source_final_revision`, `status`, `stale` optional.

`relationships.json` giữ `history[]` theo chapter và cũng có thể có `latest_consistent_chapter`.

Context cho chapter N luôn dùng state as-of N-1:

- Chapter 1: baseline rỗng.
- Chapter mới N: lấy timeline/relationship chain đến N-1 nếu chain consistent.
- Chapter cũ sau retcon: lấy state trước chapter đó theo snapshot/entries, không lấy latest của chapter xa hơn.

Retcon chapter N khi chapter sau đã final:

1. Commit retcon chỉ thay final/reconciliation/snapshot của chapter N và stale metadata downstream.
2. Không rewrite `final.md` của chapter N+1 trở đi.
3. Không ghi lùi `current_timeline` latest thành state chương N như thể chương sau biến mất.
4. Mark entries/artifacts derived from old chain after N là stale; set `latest_consistent_chapter = N`.
5. User rebuild/reconcile downstream theo thứ tự N+1, N+2... bằng action rõ ràng. Mỗi bước dùng state đã rebuild của chương trước.

Nếu user chỉ muốn tiếp tục viết tương lai sau retcon, backend phải chặn cho tới khi required downstream state được review/rebuilt hoặc user explicit retcon/reaccept theo guard.

## 9. Snapshot strategy

Snapshot không thay thế accepted artifacts; nó ghi "context đã dùng" để reproduce/kiểm tra leak.

Tạo snapshot khi:

- Generate/accept Skeleton.
- Generate Writer context.
- Finalize/Reconcile.
- Retcon rebuild/reconcile downstream.

Snapshot lưu ở `state/snapshots/` với tên có chapter/action/revision, ví dụ `snapshot_ch_0002_writer_r0001.json`.

Snapshot phải có:

- `for_chapter_id`, `for_chapter_number`.
- `created_from_action`.
- `dependency_pins`.
- IDs included/excluded do `effective_from_chapter`.
- timeline/relationship as-of refs.
- writer projection hash hoặc full projection debug khi config cho phép.

Snapshot không được chứa API key. Nếu có writer projection debug, nó vẫn phải đã được secret-filter; không lưu author-only vào writer snapshot.

Bổ sung theo schemas.md mục 4.1: snapshot của candidate Short Plan/Skeleton provisional chỉ ghi actual đã có; preparation_context và pins bridge lưu cùng revision metadata, không giả tạo state trước N. PayloadSource.id_map được lưu với candidate/run khi reserve stable ID cho entry cục bộ; retry dùng lại mapping đó, accepted payload đã normalize không còn ID tạm. Candidate provisional không có Writer projection dùng được; không dùng snapshot tồn tại làm bằng chứng unlock. Trước accept cần user review trên actual/pins mới, theo workflow.md.

## 10. Stale propagation đủ dùng

Không xây graph database. Backend tính stale bằng dependency pins, chapter ranges và artifact type.

| Thay đổi | Artifact/state bị stale | Không bị stale |
|---|---|---|
| Revise Base Idea | Premise, foundation downstream, Long/Short Plan, Skeleton, draft/review chưa final, future context snapshots | Final Manuscript đã final; raw/history |
| Revise Premise | Long/Short Plan, Skeleton, draft/review chưa final | Base Idea; Final Manuscript |
| Character/World/Foreshadow append effective future | Artifact future có chapter range >= effective chapter nếu selector liên quan; future context snapshots | Chapters/artifacts có as-of chapter < effective chapter |
| Character/World/Foreshadow append effective past/current | Affected plans/skeletons/drafts/reviews from effective chapter onward | Final prose không rewrite |
| Long Plan replace | Short Plan, Skeleton/drafts/reviews for affected arcs | Foundation; current timeline/relationship |
| Short Plan replace | Skeleton/drafts/reviews for affected chapters | Long Plan; finalized prose |
| Skeleton replace | Writer drafts/reviews/finalizing candidate for that chapter | Short Plan; already final_reconciled prose |
| Finalize/Reconcile chapter N | Context snapshots/future skeletons may need refresh if they pinned prior state | Past chapters |
| Retcon chapter N | Downstream state entries/snapshots/plans/skeletons/drafts/reviews from N+1 as scoped | Future final prose content remains as historical final until explicit retcon |

Stale record tối thiểu: `source_artifact_id`, `source_revision`, `reason`, `affected_range`, `created_at`, `can_reaccept`.

## 11. Raw output và lỗi structured output

LLM raw output được ghi trước khi parse/validation để người dùng debug schema lỗi. Raw ref phải được tạo trong cùng operation hoặc pre-operation write an toàn; raw output không làm accepted state đổi.

Structured output invalid:

1. Lưu raw output.
2. Lưu/append `StructuredOutputError` trong artifact candidate hoặc error log.
3. Giữ accepted revision cũ.
4. Nếu action là reconciliation trong `finalizing`, chapter vẫn `finalizing`, next chapter locked.

## 12. Markdown metadata

Markdown files chỉ chứa prose/base idea. Metadata nằm ở:

- `idea/base_idea.meta.json`
- `chapters/ch_xxxx/chapter.json`
- Artifact envelopes trỏ `markdown_ref`

Không dùng YAML front matter cho lifecycle trong MVP để tránh parse hai nguồn truth.
