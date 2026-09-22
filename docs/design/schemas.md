# Schema và data contract

Phiên bản: 2026-09-22 (T29 bổ sung mục 3.3, 3.4 và 11). Phụ thuộc: `workflow.md`, `decisions.md`, `novel_ai_spec_v0.2.md`.

Tài liệu này là contract dữ liệu duy nhất cho prompt, service và UI ở MVP. Tên class/file Pydantic ở T08 có thể khác, nhưng hành vi, field bắt buộc và validation cross-field phải giữ theo tài liệu này.

## 1. Quy ước chung

### 1.1. Version và ownership

- `project.schema_version` dùng giá trị `2`, theo spec v0.2.
- Các document/artifact mới trong contract này dùng `schema_version: 1`.
- **Amendment 2026-09-22 (T29):** D016/D017 chỉ thêm field **optional có default**, không đổi
  kiểu hay ý nghĩa field đang có, nên `schema_version` giữ nguyên (`2` cho project, `1` cho
  envelope). File cũ thiếu field mới đọc ra default (`""` hoặc `null`) và được phân loại
  `legacy` theo mục 3.4; không cần migration tự động khi mở project.
- App/backend sở hữu metadata, revision, lifecycle, dependency pins, stable ID, validation error và đường dẫn file.
- LLM chỉ trả payload thuộc action đang chạy. LLM không được tự quyết định `status`, `revision`, `accepted_at`, `dependency_pins`, stable ID cuối cùng hoặc file path.
- `planning_scope` (D017) là **metadata app-owned** trên `ArtifactRevision`, không phải field payload do LLM trả và không cho form sửa.
- Nếu prompt cần ID ổn định, backend phải cấp ID trước hoặc map ID tạm từ LLM sang stable ID trong bước validate. Accepted data chỉ lưu stable ID do app quản lý.

### 1.2. Kiểu cơ bản

| Tên | Kiểu | Bắt buộc | Mặc định | Ghi chú |
|---|---|---:|---|---|
| `StableId` | string | Có | — | Pattern khuyến nghị: `proj_0001`, `char_0001`, `rule_0001`, `fs_0001`, `vol_0001`, `arc_0001`, `ch_0001`, `section_0001`, `rel_0001`, `rev_0001`, `snapshot_0001`. Không dùng tên hiển thị làm khóa. |
| `ChapterNumber` | integer >= 1 | Có | — | Dùng cho hiệu lực thời gian và guard unlock. |
| `RevisionNumber` | integer >= 1 | Có | — | Tăng khi accepted revision thay đổi; candidate có revision dự kiến hoặc ID candidate riêng. |
| `IsoDateTime` | string | Có với metadata | Backend tạo | ISO 8601. Chưa cần timezone phức tạp; T03 có thể chốt lưu UTC/local, nhưng phải nhất quán. |
| `MarkdownRef` | string | Có khi payload nằm ngoài JSON | — | Đường dẫn tương đối project/repo do storage contract T03 chốt; không chứa API secret. |
| `ArtifactStatus` | enum | Có | `missing` | `missing`, `draft`, `accepted`, `stale`, `rejected`. |
| `ChapterStatus` | enum | Có | `planned` | `planned`, `skeleton_ready`, `draft`, `review_required`, `finalizing`, `final_reconciled`. |
| `SourceType` | enum | Có | — | `user`, `llm`, `import`, `service`, `recovery`. |
| `Visibility` | enum | Có khi có secret | `writer_safe` | `writer_safe`, `skeleton_only`, `planner_only`, `author_only`. |

### 1.3. Envelope cho artifact structured

Mọi structured artifact dùng envelope sau. `payload` là phần do user/LLM/service tạo theo schema riêng; metadata bên ngoài do app cấp.

| Field | Type | Required | Default | Owner | Ghi chú |
|---|---|---:|---|---|---|
| `schema_version` | integer | Có | `1` | App | Version của envelope. |
| `artifact_id` | StableId/string | Có | — | App | Ví dụ `premise`, `characters`, `long_plan`, `skeleton_ch_0001`. |
| `artifact_type` | enum/string | Có | — | App | `premise`, `characters`, `world_rules`, `foreshadow`, `long_plan`, `short_plan`, `skeleton`, `review_report`, `reconciliation`, `rolling_patch`, `impact_report`. |
| `status` | ArtifactStatus | Có | `missing` | App | Theo `workflow.md`. |
| `accepted_revision` | `ArtifactRevision<T>` hoặc null | Có | null | App | Revision đang được downstream dùng khi `accepted`. |
| `candidate_revision` | `ArtifactRevision<T>` hoặc null | Có | null | App | Draft/candidate chưa accept. |
| `stale_reasons` | array `StaleReason` | Có | `[]` | App | Chỉ có nội dung khi status `stale` hoặc accepted revision bị ảnh hưởng. |
| `history_refs` | array string | Có | `[]` | App | Trỏ snapshot/history. T03 chốt layout. |

`ArtifactRevision<T>`:

| Field | Type | Required | Default | Owner | Ghi chú |
|---|---|---:|---|---|---|
| `revision` | RevisionNumber | Có | — | App | Với candidate có thể là revision dự kiến; accepted phải duy nhất trong artifact. |
| `payload` | T | Có | — | User/LLM/service, sau đó app validate | Không chứa metadata lifecycle. |
| `payload_source` | `PayloadSource` | Có | — | App | Ghi prompt/run/raw output nếu có. |
| `dependency_pins` | array `DependencyPin` | Có | `[]` | App | Revision input dùng để tạo/accept. |
| `validation` | `ValidationResult` | Có | — | App | `valid`, `invalid`, hoặc `not_checked`. |
| `created_at` | IsoDateTime | Có | Backend tạo | App | Thời điểm candidate/revision được tạo. |
| `accepted_at` | IsoDateTime/null | Có | null | App | Chỉ set khi accepted. |
| `accepted_by` | string/null | Có | null | App | MVP thường là `user` hoặc `auto_accept`. |
| `planning_scope` | `PlanningScope`/null | Không | `null` | **App** | Horizon của revision (D017). Chỉ Long Plan dùng ở T30; artifact khác để `null`. Field thêm 2026-09-22 (T29), optional ⇒ file cũ đọc ra `null` = `legacy`. |

