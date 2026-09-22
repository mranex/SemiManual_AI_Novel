# BUG-004 — Long Plan horizon có thể bị thu gọn thành một Arc duy nhất

## Trạng thái

- Kết luận: **bug thật, đã tái hiện độc lập**.
- Mức độ: **High / Major (P1 về sai contract sản phẩm)**.
- Phạm vi phiên này: chỉ review và lập hồ sơ; **không sửa code, prompt, schema hay test**.
- Không phải lỗi corruption/security. Đây là lỗi ngữ nghĩa ở tầng planning; candidate sai vẫn có thể được validate và accept, rồi làm lệch toàn bộ Short Plan phía sau.

## Kết luận ngắn

Long Plan theo spec là tầng `Volume → Arc`, còn Short Plan là tầng `Arc → Chapter` (`novel_ai_spec_v0.2.md:1465-1495`). Theo yêu cầu sản phẩm được người dùng làm rõ trong phiên review này, `planning_scope.start..end` phải là **planning horizon cấp Long Plan**: một khoảng lớn có thể bao trùm nhiều volume và nhiều arc, thậm chí từ đầu đến cuối truyện.

Implementation hiện tại có model đúng để chứa nhiều volume/arc, nhưng không bảo đảm ngữ nghĩa trên:

1. project mới mặc định horizon thành `1..3`;
2. prompt không bắt model phân rã một horizon lớn theo các chuyển biến macro;
3. validator chỉ chặn arc vượt scope và overlap, không bắt đầu/cuối/độ phủ liên tục;
4. `planning_scope` dùng lúc generate không được lưu cùng candidate/accepted revision;
5. test happy path chỉ dùng một volume, một arc và còn assert default `1..3`.

Do đó một payload `1 volume → 1 arc → chapter 1..120` là hợp schema, hợp validator và được accept. Chính xác hơn, code **không hề diễn giải `planning_scope.end` thành “số chương của một arc”**; `end` vẫn là endpoint. Lỗi là toàn bộ pipeline cho phép model lấy cả horizon làm range của một arc mà không có tiêu chí phân rã hay semantic guard.

## Bằng chứng theo contract

### Spec và contract đúng ở cấp abstraction

- `novel_ai_spec_v0.2.md:1465-1484`: Long Plan có level `Volume → Arc` và chịu trách nhiệm hướng lớn, arc goal, conflict, start/end direction, relationship direction, major reveal và story thread.
- `novel_ai_spec_v0.2.md:1488-1514`: Short Plan có level `Arc → Chapter`.
- `docs/design/schemas.md:169-182`: `LongPlanPayload.volumes[]`; mỗi `VolumePlan` có `arcs`; mỗi `ArcPlan` có `chapter_range`.
- `novel_ai/core/models.py:542-567`: runtime model cũng đã hỗ trợ đúng nesting trên.

Không cần redesign payload chỉ để “cho phép” nhiều volume/arc. Khả năng chứa đã có sẵn.

### Quyết định triển khai đã làm scope thành local/progress-derived

- `novel_ai/services/long_planner.py:64-69`: pool mặc định 2 volume/6 arc và `MIN_PLAN_SCOPE_END = 3`.
- `novel_ai/services/long_planner.py:112-149`: khi caller không truyền scope, service trả `start=1`, còn `end=max(3, current_chapter, max arc/chapter đang có)`; nó còn suy ngược từ Short Plan và chapter metadata là các tầng downstream.
- `docs/tasks/T14-planning-services.md:77-87`: hành vi này là quyết định phát sinh của T14, không phải yêu cầu trong spec gốc.
- `novel_ai/pages/long_plan.py:124-159`: UI prefill đúng scope suy ra đó và nói rõ “tối thiểu 1–3, mở rộng theo plan/chapter hiện có”. Khi submit, UI luôn gửi cặp số này như input tường minh.

Vì vậy user mới rất dễ generate một “Long Plan” chỉ cho ba chương đầu. Đây là local window theo progress, không phải story-level horizon như yêu cầu hiện tại.

### Prompt có hướng đúng nhưng chưa khóa hành vi

- `docs/prompts/v1/long_plan.md:9`: định nghĩa `planning_scope.start/end` là số chương trong phạm vi yêu cầu.
- `docs/prompts/v1/long_plan.md:14-19`: mô tả volume/arc và yêu cầu range nằm trong scope, không overlap.
- `docs/prompts/v1/long_plan.md:43-61`: có hướng dẫn về nhiều volume, xây arc từ chuyển biến, rồi mới phân bổ range; đồng thời tránh quota cơ học.
- `docs/prompts/v1/long_plan.md:75-94`: ví dụ inline chỉ là **một `ArcPlan`**, và prompt có nói rõ đó không phải toàn output.
- `docs/design/examples/planning_prompt_cases.json:128-165`: ví dụ request/response đầy đủ lại chỉ dùng scope `1..7`, một volume và một arc `1..7`.

