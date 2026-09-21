# Báo cáo nghiệm thu MVP (T23)

Ngày chạy: 2026-09-21. Baseline: `novel_ai_spec_v0.2.md`, contract `docs/design/`.

Mục tiêu: ghi lại **thực tế đã kiểm** cho giai đoạn A–F của `IMPLEMENTATION_PLAN.md`, map 32 invariant
của `docs/design/workflow.md` mục 7 tới guard/test/manual check cụ thể, và nói rõ phần **chưa** xác minh.

## 1. Môi trường

| Thành phần | Giá trị |
|---|---|
| OS / shell | Windows, PowerShell |
| Python | 3.12.9 |
| streamlit | 1.41.1 |
| pydantic | 2.11.7 |
| pytest | 8.3.4 |
| LLM | `FakeLLMClient` offline cho mọi test; adapter `OpenAICompatibleClient` chỉ test bằng transport giả |
| Dữ liệu test | `tmp_path` + `docs/design/examples/linked_project_valid.json`; **không** đụng `projects/` thật |

Lệnh chạy chính (từ repo root):

```powershell
python -m pytest -q
python -m streamlit run novel_ai\app.py --server.headless true --server.port 8599
```

## 2. Kết quả test đã chạy

| Lệnh | Kết quả |
|---|---|
| `python -m pytest` (toàn suite) | **473 passed, 1 skipped, 0 failed** (~52s) |
| `python -m pytest tests/integration/test_mvp_acceptance.py -q` | **14 passed** (10 case bắt buộc + 4 invariant bổ sung) |
| `python -m pytest tests/unit/test_context.py tests/unit/test_arbiter.py -q` | 33 passed |
| `python -m pytest tests/integration/test_app_entrypoint.py tests/integration/test_foundation_planning_ui.py tests/unit/test_layout_router.py -q` | 40 passed (AppTest offline) |
| `python -m pytest tests/integration/test_reconcile_revision.py -q` | 32 passed (gồm crash matrix + retcon 3 chương) |

1 test **skipped**: `tests/unit/test_layout_router.py:192` — kiểm tra "page chưa nối" cho `skeleton`, tự skip
vì T21 đã nối page đó. Đây là skip có điều kiện, không phải test bị bỏ quên.

Không có test nào phụ thuộc mạng, API key hay dữ liệu người dùng. Toàn bộ chạy offline.

## 3. Manual / UI evidence

- **Streamlit smoke (T19)**: `python -m streamlit run novel_ai\app.py --server.headless true --server.port 8599`
  → `Invoke-WebRequest http://localhost:8599` trả **HTTP 200**, `/healthz` = `200 ok`, log không traceback;
  job đã được kill và port đóng lại. Không tạo `projects/` thật.
- **AppTest offline**: shell + router (T19), luồng Co-create → Architect → Short Plan (T20),
  và các page Skeleton/Writer/Review/Reconcile/Revision (T21/T22) được chạy qua
  `streamlit.testing.v1.AppTest`, gồm kiểm tra "rerun thuần không ghi file" bằng so fingerprint cây project.
- **Chưa walkthrough bằng browser thủ công**: chưa có người dùng bấm tay qua toàn bộ luồng trong trình duyệt.
  Bằng chứng UI hiện có là AppTest + HTTP smoke ở trên; phần này **chưa** được xác nhận bằng mắt.

## 4. Mapping 32 invariant

Cột "Bằng chứng" là test/manual check thật đã chạy; "Guard" là nơi backend thực thi.