`PlanningScope` (D017): `{start: ChapterNumber, end: ChapterNumber}`, `start >= 1`,
`end >= start`, đơn vị là số chương. Đây là **toàn horizon** mà candidate phải kiến trúc,
không phải kích thước arc và không phải edit window. LLM không trả field này; service ghi khi
tạo candidate và Accept giữ nguyên.

`DependencyPin`: `artifact_id`, `revision`, `scope`, `chapter_id` optional. `scope` dùng các giá trị như `base_idea`, `premise`, `characters`, `world_rules`, `foreshadow`, `long_plan`, `short_plan`, `skeleton`, `timeline_as_of`, `relationship_as_of`, `prose_revision`. (Khác `ArtifactRevision.planning_scope`: `DependencyPin.scope` là *loại* phụ thuộc, không phải khoảng chương.)

`ValidationResult`: `state` (`valid`, `invalid`, `not_checked`), `errors` array. Mỗi error có `path`, `code`, `message`, `severity`.

### 1.4. Project config

`project.json` không chứa API key.

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `schema_version` | integer | Có | `2` | Theo spec. |
| `project_id` | StableId | Có | App tạo | Không đổi khi rename project. |
| `title` | string | Có | — | Tên hiển thị. |
| `default_language` | string | Có | `vi` | MVP ưu tiên tiếng Việt. |
| `genre_prompt_id` | string | Có | `custom` | ID prompt genre, không phải file tùy tiện do LLM chọn. |
| `writing_style_id` | string | Có | `default` | Style dùng cho Writer. |
| `current_chapter` | ChapterNumber | Có | `1` | Chapter đang làm việc; không thay thế lifecycle từng chapter. |
| `default_pov` | string | Không | `""` | **D016**: POV mặc định do user nhập cho request Short Plan mới. Rỗng = chưa thiết lập (không phải default ngầm). |
| `default_length_guidance` | string | Không | `""` | **D016**: độ dài mặc định do user nhập. Rỗng = chưa thiết lập; app không bịa số từ. |
| `auto_accept_structured` | boolean | Có | `false` | Chỉ structured output sau validation. |
| `allow_relationship_replan` | boolean | Có | `true` | Rolling Plan được đề xuất đổi future relationship direction. |
| `rolling_plan_every` | integer >= 1 | Có | `3` | Chỉ tạo reminder. |
| `created_at`, `updated_at` | IsoDateTime | Có | Backend tạo | Metadata app. |

### 1.5. Co-create và Base Idea

`co_create.json` lưu working state; `idea/base_idea.md` là canon sau Finalize.

`IdeaState` payload:

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `genre` | string | Có | — | Nhãn người dùng hiểu được. |
| `tone` | string | Không | `""` | Không dùng làm genre prompt ID. |
| `protagonist` | string | Không | `""` | Text tự do trong working state. |
| `core_concept` | string | Có | — | Hạt nhân truyện. |
| `setting` | string | Không | `""` | Bối cảnh sơ bộ. |
| `conflict` | string | Không | `""` | Mâu thuẫn chính. |
| `constraints` | array string | Có | `[]` | Ràng buộc user. |
| `open_questions` | array string | Có | `[]` | Câu hỏi còn mở. |

`BaseIdeaMetadata`: `schema_version`, `status`, `revision`, `markdown_ref`, `dependency_pins`, `accepted_at`, `accepted_by`.

## 2. Foundation schemas

### 2.1. Premise payload

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `title` | string | Có | — | Tên truyện hoặc tên làm việc. |
| `logline` | string | Có | — | 1-3 câu. |
| `dramatic_question` | string | Không | `""` | Câu hỏi trung tâm. |
| `themes` | array string | Có | `[]` | Chủ đề. |
| `tone_contract` | array string | Có | `[]` | Điều nên giữ về tone. |
| `hard_constraints` | array string | Có | `[]` | Ràng buộc authority cao. |
| `non_goals` | array string | Có | `[]` | Điều không làm trong truyện/MVP. |

### 2.2. Characters payload

`CharactersPayload` có `characters` array.

`Character`:

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `character_id` | StableId | Có | App cấp | Foreign key duy nhất. |
| `display_name` | string | Có | — | Có thể đổi. |
| `aliases` | array string | Có | `[]` | Không dùng làm khóa. |
| `role` | string | Có | — | Ví dụ `protagonist`, `ally`, `antagonist`, text tiếng Việt cũng được nếu thống nhất. |
| `tier` | enum/string | Có | `supporting` | `core`, `major`, `supporting`, `minor`. |
| `effective_from_chapter` | ChapterNumber | Có | `1` | Entry tương lai không vào context quá khứ. |
| `status` | enum | Có | `accepted` | Trạng thái entry: `accepted`, `retired`, `stale`. |
| `public_profile` | object | Có | — | Field writer-safe: `description`, `traits`, `voice`, `known_history`. |
| `writer_profile` | object | Có | `{}` | Chỉ các chỉ dẫn an toàn cho Writer. |
| `author_only` | object | Có | `{}` | Secret, twist, future reveal. Không gửi Writer. |
| `future_direction` | object | Có | `{}` | Dự định phát triển; planner có thể dùng, Writer không nhận mặc định. |

### 2.3. World Rules payload

`WorldRulesPayload` có `world_rules` array.

`WorldRule`:

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `world_rule_id` | StableId | Có | App cấp | Ví dụ `rule_0001`. |
| `category` | string | Có | — | Nhóm lore/rule. |
| `summary` | string | Có | — | Mô tả ngắn. |
| `content` | string/object | Có | — | Nội dung rule. |
| `boundary` | string | Không | `""` | Ràng buộc với độc giả/logic. |
| `effective_from_chapter` | ChapterNumber | Có | `1` | Guard temporal. |
| `visibility` | Visibility | Có | `writer_safe` | `author_only` không vào Writer. |
| `writer_projection` | string/object/null | Có | null | Phiên bản an toàn nếu khác `content`. |
| `author_only` | object | Có | `{}` | Secret hoặc lore chưa reveal. |

