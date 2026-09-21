# Context selection và projection contract

Phiên bản: 2026-09-19. Phụ thuộc: `workflow.md`, `schemas.md`, `storage.md`.

Context trong MVP được build bằng rule deterministic. Không RAG, không embedding, không để LLM tự tìm file. Mọi context build trả về payload đóng gói, dependency pins và snapshot/debug đủ để biết đã gửi gì.

## 1. Nguyên tắc

1. Context chọn bằng stable ID, chapter number, dependency pins và rule hiệu lực; không chọn bằng tên hiển thị hoặc similarity.
2. Context sẵn sàng cho Writer chapter N dùng actual state as-of N-1. Chuẩn bị Short Plan/Skeleton trước đó dùng mode provisional: actual đã biết và planned_bridge tách riêng, không dùng làm Writer context.
3. Plan là future intent, không được dùng như actual timeline/relationship.
4. Writer nhận projection an toàn, không nhận author-only truth, future direction, full plan hoặc lore chưa hiệu lực.
5. Nếu quá budget, backend giảm phần mềm theo priority. Ràng buộc cứng không được cắt âm thầm.
6. Mọi context build lưu dependency pins; các action accept dùng pins để phát hiện stale candidate.

## 2. Priority và budget

Priority:

- `P0`: hard constraints và guard: Base Idea constraints, Premise hard constraints, Skeleton instruction/forbidden moves, previous chapter state required, schema/action metadata.
- `P1`: accepted current state, selected chapter plan, relevant character/world writer-safe facts, required foreshadow surface.
- `P2`: recent finalized summaries/prose excerpts, review issues, relationship history.
- `P3`: style examples, optional reference notes, long context explanations.

Budget rule:

1. Luôn giữ `P0`.
2. Nếu vượt budget, rút gọn `P3` trước bằng summary/window.
3. Sau đó rút gọn `P2` theo recent window.
4. Không rút gọn `P0`; nếu vẫn vượt budget sau khi bỏ optional context, trả lỗi `context_budget_exceeded` với danh sách phần gây vượt.

Không cắt giữa câu/field JSON để vừa token. Context builder phải chọn bớt item nguyên vẹn hoặc tóm tắt theo rule đã định.

## 3. Common filters

### 3.1. Effective chapter filter

Entity appendable hợp lệ cho chapter N khi:

```text
effective_from_chapter <= N
```

Nếu entity bị loại vì tương lai, snapshot ghi vào `excluded_due_to_effective_chapter`.

### 3.2. Visibility filter

Writer context loại bỏ các field sau ở mọi nguồn:

- `author_only`
- `truth_author_only`
- `future_direction`
- `author_only_notes`
- `planned_payoff`
- full `relationship_directions`
- full Long Plan/Short Plan beyond selected chapter
- `purpose` khi `purpose_visibility != writer_safe`

Skeleton context có thể nhận author truth cần thiết để thiết kế surface instruction. Writer chỉ nhận surface instruction đã nằm trong Skeleton.

### 3.3. Recent window

Mặc định:

- Short Plan/Rolling Plan: recent actual state hoặc summaries tối đa 3 chapter gần nhất.
- Skeleton: state as-of previous chapter, plus optional previous final summary 1 chapter.
- Writer: previous chapter summary hoặc excerpt ngắn của chapter N-1 nếu cần continuity; không nhận future final/prose.
- Review: current draft + authority/context liên quan; previous state/prose nếu cần continuity.

Các giới hạn này là default MVP; config có thể tăng sau nhưng không được phá secret/effective filters.

## 4. Context matrix

| Service/action | Sources | Projection | ID selector | As-of chapter | Recent window | Ghi chú |
|---|---|---|---|---|---|---|
| Co-create turn | User messages, project config, genre prompt id | Idea working state, questions | Không cần FK | N/A | Chat working state ngắn | Không đọc story files khác. |
| Finalize Base Idea | Co-create idea state hoặc user markdown | Markdown + metadata | N/A | N/A | N/A | Không gọi LLM bắt buộc. |
| Architect Premise | Genre prompt, Base Idea | Base Idea full, genre guidance | N/A | N/A | N/A | Không nhận Writer Draft. |
| Architect Characters | Base Idea, Premise | Premise full, allowed foundation notes | N/A | N/A | N/A | Backend cấp `character_id` khi accept. |
| Architect World Rules | Base Idea, Premise | Foundation context | N/A | N/A | N/A | Mỗi rule cần effective chapter/visibility. |
| Architect Foreshadow | Base Idea, Premise, characters/world if accepted | Planner/author context, có thể chứa secret | IDs accepted foundation | N/A hoặc planned chapter | N/A | Output truth tách surface. |
| Long Plan | Genre prompt, Base Idea, Premise, accepted Characters/World/Foreshadow, Relationship State nếu có | Dữ liệu an toàn cho planner, có thể gồm author-only khi cần | Mọi ID accepted có hiệu lực trong range plan; không dùng Writer projection | Future planning | Current relationship mới nhất còn consistent | Không nhận draft/prose. |
| Short Plan | Base Idea, Premise, selected Arc, relevant accepted Characters/World/Foreshadow, Current Timeline, Relationship State, recent finalized summaries | Chapter-level planning payload | IDs from Long Plan arc + user selection | Actual trước range; hoặc provisional với planned_bridge riêng | Last 3 finalized summaries | Plan relationship direction only. |
| Rolling Plan | Current Arc, current Short Plan, recent finalized timeline/prose summaries, Relationship State | Deviations + future patch scope | Future chapters in Short Plan | Latest consistent chapter | Since last rolling or max 3 | Không mutate until apply. |
| Skeleton | Base/Premise constraints, current Arc summary, selected Chapter Plan, Timeline/Relationship as-of N-1, relevant Characters/World, Foreshadow truth/surface policy | Section instructions, purpose, beats, safe surfaces | ChapterPlan IDs only; effective <= N | N-1 khi actual; actual đã biết + planned_bridge riêng khi provisional | Previous final summary optional | Có thể biết secret; phải ghi visibility. Chuẩn bị trước chỉ là candidate. |
| Writer | Accepted Skeleton projection, writing style, Character writer profiles, World writer projections, Timeline/Relationship as-of N-1 | Markdown prose prompt only | Skeleton IDs only; effective <= N | N-1 | Previous final summary/excerpt max 1 | Không full plan, không author-only. |
| AI Review | Draft prose, Skeleton, Base/Premise constraints, relevant Character/World, Timeline/Relationship as-of N-1, foreshadow surfaces | Issues with evidence/source | Current chapter IDs | N-1 | Previous chapter summary optional | Report không mutate. |
| Rewrite Section | Selected text/section, current draft revision, Skeleton section, style, local constraints | Replacement prose payload | Section ID/current prose revision | N-1 | Local surrounding text | Apply creates new prose revision. |
| Reconcile | Final candidate prose, prior timeline/relationship as-of N-1, character/relationship IDs | Timeline + relationship updates | Current chapter + known IDs | N-1 | Current chapter final only | Không nhận future plot. |
| Retcon Impact | Thay đổi nguồn, metadata artifact downstream, state entries/snapshots | Impact report | Range chương bị ảnh hưởng theo pins/range | Từ chương bị đổi | Metadata downstream; prose summaries nếu cần | Report không tự stale. |
| Arbiter | Metadata project, status artifact/chapter, stale reasons, pending ops | Next action gợi ý | N/A | Latest consistent | N/A | Không LLM, không mutation. |

