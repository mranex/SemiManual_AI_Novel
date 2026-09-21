# Novel AI — Specification v0.2

## 0. Tinh thần của dự án

Novel AI là một ứng dụng hỗ trợ viết tiểu thuyết bằng AI theo mô hình **human-in-the-loop**.

Mục tiêu:

> Giúp người dùng đi từ ý tưởng → kiến trúc → kế hoạch → skeleton → prose → review bằng một workflow rõ ràng, kiểm soát được và dễ debug.

Triết lý:

> **Potato but effective.**

Ứng dụng cố tình đơn giản:

- Python.
- Streamlit.
- JSON + Markdown.
- File-based.
- Không agent.
- Không multi-agent.
- Không autonomous loop.
- Không RAG ở MVP.
- Không vector database.
- Không graph database.
- Không event bus.
- Không microservice.
- Không workflow engine.
- Không background agent.
- Không “AI tự điều phối AI”.

AI được coi như một hàm:

```python
output = ai(input)
```

Project file mới là nơi lưu state.

Người dùng là authority cuối cùng.

---

# 1. Nguyên tắc cốt lõi

## 1.1. Human owns the story

Người dùng quyết định:

- Base Idea.
- Premise cuối cùng.
- World Rules.
- Character foundation.
- Foreshadow.
- Long Plan.
- Short Plan.
- Chapter Skeleton.
- Prose cuối cùng.
- Retcon.
- Canon.

AI chỉ:

- đề xuất;
- mở rộng;
- lập kế hoạch;
- viết;
- review;
- trích xuất structured state.

AI không tự quyết định canon.

## 1.2. Không có Agent

Không module nào được phép:

- tự gọi module khác;
- tự chạy workflow nhiều bước;
- tự sửa upstream;
- tự rewrite toàn project;
- tự replanning mà không có rule/config rõ ràng;
- tự quyết định bước tiếp theo thay user.

Arbiter chỉ là:

```text
Project State
+
Rules
=
Suggested Next Step
```

Không phải agent.

---

# 2. Thứ tự quyền lực của thông tin

Đây là luật cứng của hệ thống.

```text
Base Idea
    ↓
Architect / Premise
    ↓
Long Plan
    ↓
Short Plan
    ↓
Skeleton
    ↓
Writer
```

Nếu có conflict:

```text
Base Idea > Premise > Long Plan > Short Plan > Skeleton > Writer
```

Writer luôn là tầng yếu nhất.

Writer không có quyền sửa hướng truyện.

## 2.1. Các tài liệu hỗ trợ của Architect

Các tài liệu sau cũng thuộc Architect:

- Characters
- World Rules
- Foreshadow

Chúng có thể được **append thêm trong quá trình viết**.

Mỗi entry mới phải có thời điểm bắt đầu có hiệu lực.

Ví dụ:

```json
{
  "id": "world_rule_023",
  "effective_from_chapter": 100,
  "content": "Từ chapter 100 trở đi tồn tại Cơ quan phòng chống tội phạm xuyên quốc gia."
}
```

Rule này:

- có thể được dùng ở chapter 100+;
- không được đưa vào context của chapter 1–99;
- không được retroactively làm như thể nó đã tồn tại từ đầu.

Tương tự với character hoặc lore mới.

---

# 3. Canon, Draft và Accept

## 3.1. AI output mặc định là Draft

Mọi output từ AI ban đầu đều là:

```text
DRAFT
```

Nó chưa phải canon.

Ví dụ:

- Premise AI vừa generate.
- Long Plan mới regenerate.
- Short Plan đề xuất mới.
- Skeleton mới.
- Relationship adjustment.
- Timeline update proposal.
- Writer prose.

## 3.2. Accept

Structured output phải đi qua bước:

```text
AI Draft
→ Validate Structure
→ Accept
→ Merge vào accepted state
```

User có thể Accept thủ công.

Hoặc bật:

```text
auto_accept_structured = true
```

Khi auto-accept được bật:

1. AI trả structured output.
2. App validate đúng schema.
3. Nếu parse/validate thành công thì merge vào accepted state.
4. Nếu sai schema thì không merge.

Auto-accept chỉ là shortcut cho structured output.

## 3.3. Auto Accept không bypass Chapter Review

**Chapter prose không bao giờ được auto-finalize.**

Dù `auto_accept_structured = true`, workflow chapter vẫn bắt buộc:

```text
Writer Draft
→ Human Review
→ Finalize Chapter
→ Reconcile State
→ Next Chapter được unlock
```

User bắt buộc phải review từng chapter.

---

# 4. Finalized Chapter là Canon Manuscript

Một chapter chỉ trở thành canon khi user bấm:

```text
[Finalize Chapter]
```

Trước đó:

```text
Draft ≠ Canon
```

Sau Finalize:

```text
Final Manuscript = Canon prose
```

Writer của chapter tiếp theo chỉ được chạy khi chapter trước đã hoàn thành review/finalize/reconciliation.

Ví dụ:

```text
Chapter 1 Draft
→ Review
→ Finalize
→ Reconcile
→ Chapter 2 Writer unlocked
```

Long Plan, Short Plan và Skeleton của chapter sau có thể chuẩn bị trước.

Nhưng **Writer không được nhảy qua review gate**.

---

# 5. Hai hệ thống Story State

Ứng dụng dùng hai hệ thống state riêng biệt.

Không xây Knowledge Graph.

Không xây Character Memory framework.

Chỉ dùng JSON đơn giản.

## 5.1. Current Timeline

File:

```text
state/current_timeline.json
```

Mục tiêu:

> Cho module sau biết câu chuyện hiện đang ở đâu và vừa xảy ra chuyện gì.

Mỗi entry chỉ cần ngắn.

Ví dụ:

```json
{
  "chapter": 1,
  "time": "Buổi tối",
  "location": "Khách sạn Thanh Hà",
  "status": "Nam chính vừa gặp nữ chính lần đầu. Hai người còn dè chừng nhau."
}
```

Sau chapter 2:

```json
{
  "chapter": 2,
  "time": "Cùng buổi tối, khoảng một giờ sau",
  "location": "Khách sạn Thanh Hà",
  "status": "Hai người cùng phát hiện có sát thủ theo dõi khách sạn."
}
```

Current Timeline không cần chứa mọi fact trong truyện.

Chỉ chứa:

- chapter;
- time;
- location;
- status ngắn.

Mục tiêu là continuity gần.

### 5.1.1. Timeline được cập nhật khi nào?

Sau khi user đã review và Finalize Chapter:

```text
Final Manuscript
→ AI extract timeline update
→ structured output
→ validate
→ Accept / Auto Accept
→ update Current Timeline
```

Writer không cập nhật Timeline trực tiếp.

## 5.2. Relationship State

File:

```text
state/relationships.json
```

Mục tiêu:

> Lưu trạng thái hiện tại và hướng phát triển quan hệ giữa các nhân vật.

Relationship có hai khái niệm khác nhau.

### Current Relationship

Thứ đang thực sự đúng ở thời điểm hiện tại.

Ví dụ:

```json
{
  "a": "char_001",
  "b": "char_002",
  "current": "Dè chừng lẫn nhau",
  "last_updated_chapter": 3
}
```

### Planned Relationship Direction

Thứ planner muốn quan hệ phát triển thành.

Long Plan chịu trách nhiệm hướng cấp Arc.

Ví dụ:

```text
Arc 1: Kẻ thù
Arc 2: Đồng minh
Arc 3: Bạn bè / tình cảm
```

Short Plan chịu trách nhiệm đường chuyển trong Arc.

Ví dụ:

```text
Ch. 10: thù địch
Ch. 13: bắt buộc hợp tác
Ch. 16: bắt đầu tin tưởng
Ch. 20: đồng minh
```

### 5.2.1. Quyền cập nhật Relationship

```text
Long Plan
→ quyết định Arc-level relationship direction

Short Plan
→ quyết định chapter-level transition

Final Chapter + Review
→ xác nhận current relationship thực tế
```

Rolling Plan có thể đề xuất điều chỉnh đường quan hệ tương lai.

Config:

```json
{
  "allow_relationship_replan": true
}
```

Nếu bật:

- Rolling Plan được phép đề xuất thay đổi future relationship path.
- Nếu auto-accept structured đang bật, structured plan hợp lệ có thể được merge.
- Không được sửa Base Idea/Premise.
- Không được tự rewrite chapter đã final.

---

# 6. Source of Truth

Không dùng câu:

> “Mọi file đều là truth.”

Thay vào đó:

> **Accepted foundation + Final Manuscript + accepted current state là canon.**