### 2.4. Foreshadow payload

`ForeshadowPayload` có `foreshadows` array.

`ForeshadowEntry`:

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `foreshadow_id` | StableId | Có | App cấp | Ví dụ `fs_0001`. |
| `label` | string | Có | — | Tên hiển thị nội bộ. |
| `truth_author_only` | string/object | Có | — | Full author truth. Không gửi Writer. |
| `planned_planting` | array object | Có | `[]` | `chapter_id`, `surface_instruction`, `visibility`. |
| `planned_payoff` | object/null | Có | null | Chapter/arc payoff dự kiến. |
| `effective_from_chapter` | ChapterNumber | Có | `1` | Khi entry được phép được planner/skeleton xét. |
| `writer_visibility` | Visibility | Có | `skeleton_only` | Writer chỉ nhận surface instruction được Skeleton expose. |
| `status` | enum | Có | `active` | `active`, `planted`, `paid_off`, `retired`. |

## 3. Planning schemas

### 3.1. Long Plan payload

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `volumes` | array `VolumePlan` | Có | — | **Ít nhất 1 volume** (enforce ở validator, không chỉ ghi chú). Volume rỗng ⇒ lỗi `empty_long_plan`. |
| `global_threads` | array object | Có | `[]` | Thread lớn, dùng ID nếu cần. |

`VolumePlan`: `volume_id`, `title`, `theme`, `goal`, `arcs`. Mỗi volume có **ít nhất 1 arc**
(`empty_volume`).

`ArcPlan`: `arc_id`, `title`, `chapter_range` (`start`, `end`), `goal`, `core_conflict`, `start_state`, `end_state`, `major_reveals`, `character_ids`, `world_rule_ids`, `foreshadow_ids`, `relationship_directions`.

`RelationshipDirection` trong plan: `relationship_id` optional, `character_ids` `[a,b]`, `arc_direction`, `target_state`, `notes`. Đây là future direction, không phải current relationship.

**Invariant coverage theo horizon (D017, amendment T29).** `planning_scope` ở
`ArtifactRevision.planning_scope`, không ở payload. Với `scope = {start, end}`:

1. Mọi arc có `scope.start <= chapter_range.start <= chapter_range.end <= scope.end`
   (vi phạm ⇒ `out_of_scope_arc`).
2. Xét thứ tự arc theo thứ tự volume trong `volumes` rồi thứ tự arc trong volume: arc đầu
   `chapter_range.start == scope.start` (⇒ `uncovered_scope_start`), arc cuối
   `chapter_range.end == scope.end` (⇒ `uncovered_scope_end`), và
   `arc[i+1].start == arc[i].end + 1` (⇒ `gap_in_scope` khi lớn hơn, `overlap` khi nhỏ hơn
   hoặc bằng).
3. `volumes == []` ⇒ `empty_long_plan`; volume không có arc ⇒ `empty_volume`.
4. Uniqueness `volume_id`/`arc_id` và FK `character_ids`/`world_rule_ids`/`foreshadow_ids`
   giữ theo validation hiện có.

Coverage là **structural invariant**, không phải đánh giá chất lượng phân rã: một arc phủ đúng
horizon vẫn hợp lệ về cấu trúc. Số volume/arc **không** có quota; backend không suy “đủ tốt” từ
count. Khi cả plan chỉ có một arc, UI hiện warning non-blocking (mục 11.3) — warning này không
phải validator và không đổi Auto Accept.

`chapter_range` luôn là endpoint chương, không bao giờ là “số chương của arc”.

### 3.2. Short Plan payload

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `arc_id` | StableId | Có | — | Phải tồn tại trong Long Plan accepted. |
| `chapters` | array `ChapterPlan` | Có | — | Chapter theo arc. |

`ChapterPlan`:

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `chapter_id` | StableId | Có | App cấp | Ví dụ `ch_0001`. |
| `chapter_number` | ChapterNumber | Có | — | Không suy luận từ tên file. |
| `title` | string | Có | — | Tên hiển thị. |
| `summary` | string | Có | — | Dự định, không phải canon actual. |
| `hook` | string | Không | `""` | Hook dự kiến. |
| `outline` | array string/object | Có | `[]` | Beat/scene dự kiến. |
| `character_ids` | array StableId | Có | `[]` | FK tới Characters. |
| `world_rule_ids` | array StableId | Có | `[]` | FK tới World Rules, phải hiệu lực cho chapter. |
| `foreshadow_ids` | array StableId | Có | `[]` | FK tới Foreshadow. |
| `threads` | array string/object | Có | `[]` | Thread plan. |
| `relationship_changes` | array `RelationshipDirection` | Có | `[]` | Future direction trong chapter này. |
| `chapter_goal` | string | Có | — | Mục tiêu plan. |
| `planned_ending` | string | Không | `""` | Dự định kết. |

### 3.3. Resolve và validate `planning_scope` theo action (D017)

| Action | Nguồn `planning_scope` | Nếu thiếu/`null` |
|---|---|---|
| `generate` (Long Plan, tạo candidate mới) | **Input bắt buộc** từ user/form | `GuardError` `missing_planning_scope` — không suy `1..3`, không suy từ progress |
| `regenerate` / `edit` | Input nếu user chủ động đổi; ngược lại `accepted_revision.planning_scope` | `GuardError` `missing_planning_scope` (legacy ⇒ xem mục 3.4) |
| `accept` | `candidate_revision.planning_scope` | `GuardError` `missing_planning_scope`; không revalidate bằng `scope=None` |
| Revalidate sau reload / snapshot | `candidate_revision.planning_scope` (và accepted khi cần) | Chỉ **đọc/xem**; không tự sinh scope |
| Auto Accept structured | `candidate_revision.planning_scope` | Không auto accept; giữ candidate + báo lỗi |