## 5. Chapter context cases

### 5.1. Chương 1

- Timeline context: empty baseline.
- Relationship context: empty or accepted initial relationships if explicitly created with `effective_from_chapter <= 1`.
- Previous final prose: none.
- Writer unlock: Skeleton chapter 1 accepted/fresh.

### 5.2. Chương mới N — context sẵn sàng cho Writer

- Require chapter N-1 `final_reconciled`.
- Use timeline/relationship entries through N-1, and only if chain is consistent.
- Use chapter plan IDs from accepted Short Plan for N.
- Exclude future lore/characters with `effective_from_chapter > N`.
- Snapshot context before LLM call.

### 5.3. Chương cũ sau retcon

- For retcon/rebuild chapter K, context actual state comes from K-1, not latest final chapter.
- If K=1, use empty baseline.
- Entries downstream K+1...latest có thể stale; không dùng chúng làm prior state cho tới khi rebuild theo thứ tự.
- Existing future final prose may be referenced only as downstream artifact metadata/impact, not as actual state for K.

### 5.4. Chuẩn bị Short Plan/Skeleton trước khi chương trước final

Theo spec mục 4, thiếu actual N−1 không cấm chuẩn bị draft. Backend cấp ContextBasis provisional theo schemas.md mục 4.1: actual đã biết và planned_bridge riêng từ plan accepted, có pins nguồn. Snapshot chuẩn bị lưu actual entries tới actual_through_chapter; bridge được giữ trong preparation_context của candidate, không ghi current state. Filter entity theo chương mục tiêu và selector vẫn áp dụng, không đưa final tương lai vào actual.

Candidate này chưa được accept hoặc dùng Writer. Sau reconcile chương trước, cần user review/edit/regenerate với context actual và pins mới; mọi action gửi Writer phải kiểm tra mode actual, accepted/fresh Skeleton và previous-final gate. Không dựng actual bằng việc đọc planned_ending. Rolling review chỉ dùng actual, không lấy bridge làm bằng chứng.

## 6. Dạng projection cho Writer

Writer payload should be small and explicit:

```json
{
  "chapter_id": "ch_0002",
  "chapter_number": 2,
  "style": {},
  "skeleton": {
    "sections": [
      {
        "section_id": "section_0001",
        "instruction": "...",
        "writer_notes": [],
        "required_beats": [],
        "forbidden_moves": [],
        "foreshadow_surfaces": []
      }
    ],
    "global_constraints": []
  },
  "characters": [
    {
      "character_id": "char_0001",
      "display_name": "Sở Dương",
      "public_profile": {},
      "writer_profile": {}
    }
  ],
  "world_rules": [
    {
      "world_rule_id": "rule_0001",
      "summary": "...",
      "writer_projection": "..."
    }
  ],
  "state": {
    "timeline_as_of": {},
    "relationships_as_of": []
  }
}
```

Forbidden in writer payload: `truth_author_only`, `author_only`, `future_direction`, `planned_payoff`, full Long Plan, full Short Plan, future chapter summaries, raw rejected alternatives.

## 7. Context fingerprints

Each context build returns:

- `context_id`
- `context_kind`
- `for_chapter_id` optional
- `dependency_pins`
- `included_ids`
- `excluded_due_to_effective_chapter`
- `projection_hash`
- `budget_report`

Khi accept output LLM sau đó, backend phải xác minh dependency pins vẫn khớp. Nếu không khớp, từ chối như stale candidate.

## 8. Checklist chống leak theo thời gian

Before sending context to LLM:

1. Check every included character/world/foreshadow effective chapter.
2. Check no display name is used as FK.
3. Check writer payload lacks author-only/future fields.
4. Check Skeleton purpose visibility before including purpose.
5. Check relationship current comes from state, while planned direction comes only from plan context.
6. Check recent manuscript window does not include chapter >= target chapter.
7. Check retcon/rebuild context reads state before target chapter, not latest.
8. Check budget reduction did not drop Base Idea/Premise/Skeleton hard constraints.