| Loại | Canon? |
|---|---:|
| AI Draft | Không |
| Rejected Draft | Không |
| Base Idea accepted | Có |
| Premise accepted | Có |
| Character/World Rule accepted và đã có hiệu lực | Có |
| Long Plan | Plan, không phải sự kiện đã xảy ra |
| Short Plan | Plan, không phải sự kiện đã xảy ra |
| Skeleton | Instruction |
| Writer Draft | Không |
| Final Manuscript | Có |
| Current Timeline accepted | Có |
| Current Relationship accepted | Có |
| Review note | Không |
| AI inference | Không |

---

# 7. Conflict Rules

Nếu các tầng conflict:

```text
Base Idea
> Architect/Premise
> Long Plan
> Short Plan
> Skeleton
> Writer
```

Tầng dưới phải nhường tầng trên.

Ví dụ:

```text
Base Idea:
Nhân vật chính tuyệt đối không giết trẻ em.

Writer Draft:
Nhân vật chính giết một đứa trẻ.
```

Review phải trả:

```text
CONFLICT
Source: Base Idea
Target: Writer Draft
```

App không tự sửa canon.

User được quyền:

- sửa Draft;
- regenerate;
- hoặc chủ động revise Base Idea bằng hành động riêng.

---

# 8. Upstream Mutation Rule

Module downstream **không được sửa upstream**.

Ví dụ:

- Writer không sửa Skeleton.
- Skeleton không sửa Short Plan.
- Short Plan không sửa Long Plan.
- Long Plan không sửa Premise.
- Không planner nào sửa Base Idea.

Đây là luật cứng.

## 8.1. User vẫn có quyền revise Foundation

Chỉ user mới được phép chủ động sửa:

- Base Idea;
- Premise;
- World Rules;
- Character foundation.

Đây phải là hành động explicit.

Ví dụ:

```text
[Revise Base Idea]
```

Nếu user revise upstream:

- app không tự rewrite downstream;
- downstream liên quan được đánh dấu `stale`;
- Arbiter báo cần review lại.

Không AI module nào được tự làm việc này.

---

# 9. Architect Append Rule

Architect foundation có hai loại dữ liệu.

## 9.1. Foundation gần như cố định

- Base Idea
- Premise

Chỉ user chủ động revise.

## 9.2. Appendable Architecture

- Character
- World Rule
- Foreshadow
- Faction
- Location/lore nếu sau này cần

Có thể thêm trong lúc truyện đang chạy.

Mỗi entry append mới có tối thiểu:

```json
{
  "id": "stable_id",
  "effective_from_chapter": 100,
  "status": "accepted",
  "content": {}
}
```

Context Builder chỉ được lấy entry nếu:

```python
effective_from_chapter <= current_chapter
```

Nhờ đó lore tạo ở chapter 100 không leak về chapter 20.

---

# 10. Stable IDs

Các entity quan trọng dùng ID ổn định.

Ví dụ:

```text
char_001
arc_003
ch_0021
fs_005
rule_014
```

Tên hiển thị có thể đổi.

ID không đổi.

MVP không cần UUID phức tạp.

Sequential/string ID là đủ.

---

# 11. Artifact Lifecycle

Không dùng `file exists = complete`.

## 11.1. Structured Artifact

Trạng thái tối thiểu:

```text
missing
draft
accepted
stale
rejected
```

Ví dụ:

```json
{
  "status": "accepted",
  "revision": 3
}
```

## 11.2. Chapter

Chapter có lifecycle:

```text
planned
→ skeleton_ready
→ draft
→ review_required
→ finalizing
→ final_reconciled
```

Writer chapter N+1 chỉ được unlock nếu chapter N là:

```text
final_reconciled
```

---

# 12. Regenerate

Regenerate không overwrite Accepted ngay lập tức.

Flow:

```text
Accepted Revision
→ Regenerate
→ New Draft Candidate
→ Validate
→ Accept / Auto Accept
→ Replace Accepted Revision
```

Trước khi replace accepted revision:

```text
snapshot old revision
```

Không cần Git.

Chỉ cần copy file cũ vào:

```text
history/
```

---

# 13. Deterministic Context Selection

MVP không dùng RAG.

Không embedding.

Không semantic search.

Không để AI tự mò project.

Context được build bằng rule rõ ràng.

## 13.1. Long Plan Context

Long Plan nhận:

```text
Genre Prompt
Base Idea
Premise
Accepted Characters
Accepted World Rules
Accepted Foreshadow
Relationship State nếu có
```

Long Plan không nhận Writer Draft.

## 13.2. Short Plan Context

Short Plan nhận:

```text
Base Idea
Premise
Current Arc từ Long Plan
Relevant accepted Characters
Relevant accepted World Rules
Relevant Foreshadow
Current Timeline
Relationship State
Recent finalized chapter status
```

Relevant entity được chọn bằng ID explicit trong plan.

Không similarity search.

## 13.3. Skeleton Context

Skeleton nhận:

```text
Base Idea constraints
Premise constraints
Current Arc
Current Chapter Plan
Current Timeline
Relevant Characters
Relevant World Rules
Current Relationship State
Foreshadow author instruction
```

Skeleton được phép biết secret cần thiết để thiết kế cách hint/reveal.

## 13.4. Writer Context

Writer bị cố tình giới hạn.

Writer chỉ nhận:

```text
Skeleton
Writing Style
Relevant Character profile
Current Relationship State
Relevant World Rules
Current Timeline
Language / POV constraints
```

Writer không cần:

- Long Plan đầy đủ;
- Short Plan đầy đủ;
- full Foreshadow database;
- secret answer không được Skeleton surface;
- rejected alternatives;
- future plot không cần thiết.

Writer chỉ biết đủ để viết đúng Skeleton.

---

# 14. Foreshadow Policy

Planner/Architect có thể biết:

```text
Full Author Truth
```

Skeleton chịu trách nhiệm chuyển truth thành instruction an toàn.

Ví dụ:

```text
Foreshadow truth:
Thanh kiếm thật ra thuộc về cha của nhân vật chính.

Skeleton instruction:
Ở đoạn 7, mô tả thanh kiếm phản ứng nhẹ khi nghe tên gia tộc X.
Không giải thích nguyên nhân.
Không reveal nguồn gốc.
```

Writer chỉ nhận:

```text
Skeleton instruction
```

Writer không nhận full secret.

---

# 15. Skeleton là bắt buộc

Không có đường:

```text
Short Plan → Writer
```

Luồng bắt buộc:

```text
Short Plan
→ Skeleton
→ Writer
```

Writer không được generate chapter nếu Skeleton chưa Accepted.

Skeleton là dàn ý tuyệt đối của chapter.

Nó có thể mô tả:

- đoạn mô tả;
- hành động;
- đối thoại;
- mục đích đoạn;
- emotion beat;
- information reveal;
- foreshadow instruction;
- transition;
- ending beat.

---

# 16. Writer

Writer có đúng một nhiệm vụ:

> Biến Skeleton thành prose.

Writer không:

- lập plan;
- thêm major plot;
- đặt foreshadow mới;
- sửa relationship direction;
- đổi World Rules;
- tạo twist lớn;
- quyết định Arc;
- sửa premise;
- sửa timeline state.

Nếu Writer cần invention nhỏ để prose tự nhiên như tên món ăn, thời tiết hoặc động tác nhỏ thì có thể viết.

Nhưng invention nhỏ đó chỉ trở thành canon prose khi user Finalize Chapter.

Nó không tự trở thành Architect data.

---

# 17. Review bắt buộc sau mỗi Chapter

Đây là gate cứng.

```text
Writer
→ Review
→ Finalize
→ Reconcile
→ Next Writer
```

Review gồm:

## Manual Review

User đọc và sửa prose.

## AI Review

AI kiểm tra:

- Skeleton adherence.
- Base Idea conflict.
- Premise conflict.
- Character conflict.
- World Rule conflict.
- Timeline continuity.
- Relationship continuity.
- Foreshadow instruction.
- Obvious logic issues.

AI Review chỉ báo vấn đề.

Không phải validator tuyệt đối.

---

# 18. Review Edit

MVP không cần rich-text editor phức tạp.

Có thể:

- sửa cả text area;
- rewrite theo section/skeleton block;
- copy đoạn cần sửa vào box.

Không cần selected-text editor cao cấp.

---

# 19. Finalize Chapter Transaction

Finalize Chapter là một hành động first-class.

Flow:

```text
1. User review xong Draft
2. User bấm Finalize Chapter
3. Draft được ghi thành Final Manuscript candidate
4. AI extract structured state update
5. Validate schema
6. Accept / Auto Accept structured state
7. Update Current Timeline
8. Update Current Relationship nếu có thay đổi
9. Chapter status = final_reconciled
10. Writer chapter tiếp theo được unlock
```