Scope đã ghi trên candidate **không** bị form ghi đè. Đổi horizon là action tường minh: tạo
candidate mới với scope mới; accepted cũ giữ nguyên cho tới khi candidate mới được Accept.

`ActionResult.data` vẫn trả `planning_scope` để UI hiển thị, nhưng **nguồn sự thật** là
`ArtifactRevision.planning_scope`, không phải `ActionResult`.

### 3.4. Compatibility matrix cho Long Plan legacy (D017)

`legacy` = revision/candidate được tạo trước amendment T29, tức `planning_scope == null`.

| Tình huống | Đọc/xem | Generate/Regenerate/Edit | Accept | Auto Accept | Ghi file khi chỉ mở |
|---|---|---|---|---|---|
| Accepted revision **có** scope | Bình thường | Dùng scope đã lưu | Revalidate theo scope | Theo config | Không |
| Accepted revision **legacy** (không scope) | Được, UI gắn nhãn `legacy scope` | Chặn `missing_planning_scope` cho tới khi user xác nhận horizon | Chặn nếu candidate không có scope | Không auto accept | Không |
| Candidate **có** scope | Được | Dùng scope của candidate/accepted | Revalidate theo scope | Theo config | Không |
| Candidate **legacy** (không scope) | Được, gắn nhãn `legacy` | Chặn; user phải regenerate với scope tường minh | Chặn `missing_planning_scope` | Không | Không |

Action xác nhận horizon (user chủ động): user nhập `{start, end}` cho accepted revision legacy,
app **snapshot revision cũ trước** (storage mục 9) rồi ghi `planning_scope` vào accepted
revision theo cơ chế app-owned của mục 3.3. Migration:

- chỉ chạy khi user bấm action, **không** tự chạy khi mở project;
- retry an toàn: chạy lại khi đã có scope ⇒ no-op, không nhân bản revision/snapshot;
- không đổi payload `volumes`/`arcs`, không sửa manuscript, không đổi `short_plan`/chapter;
- **không** lấy `min/max` arc hiện có làm horizon gốc đã xác nhận;
- nếu user xác nhận scope hẹp hơn vùng arc đang có ⇒ validator coverage báo lỗi và accepted
  giữ nguyên; user phải regenerate plan cho horizon đó.

## 4. Skeleton và projection

Quy ước prompt T05: một item object trong `ChapterPlan.outline` mang `{language, pov, length_guidance}` (ba string không rỗng), các item còn lại mô tả beat. Đây là cách dùng type string/object hiện hữu, không thêm field top-level. Backend cấp yêu cầu viết trước Short Plan; Skeleton chuyển chúng thành `global_constraints` writer-safe. Khi thiếu yêu cầu, service yêu cầu bổ sung trước generate, không tự đoán POV/độ dài.

**Amendment 2026-09-22 (D016).** Giá trị ba field trên là **giá trị hiệu lực**: override của
chương nếu user nhập, ngược lại `default_pov`/`default_length_guidance`/`default_language` của
`project.json`. Nguồn default **không** nằm trong payload plan (project config là nơi duy nhất);
`ChapterPlan.outline` chỉ mang object contract đã resolve. Service kiểm đủ ba field cho **mọi**
assigned chapter trước khi build context/gọi LLM; thiếu hoặc có ID thừa/trùng ⇒ `GuardError`
(`missing_writing_contract`, `unknown_chapter_constraint`, `duplicate_chapter_constraint`) kèm
`chapter_id` + field, không gọi LLM. Model không được thay contract đã cấp: giá trị khác trong
output ⇒ `invalid_outline_contract_item`.

`SkeletonPayload`: `chapter_id`, `chapter_number`, `sections`, `global_constraints`, `writer_context_policy`.

Quy ước v1: `global_constraints` là array string writer-safe; `writer_context_policy` có `include_author_only: false`, `include_future_plan: false` theo fixture T02. Backend không cho output nới chính sách này. Section IDs cũ được giữ khi còn tương ứng; entry mới dùng pool hoặc ID cục bộ được backend map theo mục 4.1, index liên tục từ 1; `foreshadow_surfaces.reveal_policy` là string mô tả mức reveal không chứa secret (ví dụ `hint_only`), không phải quyền tự reveal và không được gửi thay surface instruction.

`SkeletonSection`:

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `section_id` | StableId | Có | App cấp | Ví dụ `section_0001`. |
| `index` | integer >= 1 | Có | — | Thứ tự. |
| `type` | enum/string | Có | — | `description`, `action`, `dialogue`, `foreshadow`, `transition`, `ending`, custom. |
| `instruction` | string | Có | — | Writer nhận. |
| `purpose` | string | Có | — | Mục đích thiết kế; không tự động gửi Writer nếu có secret. |
| `purpose_visibility` | Visibility | Có | `planner_only` | Chỉ gửi Writer khi `writer_safe`. |
| `writer_notes` | array string | Có | `[]` | An toàn cho Writer. |
| `author_only_notes` | array string | Có | `[]` | Không gửi Writer. |
| `required_beats` | array string | Có | `[]` | Có thể gửi Writer nếu không chứa secret. |
| `forbidden_moves` | array string | Có | `[]` | Writer nhận. |
| `character_ids`, `world_rule_ids`, `foreshadow_ids` | array StableId | Có | `[]` | FK. |
| `foreshadow_surfaces` | array object | Có | `[]` | `foreshadow_id`, `surface_instruction`, `reveal_policy`. Đây là cầu nối an toàn từ secret sang Writer. |

Projection sang Writer chỉ gồm: `instruction`, `writer_notes`, `forbidden_moves`, `required_beats` đã qua lọc, `purpose` khi `purpose_visibility = writer_safe`, `foreshadow_surfaces.surface_instruction`, writer-safe character/world projections và current state. Không gửi `author_only_notes`, `truth_author_only`, future plan đầy đủ hoặc `purpose` planner-only.

