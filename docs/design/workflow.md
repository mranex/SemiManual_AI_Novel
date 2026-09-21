# Workflow và authority contract

Phiên bản: 2026-09-19. Nguồn sản phẩm: `novel_ai_spec_v0.2.md`.

Tài liệu này chốt hành vi workflow ở mức contract để T02+ có thể thiết kế schema, storage, service và UI mà không tự diễn giải lại luật sản phẩm.

## 1. Phạm vi và thuật ngữ

### 1.1. Chủ thể

- `User`: người dùng. Là authority cuối cùng với story canon và prose.
- `Backend`: Python domain/service layer. Thực thi guard, lifecycle, validation, transaction và stale marking. Backend không gọi LLM trừ khi action rõ ràng yêu cầu.
- `UI`: Streamlit. Hiển thị state, gửi action rõ ràng vào backend. UI không phải guard duy nhất.
- `LLM`: hàm sinh draft hoặc report theo prompt. Output mặc định không phải canon.
- `Prompt`: contract hướng dẫn LLM. Prompt không được yêu cầu tự đọc file, gọi tool, tiếp tục workflow hoặc lưu state.
- `Arbiter`: hàm Python đọc state và gợi ý next step. Arbiter không gọi LLM và không mutate state.

### 1.2. Authority stack

Khi có conflict, tầng dưới phải nhường tầng trên:

```text
Base Idea
> Premise / Architect foundation
> Long Plan
> Short Plan
> Skeleton
> Writer Draft
```

Architect foundation gồm Premise, Characters, World Rules, Foreshadow và các entry appendable cùng loại. Accepted foundation, Final Manuscript đã reconcile và accepted current state là canon. Plan mô tả ý định tương lai, không chứng minh sự kiện đã xảy ra.

### 1.3. Revision và candidate

Mọi artifact quan trọng có:

- `accepted_revision`: revision đã được accept, nếu có.
- `candidate_revision`: draft mới, nếu có.
- `dependency_pins`: revision của input đã dùng để tạo hoặc accept artifact.
- `status`: trạng thái lifecycle.

Candidate không thay thế accepted revision cho tới khi action `Accept` commit thành công. Regenerate tạo candidate riêng. Reject hoặc lỗi validation không làm đổi accepted revision.

## 2. Structured artifact lifecycle

Áp dụng cho Premise, Characters, World Rules, Foreshadow, Long Plan, Short Plan, Skeleton, Rolling Plan proposal, Review report, Reconciliation proposal và Retcon impact report khi chúng là JSON/structured output.

| Status | Ý nghĩa | Dùng làm input phụ thuộc? |
|---|---|---:|
| `missing` | Chưa có accepted revision hoặc candidate hợp lệ. | Không |
| `draft` | Có candidate chưa accept, hoặc raw output cần user xử lý. | Không |
| `accepted` | Đã validate và được user accept hoặc auto-accept hợp lệ. | Có, nếu không stale và đúng phạm vi chương |
| `stale` | Accepted revision từng hợp lệ nhưng upstream/dependency đổi. | Không cho action phụ thuộc, trừ khi action là review/reaccept/regenerate |
| `rejected` | Candidate bị user reject hoặc bị hủy. Accepted cũ, nếu có, vẫn giữ nguyên. | Không |

Transition bắt buộc:

| Từ | Action | Điều kiện | Sang | Ghi chú |
|---|---|---|---|---|
| `missing` | Generate/Edit/Import | Input đủ theo action | `draft` | Lưu candidate hoặc raw output. |
| `draft` | Accept | Schema, ID, dependency, freshness, scope đều hợp lệ; user accept hoặc auto-accept structured hợp lệ | `accepted` | Snapshot accepted cũ trước khi thay, nếu có. |
| `draft` | Reject | User reject | `rejected` | Không đổi accepted cũ. |
| `draft` | Retry generate | User action rõ ràng | `draft` | Candidate mới, raw cũ giữ theo policy T03. |
| `draft` | Cancel | User hủy candidate | `rejected` hoặc `missing` | Nếu chưa từng có accepted thì quay về `missing`; nếu có accepted thì chỉ xóa candidate. |
| `accepted` | Regenerate | User action rõ ràng | `draft` + accepted giữ nguyên | Không silent overwrite. |
| `accepted` | Upstream changed | Dependency revision/authority đổi | `stale` | Backend đánh dấu theo dependency rule. |
| `stale` | Reaccept | User review nội dung cũ, backend validate lại schema/ID/scope/freshness | `accepted` | Ghi decision; cập nhật dependency pins. Có thể tăng revision metadata dù content không đổi. |
| `stale` | Regenerate/Edit | User action rõ ràng | `draft` | Stale accepted vẫn giữ cho audit, nhưng không dùng cho action phụ thuộc. |