Nếu structured reconciliation lỗi:

```text
chapter không đạt final_reconciled
```

Writer chapter sau vẫn bị khóa.

---

# 20. Chapter Reconciliation Output

Structured output tối thiểu:

```json
{
  "timeline": {
    "chapter": 21,
    "time": "Đêm",
    "location": "Thanh Hà Thành",
    "status": "Nhân vật chính thoát khỏi cuộc truy sát và trú tại tửu lâu."
  },
  "relationship_updates": [
    {
      "a": "char_001",
      "b": "char_002",
      "current": "Bắt đầu tin tưởng lẫn nhau"
    }
  ]
}
```

Không cần trích xuất mọi fact.

Chỉ những state cần cho chapter sau.

---

# 21. Auto Accept trong Finalize

Nếu:

```text
auto_accept_structured = true
```

thì sau khi user đã review prose và bấm Finalize:

```text
AI reconciliation
→ schema valid
→ auto merge timeline/relationship
→ final_reconciled
```

Nếu auto accept tắt:

```text
AI reconciliation
→ user xem JSON
→ Accept/Edit
→ merge
→ final_reconciled
```

Human review prose vẫn bắt buộc trong cả hai trường hợp.

---

# 22. Rolling Plan

Rolling Plan không phải subsystem thông minh riêng.

Nó là action của Short Plan.

Ví dụ:

```text
[Review Plan Against Recent Chapters]
```

Config:

```json
{
  "rolling_plan_every": 3
}
```

Rolling Plan đọc:

```text
Current Arc
Recent Final Chapters / Timeline
Relationship State
Current Short Plan
```

Nó trả:

```json
{
  "status": "ok_or_adjust",
  "deviations": [],
  "short_plan_changes": [],
  "relationship_plan_changes": []
}
```

Apply chỉ sửa **future plan**.

Không sửa:

- Base Idea.
- Premise.
- Final Manuscript.
- Current Timeline quá khứ.

---

# 23. Retcon

Finalized chapter được phép sửa lại.

Nhưng retcon là explicit action.

Flow:

```text
Open Final Chapter
→ Create editable Draft from Final
→ User Edit
→ Review Again
→ Finalize Again
→ Reconcile Again
```

Không tự rewrite chapter sau.

Không auto retcon toàn novel.

Nếu retcon làm timeline/relationship cũ không còn đúng:

```text
mark downstream derived state = stale
```

App có thể cung cấp:

```text
[Analyze Downstream Impact]
```

AI chỉ tạo report.

User quyết định sửa gì.

---

# 24. Stale Rules

`stale` nghĩa là:

> Artifact từng hợp lệ nhưng có upstream dependency đã thay đổi.

Ví dụ:

```text
User revise Premise
→ Long Plan stale
→ Short Plan stale
→ Skeleton future stale
```

Không auto regenerate.

Arbiter chỉ báo:

```text
⚠ Long Plan may be stale
[Review]
```

## 24.1. Architect Append không làm quá khứ stale

Nếu chỉ append một rule:

```text
effective_from_chapter = 100
```

thì chapter 1–99 không stale.

Đây không phải retcon.

---

# 25. Base Idea / Premise Revision

Base Idea và Premise không được AI downstream sửa.

Nhưng user có quyền explicit revise.

Revision:

```text
old accepted
→ snapshot
→ user revision
→ new accepted
→ downstream planning marked stale
```

Final manuscript không bị auto rewrite.

---

# 26. Failure Handling

AI/API failure không được làm hỏng accepted state.

## Structured output sai schema

```text
Do not merge
Keep raw output
Show error
Optional retry format once
```

## Network timeout

```text
Accepted files unchanged
```

## Writer stream bị ngắt

Draft có thể được giữ như partial draft.

Nhưng:

```text
status != review_required
status != final
```

User có thể:

```text
[Continue]
[Regenerate]
[Discard]
```

## Atomic save

Accepted JSON được ghi:

```text
write temp
→ validate
→ replace
```

Không ghi nửa file.

---

# 27. Prompt System

Prompt nằm trong:

```text
docs/
```

Không hardcode prompt lớn trong Python.

Cấu trúc:

```text
docs/
├── genres/
│   ├── xianxia.md
│   ├── wuxia.md
│   ├── fantasy.md
│   ├── romance.md
│   └── custom.md
│
├── co_create.md
│
├── architect/
│   ├── premise.md
│   ├── characters.md
│   ├── world_rules.md
│   └── foreshadow.md
│
├── long_plan.md
├── short_plan.md
├── rolling_plan.md
├── skeleton.md
├── writer.md
├── review.md
└── reconcile.md
```

Genre là prompt khởi đầu.

Không phải config preset.

Architect dùng:

```text
Genre Prompt
+
Base Idea
```

để xây foundation.

---

# 28. Giao diện tổng thể

Layout:

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ Co-create | Architect | Long | Short | Skeleton | Writer | Review       │
├──────────────────┬─────────────────────────────┬─────────────────────────┤
│ PROJECT          │                             │ ARBITER                 │
│                  │                             │                         │
│ Tree / Documents │      CURRENT WORKSPACE      │ Current Chapter         │
│                  │                             │ Status                  │
│                  │                             │ Conflict / Stale        │
│                  │                             │ Next Step               │
├──────────────────┴─────────────────────────────┴─────────────────────────┤
│ API ● Connected | Provider | Model | Project                            │
└──────────────────────────────────────────────────────────────────────────┘
```

Không cần IDE hoàn chỉnh.

---

# 29. Arbiter

Arbiter chỉ đọc status.

Ví dụ:

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

Logic Python đơn giản.

Ví dụ:

```python
def next_action(chapter):
    if not chapter.short_plan_ready:
        return "short_plan"
    if not chapter.skeleton_accepted:
        return "skeleton"
    if not chapter.draft_complete:
        return "writer"
    if not chapter.review_complete:
        return "review"
    if not chapter.reconciled:
        return "reconcile"
    return "next_chapter"
```

Không LLM cần thiết.

---

# 30. Project Structure

```text
projects/
└── my_novel/
    ├── project.json
    │
    ├── idea/
    │   └── base_idea.md
    │
    ├── architect/
    │   ├── premise.json
    │   ├── characters.json
    │   ├── world_rules.json
    │   └── foreshadow.json
    │
    ├── state/
    │   ├── current_timeline.json
    │   └── relationships.json
    │
    ├── plans/
    │   ├── long_plan.json
    │   └── short_plan.json
    │
    ├── chapters/
    │   ├── ch_0001/
    │   │   ├── chapter.json
    │   │   ├── skeleton.json
    │   │   ├── draft.md
    │   │   ├── final.md
    │   │   └── review.json
    │   │
    │   └── ch_0002/
    │       └── ...
    │
    └── history/
```

---

# 31. project.json

Ví dụ:

```json
{
  "schema_version": 2,
  "title": "My Novel",
  "genre_prompt": "xianxia.md",
  "current_chapter": 21,
  "auto_accept_structured": false,
  "allow_relationship_replan": true,
  "rolling_plan_every": 3
}
```

Không nhồi API secret vào project file.

API/model config có thể ở app config riêng.

---

# 32. Co-create

Co-create có hai state:

```text
working
finalized
```

AI chat trả:

```json
{
  "message": "...",
  "idea_state": {
    "genre": "...",
    "tone": "...",
    "protagonist": "...",
    "core_concept": "...",
    "open_questions": []
  }
}
```

Khi user:

```text
[Finalize Idea]
```

app tạo:

```text
idea/base_idea.md
```

Sau đó Architect dùng Base Idea này.

Không cần per-sentence canon.

---

# 33. Architect

Architect UI:

```text
Base Idea
Premise
Characters
World Rules
Foreshadow
```

Các action:

```text
Generate
Regenerate
Edit
Accept
Append
```

Regenerate tạo Draft.

Append dùng cho material phát sinh về sau.

Mọi append phải có:

```text
effective_from_chapter
```

---

# 34. Long Plan

Level:

```text
Volume
→ Arc
```

Long Plan chịu trách nhiệm:

- hướng lớn;
- arc goal;
- core conflict;
- start/end direction;
- Arc-level relationship direction;
- major reveal direction;
- important story thread.

Long Plan không viết chapter prose.

---

# 35. Short Plan

Level:

```text
Arc
→ Chapter
```

Mỗi chapter tối thiểu:

```json
{
  "chapter_id": "ch_0021",
  "summary": "",
  "hook": "",
  "outline": [],
  "characters": [],
  "world_rules": [],
  "threads": [],
  "relationship_changes": [],
  "chapter_goal": "",
  "planned_ending": ""
}
```

Các ID này cũng là deterministic context selector.

---

# 36. Skeleton

Skeleton bắt buộc.

Ví dụ:

```json
{
  "chapter_id": "ch_0021",
  "sections": [
    {
      "index": 1,
      "type": "description",
      "instruction": "Tả Thanh Hà Thành trong đêm.",
      "purpose": "Giữ continuity với chapter trước."
    },
    {
      "index": 2,
      "type": "dialogue",
      "characters": ["char_001", "char_002"],
      "instruction": "Hai người nói chuyện dè chừng.",
      "purpose": "Dịch chuyển quan hệ theo Short Plan."
    },
    {
      "index": 3,
      "type": "foreshadow",
      "instruction": "Thanh kiếm rung nhẹ khi nghe tên gia tộc X. Không giải thích.",
      "purpose": "Cài foreshadow."
    }
  ]
}
```

---

# 37. Writer

Writer UI:

```text
Chapter 21