### 4.1. Bổ sung T05 theo spec mục 4, 14–16 — chuẩn bị trước và ID

**ContextBasis** là input backend cấp cho Short Plan/Skeleton, gồm `mode` (`actual` hoặc `provisional`), `actual_through_chapter` (integer >= 0), `planned_bridge` (array `{chapter_id, chapter_number, summary}`). Với actual, actual_through_chapter bằng N−1 (Short Plan: chương đầu range−1), bridge rỗng; baseline chương 1 dùng 0. Với provisional, actual_through_chapter < N−1; bridge bao phủ các chương từ actual_through_chapter+1 đến N−1 theo thứ tự, chỉ lấy intent từ plan accepted và ID đã có. Không tạo TimelineEntry/RelationshipState giả cho bridge.

Backend lưu nguyên ContextBasis và pins của plan nguồn trong field app-owned `preparation_context` của ArtifactRevision Short Plan/Skeleton mới. Field này không do LLM trả; top-level payload không đổi. Candidate provisional được lưu để chuẩn bị trước, không được accept/auto-accept hoặc gửi Writer. Khi actual đầu chương đầy đủ, user review/edit candidate hoặc regenerate, backend build context actual, validate và lưu revision candidate/pins cập nhật trước accept. Không tự đổi mode, tự reaccept hoặc khẳng định bridge trùng actual. Accepted cũ giữ nguyên. Các fixture T02 cũ chưa có field này là lịch sử; backend không được suy thiếu metadata nghĩa là Writer-ready, phải dựng/kiểm tra actual trước dùng.

**ID thiết kế:** `assigned_volume_ids`, `assigned_arc_ids`, `assigned_section_ids` là pool ID mới đã reserve cộng ID cũ có thể dùng trong scope. Không buộc dùng hết. Long Plan/Skeleton thiết kế số entry theo nội dung. Giữ ID cũ khi vẫn cùng entry; nếu cần thêm quá pool, output dùng `tmp_volume_<n>`, `tmp_arc_<n>` hoặc `tmp_section_<n>` (n integer >= 1) duy nhất theo type trong response. Chỉ cho phép tại vị trí ID của entry mới; không dùng làm character/world/foreshadow/chapter/relationship FK.

Backend giữ raw response, tạo mapping một lần theo candidate/run, reserve stable IDs và lưu mapping trong PayloadSource app-owned `id_map` trước accept; retry cùng candidate dùng lại mapping. Thay ID tạm ở vị trí định danh và tham chiếu ID có cấu trúc nội bộ cùng payload, không replace substring trong prose. Từ chối reference tạm không khai báo, trùng ID, ID cũ ngoài scope; validate uniqueness/FK/scope sau mapping. Accepted payload không chứa ID tạm. Không đổi ID foundation hoặc sửa reference ngoài artifact từ output này. Regenerate có thể thay số section/arc trong scope, nhưng chỉ thay accepted sau snapshot/accept; entry vắng mặt trong replacement candidate không làm mất accepted cũ ngay khi generate. T04 và assigned_chapters giữ protocol riêng, không áp fallback này.

## 5. Chapter, prose và review schemas

### 5.1. Chapter metadata

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `schema_version` | integer | Có | `1` | Metadata chapter. |
| `chapter_id` | StableId | Có | — | FK từ Short Plan. |
| `chapter_number` | ChapterNumber | Có | — | Guard unlock. |
| `title` | string | Có | — | Có thể copy từ plan. |
| `status` | ChapterStatus | Có | `planned` | Theo workflow. |
| `previous_chapter_id` | StableId/null | Có | null | Chapter 1 dùng null. |
| `short_plan_pin` | DependencyPin | Có | — | Plan revision của chapter. |
| `skeleton_pin` | DependencyPin/null | Có | null | Set khi skeleton accepted. |
| `drafts` | array `ProseRevision` | Có | `[]` | Markdown refs. |
| `current_draft_revision` | RevisionNumber/null | Có | null | Revision đang hiển thị. |
| `human_review` | `HumanReviewRecord`/null | Có | null | Gắn prose revision. |
| `ai_review_reports` | array `ReviewReportRef` | Có | `[]` | Báo cáo hỗ trợ. |
| `final_candidate` | `FinalCandidate`/null | Có | null | Set trong `finalizing`. |
| `final_revision` | `FinalRevision`/null | Có | null | Canon prose sau reconcile. |
| `reconciliation_pin` | DependencyPin/null | Có | null | Accepted reconciliation. |

`ProseRevision`: `revision`, `markdown_ref`, `source_type`, `is_complete`, `created_at`, `dependency_pins`.

`HumanReviewRecord`: `prose_revision`, `reviewed_at`, `reviewed_by`, `notes`, `valid_for_current_revision`.

`FinalCandidate`: `prose_revision`, `markdown_ref`, `created_at`, `reconciliation_status` (`missing`, `draft`, `accepted`, `failed`).

### 5.2. Review report

`ReviewReportPayload`: `chapter_id`, `prose_revision`, `issues`, `summary`.

`ReviewIssue`:

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `issue_id` | StableId | Có | App cấp hoặc validate từ LLM | — |
| `severity` | enum | Có | — | `info`, `minor`, `major`, `blocking`. |
| `category` | enum/string | Có | — | `base_idea_conflict`, `premise_conflict`, `character_conflict`, `world_rule_conflict`, `skeleton_deviation`, `relationship_inconsistency`, `timeline_inconsistency`, `foreshadow_issue`, `logic_issue`, `style_issue`. |
| `source` | object | Có | — | `authority_kind`, `artifact_id`, `revision`, `field_path` optional. |
| `evidence` | object | Có | — | `prose_revision`, `quote` optional, `section_id` optional. |
| `message` | string | Có | — | Mô tả vấn đề. |
| `suggested_action` | string | Không | `""` | Gợi ý, không tự mutate. |
| `status` | enum | Có | `open` | `open`, `dismissed_by_user`, `resolved_in_revision`. |

### 5.3. Rewrite Section