Structured validation chỉ bảo đảm shape, ID, phạm vi và freshness. Nó không chứng minh output đúng về mặt văn chương hoặc ngữ nghĩa truyện.

## 3. Chapter lifecycle

Chapter có lifecycle riêng. Các review gắn với prose revision, không chỉ gắn với file.

| Status | Ý nghĩa | Guard chính |
|---|---|---|
| `planned` | Chapter đã có Short Plan accepted, chưa có Skeleton accepted. | Writer bị khóa. |
| `skeleton_ready` | Skeleton accepted và không stale. | Writer có thể chạy nếu previous chapter guard đạt. |
| `draft` | Writer draft tồn tại nhưng chưa sẵn sàng review, hoặc stream bị ngắt/partial. | Review/finalize bị khóa nếu draft chưa complete. |
| `review_required` | Draft prose complete và cần human review. | Finalize bị khóa tới khi human review của revision hiện tại complete. |
| `finalizing` | User đã chọn prose revision để finalize; final candidate và reconciliation đang pending. | Chapter sau vẫn khóa; accepted current state chưa đổi. |
| `final_reconciled` | Final manuscript và accepted state update đã commit thành công. | Chapter kế tiếp có thể unlock nếu các guard khác đạt. |

### 3.1. Human Review và AI Review

- Human Review là gate cứng. Nó có thể là một action riêng (`Mark reviewed`) hoặc một xác nhận rõ ràng trong action `Finalize Chapter`. Backend phải ghi `human_review.prose_revision`.
- AI Review là action hỗ trợ mặc định. Nó sinh report cho user, gắn với `prose_revision` và không tự sửa prose, không tự accept, không tự finalize.
- Nếu prose revision thay đổi sau Human Review, `human_review` của revision cũ không còn hiệu lực.
- Nếu prose revision thay đổi sau AI Review, report AI cũ chuyển thành stale hoặc chỉ còn là historical report.
- Không có config MVP nào cho phép AI Review thay thế Human Review.

### 3.2. Final candidate

Khi user finalize:

1. Backend đóng băng prose revision được chọn thành `final_candidate`.
2. Chapter chuyển sang `finalizing`.
3. Backend yêu cầu hoặc nhận reconciliation proposal.
4. Chỉ khi reconciliation commit thành công thì `final_candidate` trở thành Final Manuscript canon và chapter chuyển `final_reconciled`.

Trong `finalizing`, final candidate được hiển thị như bản chờ commit, chưa unlock chapter sau. Nếu reconciliation lỗi schema, timeout hoặc user cancel, accepted state không đổi. Với retcon, final cũ vẫn là canon cho tới khi commit retcon hoàn tất.

## 4. Action contract

### 4.1. Foundation và Architect

| Action | Actor | Precondition | Output | Success transition | Reject/Retry/Cancel |
|---|---|---|---|---|---|
| Co-create turn | User -> Backend -> LLM | Project open; Co-create chưa finalized hoặc user explicit reopen draft | Message + `idea_state` draft | Co-create working state updated | Lỗi LLM lưu raw/error, không finalize. User retry hoặc cancel turn. |
| Finalize Base Idea | User -> Backend | Co-create `idea_state` đủ tối thiểu hoặc user nhập manual Base Idea | `idea/base_idea.md` accepted + metadata | Base Idea accepted; Architect actions unlock | Cancel giữ Co-create working. Sửa Base Idea sau này là upstream revise action riêng. |
| Generate/Regenerate Premise/Character/World/Foreshadow | User -> Backend -> LLM | Base Idea accepted; input dependency không stale | Structured candidate | Artifact `draft` | Reject giữ accepted cũ. Retry tạo candidate mới. Cancel xóa candidate. |
| Edit structured candidate | User -> Backend | Candidate tồn tại hoặc user tạo manual candidate | Candidate mới | Artifact `draft` | Validation lỗi giữ draft, không accept. |
| Accept structured foundation | User hoặc auto-accept sau validation | Candidate valid; scope hợp lệ; không vi phạm guard deterministic | Accepted revision | Artifact `accepted` | Reject/cancel không đổi accepted. |
| Append Architect entry | User -> Backend, có thể dùng LLM draft | Foundation artifact accepted; user nêu append; có `effective_from_chapter` | Candidate entry với stable ID | Entry accepted sau validation/accept | Lỗi ID/effective chapter giữ draft. Không làm quá khứ stale nếu effective_from ở tương lai. |
| Revise Base Idea/Premise | User -> Backend | Explicit upstream revision action | New accepted revision + stale marks downstream | Downstream planning/skeleton/drafts liên quan `stale` | Cancel giữ accepted cũ. Không rewrite final manuscript. |