Skeleton: accepted
Timeline: synced
Relationship: synced

[Generate]

──────────────────

stream prose...

──────────────────

[Save Draft]
[Go to Review]
```

MVP có streaming nếu provider hỗ trợ.

Stop generation không bắt buộc.

---

# 38. Review

Review là bước bắt buộc.

UI:

```text
Final chapter editor

AI Issues:
- Base Idea conflict
- Character conflict
- World Rule conflict
- Skeleton deviation
- Relationship inconsistency
- Timeline inconsistency

[Rewrite Section]
[Edit]
[Finalize Chapter]
```

Không có `Apply All` rewrite toàn chapter trong MVP.

---

# 39. Source Code Structure

```text
novel_ai/
│
├── app.py
├── config.py
│
├── ui/
│   ├── layout.py
│   ├── project_tree.py
│   ├── arbiter.py
│   └── status_bar.py
│
├── pages/
│   ├── co_create.py
│   ├── architect.py
│   ├── long_plan.py
│   ├── short_plan.py
│   ├── skeleton.py
│   ├── writer.py
│   └── review.py
│
├── core/
│   ├── project.py
│   ├── storage.py
│   ├── prompts.py
│   ├── context.py
│   ├── validation.py
│   └── llm.py
│
├── services/
│   ├── architect.py
│   ├── long_planner.py
│   ├── short_planner.py
│   ├── skeleton.py
│   ├── writer.py
│   ├── reviewer.py
│   └── reconcile.py
│
├── docs/
└── projects/
```

Không thêm layer khác nếu chưa cần.

---

# 40. LLM Adapter

Interface đơn giản:

```python
class LLMClient:
    def generate(self, messages, schema=None):
        ...

    def stream(self, messages):
        ...