`RewriteSectionRequest`: `chapter_id`, `prose_revision`, `section_id` optional, `selected_text` optional, `instruction`, `constraints`, `context_pins`.

`RewriteSectionPayload`: `replacement_markdown`, `notes`, `changed_intent` boolean. Nếu `changed_intent = true`, backend phải cảnh báo vì rewrite có thể vượt quyền Writer.

Apply rewrite tạo `ProseRevision` mới và làm Human Review cũ mất hiệu lực.

## 6. State, reconciliation và snapshots

### 6.1. Current Timeline

`CurrentTimelineDocument`: `schema_version`, `latest_final_chapter`, `entries`.

`TimelineEntry`: `timeline_id`, `chapter_id`, `chapter_number`, `time`, `location`, `status`, `source_final_revision`, `accepted_at`.

Timeline chỉ cập nhật từ accepted reconciliation sau Finalize. Short Plan không được ghi timeline actual.

### 6.2. Relationship State

`RelationshipStateDocument`: `schema_version`, `relationships`.

`RelationshipState`:

| Field | Type | Required | Default | Ghi chú |
|---|---|---:|---|---|
| `relationship_id` | StableId | Có | App cấp | Stable FK cho relationship. |
| `character_ids` | array StableId length 2 | Có | — | Không dùng tên. |
| `current` | string | Có | — | Actual state hiện tại. |
| `last_updated_chapter` | ChapterNumber | Có | — | Từ reconciliation. |
| `history` | array object | Có | `[]` | `chapter_id`, `chapter_number`, `current`, `source_final_revision`. |

Planned relationship direction nằm trong Long/Short Plan, không ghi đè field `current`.

### 6.3. Per-chapter snapshot

`ChapterContextSnapshot`: `snapshot_id`, `for_chapter_id`, `for_chapter_number`, `created_from_action`, `dependency_pins`, `timeline_entry_ids`, `relationship_versions`, `effective_character_ids`, `effective_world_rule_ids`, `effective_foreshadow_ids`, `excluded_due_to_effective_chapter`, `writer_projection_hash` optional.

Snapshot dùng cho Writer chapter N phải dùng state trước N. Snapshot chuẩn bị provisional chỉ ghi actual đã biết, không ghi planned_bridge thành state; preparation_context được giữ riêng theo mục 4.1. T03 chốt nơi lưu và recovery.

### 6.4. Reconciliation payload

`ReconciliationPayload`: `chapter_id`, `chapter_number`, `source_final_candidate`, `timeline`, `relationship_updates`, `notes`.

`timeline`: `time`, `location`, `status`. Backend cấp `timeline_id`, source và accepted metadata khi commit.

`relationship_updates`: array `{relationship_id optional, character_ids, current}`. Nếu relationship mới, backend cấp `relationship_id`; nếu có, phải resolve đúng character IDs.

### 6.5. Rolling patch

`RollingPatchPayload`: `status` (`ok`, `adjust`), `reviewed_chapter_range`, `deviations`, `short_plan_changes`, `relationship_plan_changes`, `blocked_by_authority`.

`short_plan_changes` chỉ được nhắm chapter chưa final và phải giữ trong Long Plan accepted. `relationship_plan_changes` chỉ đổi future direction, không đổi Relationship State `current`.

Chi tiết object chốt ở T05 (2026-09-20) để prompt và T08/T14 cùng dùng, giữ nguyên top-level payload:

- `reviewed_chapter_range`: `{start, end}`, số chương >= 1, start <= end; chỉ range actual đã cấp để review.
- `deviations`: array `{chapter_id, planned, actual, evidence}`; ba field cuối là string, evidence chỉ nguồn actual từ input.
- `short_plan_changes`: array `{chapter_id, changes, reason}`; reason là string, changes là object không rỗng chỉ chứa field ChapterPlan trong `title`, `summary`, `hook`, `outline`, `threads`, `chapter_goal`, `planned_ending`, với type theo mục 3.2. Giá trị thay toàn field; không JSON Pointer tùy ý, không thêm/xóa chương hoặc selector.
- `relationship_plan_changes`: array `{chapter_id, relationship_changes, reason}`; array RelationshipDirection thay toàn field cùng tên của ChapterPlan. Cặp character IDs phải có trong selector chapter; relationship_id nếu có phải resolve đúng cặp. Không có current-state mutation.
- `blocked_by_authority`: array `{chapter_id, authority, reason}`; chapter_id là ID đã biết hoặc null cho vấn đề chung, authority/reason là string. Chỉ report.
- Mỗi chapter tối đa một item trong mỗi loại change; hai loại ghi field riêng. Chỉ target trong eligible scope backend cấp: số chương > latest consistent chapter, chưa final, không finalizing/retcon bản đã final, thuộc arc/Short Plan hiện hành.
- `allow_relationship_replan = false` bắt buộc relationship_plan_changes rỗng; short patch cũng không được đổi ý định quan hệ gián tiếp qua summary/outline. Khi true vẫn không được vượt Long Plan.
- `status = ok` khi không deviation/change/blocked; `adjust` nếu có mục cần thay đổi hoặc user review, kể cả toàn blocked. Không đồng nghĩa proposal đã apply.
- Apply ghép vào candidate Short Plan, giữ field/chương ngoài patch; validate toàn candidate, scope/config và pins mới nhất trước accept. Không apply một phần. Chất lượng ngữ nghĩa/authority vẫn cần review ngoài validation cấu trúc.

### 6.6. Retcon impact report

`ImpactReportPayload`: `source_change`, `affected_items`, `risk_summary`, `suggested_actions`.

`affected_items`: `item_kind`, `item_id`, `reason`, `severity`, `suggested_action`. Report không tự đánh dấu stale; stale marking là backend deterministic khi commit retcon/upstream revise.

### 6.7. Structured output error

`StructuredOutputError`: `error_id`, `artifact_id`, `candidate_revision`, `raw_output_ref`, `errors`, `created_at`, `retryable`.

`errors`: array `ValidationError`. `path` dùng JSON Pointer hoặc dotted path; ví dụ `/payload/chapters/0/character_ids/1`.