### 4.2. Planning

| Action | Actor | Precondition | Output | Success transition | Reject/Retry/Cancel |
|---|---|---|---|---|---|
| Generate/Regenerate Long Plan | User -> Backend -> LLM | Base Idea + Premise accepted; relevant Architect foundation accepted; dependencies not stale | Long Plan candidate | `draft`, then `accepted` on accept | Reject keeps old plan. Retry creates new candidate. |
| Accept Long Plan | User hoặc auto-accept structured hợp lệ | Candidate valid; references resolve; within Premise/Base Idea authority | Accepted Long Plan | Short Plan can be generated | Reject/cancel keeps previous accepted. |
| Generate/Regenerate Short Plan | User -> Backend -> LLM | Long Plan accepted; selected arc exists; context dependencies fresh | Short Plan candidate | `draft`, then `accepted` on accept | Same structured lifecycle. |
| Accept Short Plan | User hoặc auto-accept structured hợp lệ | Candidate valid; chapter IDs stable; referenced entity IDs allowed for chapter range | Accepted Short Plan | Skeleton for planned chapters unlocks | Reject/cancel keeps old. |
| Rolling Plan Review | User -> Backend -> LLM | Short Plan accepted; current timeline/relationship available; rolling reminder due or user manual trigger | Rolling Plan proposal | `draft` proposal | Reject means no plan mutation. Retry allowed. |
| Apply Rolling Plan | User hoặc auto-accept structured hợp lệ | Proposal valid; only future Short Plan/relationship direction changes; does not conflict with Long Plan | Updated future plan candidate or accepted patch | Future planning updated; affected future skeletons stale | Reject/cancel keeps current accepted plan. |

Rolling Plan cannot edit Base Idea, Premise, Long Plan, Final Manuscript or past/current accepted state. If proposal needs those changes, it must report an issue for user decision, not apply.

### 4.3. Skeleton và Writer

| Action | Actor | Precondition | Output | Success transition | Reject/Retry/Cancel |
|---|---|---|---|---|---|
| Generate/Regenerate Skeleton | User -> Backend -> LLM | Short Plan chapter accepted/fresh; context actual hợp lệ hoặc context provisional chuẩn bị trước theo schemas.md mục 4.1 | Skeleton candidate | `draft`, then `accepted` on accept | Reject keeps old accepted skeleton. Retry creates new candidate. |
| Accept Skeleton | User hoặc auto-accept structured hợp lệ | Candidate valid; đã review trên actual đầu chương (không provisional); section instructions scoped to chapter; foreshadow secrets surfaced safely | Skeleton accepted | Chapter `skeleton_ready` if previous chapter guard also ok | Reject/cancel keeps old. |
| Generate Writer Draft | User -> Backend -> LLM | Skeleton accepted and fresh; writer context projection passes secret filtering; chapter 1 or previous chapter `final_reconciled` | Markdown prose draft | Chapter `draft` while streaming, `review_required` when complete | Stream lỗi giữ partial draft; status không review/final. Retry/regenerate explicit. |
| Continue partial draft | User -> Backend -> LLM | Partial draft exists; same skeleton/context revision or user accepts mismatch handling | More markdown | `draft` or `review_required` when complete | Lỗi giữ partial. |
| Edit/Save Draft | User -> Backend | Draft exists or user creates manual draft | New prose revision | `draft` or `review_required` depending complete flag | Any existing review tied to older revision becomes stale. |

Writer does not plan, does not update state, does not append Architect data and does not receive full future plot or author-only secrets.

### 4.4. Review, Finalize và Reconcile