```

MVP chỉ cần một provider implementation tốt.

Ví dụ:

```text
OpenAICompatibleClient
```

Local server nào expose OpenAI-compatible API cũng dùng được.

Adapter khác để sau.

---

# 41. MVP Boundary

## Build ngay

- Project create/load/save.
- Co-create.
- Genre prompt.
- Architect.
- Long Plan.
- Short Plan.
- Mandatory Skeleton.
- Writer.
- Streaming nếu dễ.
- Manual Review.
- AI Review.
- Mandatory per-chapter review gate.
- Current Timeline.
- Relationship State.
- Reconciliation.
- Draft/Accept/Final lifecycle.
- Auto Accept structured.
- Deterministic context.
- Regenerate candidate.
- Stale flags.
- Snapshot before overwrite.
- Atomic JSON save.
- Arbiter rule-based.
- Prompt files trong `docs/`.

## Không build trong MVP

- Agent.
- Multi-agent.
- RAG.
- Embedding.
- Vector DB.
- Graph DB.
- SQLite nếu chưa cần.
- Automatic global continuity checker.
- Automatic retcon propagation.
- Automatic manuscript rewrite.
- Rich text editor.
- Selected-text magic rewrite.
- Chapter reorder.
- Character merge.
- Complex dependency graph.
- Background worker.
- Multi-user.
- Authentication.
- Cloud sync.
- Fancy analytics.

---

# 42. Invariants

Các luật này không được phá.

1. AI generation không tự trở thành canon.
2. Structured AI output chỉ merge sau schema validation.
3. Auto Accept không bypass Chapter Review.
4. User phải review chapter N trước khi Writer được viết chapter N+1.
5. Base Idea mạnh hơn mọi downstream artifact.
6. Premise mạnh hơn plan.
7. Long Plan mạnh hơn Short Plan.
8. Short Plan mạnh hơn Skeleton.
9. Skeleton mạnh hơn Writer.
10. Downstream không được sửa upstream.
11. Writer không plan.
12. Writer không sở hữu Foreshadow truth.
13. Skeleton bắt buộc.
14. Planner có thể biết secret; Writer chỉ biết surface instruction.
15. Plan không chứng minh sự kiện đã xảy ra.
16. Final Manuscript mới là canon prose.
17. Current Timeline phản ánh continuity thực tế gần nhất.
18. Current Relationship phản ánh quan hệ thực tế gần nhất.
19. Long/Short Plan chỉ quản lý hướng relationship tương lai.
20. Finalize Chapter phải reconcile state.
21. Chapter sau chỉ unlock khi chapter trước `final_reconciled`.
22. Regenerate tạo Draft Candidate.
23. Accepted revision không bị silent overwrite.
24. Architect append phải có `effective_from_chapter`.
25. Append tương lai không retroactively xuất hiện trong quá khứ.
26. Upstream user revision không tự rewrite downstream.
27. Retcon không tự rewrite future chapter.
28. AI failure không làm thay đổi accepted state.
29. File exists không đồng nghĩa complete.
30. Không subsystem nào được tự biến thành agent.
31. Không thêm công nghệ nếu rule + JSON giải quyết được.
32. Human là authority cuối cùng.

---

# 43. Core Loop v0.2

```text
CO-CREATE
    ↓
BASE IDEA
    ↓
ARCHITECT
    ↓
LONG PLAN
    ↓
SHORT PLAN
    ↓
SKELETON
    ↓
WRITER DRAFT
    ↓
HUMAN REVIEW
    ↓
FINALIZE CHAPTER
    ↓
RECONCILE
    ├── Current Timeline
    └── Relationship State
    ↓
NEXT CHAPTER
```

Rolling Plan đứng cạnh Short Plan.

Arbiter đứng cạnh workflow.

Không ai tự chạy workflow thay user.

---

# 44. Tóm tắt triết lý kỹ thuật

```text
Files store state.
Schemas keep AI output predictable.
Humans approve prose.
Plans control intent.
Skeleton controls execution.
Writer only writes.
Review protects upstream intent.
Timeline protects temporal continuity.
Relationship State protects relationship continuity.
Arbiter only guides.
```

Không cần hệ thống thông minh hơn.

Cần hệ thống **rõ hơn**.

---

# 45. Những quyết định có thể để sau

Các điểm sau không cần chốt trước khi code MVP:

- chính xác ID format dùng số hay UUID;
- giữ bao nhiêu bản snapshot;
- chapter reorder;
- character merge;
- export EPUB/DOCX;
- rich text editor;
- advanced prompt management UI;
- nhiều provider native;
- global timeline visualization;
- automatic impact analysis;
- semantic retrieval;
- RAG;
- database.

Có thể bắt đầu implementation khi các rule trong spec này được coi là baseline.

---

# 46. Hai assumption đã được khóa trong v0.2

Để tránh coding phải tự đoán, v0.2 khóa thêm hai interpretation sau:

### A. Auto Accept

Auto Accept chỉ tự động merge **structured output sau schema validation**.

Nó không được tự Finalize prose.

### B. User sửa upstream

AI downstream không bao giờ được sửa upstream.

User vẫn được phép chủ động revise Base Idea/Premise bằng explicit action.

Khi đó downstream planning có thể `stale`, nhưng Final Manuscript không bị tự rewrite.

---

# 47. Kết luận

Novel AI v0.2 vẫn là một app rất đơn giản.

Nó không cần AI Agent để quản lý long-form fiction.

Nó chỉ cần:

```text
Prompt
+
Structured Files
+
Explicit Hierarchy
+
Mandatory Skeleton
+
Human Review
+
Current Timeline
+
Relationship State
+
Simple Reconciliation
```

Phần khó nhất không được giải bằng công nghệ phức tạp.

Nó được giải bằng luật rõ ràng.

> **Simple first.**

> **Human-in-the-loop.**

> **Base Idea wins.**

> **Writer only writes.**

> **No agents.**

> **Potato, but effective.**