## 7. Validation cross-field bắt buộc

1. Stable ID duy nhất trong phạm vi type và không lấy từ display name.
2. Mọi FK `character_ids`, `world_rule_ids`, `foreshadow_ids`, `arc_id`, `chapter_id`, `relationship_id` phải resolve trong accepted artifact liên quan.
3. Entity appendable chỉ vào context chapter N nếu `effective_from_chapter <= N`.
4. World rule/lore chương 100 không được xuất hiện trong context/skeleton/writer projection chương 20.
5. `author_only`, `truth_author_only`, `future_direction` và `author_only_notes` không được có trong Writer context.
6. Skeleton chỉ được tạo cho `chapter_id` có trong Short Plan accepted.
7. Writer chỉ được chạy khi Skeleton accepted/fresh và previous chapter guard đạt.
8. Human Review chỉ hợp lệ cho đúng `prose_revision`.
9. Reconciliation `chapter_number` phải khớp Chapter metadata và final candidate.
10. Current Timeline/Relationship chỉ cập nhật từ accepted reconciliation.
11. Plan relationship direction không ghi vào Relationship State current.
12. Rolling patch không sửa chapter đã final, foundation, Long Plan hoặc current state.
13. Retcon commit không rewrite future final prose; chỉ đánh dấu stale theo dependency/scope.
14. Auto Accept structured không áp dụng cho Markdown prose hoặc review confirmation.

## 8. Mapping từ Structure.md

| Structure.md | Schema mới | Quyết định |
|---|---|---|
| `Draft Chapter.chapter`, `title` | `ChapterPlan.chapter_number/title`, `ChapterMetadata.chapter_number/title` | Reuse, nhưng chapter ID app cấp là FK chính. |
| `Draft Chapter.goal`, `conflict`, `hook`, `emotion_arc` | `ChapterPlan.chapter_goal`, `summary/hook`, `Skeleton.global_constraints` hoặc section purpose | Reuse có đổi tên. |
| `Draft Chapter.contract.required_beats` | `SkeletonSection.required_beats` hoặc `instruction` | Reuse sau khi chia section. |
| `forbidden_moves` | `SkeletonSection.forbidden_moves` | Reuse; writer-safe nếu không chứa secret quá mức. |
| `continuity_checks` | Skeleton constraints + Review focus | Reuse chọn lọc; không tự ghi vào actual state. |
| `evaluation_focus` | Review prompt/input hoặc AI Review criteria | Reuse cho prompt/review, không là canon. |
| `payoff_points` | Foreshadow planned planting/payoff + Skeleton surface | Reuse, nhưng truth vào `truth_author_only`. |
| `Character.name` | `Character.display_name` | Reuse, nhưng không làm FK. |
| `Character.aliases`, `role`, `description`, `arc`, `traits`, `tier` | `aliases`, `role`, `public_profile`, `future_direction`, `public_profile.traits`, `tier` | Reuse và tách secret/future. |
| `Foreshadow.id` | `foreshadow_id` | Có thể migrate nếu pattern hợp lệ, nhưng app sở hữu ID cuối. |
| `Foreshadow.description` | `truth_author_only` hoặc `planned_planting.surface_instruction` tùy nội dung | Tách secret vs surface. |
| `Foreshadow.planted_at` | `planned_planting.chapter_id` hoặc `effective_from_chapter` | Đổi tên rõ nghĩa. |
| `Layered Outline.index/title/theme/arcs` | `LongPlan.volumes/arcs` | Reuse cấu trúc volume/arc. |
| `Outline.chapter/core_event/hook/scenes` | `ShortPlan.chapters.summary/hook/outline` | Reuse như plan, không actual timeline. |
| `Relationship State.character_a/b` | `RelationshipState.character_ids` | Loại bỏ FK bằng tên; phải map qua `Character.character_id`. |
| `Relationship State.relation/chapter` | `current`, `last_updated_chapter` | Reuse actual state. |
| `Timeline jsonl` | `CurrentTimeline.entries` | Reuse nội dung, bỏ JSONL runtime để dễ validate atomic JSON. |
| `World rule.category/rule/boundary` | `WorldRule.category/content/boundary` | Reuse, thêm ID/effective/visibility. |
| `Summary.summary/key_events/characters` | `ReconciliationPayload.timeline.status`, optional review/context summary | Reuse như output reconcile, characters phải là IDs. |

Loại bỏ khỏi MVP: liên kết bằng tên nhân vật, timeline JSONL append trực tiếp, layered outline gộp mọi tầng vào một artifact runtime, và mọi field yêu cầu AI tự đọc file/gọi tool.

## 9. Ví dụ

- Project nhỏ hợp lệ: `examples/linked_project_valid.json`.
- Base Idea markdown: `examples/base_idea.md`.
- Final manuscript markdown chương 1: `examples/chapter_0001_final.md`.
- Các case không hợp lệ có lý do: `examples/invalid_examples.json`.

## 10. Cụ thể hóa input/output prompt T06

Không đổi top-level schema/spec. Manifest dùng aliases IdeaStateResponse (`message`, `idea_state`), LongPlanPayload, ShortPlanPayload, MarkdownProse (string). Đây là tên contract, chưa có executable JSON Schema/models.

- Review summary là string; issue_id `issue_<n>` duy nhất trong report (ví dụ issue_0001), backend scope theo report và validate. Source phải khớp nguồn do backend cấp; evidence.prose_revision khớp report; quote nếu có phải là nguyên văn. Severity dùng info/minor/major/blocking; status output luôn open, việc dismiss/resolve là action sau của user/backend.
- Rewrite notes là array string; replacement_markdown string; changed_intent boolean. JSON hợp lệ không tự apply/finalize prose. No-op không tạo revision mới; apply explicit tạo revision và mất hiệu lực review cũ.
- Reconciliation source_final_candidate là object `{prose_revision, markdown_ref}` đúng binding bản đóng băng theo ví dụ T02. Notes là array string; timeline ba string time/location/status; update chỉ cặp known/effective IDs khác nhau. Cặp mới bỏ relationship_id, không null/ID tự tạo; cặp có ID phải resolve đúng hai nhân vật. Unknown alias không tự tạo foundation. Mỗi cặp tối đa một update; không đồng nghĩa relationship chưa xuất hiện phải xóa.
- Impact source_change `{item_kind, item_id, from_revision, to_candidate_revision}`; risk_summary string; suggested_actions array string; severity cùng enum Review. Affected items chỉ nằm trong downstream input, reason nêu cơ sở và giới hạn; không thêm mutation fields.