Như vậy nhận định “prompt chỉ có ví dụ một arc nên model bị ép sinh một arc” là nói quá. Prompt hiện đã biết multi-volume/multi-arc và không có hardcode “một arc”. Tuy nhiên nó thiếu một câu contract đủ rõ kiểu: scope là toàn horizon cần kiến trúc; hãy nhận diện các major phase rồi phân rã thành volume/arc trước khi gán range; không gom toàn horizon vào một arc chỉ vì range cho phép. Full fixture một-arc cũng không tạo regression signal cho horizon lớn.

### Validator chấp nhận collapse, gap, thiếu đầu/cuối và plan rỗng

- `novel_ai/services/long_planner.py:242-272`: `_range_issues()` chỉ kiểm tra out-of-scope và overlap.
- Hàm không kiểm tra arc đầu bắt đầu tại `scope.start`, arc cuối kết tại `scope.end`, hoặc `next.start == previous.end + 1`.
- `novel_ai/core/models.py:557-567`: `volumes` và `arcs` là `list` không có `min_length`; runtime không enforce ghi chú “ít nhất 1 volume” ở `docs/design/schemas.md:175`.
- `novel_ai/core/validation.py:866-926`: validator Long Plan duyệt các entry có sẵn và uniqueness/FK; không từ chối payload không có volume/arc.

Probe offline qua service thật + `FakeLLMClient` cho kết quả:

```text
single_arc_1_120: ACCEPTED; volumes=1 arcs=1 ranges=['1-120'] scope={'start': 1, 'end': 120}
gap_21:           ACCEPTED; volumes=1 arcs=2 ranges=['1-20', '22-40'] scope={'start': 1, 'end': 40}
uncovered_edges:  ACCEPTED; volumes=1 arcs=1 ranges=['20-40'] scope={'start': 1, 'end': 120}
empty_plan:       ACCEPTED; volumes=0 arcs=0 ranges=[] scope={'start': 1, 'end': 120}
overlap:          REJECTED
out_of_scope:     REJECTED
```

`single_arc_1_120` tái hiện đúng bug user báo. Ba case kế tiếp chứng minh đây không chỉ là chất lượng lời văn của model mà là thiếu semantic invariant ở backend.

### Horizon bị mất sau generate

- `novel_ai/services/long_planner.py:431-440`: `planning_scope` chỉ có trong `ActionResult.data`.
- `LongPlanPayload`, `ArtifactRevision` và `PayloadSource` hiện không lưu scope dùng để sinh candidate.
- `novel_ai/services/long_planner.py:488-500`: khi Accept, service gọi `_range_issues(candidate.payload, scope=None)`.

Hệ quả: nếu request horizon `1..120` nhưng candidate chỉ phủ `20..40`, generation vẫn pass; sau reload/Accept, backend không còn dữ liệu để đối chiếu candidate với horizon gốc. Lần mặc định sau lại suy `end` từ max arc đã lưu, nên horizon dự kiến có thể co từ 120 xuống 40 mà không có lỗi.

Điểm này làm đề xuất “không cần thay schema” của bản review trước chỉ đúng một nửa: **không cần đổi cấu trúc `VolumePlan`/`ArcPlan`**, nhưng muốn revalidate/recover chính xác thì phải chốt nơi persist horizon (payload hoặc metadata/revision context). Chỉ sửa prompt không giải quyết được.

### Contract edit/regenerate đang tự mâu thuẫn

- `docs/prompts/v1/long_plan.md:102`: edit/regenerate trả toàn payload nhưng đồng thời phải giữ phần ngoài phạm vi yêu cầu.
- `docs/design/prompt-catalog.md:553-555`: lặp lại rằng Long Plan trả toàn payload trong scope và “không tự xóa phần ngoài scope”.
- `_range_issues(..., scope=scope)` lại đánh mọi arc ngoài scope là `out_of_scope`.

Nếu scope là full horizon thì không có “phần ngoài scope” cần giữ. Nếu scope là edit window cục bộ thì service phải có merge semantics thay vì từ chối phần ngoài. Hiện hai cách hiểu cùng tồn tại.

## Bằng chứng từ project thật

Lịch sử project `projects/acc` cho thấy cả hai mặt:

- revision đầu: `1 volume / 1 arc / range 1..3`, validation `valid` và từng được accept;
- revision hiện tại: `3 volumes / 5 arcs / ranges 1..3, 4..5, 6..7, 8..9, 10..10`, validation `valid` và accepted.