| # | Invariant | Guard trong code | Bằng chứng |
|---:|---|---|---|
| 1 | AI generation không tự thành canon | `lifecycle.set_candidate`/`accept_candidate`; `services.architect.generate` | `test_foundation_services.py`; `test_mvp_acceptance.py::test_case2_*` |
| 2 | Structured output chỉ merge sau validation | `validation.validate_artifact_payload`; `co_create.parse_structured_or_fail` | `test_validation.py`; `test_case3_*` |
| 3 | Auto Accept không bypass Chapter Review | `validation.validate_auto_accept_scope`; `lifecycle.guard_finalize` | `test_validation.py::test_auto_accept_*`; `test_case2_*` |
| 4 | User review chapter N trước Writer N+1 | `context.build_writer_context`; `lifecycle.guard_writer` | `test_context.py`; `test_case1_*` |
| 5 | Base Idea > downstream | `services.revision.revise_base_idea` + `lifecycle.mark_downstream_stale` | `test_reconcile_revision.py`; `test_arbiter.py::test_stale_artifact_*` |
| 6 | Premise > plan | `services.revision.revise_premise` | `test_case7_*` |
| 7 | Long Plan > Short Plan | `validation` (`arc_id` phải resolve trong Long Plan accepted) | `test_planning_services.py`; `test_case9_*` |
| 8 | Short Plan > Skeleton | `validation._validate_skeleton` (chapter phải có trong Short Plan) | `test_validation.py`; `test_skeleton_writer_services.py` |
| 9 | Skeleton > Writer | `context.build_writer_context` (skeleton accepted + pin khớp) | `test_context.py::test_writer_context_blocks_*`; `test_case1_*` |
| 10 | Downstream không sửa upstream | Ranh giới service; không có đường ghi ngược | `test_mvp_acceptance.py::test_writer_output_never_mutates_upstream_artifacts` |
| 11 | Writer không plan | `services.writer` chỉ trả/ghi markdown | `test_writer_review_services.py` |
| 12 | Writer không sở hữu Foreshadow truth | `context._writer_section_projection` bỏ `author_only_notes`/`purpose` kín | `test_case4_*`; `test_context.py` |
| 13 | Skeleton bắt buộc | `guard_writer`/`build_writer_context` raise trước khi gọi LLM | `test_case1_*` (`client.calls == []`) |
| 14 | Planner biết secret, Writer chỉ surface | `context` projection + `validate_writer_projection` | `test_case4_*`; `test_validation.py::test_writer_projection_*` |
| 15 | Plan không chứng minh sự kiện đã xảy ra | Timeline chỉ ghi trong `accept_reconciliation` | `test_mvp_acceptance.py::test_accepting_short_plan_does_not_write_actual_state` |
| 16 | Final Manuscript mới là canon prose | `lifecycle` + `reconcile.accept_reconciliation` | `test_case1_*`, `test_case6_*` |
| 17 | Current Timeline chỉ từ reconcile | `reconcile.accept_reconciliation` (transaction) | `test_case5_*`; `test_reconcile_revision.py` |
| 18 | Current Relationship chỉ từ reconcile | như trên | `test_case5_*` |
| 19 | Plan chỉ quản hướng relationship tương lai | `validation.validate_plan_does_not_mutate_state` | `test_validation.py::test_plan_payload_*`; `test_case9_*` |
| 20 | Finalize phải reconcile state | `reconcile.finalize_chapter` → `finalizing` | `test_case2_*`, `test_case6_*` |
| 21 | Chapter sau chỉ unlock khi trước `final_reconciled` | `build_writer_context`/`guard_writer` | `test_case1_*`; `test_case6_*` |
| 22 | Regenerate tạo candidate riêng | `lifecycle.set_candidate` giữ accepted | `test_case3_*` |
| 23 | Accepted không bị silent overwrite | `storage` fingerprint + `history/<op>/before` | `test_storage_transaction.py`; `test_case3_*` |
| 24 | Architect append phải có `effective_from_chapter` | Schema bắt buộc + `architect.append_entries` | `test_foundation_services.py` |
| 25 | Append tương lai không xuất hiện trong quá khứ | `validation.partition_by_effective_chapter` | `test_context.py`; `test_case4_*` |
| 26 | Upstream revision không tự rewrite downstream | `mark_downstream_stale` chỉ đánh dấu | `test_case7_*` |
| 27 | Retcon không tự rewrite future chapter | `revision.start_retcon` + `reset_consistency_after_retcon` | `test_case8_*`; `test_reconcile_revision.py` |
| 28 | AI failure không đổi accepted state | `co_create.save_llm_error`/`save_structured_error` | `test_case3_*`, `test_case6_*` |
| 29 | File exists ≠ artifact hoàn thành | Mọi guard đọc metadata, không đọc sự tồn tại file | `test_context.py::test_writer_context_blocks_when_skeleton_missing`; `test_case6_*`, `test_case10_*` |
| 30 | Không subsystem nào thành agent | Không có vòng lặp; guard raise trước khi gọi LLM | `test_case1_*`, `test_case8_*` assert `client.calls == []`; review code: không service gọi service để chạy tiếp |
| 31 | Không thêm công nghệ ngoài rule + JSON | `pyproject.toml` chỉ streamlit/pydantic/python-dotenv (+pytest dev) | Kiểm bằng mắt trên `pyproject.toml`; adapter HTTP dùng stdlib `urllib.request` |
| 32 | Human là authority cuối cùng | `services/*.accept_*` cần user (hoặc auto-accept chỉ cho structured hợp lệ) | `test_case2_*`; `test_foundation_services.py` (auto accept on/off) |

