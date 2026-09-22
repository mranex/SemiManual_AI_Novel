# BUG-003 — `length_guidance` rỗng vẫn đi tới API Short Plan

## Metadata

| Thuộc tính | Giá trị |
|---|---|
| Trạng thái | Confirmed / chưa sửa |
| Mức độ | Major; có thể gây request API vô ích |
| Ưu tiên đề xuất | P1 |
| Phát hiện | 2026-09-22 |
| Project có bằng chứng thật | `projects/acc` |
| Thành phần | Short Plan UI, chapter constraints, `short_planner.generate` |
| Workaround | Điền `length_guidance` và POV cho mọi chapter trước Generate |

## Mô tả

UI Short Plan để trống `length_guidance` mặc định nhưng vẫn cho submit. Request LLM được gửi với constraint rỗng. Nếu model giữ nguyên giá trị rỗng trong output, service chỉ phát hiện sau API và trả `ValidationFailure`.

Theo yêu cầu mới nhất của người dùng, app phải yêu cầu thiết lập `length_guidance` mặc định trước khi lập Short Plan. Backend vẫn cần guard độc lập để caller trực tiếp không bypass UI.

## Bằng chứng thật trong `ACC`

Error record:

```text
projects/acc/raw/2026-09-22/
op_ui_c0565cffe594ed6b95d26d44_short_plan.error.json
```

Record chứa ba blocking errors:

```text
/payload/chapters/ch_0001/outline/0 — invalid_outline_contract_item — thiếu length_guidance
/payload/chapters/ch_0002/outline/0 — invalid_outline_contract_item — thiếu length_guidance
/payload/chapters/ch_0003/outline/0 — invalid_outline_contract_item — thiếu length_guidance
```

Raw response tương ứng cho thấy model trả:

```json
{
  "language": "vi",
  "pov": "Ngôi thứ ba giới hạn theo Sở Dương",
  "length_guidance": ""
}
```

cho cả ba chapter. Như vậy API đã hoàn thành và raw output đã được lưu trước khi validation từ chối candidate.

## Kết quả mong đợi

- User phải thiết lập `length_guidance` mặc định trước khi Generate Short Plan.
- Mọi assigned chapter nhận constraint không rỗng hoặc yêu cầu override rõ ràng.
- Nếu thiếu, UI báo ngay và service từ chối trước LLM.
- `client.calls == []`, không tạo raw response và không tiêu API usage.

Theo contract hiện hành, `pov` cũng là field bắt buộc không rỗng; fix không nên chỉ bảo vệ mỗi `length_guidance` rồi vẫn để cùng lỗi xảy ra vì POV.

## Kết quả thực tế

### UI tạo constraint rỗng

`novel_ai/pages/short_plan.py:209`–`233`:

- `language` có `value=project.config.default_language`.
- `pov` không có default.
- `length_guidance` không có default.
- Không field nào được đánh dấu/validate required trước submit.

`build_chapter_constraint_values()` tại `novel_ai/pages/short_plan.py:59`–`81` chủ động giữ POV/length rỗng.

### Payload vẫn được gửi

`novel_ai/pages/_common.py:555`–`588` chỉ coi cả constraint là empty khi **cả ba** field rỗng. Vì language mặc định là `vi`, payload vẫn chứa:

```json
{
  "chapter_id": "ch_0001",
  "language": "vi",
  "pov": "",
  "length_guidance": ""
}
```

### Backend không có guard input

`novel_ai/services/short_planner.py:644`–`721` kiểm tra action, dependency, arc và assigned chapters; sau đó chuyển thẳng `chapter_constraints` vào context và gọi `complete_json()`.

Không có bước xác nhận:

- mỗi assigned chapter có đúng một constraint;
- `language`, `pov`, `length_guidance` đều không rỗng;
- không có chapter thừa/thiếu hoặc ID lạ.

### Validator chỉ bắt output sau API

`novel_ai/core/validation.py:780`–`800` kiểm tra object contract trong `ChapterPlan.outline` và phát `invalid_outline_contract_item` nếu một trong ba field rỗng. Validation này chạy tại `short_planner.py:731`, sau `complete_json()` tại dòng 713.

## Probe offline đã chạy

### Probe 1 — model tự điền giá trị hợp lệ

Input UI mô phỏng:

```text
language=vi, pov="", length_guidance=""
```

Service vẫn gọi FakeLLM đúng một lần. Khi fake response tự có object hợp lệ, service tạo Short Plan candidate draft. Điều này chứng minh guard input không tồn tại; model có thể tự chọn contract viết thay user.

### Probe 2 — model giữ giá trị rỗng

Service vẫn gọi FakeLLM đúng một lần rồi raise:

```text
ValidationFailure(code="validation_failed")
```

Error record và raw output được lưu. Đây là cùng failure mode đã xảy ra trong `ACC`.

## Contract bị vi phạm

- `docs/design/schemas.md:211`: `{language, pov, length_guidance}` là ba string không rỗng; khi thiếu, service yêu cầu bổ sung trước generate và không tự đoán POV/độ dài.
- `docs/design/prompt-catalog.md:566`: backend chặn yêu cầu viết bắt buộc bị thiếu trước gọi.
- `docs/prompts/v1/short_plan.md:22`: model phải ghi contract được cấp vào outline để truyền xuống Skeleton.

## Mâu thuẫn cần chốt khi sửa