Điều này bác bỏ cách diễn đạt “implementation luôn chỉ sinh đúng một arc”. Runtime có thể sinh nhiều volume/arc, và prompt hiện tại từng làm được khi user đưa horizon `1..10`. Bug thật là **không có guarantee và default/contract đang dẫn sai abstraction**; model output một-arc cho horizon lớn vẫn được xem là thành công.

## Ảnh hưởng downstream

Sai Long Plan không dừng ở màn hình plan:

- `novel_ai/services/short_planner.py:231-258` mặc định lấy **mọi chapter** trong `arc.chapter_range` chưa được plan.
- Nếu một arc bị collapse thành `1..120`, Short Plan mặc định sẽ reserve/đòi ChapterPlan cho 120 chương trong một request và UI tạo constraint input cho toàn bộ danh sách.
- Macro phase, arc-level relationship direction, reveal timing và selector theo arc mất ranh giới.
- Rolling Plan chỉ được phép sửa future Short Plan, không sửa Long Plan; nó không thể phục hồi kiến trúc đã collapse.
- Candidate vẫn `valid`, nên Auto Accept structured có thể đưa lỗi ngữ nghĩa này thành accepted plan mà không có warning.

Vì lỗi ở tầng authority cao hơn, mọi Skeleton/Writer phía sau có thể bám một cấu trúc sai nhưng vẫn đi qua lifecycle hợp lệ.

## Khoảng trống kiểm thử

- `tests/integration/test_planning_services.py:135-164`: helper Long Plan luôn tạo một volume/một arc.
- `tests/integration/test_planning_services.py:376-391`: happy path assert default scope `1..3` và chỉ đọc `volumes[0].arcs[0]`.
- Test regenerate tại `tests/integration/test_planning_services.py:432-458` cũng chỉ thay một arc bằng một arc.
- Không có test multi-volume/multi-arc cho horizon lớn, gap, uncovered start/end, empty plan hoặc giữ/persist horizon qua reload/accept.
- `docs/design/examples/check_planning_examples.py:98-105` chỉ yêu cầu mỗi arc nằm trong scope; không kiểm tra coverage/decomposition.

Toàn bộ test Long Plan hiện pass vì chúng đang xác nhận contract thiếu, không chứng minh hành vi user cần.

## Đánh giá reply của AI trước

### Đúng

- Severity cao và root cause nằm xuyên scope → prompt → validation → UI → test.
- Schema hiện đã chứa multi-volume/multi-arc.
- Default `1..3` sai hướng với horizon cấp toàn truyện.
- One-arc toàn scope được validator chấp nhận.
- Không nên chữa bằng quota như “ít nhất 2 volume”, “mỗi arc 8 chương”.
- Edit/regenerate wording ngoài scope đang mâu thuẫn với validator.
- Cần regression test horizon lớn và kiểm tra token/output budget.

### Cần hiệu chỉnh

1. **Không có code nào dùng `planning_scope.end` như chapter count của arc.** Nó là endpoint; model có thể chọn một arc phủ endpoint đó và backend không phản đối.
2. **Không phải prompt chỉ có một ví dụ toàn output.** Inline JSON là một `ArcPlan` được ghi rõ; nhưng fixture đầy đủ vẫn chỉ cover một volume/một arc và không giúp bắt bug.
3. **Implementation không luôn sinh một arc.** Project `acc` hiện có accepted 3 volume/5 arc. Bug là thiếu guarantee/guard và default sai abstraction.
4. **“Không cần schema/migration” chưa đủ.** Array schema không cần redesign, nhưng horizon phải được persist ở đâu đó nếu muốn Accept/reload revalidate chính xác.
5. **Exact gap-free coverage chưa được spec v0.2 viết thành invariant rõ.** Yêu cầu mới của user đã làm rõ `planning_scope` là complete horizon; khi triển khai fix cần ghi nó thành contract chính thức trước rồi mới thêm validator. Không nên lặng lẽ biến đề xuất trong bug report thành spec.
6. **Thiếu horizon ban đầu phải fail** là một hướng UX hợp lý, nhưng chưa phải luật có sẵn trong spec. Có thể chọn required input hoặc một explicit “ước lượng độ dài”; điều bắt buộc là không âm thầm coi `1..3` như whole-story default.
7. **Output limit chưa được chứng minh là root cause.** Default app là 4096 tokens, còn run thật tạo plan 3 volume/5 arc khoảng 18 KB đã thành công với cấu hình runtime hiện tại. Horizon rất lớn vẫn có rủi ro truncation; cần benchmark sau khi chốt độ chi tiết/chunking, không kết luận trước.

## Contract cần chốt trước khi sửa