Invariant 30 và 31 là hai chỗ **không** có test tự động chứng minh đầy đủ: chúng dựa trên code review
(không có scheduling/loop; không có dependency mới). Các invariant còn lại đều có test đã chạy.

## 5. 10 case bắt buộc của `IMPLEMENTATION_PLAN.md` mục 8

| # | Case | Test |
|---:|---|---|
| 1 | Chương 1 review → finalize → reconcile mới unlock Writer chương 2 | `test_mvp_acceptance.py::test_case1_two_chapter_flow_unlocks_writer_only_after_reconcile` |
| 2 | Auto Accept bật vẫn không tự finalize prose | `test_case2_auto_accept_structured_never_finalizes_prose` |
| 3 | Accepted không đổi khi regenerate lỗi / candidate chưa accept | `test_case3_accepted_state_survives_regenerate_failure` |
| 4 | Lore chương 100 không vào context chương 20; secret không lọt profile/Skeleton | `test_case4_late_lore_and_author_secrets_never_reach_early_prompts` |
| 5 | Retry Finalize/reconcile và rerun không merge/sinh request trùng | `test_case5_retry_finalize_and_reconcile_do_not_duplicate_state` |
| 6 | Crash giữa commit nhiều file → recovery nhất quán, không unlock sớm | `test_case6_crash_mid_commit_recovers_consistently_and_unlocks_late` |
| 7 | Sửa Premise đánh dấu planning stale, giữ nguyên Final Manuscript | `test_case7_revise_premise_marks_planning_stale_but_keeps_final_manuscript` |
| 8 | Retcon chương cũ dùng state đúng thời điểm, không ghi lùi current state | `test_case8_retcon_old_chapter_does_not_roll_back_later_state` |
| 9 | Rolling Plan không sửa chương final/foundation/Long Plan; config quan hệ | `test_case9_rolling_patch_cannot_touch_final_chapter_or_foundation` |
| 10 | Đóng/mở lại app vẫn thấy accepted, candidate, partial, pending | `test_case10_state_survives_close_and_reopen` |

Case 6 tiêm lỗi vào **target thật** của commit (không phải file staged) ngay trước `chapter.json`, tức
sau khi final markdown, reconciliation, timeline, relationships và snapshot đã replace — đúng điểm khó
nhất của crash matrix (`storage.md` mục 7). Recovery tự hoàn tất, không cần người can thiệp, và gọi
recovery lần hai không nhân đôi timeline.

## 6. Secret và cấu hình

- API key/endpoint chỉ nằm ở app config (`.env`/môi trường). `project.json` không có field secret;
  `LLMConfig.__repr__`/`redacted()` chỉ trả `set`/`not set`.
- Context Writer/Rewrite/Review dùng projection writer-safe và bundle tự gọi
  `validate_writer_projection`, raise `secret_leak` thay vì gửi prompt.
- Test sentinel thật (`mảnh vật phẩm cổ`, `liên quan tới bí mật của cái nồi`,
  `Gieo mầm vật phẩm có nguồn gốc bất thường`, `Cơ quan phòng chống tội phạm xuyên giới`) được assert
  không xuất hiện trong prompt Writer chương 1 và không nằm trong `excluded`/included sai chỗ.

## 7. Live API — **chưa xác minh**

Không có endpoint/model/API key trong môi trường này, nên **chưa** chạy live smoke. Mọi kết quả ở trên
là fake LLM offline. Adapter `OpenAICompatibleClient` chỉ được kiểm bằng transport giả (thành công,
JSON sai, timeout, 401, 5xx, stream đứt, `response_format` khi bật native structured output,
redaction). Vì vậy **không** có tuyên bố tương thích với provider thật.