| Action | Actor | Precondition | Output | Success transition | Reject/Retry/Cancel |
|---|---|---|---|---|---|
| Run AI Review | User -> Backend -> LLM | Complete prose draft exists; context dependencies available | Review report candidate/accepted report | Report displayed for same prose revision | Lỗi report không khóa Human Review. Retry allowed. |
| Human Review complete | User -> Backend | User has seen/editable prose revision; draft complete | Human review record bound to `prose_revision` | Finalize becomes available | Editing prose invalidates record. |
| Rewrite Section | User -> Backend -> LLM | User selects section or provides text; current draft revision known | Replacement prose candidate | User may apply to draft, producing new prose revision | Reject keeps draft. Apply invalidates prior review. |
| Finalize Chapter | User -> Backend | Latest prose revision complete; Human Review valid for that revision or confirmed in finalize action; Skeleton fresh; previous chapter guard satisfied | Frozen `final_candidate`; reconciliation request/proposal | Chapter `finalizing` | Cancel discards pending final candidate and returns to `review_required`; accepted state unchanged. |
| Reconcile Chapter | Backend -> LLM or User submits manual JSON | Chapter `finalizing`; final candidate exists | Reconciliation proposal | `draft` proposal | Schema/timeout error leaves chapter `finalizing`; raw output saved; next chapter locked. |
| Accept/Edit Reconciliation | User or auto-accept structured hợp lệ | Proposal schema valid; chapter number matches; relationship IDs valid; transaction can commit all files | Final manuscript + timeline/relationship commit | Chapter `final_reconciled` | Reject keeps finalizing pending; user can retry, edit, or cancel finalizing. |
| Retry Reconcile | User -> Backend -> LLM | Chapter `finalizing`; accepted state unchanged since finalizing or user acknowledges refreshed pins | New proposal | `draft` proposal | Must be idempotent; no duplicate timeline/relationship merge. |

AI Review is not required to run before Finalize in MVP. Human Review is required. Future config may make AI Review recommended in UI, but not a backend gate unless a later task explicitly changes this contract.

### 4.5. Retcon và stale handling

| Action | Actor | Precondition | Output | Success transition | Reject/Retry/Cancel |
|---|---|---|---|---|---|
| Start Retcon | User -> Backend | Target chapter has Final Manuscript | Editable retcon draft copied from final | Chapter gets retcon draft; old final remains canon | Cancel deletes retcon draft; no downstream changes. |
| Finalize Retcon | User -> Backend | Retcon draft complete; Human Review valid for retcon prose revision | Pending replacement final candidate + reconciliation proposal | Chapter `finalizing` for retcon | Same finalize/reconcile failure behavior. |
| Commit Retcon Reconciliation | User or auto-accept structured hợp lệ | Proposal valid; transaction can replace chapter final and mark affected downstream derived state | New final revision; downstream artifacts/state marked stale by scope | Target chapter `final_reconciled`; dependent future artifacts may block | Failure leaves old final canon and retcon pending. |
| Analyze Downstream Impact | User -> Backend -> LLM optional | Retcon draft/final candidate exists or upstream revision accepted | Impact report | Report displayed; no mutation except report storage | Reject report has no state effect. |
| Review/Reaccept Stale Artifact | User -> Backend | Artifact `stale`; dependencies now accepted; schema/ID/scope still valid | Decision record | Artifact `accepted` with refreshed pins | If invalid, remains `stale`; user may regenerate. |

Retcon of chapter N after later chapters are final does not silently rewrite chapter N+1 onward and does not silently write old chapter reconciliation into the current latest state. Downstream final manuscripts remain as historical canon until user explicitly retcons them, but derived state, future plans, skeletons and drafts affected by the change must be marked stale or blocked according to dependency scope.

## 5. Guard rules

### 5.1. Unlock rules

- Chapter 1 Writer unlocks when Skeleton for chapter 1 is accepted and fresh.
- Chapter N Writer for N > 1 unlocks only when chapter N-1 is `final_reconciled`, Skeleton N is accepted and fresh, and context state for chapter N can be built from accepted snapshots.
- File existence never unlocks a step. Backend reads lifecycle metadata and dependency pins.
- UI may hide buttons, but backend rejects invalid calls even if invoked directly.

### 5.2. Stale rules

- Accepted artifacts become `stale` when an upstream accepted revision they depend on changes, unless the change is an append not effective for that artifact's chapter range.
- Stale structured artifacts cannot be used for downstream generation or finalize.
- User may reaccept stale artifacts after review if deterministic validation still passes.
- User may regenerate stale artifacts; old accepted stale revision remains available for audit/history until replaced.
- Stale does not mean wrong; it means "requires explicit user review before use."

### 5.3. Auto Accept

Auto Accept can apply only to structured output after backend validation:

- parseable schema;
- stable IDs and references resolve;
- dependency pins are current;
- output scope matches requested action/chapter;
- action is allowed to mutate that artifact.

Auto Accept cannot:

- finalize prose;
- mark Human Review complete;
- accept AI Review as story truth;
- bypass Skeleton;
- bypass previous chapter `final_reconciled`;
- mutate upstream from downstream output.

### 5.4. Rolling reminder

Arbiter may suggest Rolling Plan when `rolling_plan_every` says it is due, when recent finalized chapters diverge from future Short Plan, or when user manually asks. Suggestion does not mutate state. Applying a rolling proposal follows structured accept rules and touches only future plan/relationship direction in allowed scope.

### 5.5. Error and partial output

- API/schema/timeout failure never modifies accepted state.
- Raw invalid structured output may be stored for debugging, marked `draft`/error, not accepted.
- Interrupted writer stream may be stored as partial draft. It cannot become `review_required`, final, or canon until user completes/accepts the draft path.
- Retry must be idempotent where prior attempt reached `finalizing` or transaction recovery.

## 6. Manual walkthroughs

### 6.1. Chapter 1 -> Chapter 2

1. User finalizes Base Idea.
2. User generates and accepts Premise/Architect foundation.
3. User generates and accepts Long Plan, then Short Plan with chapter 1 and 2.
4. User generates and accepts Skeleton chapter 1.
5. Writer chapter 1 unlocks because there is no previous chapter.
6. User generates draft chapter 1. Status becomes `review_required` only when draft complete.
7. User reviews/edit prose. Human Review record is bound to that prose revision.
8. User finalizes chapter 1. Status becomes `finalizing`; chapter 2 Writer remains locked.
9. Reconciliation proposal validates and is accepted. Backend commits final manuscript, timeline and relationship update atomically.
10. Chapter 1 becomes `final_reconciled`.
11. Skeleton chapter 2 accepted + chapter 1 `final_reconciled` unlocks Writer chapter 2.

### 6.2. Schema lỗi khi finalize

1. Chapter is `finalizing` with frozen final candidate.
2. LLM returns invalid reconciliation JSON.
3. Backend stores raw output/error and does not update timeline, relationship or chapter status to `final_reconciled`.
4. Chapter after it remains locked.
5. User can retry reconciliation, edit JSON manually then accept, or cancel finalizing. Cancel keeps draft path; accepted state unchanged.

### 6.3. Sửa draft sau review

1. Draft revision `r3` has Human Review complete and optional AI Review report.
2. User edits prose, creating revision `r4`.
3. Backend marks Human Review `r3` invalid for current draft. AI Review `r3` becomes stale/historical.
4. Finalize rejects until Human Review is complete for `r4` or finalize action includes explicit review confirmation for `r4`.

### 6.4. Retcon chapter 1 sau khi chapter 3 đã final

1. Chapters 1, 2, 3 are `final_reconciled`.
2. User starts retcon chapter 1. Backend creates retcon draft from chapter 1 final; old chapter 1 final remains canon.
3. User edits/reviews/finalizes retcon draft. Backend enters `finalizing` with replacement final candidate.
4. Reconciliation validates and commits replacement chapter 1 final plus chapter 1 derived state/snapshot.
5. Backend marks affected downstream plans, skeletons, drafts, current state projections or reports stale according to dependency scope.
6. Backend does not rewrite chapters 2 or 3 and does not silently replace latest current timeline/relationship for chapter 3 with chapter 1 state.
7. Future Writer actions are blocked until required stale dependencies are reviewed/reaccepted/regenerated.

## 7. Invariant mapping