Theo yêu cầu người dùng trong phiên này, hướng nhất quán nhất là:

1. `planning_scope = {start, end}` là **toàn horizon mà candidate Long Plan phải kiến trúc**, không phải edit window và không phải arc size.
2. Long Plan trả full payload cho horizon đó; local edit instruction vẫn trả full candidate, giữ phần không đổi theo ID.
3. Arc ranges phải nằm trong horizon, không overlap, không gap và phủ từ `start` tới `end`.
4. Số volume/arc do narrative structure quyết định; one-volume/one-arc vẫn hợp lệ cho scope nhỏ hoặc premise thật sự chỉ có một phase. Backend không thể chứng minh “đủ hay” chỉ bằng count.
5. Initial horizon phải là lựa chọn rõ của user hoặc một field foundation/config đã được user chấp thuận; không suy từ Short Plan/chapter progress.
6. Horizon phải được persist để generate, reload, Accept và regenerate dùng cùng một contract.

Điểm khó còn lại: validator cấu trúc có thể bắt coverage nhưng không thể tự biết một horizon `1..120` “xứng đáng” bao nhiêu arc. Chống collapse cho horizon lớn cần phối hợp prompt, UX preview/warning và Human Accept; không nên giả vờ một quota số lượng là semantic validation.

## Kiểm tra đã chạy

| Kiểm tra | Kết quả |
|---|---|
| Probe service thật với FakeLLM: one arc `1..120` | Accepted — tái hiện bug |
| Probe gap `1..20, 22..40` trong scope `1..40` | Accepted — thiếu coverage guard |
| Probe chỉ phủ `20..40` trong scope `1..120` | Accepted — thiếu edge coverage guard |
| Probe `volumes: []` | Accepted — lệch schema doc “ít nhất 1 volume” |
| Probe overlap | Rejected — guard hiện có hoạt động |
| Probe out-of-scope | Rejected — guard hiện có hoạt động |
| `pytest tests/integration/test_planning_services.py tests/unit/test_models_roundtrip.py tests/unit/test_validation.py -q` | 65 passed |
| `python -X utf8 docs/design/examples/check_planning_examples.py` | PASS; chính script ghi rõ chỉ là document checks |
| Toàn bộ `python -m pytest -q` | 493 passed, 4 failed, 1 skipped; bốn lỗi cấu hình `.env` đã biết, không liên quan đường tái hiện BUG-004 |

## Phạm vi thay đổi phiên review

- Không sửa source, prompt, schema, fixture, project data hay test.
- Chỉ thêm hồ sơ bug này và cập nhật `DEBUG_REPORT.md`.

## Resolution — T30 (2026-09-22)

**Đã sửa.** Quyết định D017: `planning_scope {start,end}` là **complete horizon** do user nhập, là
metadata app-owned trên `ArtifactRevision` (sống qua candidate/reload/Accept, không sửa được từ form
hay raw JSON). Validator `long_plan_horizon_issues` chặn gap/overlap/out-of-scope/thiếu một trong hai
đầu/plan rỗng/volume rỗng; prompt và fixture ví dụ đã nâng lên multi-volume/multi-arc 1–120. Plan chỉ
có một arc vẫn hợp lệ nhưng hiện warning non-blocking — app **không** đặt quota arc/chương.

Bằng chứng (đợt nghiệm thu T40, 2026-09-22):

- `tests/integration/test_t40_acceptance.py::test_full_shell_walkthrough_long_plan_to_chapter_two` và
  `::test_shell_keeps_user_horizon_and_does_not_infer_legacy_scope` — horizon 1–4 với hai arc `1–2`/
  `3–4` sống qua Accept, reload, reopen; accepted legacy không bị app tự suy horizon và cần xác nhận
  tường minh; mở lại project không ghi file.
- `tests/integration/test_planning_services.py`, `tests/unit/test_validation.py::test_horizon_issues_*`
  (gap, uncovered start/end, overlap, out-of-scope, empty payload/volume, single-arc valid),
  `tests/unit/test_models_roundtrip.py::test_planning_scope_*`.
- Document check: `check_planning_examples.py` PASS 9 case complete-horizon + 9 negative probe;
  `check_t29_contracts.py` PASS 9 long-plan coverage case + 4 legacy matrix case.
- `.\.venv\Scripts\python.exe -m pytest -p no:randomly` → 658 passed, 1 skipped, 0 failed.

Chi tiết bàn giao: [docs/tasks/T30-long-plan-horizon.md](../tasks/T30-long-plan-horizon.md);
quyết định: `docs/design/decisions.md` D017; coverage matrix:
[docs/design/fix-acceptance-report.md](../design/fix-acceptance-report.md).