## 8. Hạn chế đã biết

1. `writer.discard_draft` chỉ bỏ được prose revision **cũ**: `storage.save_chapter` chặn lùi
   `current_draft_revision`, nên muốn thay bản đang hiển thị phải Save/Regenerate rồi bỏ bản cũ.
2. `writer.continue_draft` chưa nhận `user_instruction` (chữ ký API đã chốt).
3. `reviewer.mark_reviewed` vẫn cho phép khi chapter `finalizing`; siết lại thuộc quyết định của T17
   (hiện finalize đã tự kiểm tra review hợp lệ theo revision).
4. Rolling Plan có UI đầy đủ nhưng **chưa** có AppTest end-to-end (cần chapter `final_reconciled` thật);
   hiện kiểm bằng helper thuần + service T14.
5. `lifecycle.mark_downstream_stale` đánh dấu theo **family** (ví dụ toàn bộ Short Plan/Skeleton) chứ
   không theo từng arc/chapter, nên có thể đánh dấu rộng hơn phạm vi bị ảnh hưởng. Đúng tinh thần
   "chỉ đánh dấu, không rewrite", nhưng người dùng sẽ phải review nhiều hơn mức tối thiểu.
6. T14/T15 lưu `ContextBasis`/pins của candidate chuẩn bị trước ở `ChapterMetadata.preparation_context`
   (guard tương đương: candidate provisional không auto-accept, không dùng cho Writer).
   `ArtifactRevision.preparation_context` đã được bổ sung theo `schemas.md` mục 4.1 nhưng hiện chưa
   được service ghi vào.
7. Không có migration framework khi `schema_version` đổi: version chưa hỗ trợ bị **từ chối** kèm thông báo,
   không tự chuyển đổi.
8. Không có nút stop-generation, không rich-text editor, không section→range mapping đầy đủ cho
   Rewrite Section (chỉ resolve theo `selected_text`), đúng phạm vi "ngoài MVP".
9. UI chưa được walkthrough thủ công bằng browser (xem mục 3).
10. Sửa **nội dung** draft retcon qua Writer/Review hiện bị service guard `chapter_already_final` chặn
    (UI nói rõ lý do và dẫn sang đường retcon, nhưng chưa có action service riêng để regenerate prose
    retcon). Candidate của Rewrite Section chỉ sống trong session state, chưa lưu thành artifact.
11. `pyproject.toml` vẫn mô tả "scaffold T07" và README chưa được cập nhật cho build MVP — thuộc T24.

## 9. Kết luận trong phạm vi MVP

- Không còn lỗi phá hard invariant trong các case đã xác định: 473 test pass, 0 fail.
- Accepted state được bảo vệ trong mọi failure path đã kiểm (LLM lỗi, schema sai, timeout, crash
  giữa transaction, retry lặp).
- Guard nằm ở backend: nhiều test **gọi service trực tiếp** không qua UI và vẫn bị chặn.
- Không tuyên bố gì về chất lượng **ngữ nghĩa/văn chương** của output LLM (D009) và không tuyên bố
  tương thích provider thật khi chưa chạy live API.

## 10. Cập nhật sau T24/T25

Báo cáo này giữ nguyên số liệu của phiên T23. Hai phiên sau đã thay đổi code và số test:

| Phiên | Thay đổi chính | `python -m pytest` |
|---|---|---|
| T24 | Review toàn repo, sửa 9 bug (leak author truth vào Writer context, guard chain sau retcon, idempotency/audit ở storage, …) + viết README/user-guide/troubleshooting | 484 passed, 1 skipped |
| T25 | Bố cục UI theo spec mục 28 (nav trên, 3 pane, status bar đáy); xử lý F-B1/B2/B6, C1/C2/C3 và một phần F-B7; thêm test F-A4/F-A9 | **494 passed, 1 skipped** |

Chi tiết: [review-findings-t24.md](review-findings-t24.md) và
[docs/tasks/T25-ui-layout-spec28.md](../tasks/T25-ui-layout-spec28.md). Giới hạn còn lại của build
hiện tại nằm ở mục "Trạng thái và giới hạn" của [README.md](../../README.md).