Yêu cầu mới nhất nói phải yêu cầu thiết lập **`length_guidance` mặc định** trước. Contract D013 hiện ghi yêu cầu viết trong `chapter_constraints` và “không thêm project config”. Vì vậy turn implementation cần chọn rõ phạm vi lưu default:

- default theo project;
- default theo arc/lần lập Short Plan;
- hoặc input bắt buộc cho từng chapter với nút áp dụng hàng loạt.

Dù chọn UI nào, backend guard cho payload cuối cùng vẫn bắt buộc và không phụ thuộc session state.

## Blast radius

- Mọi lần generate/regenerate/edit Short Plan có field bắt buộc rỗng.
- Có thể tốn token/chi phí trước khi báo lỗi.
- Accepted Short Plan cũ được giữ nguyên theo failure handling; chưa thấy corruption.
- Raw/error record được lưu đúng, nhưng đó chỉ là recovery/debug, không bù cho guard bị thiếu.
- Nếu model tự bịa giá trị không rỗng, structural validation có thể pass nhưng ý định độ dài/POV không còn do user quyết định.

## Khoảng trống và test đang củng cố hành vi sai

- `tests/integration/test_foundation_planning_ui.py:472`–`493` chỉ kiểm default language; không yêu cầu POV/length đầy đủ.
- `test_chapter_constraints_payload_skips_empty_entries` cho phép toàn constraint rỗng bị bỏ qua.
- Flow UI chính chỉ nhập POV cho một chapter và length cho chapter khác, thay vì bắt cả ba field cho từng chapter.
- Nhiều test service trong `tests/integration/test_planning_services.py` gọi `short_planner.generate()` không truyền `chapter_constraints` nhưng vẫn kỳ vọng thành công.
- Không có test “missing length guidance → GuardError trước LLM → zero calls”.

## Hướng sửa đề xuất cho turn sau

Chưa triển khai. Fix cần có hai lớp:

1. UI yêu cầu user thiết lập default và hiển thị rõ giá trị áp dụng cho từng chapter trước submit.
2. Service validate toàn bộ `chapter_constraints` trước `build_short_plan_context()`/`complete_json()`; thiếu/sai scope phải raise error ổn định và không gọi LLM.

Không nên:

- dựa vào prompt để model tự sửa input;
- chỉ validate response sau API;
- hardcode một số từ tùy ý trong Python;
- silently bỏ chapter thiếu constraint.

## Regression test bắt buộc khi sửa

1. UI chưa có default: Generate disabled hoặc hiện lỗi rõ, không gọi client.
2. Service direct call thiếu `length_guidance`: GuardError, `client.calls == []`.
3. Service direct call thiếu POV/language: cùng hành vi.
4. Thiếu constraint cho một assigned chapter: chặn trước LLM và nêu đúng chapter ID/field.
5. Constraint có chapter ID ngoài assigned scope hoặc duplicate: chặn trước LLM.
6. Default hợp lệ được áp dụng cho mọi chapter; override hợp lệ được giữ.
7. Request hợp lệ gửi đúng một LLM call và output contract tiếp tục được validate.
8. Accepted state cũ giữ nguyên khi guard chặn.

## Acceptance criteria

- Không thể gọi Short Plan API khi bất kỳ assigned chapter nào thiếu language/POV/length.
- `length_guidance` mặc định do user thiết lập, không do LLM hay code tự bịa.
- UI hiển thị rõ default và override trước Generate.
- Backend guard hoạt động cả khi gọi service trực tiếp.
- Failure trước LLM không tạo raw response/candidate và không tiêu API usage.
- Error chỉ đúng chapter và field cần bổ sung.

## Trạng thái xử lý trong phiên review

**Chưa sửa theo yêu cầu người dùng.** Chỉ tái hiện, đối chiếu contract và ghi nhận bằng chứng `ACC`.

## Resolution — T31 (2026-09-22)

**Đã sửa.** Quyết định D016 (default viết lưu theo project + override từng chương) và guard backend
`short_planner.require_writing_contract`/`resolve_constraint_issues` chạy **trước** khi gọi LLM, cho
cả đường gọi service trực tiếp. UI hiển thị contract viết **hiệu lực** của từng chapter và chặn sớm
bằng cùng luật; đổi default không rewrite Short Plan đã accepted.

Bằng chứng (đợt nghiệm thu T40, 2026-09-22):

- `tests/integration/test_t40_acceptance.py::test_shell_blocks_short_plan_before_llm_when_writing_contract_missing`
  — trigger gốc qua shell: constraint rỗng ⇒ **0 LLM call** và không có artifact Short Plan; lưu
  default viết qua form thật ⇒ 1 call và có candidate.
- `tests/integration/test_foundation_planning_ui.py` (nhóm default/override resolved),
  `tests/integration/test_planning_services.py` (guard ở tầng service), `tests/unit/test_config.py`.
- Fixture/contract: `docs/design/examples/linked_project_valid.json` có `default_pov`/
  `default_length_guidance`; `check_t29_contracts.py` PASS 6 case writing-default.
- `.\.venv\Scripts\python.exe -m pytest -p no:randomly` → 658 passed, 1 skipped, 0 failed.

Chi tiết bàn giao: [docs/tasks/T31-short-plan-writing-defaults.md](../tasks/T31-short-plan-writing-defaults.md);
coverage matrix: [docs/design/fix-acceptance-report.md](../design/fix-acceptance-report.md).