| # | Invariant | Responsibility | Kiểm tra dự kiến |
|---:|---|---|---|
| 1 | AI generation không tự trở thành canon. | Backend lifecycle; UI labels draft/candidate. | Generate Architect/Writer and verify accepted/final unchanged. |
| 2 | Structured AI output chỉ merge sau schema validation. | Backend validation before accept/auto-accept. | Invalid JSON candidate leaves accepted unchanged. |
| 3 | Auto Accept không bypass Chapter Review. | Backend finalize guard. | Auto accept on; Writer draft still requires Human Review. |
| 4 | User phải review chapter N trước Writer N+1. | Chapter lifecycle and unlock guard. | Chapter 2 Writer rejected until ch1 final_reconciled. |
| 5 | Base Idea mạnh hơn mọi downstream artifact. | Prompt authority, conflict review, stale on revise. | Revise Base Idea marks plans/skeleton stale. |
| 6 | Premise mạnh hơn plan. | Plan generation context and stale guard. | Revise Premise blocks old Long/Short Plan use. |
| 7 | Long Plan mạnh hơn Short Plan. | Short Plan validation/prompt; rolling scope. | Short Plan candidate referencing arc outside Long Plan rejected or flagged. |
| 8 | Short Plan mạnh hơn Skeleton. | Skeleton generation validation. | Skeleton for chapter not in Short Plan rejected. |
| 9 | Skeleton mạnh hơn Writer. | Writer prompt/context; AI Review checks adherence. | Writer cannot run without accepted Skeleton; review can flag deviation. |
| 10 | Downstream không được sửa upstream. | Service boundaries; output scopes. | Writer/reconcile output cannot mutate Skeleton/Plan/Foundation. |
| 11 | Writer không plan. | Writer prompt and service output kind markdown only. | Writer response saved only as prose; no plan merge path. |
| 12 | Writer không sở hữu Foreshadow truth. | Context projection and prompt. | Author-only foreshadow absent from Writer context. |
| 13 | Skeleton bắt buộc. | Writer precondition. | Direct Writer call without Skeleton rejected. |
| 14 | Planner biết secret; Writer chỉ biết surface instruction. | Context builder separates author-only vs writer-safe fields. | Secret in Foreshadow not present in writer payload; Skeleton instruction can be present. |
| 15 | Plan không chứng minh sự kiện đã xảy ra. | Canon definitions and reconciliation rules. | Timeline not updated when Short Plan accepted. |
| 16 | Final Manuscript mới là canon prose. | Chapter final commit. | Draft exists but canon reader returns previous/missing final. |
| 17 | Current Timeline phản ánh continuity thực tế gần nhất. | Reconcile service only after final. | Timeline updates only through final reconciliation. |
| 18 | Current Relationship phản ánh quan hệ thực tế gần nhất. | Reconcile service; plan stores directions separately. | Relationship current updates only from accepted reconciliation. |
| 19 | Long/Short Plan chỉ quản lý hướng relationship tương lai. | Schema separation in T02; service scope. | Plan relationship direction does not overwrite current relationship. |
| 20 | Finalize Chapter phải reconcile state. | Finalizing transaction. | Chapter not final_reconciled until timeline/relationship commit done. |
| 21 | Chapter sau chỉ unlock khi chapter trước `final_reconciled`. | Backend unlock guard. | Direct call Writer N+1 rejected if N is finalizing. |
| 22 | Regenerate tạo Draft Candidate. | Artifact lifecycle. | Accepted revision unchanged after regenerate. |
| 23 | Accepted revision không bị silent overwrite. | Snapshot before replace; explicit accept. | Accept creates history/snapshot metadata; failure leaves old. |
| 24 | Architect append phải có `effective_from_chapter`. | Validation. | Append without chapter rejected. |
| 25 | Append tương lai không retroactively xuất hiện trong quá khứ. | Context effective filtering. | Rule effective chapter 100 absent from chapter 20 context. |
| 26 | Upstream user revision không tự rewrite downstream. | Stale marking only. | Revise Premise leaves final manuscript files unchanged. |
| 27 | Retcon không tự rewrite future chapter. | Retcon transaction scope. | Retcon ch1 leaves ch2/ch3 final prose unchanged. |
| 28 | AI failure không làm thay đổi accepted state. | Error handling and transaction. | Timeout during reconcile leaves state and status unchanged except error/raw. |
| 29 | File exists không đồng nghĩa complete. | Backend metadata guard. | `final.md` without final_reconciled metadata does not unlock next chapter. |
| 30 | Không subsystem nào được tự biến thành agent. | No autonomous loop; action-scoped services. | Service tests call one action and assert no chained downstream mutation. |
| 31 | Không thêm công nghệ nếu rule + JSON giải quyết được. | Architecture review and task scope. | T07 dependency manifest remains Python/Streamlit/Pydantic/pytest unless approved. |
| 32 | Human là authority cuối cùng. | User accept/finalize/revise actions required. | AI reports/issues do not mutate canon without user/auto-accept where allowed. |

## 8. Link nội bộ liên quan

- Product spec: `../../novel_ai_spec_v0.2.md`
- Implementation plan: `../../IMPLEMENTATION_PLAN.md`
- Task registry: `../tasks/README.md`
- Decisions for this contract: `decisions.md`

