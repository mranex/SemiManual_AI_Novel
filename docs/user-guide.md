# Hướng dẫn sử dụng Manual AI Novel

Tài liệu này đi sâu vào từng workspace, các action và luật hành vi của app. Cài đặt, cấu hình và
lệnh chạy nằm ở [README.md](../README.md).

- [Mô hình tư duy](#mô-hình-tư-duy)
- [Bố cục màn hình](#bố-cục-màn-hình)
- [Các khái niệm trạng thái](#các-khái-niệm-trạng-thái)
- [Workspace Co-create](#workspace-co-create)
- [Workspace Architect](#workspace-architect)
- [Workspace Long Plan](#workspace-long-plan)
- [Workspace Short Plan](#workspace-short-plan)
- [Workspace Skeleton](#workspace-skeleton)
- [Workspace Writer](#workspace-writer)
- [Workspace Review](#workspace-review)
- [Workspace Finalize / Reconcile](#workspace-finalize--reconcile)
- [Workspace Revision / Retcon / Recovery](#workspace-revision--retcon--recovery)
- [Arbiter đọc gì](#arbiter-đọc-gì)
- [Luồng hai chương đầy đủ](#luồng-hai-chương-đầy-đủ)
- [Điều app cố tình **không** làm](#điều-app-cố-tình-không-làm)

## Mô hình tư duy

App coi AI như một hàm: `output = ai(input)`. Không có runtime agent, không có vòng lặp tự chạy,
không module nào tự gọi module khác để đi tiếp workflow. Mỗi bước là một action do **bạn** bấm.

Hai câu cần nhớ:

1. **AI output là draft.** Nó chỉ thành canon sau khi bạn Accept (structured) hoặc Finalize (prose).
2. **Tầng trên thắng tầng dưới.** `Base Idea > Premise > Long Plan > Short Plan > Skeleton > Writer`.
   Khi mâu thuẫn, tầng dưới phải nhường; app không tự sửa canon mà chỉ báo.

## Bố cục màn hình

Theo `novel_ai_spec_v0.2.md` mục 28, màn hình có nav ở đỉnh, ba vùng giữa và status bar ở đáy:

| Vùng | Nội dung | Ghi chú |
|---|---|---|
| Nav (đỉnh) | 9 workspace | Bấm nav chỉ **đổi workspace**; không chạy action, không gọi LLM. |
| `PROJECT` (trái) | Cây artifact/chapter | Glyph: `●` accepted, `◆` candidate, `▲` STALE, `○` chưa có, `⚠` recovery. Cây đọc **metadata**, không suy từ file. |
| `CURRENT WORKSPACE` (giữa) | Page của workspace đang chọn | Mọi action ghi dữ liệu nằm ở đây. |
| `ARBITER` (phải) | Trạng thái + bước tiếp theo | Chỉ đọc state; nút "Chuyển tới …" đổi workspace giúp bạn. |
| Status bar (đáy) | API/model/context/project/chapter/prose | Không tự báo "Connected": chỉ hiện `Đã kết nối` sau khi bạn bấm **Kiểm tra kết nối** và request thật thành công. |
| Sidebar | Chọn/tạo project + kiểm tra kết nối LLM | Streamlit sidebar; phần workspace không còn ở đây. |

Cây project còn có một selectbox **"Node đang xem"** để bạn chọn nhanh một artifact/chapter và xem
ngữ cảnh của nó; nó chỉ là tiện dụng, không đổi workspace và không ghi gì.

Kết quả của một action hiện ở đầu workspace **đã bấm action đó** và không rò sang workspace khác.

## Các khái niệm trạng thái

### Structured artifact

Mọi artifact structured (Premise, Characters, World Rules, Foreshadow, Long Plan, Short Plan,
Skeleton, reconciliation, impact report) có lifecycle:

| Trạng thái | Nghĩa | Thấy trong cây project |
|---|---|---|
| `missing` | Chưa có gì. | `—` |
| `draft` | Có candidate đang chờ bạn xem/accept. Accepted cũ (nếu có) **giữ nguyên**. | `candidate` |
| `accepted` | Đã được duyệt; là canon (trừ plan/skeleton chỉ là intent/instruction). | `accepted` |
| `stale` | Từng hợp lệ nhưng upstream đã đổi; cần review lại. Không tự regenerate. | `STALE` |
| `rejected` | Candidate bị từ chối; accepted cũ không bị xoá. | `rejected` |

Điểm quan trọng: **file tồn tại không có nghĩa artifact hoàn thành.** App luôn đọc metadata
(`status`, `revision`), không suy từ sự tồn tại của file.

### Chapter

```text
planned → skeleton_ready → draft → review_required → finalizing → final_reconciled
```

Writer của chương N+1 chỉ được unlock khi chương N ở `final_reconciled`. Long Plan / Short Plan /
Skeleton của chương sau có thể chuẩn bị trước, nhưng **Writer không nhảy qua review gate**.

### Regenerate và candidate

Regenerate **không** ghi đè accepted revision ngay. Nó tạo candidate riêng; accepted cũ giữ nguyên
cho tới khi bạn accept candidate mới. Bản accepted cũ được snapshot vào
`history/<operation_id>/before/` trước khi bị thay.

### Partial draft

Nếu stream bị đứt, draft được giữ với `is_complete = False`. Chapter ở `draft` — **không** mở
review/finalize. Bạn có thể Continue, Regenerate hoặc Discard.

## Workspace Co-create

Mục đích: biến ý tưởng rời rạc thành một Base Idea chốt.

- Chat với AI; mỗi lượt AI trả `message` cho bạn và `idea_state`
  (`genre`, `tone`, `protagonist`, `core_concept`, `setting`, `conflict`, `constraints`,
  `open_questions`).
- `idea_state` là **working state**, chưa phải canon.
- **Finalize Idea** ghi `idea/base_idea.md` + metadata accepted, và đó mới là Base Idea.
- Lỗi API/timeout không làm hỏng state: raw output được lưu ở `raw/` để bạn xử lý.

## Workspace Architect

Bốn phần: **Premise**, **Characters**, **World Rules**, **Foreshadow**. Premise là tầng authority
cao thứ hai; ba phần còn lại thuộc Architect nhưng append được trong lúc viết.

Action chung: `Generate`, `Regenerate`, `Edit`, `Accept`, `Reject`. Riêng Characters / World Rules /
Foreshadow có `Append`.

### Append theo chương (`effective_from_chapter`)

Entry mới **bắt buộc** có `effective_from_chapter`. Ví dụ:

```json
{
  "world_rule_id": "rule_0100",
  "effective_from_chapter": 100,
  "content": "Từ chương 100 trở đi tồn tại Cơ quan phòng chống tội phạm xuyên giới."
}
```

Rule này **không** vào context của chương 1–99, và không retroactively làm như nó đã tồn tại từ đầu.
Append không làm quá khứ `stale` (đây không phải retcon).

### Secret và visibility

`WorldRule.visibility` nhận `writer_safe` / `skeleton_only` / `planner_only` / `author_only`:

- `writer_safe`: `content` được gửi cho Writer nếu không có `writer_projection`.
- Các mức còn lại: **không bao giờ** gửi `content` cho Writer. Chỉ gửi `writer_projection` khi bạn
  khai tường minh — hãy viết field này như một bản diễn đạt an toàn (không giải thích truth).

Foreshadow dùng `truth_author_only` (truth đầy đủ) và `writer_visibility`; Skeleton chuyển truth
thành `foreshadow_surfaces[].surface_instruction` an toàn cho Writer. Writer **không** nhận truth.

## Workspace Long Plan

Cấp `Volume → Arc`. Long Plan quản lý:

- hướng lớn và arc goal;
- core conflict;
- start/end direction;
- **Arc-level relationship direction**;
- major reveal direction;
- important story thread.

Long Plan **không** viết chapter prose và **không** chứng minh sự kiện đã xảy ra.

## Workspace Short Plan

Cấp `Arc → Chapter`. Mỗi chapter tối thiểu gồm `chapter_id`, `chapter_number`, `summary`, `hook`,
`outline`, `characters`, `world_rules`, `foreshadow_ids`, `threads`, `relationship_changes`,
`chapter_goal`, `planned_ending`. Các ID này đồng thời là **deterministic context selector**: chỉ
entity được khai mới vào context (không similarity search).

`outline` nhận item string (beat) hoặc item object `{language, pov, length_guidance}`.

Accept Short Plan sẽ tạo `chapter.json` cho các chapter trong arc với `short_plan_pin`.

Hai luật cần biết khi lập lại plan:

- **Chapter đã `finalizing`/`final_reconciled` không được lập lại.** Nó bị lọc khỏi
  `assigned_chapters`; nếu mọi chapter được giao đều đã final thì action bị từ chối
  (`chapter_already_final`). Plan của chương đã final là canon.
- **Candidate `provisional` không accept được nếu đã bị actual vượt qua.** Khi một chương mà plan chỉ
  mô tả theo *ý định* (`planned_bridge`) đã có prose, bạn phải regenerate/review trên state thật rồi
  mới accept (`provisional_candidate_not_writer_ready`). Lập plan trước khi viết thì vẫn bình thường.

Accept Short Plan **không** hạ trạng thái chapter đang có prose: chapter `draft`/`review_required`/
`skeleton_ready` giữ nguyên lifecycle, chỉ `title`/`previous_chapter_id`/`short_plan_pin` được cập nhật.

### Rolling Plan

Rolling Plan là action **của Short Plan**, không phải subsystem riêng. Khi `rolling_plan_every`
(mặc định 3) đạt ngưỡng, Arbiter nhắc. Bạn bấm *Review Plan Against Recent Chapters*; output gồm
`deviations`, `short_plan_changes`, `relationship_plan_changes`. Apply chỉ sửa **future plan**:
không Base Idea, không Premise, không Final Manuscript, không Current Timeline quá khứ. Nếu
`allow_relationship_replan = false`, mọi đề xuất đổi hướng quan hệ bị từ chối.

## Workspace Skeleton

Skeleton là **bắt buộc**; không có đường `Short Plan → Writer`. Skeleton là dàn ý tuyệt đối của
chapter, gồm các section với `index` liên tục từ 1. Mỗi section có `type`, `instruction`,
`purpose`, `purpose_visibility`, `writer_notes`, `required_beats`, `forbidden_moves`,
`foreshadow_surfaces`.

Chỉ field writer-safe được gửi Writer:

- `instruction`, `writer_notes`, `required_beats`, `forbidden_moves`,
  `foreshadow_surfaces[].surface_instruction`;
- `purpose` **chỉ khi** `purpose_visibility = writer_safe`.

`author_only_notes` và `purpose` kín không bao giờ tới Writer. Backend tự chạy guard
`validate_writer_projection` trước khi gửi prompt; nếu phát hiện field cấm thì **raise** thay vì gửi.

Accept Skeleton set `chapter.skeleton_pin` và đưa chapter sang `skeleton_ready`. Nếu pin lệch với
Skeleton accepted hiện tại (ví dụ bạn regenerate Skeleton), Writer và Finalize bị chặn cho tới khi
accept lại.

### Provisional candidate

Nếu chương trước chưa có state **hợp lệ** (`final_reconciled` và không stale), context của
Skeleton/Short Plan chỉ có `actual` tới chương đã hợp lệ; phần còn lại là `planned_bridge` (ý
**định** từ plan, không phải sự kiện). Candidate khi đó ở mode **provisional**:

- Skeleton provisional: **không** accept được (guard cứng, như trên).
- Short Plan provisional: accept được khi bạn vẫn đang lập kế hoạch trước; **không** accept được nếu
  một chương trong bridge đã có prose — lúc đó regenerate/review trên actual trước.

Sau retcon, `timeline.latest_consistent_chapter` bị hạ xuống chương retcon, nên plan/Skeleton dựng cho
chương sau đó là provisional cho tới khi bạn rebuild state (`Reconcile downstream`) — dù chapter
metadata vẫn ghi `final_reconciled`.

## Workspace Writer

Writer có đúng một nhiệm vụ: biến Skeleton thành prose. Guard chạy **trước** khi gọi LLM, nên guard
fail thì không có request nào được phát đi.

Writer context **chỉ** gồm: Skeleton projection, writing style, relevant character profile, current
relationship state, relevant world rules, current timeline, language/POV constraints.

Writer **không** nhận: Long Plan/Short Plan đầy đủ, full foreshadow database, secret chưa được
Skeleton surface, rejected alternatives, future plot.

Writer cũng **không**: lập plan, thêm major plot, đặt foreshadow mới, sửa relationship direction, đổi
World Rules, tạo twist lớn, sửa premise, sửa timeline state. Invention nhỏ (tên món ăn, thời tiết,
động tác) được phép, nhưng chỉ thành canon prose khi bạn Finalize.

Action: `Generate`, `Continue`, `Save Draft`, `Regenerate`, `Discard`.

## Workspace Review

Review là gate cứng sau mỗi chapter.

**Manual Review**: bạn đọc và sửa prose. Có thể sửa cả text area, hoặc rewrite theo đoạn (chọn
`section_id` hoặc dán `selected_text` + instruction). Không có `Apply All` rewrite toàn chapter.

**AI Review**: AI kiểm tra skeleton adherence, Base Idea/Premise/Character/World Rule conflict,
timeline + relationship continuity, foreshadow instruction và logic issue. AI Review **chỉ báo vấn
đề** trong một report — nó không sửa prose, không finalize, không phải validator tuyệt đối.

Sau khi sửa xong, bấm xác nhận **Human Review** cho revision hiện tại. Mọi prose revision mới (Save,
Regenerate, Continue) làm Human Review cũ mất hiệu lực.

## Workspace Finalize / Reconcile

Finalize Chapter là hành động first-class, không phải "lưu file".

1. Bạn bấm **Finalize Chapter** (Human Review phải hợp lệ cho draft hiện tại).
2. Prose được đóng băng thành final manuscript candidate; chapter sang `finalizing`.
3. **Generate reconciliation**: AI trích `timeline` (`time`, `location`, `status`) và
   `relationship_updates` từ final candidate. Raw output được lưu trước khi parse.
4. Bạn xem JSON, có thể sửa tay rồi **Accept reconciliation** (hoặc `Reject` / `Cancel finalizing`).
5. Transaction commit nhiều file: final prose → reconciliation artifact → `current_timeline.json` →
   `relationships.json` → snapshot chương sau → `chapter.json` (luôn **cuối cùng**).
6. Chỉ khi commit xong chapter mới `final_reconciled` và Writer chương sau mới unlock.

Nếu schema sai / ID không resolve / timeout: chapter **vẫn** `finalizing`, chương sau **vẫn** khóa,
raw output + error record được giữ để bạn sửa tay hoặc retry. Retry cùng `operation_id` là idempotent
— không nhân đôi timeline/relationship.

Nếu `auto_accept_structured = true`: proposal hợp lệ được auto-accept ngay sau khi bạn đã review
prose và bấm Finalize. Human Review prose **vẫn** bắt buộc trong mọi trường hợp.

## Workspace Revision / Retcon / Recovery

### Revise Base Idea / Premise

Action tường minh của user. Tạo accepted revision mới rồi đánh dấu downstream liên quan `stale`:

```text
User revise Premise → Long Plan stale → Short Plan stale → Skeleton future stale
```

App **không** tự rewrite Final Manuscript và **không** tự regenerate. Bạn phải tự review/reaccept
hoặc regenerate từng artifact.

### Reaccept artifact stale

*Reaccept* validate lại nội dung accepted cũ theo schema/ID/scope/freshness rồi cập nhật pin, giữ
nguyên nội dung. Nếu nội dung cũ không còn pass (ví dụ ID không resolve nữa) thì reaccept bị từ chối
và bạn phải regenerate.

### Retcon

1. Mở final chapter → **Bắt đầu retcon**: app tạo draft từ final manuscript. Final cũ **vẫn là canon**
   cho tới khi retcon commit.
2. Sửa draft, review lại, Finalize lại, Reconcile lại.
3. `reset_consistency_after_retcon` hạ `latest_consistent_chapter` về chương retcon và đánh dấu
   `stale` state của các chương sau. Timeline/relationship `current` **không** bị ghi lùi.
4. **Không** tự rewrite chương sau. Dùng *reconcile_downstream* để rebuild từng chương theo thứ tự.

### Recovery

Nếu crash giữa transaction commit nhiều file:

- Banner cảnh báo hiện và bạn bấm *Chạy recovery (backend)*.
- Recovery hoàn tất commit đã staged; gọi lần hai là idempotent, không nhân đôi timeline/relationship.
- Nếu target bị sửa ngoài app hoặc thiếu staged file, project chuyển **read-only** và cần xử lý tay
  — xem [troubleshooting.md](troubleshooting.md).

## Arbiter đọc gì

Arbiter là hàm Python thuần trên state/rule. Nó **chỉ gợi ý**; không gọi LLM, không tự chạy bước
tiếp. Ví dụ output:

```text
Current Chapter: 21

Long Plan        accepted
Short Plan       accepted
Skeleton         accepted
Writer Draft     ready
Review           required
Timeline         chapter 20
Relationship     synced

Next:
Review Chapter 21
```

Arbiter cũng báo artifact `stale`, pending operation cần recovery, và nhắc Rolling Plan khi tới mốc.

## Luồng hai chương đầy đủ

```text
1.  Tạo project
2.  Co-create → Finalize Idea                    → idea/base_idea.md accepted
3.  Architect: Premise → Characters → World Rules → Foreshadow (Accept từng phần)
4.  Long Plan: generate → Accept
5.  Short Plan: chọn arc, gán ch1 → generate → Accept   → chapter.json ch_0001 (planned)
6.  Skeleton ch1: generate → Accept                     → skeleton_pin, ch1 skeleton_ready
7.  Writer ch1: Generate                                → draft r1 complete, ch1 review_required
8.  Review ch1: AI Review + sửa prose + Human Review
9.  Finalize ch1 → Generate reconciliation → Accept     → ch1 final_reconciled
10. Short Plan: gán ch2 → generate → Accept             → chapter.json ch_0002
11. Skeleton ch2 → Accept
12. Writer ch2: Generate      ← chỉ tới đây mới chạy được
13. Review ch2 → Finalize ch2 → Reconcile ch2
```

Bước 12 là gate quan trọng nhất: nếu bước 9 chưa xong, `guard_writer` chặn và client LLM **không**
được gọi lần nào.

## Điều app cố tình **không** làm

- Không agent, multi-agent, autonomous loop, "AI tự điều phối AI".
- Không RAG/embedding/vector DB/graph DB/semantic search.
- Không database, queue, background worker, microservice, workflow engine.
- Không tự rewrite toàn truyện, không automatic retcon propagation.
- Không rich-text editor, không selected-text magic rewrite.
- Không auto finalize prose trong bất kỳ cấu hình nào.
- Không multi-user, auth, cloud sync.