Input registry, Writer/Rewrite projection, reference selection và failure policy theo prompt-catalog mục 8. Models/loader/service ở T08/T10/T12/T15–T18 phải enforce; prompt không thay backend guard.

## 11. Generation event và editor working copy (T29, 2026-09-22)

Contract này thuộc D018/D019. Đây là **hình dạng dữ liệu và luật hành vi** cho T33–T39; T29
không triển khai runtime và không khai đã có validator thực thi.

### 11.1. `GenerationEvent`

Dataclass/protocol thuần Python (không import Streamlit) trong `novel_ai/core`; service/adapter
phát, page render.

| Field | Type | Bắt buộc | Ghi chú |
|---|---|---|---|
| `status` | enum | Có | Xem 11.2 |
| `operation_id` | string | Có | Khóa idempotency theo `storage.md` mục 4 |
| `action` | string | Có | Ví dụ `generate`, `regenerate`, `rewrite`, `reconcile` |
| `prompt_id` | string/null | Không | Prompt registry id nếu có |
| `artifact_id` | string/null | Không | Artifact đích |
| `chapter_id` | string/null | Không | Chapter đích nếu có |
| `text_delta` | string | Không, default `""` | Chỉ raw text tăng dần; **không** phải payload đã validate |
| `attempt` | integer >= 1 | Có | Lần thử hiện tại của cùng `operation_id` |
| `transport` | enum | Có | `streaming` \| `non_streaming` |
| `detail` | string | Không, default `""` | Thông báo tiếng Việt an toàn để hiển thị |
| `raw_ref` | string/null | Không | Đường dẫn raw/partial theo storage, nếu đã lưu |

Bất biến: event **không** chứa API key, full prompt, author-only context hay future plot.

### 11.2. State machine

| State | Nghĩa | Chuyển tiếp hợp lệ |
|---|---|---|
| `idle` | Chưa chạy action trong phiên | → `connecting` |
| `connecting` | Đã phát request, chưa có dữ liệu | → `streaming` \| `non_streaming` \| `error` |
| `non_streaming` | Provider không stream; chờ response trọn | → `transport_complete` \| `error` |
| `streaming` | Đang nhận `text_delta` | → `transport_complete` \| `partial` \| `error` |
| `transport_complete` | Đã nhận xong response | → `validating` |
| `validating` | Đang parse/validate/ghi (chưa có candidate complete) | → `saved` \| `partial` \| `invalid` \| `error` |
| `saved` | Candidate/draft hoàn chỉnh đã ghi bền | terminal |
| `partial` | Stream đứt/timeout/`finish_reason` cắt, hoặc chỉ giữ được bản dở đã lưu | terminal |
| `invalid` | Parse/schema/scope sai sau khi nhận xong | terminal |
| `error` | Lỗi trước khi có payload dùng được | terminal |

Luật terminal:

- `saved` là **mốc duy nhất** cho phép UI báo “candidate ready” và cho Accept/Review/Finalize.
  `transport_complete` **không** được báo là hoàn tất.
- `partial`, `invalid`, `error` **không** tạo candidate complete, **không** auto accept, **không**
  mở Review/Finalize; accepted/candidate hợp lệ cũ giữ nguyên; raw/partial/error lưu theo
  `storage.md` mục 11.
- Provider `non_streaming` phải hiển thị đúng; **không** phát delta giả.
- Sau `partial`, **không** tự gửi request fallback thứ hai. Retry là action explicit của user và
  dùng lại `operation_id`; replay operation đã `saved` không gọi/merge trùng.

### 11.3. Preview coverage và warning one-arc (D017)

UI Long Plan hiển thị trước Accept: tổng horizon, từng volume/arc và `chapter_range`, tổng số
chương phủ, và cảnh báo **non-blocking** khi cả plan chỉ có một arc. Đây là preview/warning dựa
trên chính output validator trả về — không phải semantic validator, không phải quota và không
đổi Auto Accept (D017 điểm 6–7).

### 11.4. Editor working copy

| Khái niệm | Contract |
|---|---|
| Phạm vi | Session-scoped, khóa theo `project_id` + `artifact_id` + `revision` (+ `chapter_id` nếu có). Không dùng chung giữa project hay workspace. |
| Nội dung | Chỉ field nội dung người dùng sửa được. `artifact_id`, `status`, `revision`, `dependency_pins`, `planning_scope`, stable ID là read-only. |
| Save | Mutation tường minh → service edit-candidate → validate → ghi **candidate**. Không ghi accepted, không lưu theo keystroke. |
| Auto Accept | Save thủ công **không** tự Accept dù `auto_accept_structured = true`. Output AI đã auto-accept ⇒ view ghi `accepted` + action **Revise** tường minh. |
| Roundtrip | Field optional/nested không có widget phải giữ nguyên khi Save. |
| Stale | Working copy có `revision`/pin cũ hơn revision hiện tại ⇒ Save bị từ chối (`stale_working_copy`), không overwrite revision mới; UI giữ input và chỉ field/card lỗi. |
| Raw JSON | Đi qua **cùng** service/validation như form schema-aware; không phải đường ghi tắt. |
| Prose | Save prose tạo prose revision mới và vô hiệu Human Review cũ (D002); Finalize/Retcon giữ action riêng. |

Add/remove/reorder trong working copy phải qua ID allocator + scope/FK/freshness guard của app.
Không thêm form framework mới: form dựng trực tiếp theo schema thật.

