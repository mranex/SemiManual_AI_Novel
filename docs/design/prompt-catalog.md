# Prompt catalog v1 — Bộ prompt MVP

Phiên bản nội dung: foundation refresh 2026-09-19; planning refresh theo yêu cầu tác giả 2026-09-20. Giữ prompt ID v1 và payload T02/T05. Phụ thuộc: `schemas.md`, `context.md`, `storage.md`, `workflow.md`.

Catalog này đăng ký toàn bộ prompt MVP; manifest máy đọc nằm tại `docs/prompts/v1/manifest.json` (T06). Prompt cũ trong `docs/prompts/` chỉ là reference. T10 sẽ triển khai loader theo manifest explicit, không fallback sang prompt cũ.

## 1. Luật chung cho prompt v1

- Prompt nhận input do backend đóng gói, không tự tìm file và không tự chạy bước tiếp theo.
- Prompt chỉ trả payload của action hiện tại. Backend sở hữu envelope, lifecycle metadata, revision, dependency pins, path, raw output ref và stable ID cuối cùng.
- AI output mặc định là draft/candidate. Prompt không được tự nói output đã trở thành canon.
- Base Idea cao hơn Premise/Architect; Architect cao hơn mọi plan/skeleton/prose.
- Genre prompt là guidance, không phải preset ép motif.
- Structured output phải là JSON hợp lệ. Writer trả Markdown draft; Base Idea Markdown do action finalize riêng xử lý. Rewrite có prose trong field replacement_markdown của JSON, không được auto-apply như accepted prose.
- Entry-level `status` trong schema Character/Foreshadow là field payload theo `schemas.md`; nó không thay thế artifact lifecycle do backend quản lý.

T06 bổ sung ngày 2026-09-20: manifest explicit cho 14 prompt, 6 genre và 1 style đã rà soát. Chi tiết tích hợp mới ở mục 8; mô tả lịch sử T04/T05 không thay thế registry hiện tại.

## 2. Prompt registry

| Prompt ID | Path | Output kind | Schema ref | Template variables chính |
|---|---|---|---|---|
| `co_create.v1` | `docs/prompts/v1/co_create.md` | JSON `message + idea_state` | `IdeaState` | `language`, `genre_prompt`, `conversation_summary`, `current_idea_state`, `user_message` |
| `architect.premise.v1` | `docs/prompts/v1/architect/premise.md` | JSON `PremisePayload` | `Premise payload` | `action`, `language`, `genre_prompt`, `base_idea_markdown`, `user_instruction`, `previous_premise` |
| `architect.characters.v1` | `docs/prompts/v1/architect/characters.md` | JSON `CharactersPayload` | `Characters payload` | `action`, `language`, `base_idea_markdown`, `premise`, `genre_prompt`, `existing_characters`, `assigned_character_ids`, `effective_from_chapter`, `user_instruction` |
| `architect.world_rules.v1` | `docs/prompts/v1/architect/world_rules.md` | JSON `WorldRulesPayload` | `World Rules payload` | `action`, `language`, `base_idea_markdown`, `premise`, `genre_prompt`, `existing_world_rules`, `assigned_world_rule_ids`, `effective_from_chapter`, `user_instruction` |
| `architect.foreshadow.v1` | `docs/prompts/v1/architect/foreshadow.md` | JSON `ForeshadowPayload` | `Foreshadow payload` | `action`, `language`, `base_idea_markdown`, `premise`, `characters`, `world_rules`, `genre_prompt`, `existing_foreshadows`, `assigned_foreshadow_ids`, `effective_from_chapter`, `user_instruction` |

| `long_plan.v1` | `docs/prompts/v1/long_plan.md` | JSON | `Long Plan payload` | `action`, `language`, `genre_prompt`, `base_idea_markdown`, `premise`, `characters`, `world_rules`, `foreshadows`, `relationships_as_of`, `planning_scope`, `assigned_volume_ids`, `assigned_arc_ids`, `previous_long_plan`, `user_instruction` |
| `short_plan.v1` | `docs/prompts/v1/short_plan.md` | JSON | `Short Plan payload` | `action`, `language`, `base_idea_markdown`, `premise`, `current_arc`, `characters`, `world_rules`, `foreshadows`, `timeline_as_of`, `relationships_as_of`, `recent_finalized_summaries`, `assigned_chapters`, `chapter_constraints`, `context_basis`, `previous_short_plan`, `user_instruction` |
| `rolling_plan.v1` | `docs/prompts/v1/rolling_plan.md` | JSON | `RollingPatchPayload` | `current_arc`, `current_short_plan`, `timeline_as_of`, `relationships_as_of`, `recent_finalized_summaries`, `reviewed_chapter_range`, `latest_consistent_chapter`, `eligible_chapters`, `allow_relationship_replan`, `user_instruction` |
| `skeleton.v1` | `docs/prompts/v1/skeleton.md` | JSON | `SkeletonPayload` | `action`, `base_idea_constraints`, `premise_constraints`, `current_arc`, `chapter_plan`, `timeline_as_of`, `relationships_as_of`, `previous_final_summary`, `characters`, `world_rules`, `foreshadows`, `assigned_section_ids`, `context_basis`, `previous_skeleton`, `user_instruction` |

| `writer.v1` | `docs/prompts/v1/writer.md` | Markdown | `MarkdownProse` | `chapter_id`, `chapter_number`, `title`, `base_idea_constraints`, `premise_constraints`, `skeleton`, `characters`, `world_rules`, `timeline_as_of`, `relationships_as_of`, `previous_final_summary`, `style`, `user_instruction` |
| `review.v1` | `docs/prompts/v1/review.md` | JSON | `ReviewReportPayload` | `chapter_id`, `prose_revision`, `prose_markdown`, `constraint_sources`, `skeleton`, `characters`, `world_rules`, `timeline_as_of`, `relationships_as_of`, `previous_final_summary`, `style`, `review_focus` |
| `rewrite_section.v1` | `docs/prompts/v1/rewrite_section.md` | JSON | `RewriteSectionPayload` | `request`, `target_markdown`, `surrounding_before`, `surrounding_after`, `skeleton`, `base_idea_constraints`, `premise_constraints`, `characters`, `world_rules`, `timeline_as_of`, `relationships_as_of`, `style` |
| `reconcile.v1` | `docs/prompts/v1/reconcile.md` | JSON | `ReconciliationPayload` | `chapter_id`, `chapter_number`, `source_final_candidate`, `final_candidate_markdown`, `timeline_as_of`, `relationships_as_of`, `known_characters` |
| `retcon_impact.v1` | `docs/prompts/v1/retcon_impact.md` | JSON | `ImpactReportPayload` | `source_change`, `before_content`, `after_content`, `downstream_items`, `state_before_change`, `analysis_scope` |

## 3. Genre registry

| Genre ID | Path | Dùng cho |
|---|---|---|
| `xianxia` | `docs/genres/xianxia.md` | Tu tiên/xianxia, đạo tâm, cảnh giới, nhân quả, tài nguyên tu luyện |
| `wuxia` | `docs/genres/wuxia.md` | Võ hiệp/giang hồ, ân oán, đạo nghĩa, môn phái, thanh danh |
| `fantasy` | `docs/genres/fantasy.md` | Kỳ huyễn/fantasy, ma pháp, văn hóa, thế lực, hành trình |
| `romance` | `docs/genres/romance.md` | Lãng mạn, phát triển quan hệ, xung đột cảm xúc |
| `suspense` | `docs/genres/suspense.md` | Trinh thám/ly kỳ, mystery, clue, red herring, nguy cơ |
| `custom` | `docs/genres/custom.md` | Chưa rõ genre hoặc pha trộn thể loại |

Genre prompt chỉ được đưa vào context foundation/planning khi project chọn `genre_prompt_id`. Writer không nhận genre prompt trực tiếp nếu style prompt riêng đã đủ.

## 4. Prompt details

### 4.1. `co_create.v1`

Mục đích: đối thoại ngắn và tích lũy brief đầy đủ trong `message`, đồng thời trả toàn bộ `idea_state` mỗi lượt. Chỉ ý tác giả chọn đi vào các field mô tả; output chưa phải Base Idea accepted.

Ví dụ request (backend cấp input đã kiểm tra):

```json
{
  "language": "vi",
  "genre_prompt": "Genre Prompt: Suspense...",
  "conversation_summary": "",
  "current_idea_state": {
    "genre": "",
    "tone": "",
    "protagonist": "",
    "core_concept": "",
    "setting": "",
    "conflict": "",
    "constraints": [],
    "open_questions": []
  },
  "user_message": "Tôi muốn một nữ pháp y điều tra án mạng trong thành phố nổi."
}
```

Output ví dụ:

```json
{
  "message": "Bối cảnh này có thể tạo trở ngại điều tra rất riêng. Bạn muốn ưu tiên suy luận từ dấu vết vật lý hay áp lực từ quyền tiếp cận các tầng?\n\n## Ý tưởng hiện tại\n- Một nữ pháp y điều tra án mạng trong thành phố nổi.\n\n## Nhân vật và xung đột\n- Nhân vật trung tâm là nữ pháp y; cô cần làm rõ vụ án.\n\n## Bối cảnh và sắc thái\n- Thành phố nổi; cơ chế vận hành và tone chưa chọn.\n\n## Ràng buộc đã chốt\n- Chưa có yêu cầu bổ sung.\n\n## Điều còn mở\n- Đề xuất chưa chọn: dấu vết bị luồng gió làm sai lệch, hoặc việc tiếp cận các tầng bị hạn chế.\n\n## Bạn có thể nói tiếp\n- Tôi muốn ưu tiên suy luận vật lý.\n- Tôi muốn tập trung vào áp lực xã hội.",
  "idea_state": {
    "genre": "suspense",
    "tone": "",
    "protagonist": "Một nữ pháp y.",
    "core_concept": "Một nữ pháp y điều tra án mạng trong thành phố nổi.",
    "setting": "Thành phố nổi.",
    "conflict": "Điều tra để làm rõ vụ án.",
    "constraints": [],
    "open_questions": [
      "Ưu tiên suy luận từ dấu vết vật lý hay áp lực từ quyền tiếp cận các tầng?",
      "Cơ chế thành phố nổi và sắc thái truyện là gì?"
    ]
  }
}
```

Missing/conflict policy: thiếu genre thì dùng `custom` hoặc nhãn gần nhất; thiếu core concept thì hỏi thêm; conflict với genre thì giữ user constraint.

### 4.2. `architect.premise.v1`

Mục đích: tạo Premise payload từ accepted Base Idea và genre guidance.

Ví dụ request (backend cấp input đã kiểm tra):

```json
{
  "action": "generate",
  "language": "vi",
  "genre_prompt": "Genre Prompt: Suspense...",
  "base_idea_markdown": "# Thành phố nổi\nMột nữ pháp y điều tra án mạng trong thành phố nổi. Truyện tập trung vào suy luận vật lý và cái giá của an toàn tập thể. Nữ chính không tra tấn nghi phạm. Manh mối phải công bằng khi đọc lại; không giải án bằng bằng chứng đột ngột ở cuối. Không có twist toàn bộ chỉ là giấc mơ.",
  "user_instruction": "Giữ tone u tối nhưng không kinh dị máu me.",
  "previous_premise": null
}
```

Output ví dụ:

```json
{
  "title": "Tầng Rơi Không Gió",
  "logline": "Một nữ pháp y điều tra các thi thể rơi từ những tầng không có điểm rơi trong thành phố nổi. Càng lần theo dấu vết, cô càng phát hiện luật vận hành của thành phố đang che giấu một tội ác được hợp pháp hóa.",
  "dramatic_question": "Cô có thể phơi bày sự thật mà không phá hủy nơi trú ẩn duy nhất của hàng triệu người không?",
  "themes": [
    "sự thật và trật tự",
    "cái giá của an toàn tập thể"
  ],
  "tone_contract": [
    "u tối vừa phải, tập trung suy luận và áp lực đạo đức",
    "không sa vào gore",
    "manh mối phải công bằng khi đọc lại"
  ],
  "hard_constraints": [
    "Nhân vật chính không tra tấn nghi phạm để lấy lời khai.",
    "Lời giải vụ án không dựa vào bằng chứng xuất hiện đột ngột ở cuối."
  ],
  "non_goals": [
    "Không biến truyện thành kinh dị sinh tồn thuần túy.",
    "Không dùng twist phủ nhận toàn bộ vụ án là giấc mơ."
  ]
}
```

Missing/conflict policy: thiếu title thì đề xuất tên làm việc; thiếu dramatic question thì suy từ conflict; Base Idea thắng mọi user instruction downstream.

### 4.3. `architect.characters.v1`

Mục đích: tạo hoặc append Characters payload. Backend cấp `assigned_character_ids`.

Ví dụ request (backend cấp input đã kiểm tra):

```json
{
  "action": "generate",
  "language": "vi",
  "base_idea_markdown": "# Thành phố nổi\nMột nữ pháp y điều tra án mạng trong thành phố nổi. Truyện tập trung vào suy luận vật lý và cái giá của an toàn tập thể. Nữ chính không tra tấn nghi phạm. Manh mối phải công bằng khi đọc lại; không giải án bằng bằng chứng đột ngột ở cuối. Không có twist toàn bộ chỉ là giấc mơ.",
  "premise": {
    "title": "Tầng Rơi Không Gió",
    "logline": "Một nữ pháp y điều tra các thi thể rơi từ những tầng không có điểm rơi trong thành phố nổi. Càng lần theo dấu vết, cô càng phát hiện luật vận hành của thành phố đang che giấu một tội ác được hợp pháp hóa.",
    "dramatic_question": "Cô có thể phơi bày sự thật mà không phá hủy nơi trú ẩn duy nhất của hàng triệu người không?",
    "themes": [
      "sự thật và trật tự",
      "cái giá của an toàn tập thể"
    ],
    "tone_contract": [
      "u tối vừa phải, tập trung suy luận và áp lực đạo đức",
      "không sa vào gore",
      "manh mối phải công bằng khi đọc lại"
    ],
    "hard_constraints": [
      "Nhân vật chính không tra tấn nghi phạm để lấy lời khai.",
      "Lời giải vụ án không dựa vào bằng chứng xuất hiện đột ngột ở cuối."
    ],
    "non_goals": [
      "Không biến truyện thành kinh dị sinh tồn thuần túy.",
      "Không dùng twist phủ nhận toàn bộ vụ án là giấc mơ."
    ]
  },
  "genre_prompt": "Genre Prompt: Suspense...",
  "existing_characters": [],
  "assigned_character_ids": [
    "char_0001",
    "char_0002"
  ],
  "effective_from_chapter": 1,
  "user_instruction": "Tạo nữ chính và một đồng minh trong đội bảo trì khí cầu. Tôi muốn nữ chính có một phần ký ức vụ rơi tầng cũ bị chỉnh sửa; đây là bí mật chưa lộ, không ghi vào profile Writer."
}
```

Output ví dụ:

```json
{
  "characters": [
    {
      "character_id": "char_0001",
      "display_name": "Lâm An",
      "aliases": [
        "Bác sĩ Lâm"
      ],
      "role": "protagonist",
      "tier": "core",
      "effective_from_chapter": 1,
      "status": "accepted",
      "public_profile": {
        "description": "Nữ pháp y tỉ mỉ, nổi tiếng vì tái dựng hiện trường từ dấu vết rất nhỏ.",
        "traits": [
          "kiên nhẫn",
          "khó tin người",
          "tôn trọng chứng cứ"
        ],
        "voice": "ngắn, chính xác, ít dùng cảm thán",
        "known_history": "Từng làm ở tầng đáy thành phố trước khi chuyển lên cơ quan pháp y trung tâm."
      },
      "writer_profile": {
        "dialogue_style": "đặt câu hỏi trực diện, hiếm khi giải thích cảm xúc của mình",
        "behavior_notes": [
          "quan sát tay và giày của người đối diện",
          "ghi chú bằng ký hiệu riêng"
        ],
        "do_not_write": [
          "không hành động bốc đồng nếu chưa có áp lực đủ lớn"
        ]
      },
      "author_only": {
        "secret": "Ký ức về một vụ rơi tầng cũ của cô đã bị chỉnh sửa."
      },
      "future_direction": {
        "arc_hint": "Từ người chỉ tin chứng cứ vật lý sang người dám tin lời khai đau đớn của nạn nhân sống sót."
      }
    },
    {
      "character_id": "char_0002",
      "display_name": "Tạ Minh",
      "aliases": [],
      "role": "ally",
      "tier": "major",
      "effective_from_chapter": 1,
      "status": "accepted",
      "public_profile": {
        "description": "Kỹ sư bảo trì khí cầu hiểu rõ các đường gió cấm và thói quen vận hành tầng nổi.",
        "traits": [
          "thực tế",
          "nói nhiều khi căng thẳng",
          "trung thành với đội của mình"
        ],
        "voice": "bình dân, hay ví von bằng máy móc",
        "known_history": "Lớn lên ở khu bảo trì, quen với tầng thấp và các đường đi không chính thức."
      },
      "writer_profile": {
        "dialogue_style": "nói vòng qua chuyện kỹ thuật rồi mới vào ý chính",
        "behavior_notes": [
          "kiểm tra móc dây an toàn theo thói quen"
        ],
        "do_not_write": [
          "không biến anh thành comic relief đơn thuần"
        ]
      },
      "author_only": {},
      "future_direction": {
        "relationship_with_protagonist": "Từ người cung cấp thông tin sang đồng minh dám phản lại hệ thống bảo trì."
      }
    }
  ]
}
```

Missing/conflict policy: không tự tạo ID tạm; bỏ entry không có ID cấp sẵn; backend phải phân biệt output thiếu với no-op. Secret vào `author_only`; append chỉ trả entry mới.

### 4.4. `architect.world_rules.v1`

Mục đích: tạo hoặc append World Rules payload. Rule phải có hiệu lực chương rõ ràng.

Ví dụ request (backend cấp input đã kiểm tra):

```json
{
  "action": "generate",
  "language": "vi",
  "base_idea_markdown": "# Thành phố nổi\nMột nữ pháp y điều tra án mạng trong thành phố nổi. Truyện tập trung vào suy luận vật lý và cái giá của an toàn tập thể. Nữ chính không tra tấn nghi phạm. Manh mối phải công bằng khi đọc lại; không giải án bằng bằng chứng đột ngột ở cuối. Không có twist toàn bộ chỉ là giấc mơ.",
  "premise": {
    "title": "Tầng Rơi Không Gió",
    "logline": "Một nữ pháp y điều tra các thi thể rơi từ những tầng không có điểm rơi trong thành phố nổi. Càng lần theo dấu vết, cô càng phát hiện luật vận hành của thành phố đang che giấu một tội ác được hợp pháp hóa.",
    "dramatic_question": "Cô có thể phơi bày sự thật mà không phá hủy nơi trú ẩn duy nhất của hàng triệu người không?",
    "themes": [
      "sự thật và trật tự",
      "cái giá của an toàn tập thể"
    ],
    "tone_contract": [
      "u tối vừa phải, tập trung suy luận và áp lực đạo đức",
      "không sa vào gore",
      "manh mối phải công bằng khi đọc lại"
    ],
    "hard_constraints": [
      "Nhân vật chính không tra tấn nghi phạm để lấy lời khai.",
      "Lời giải vụ án không dựa vào bằng chứng xuất hiện đột ngột ở cuối."
    ],
    "non_goals": [
      "Không biến truyện thành kinh dị sinh tồn thuần túy.",
      "Không dùng twist phủ nhận toàn bộ vụ án là giấc mơ."
    ]
  },
  "genre_prompt": "Genre Prompt: Suspense...",
  "existing_world_rules": [],
  "assigned_world_rule_ids": [
    "rule_0001"
  ],
  "effective_from_chapter": 1,
  "user_instruction": "Chốt luật vật lý của thành phố nổi."
}
```

Output ví dụ:

```json
{
  "world_rules": [
    {
      "world_rule_id": "rule_0001",
      "category": "city_physics",
      "summary": "Cầu và thang giữa các tầng tự khóa khi chênh lệch độ cao vượt ngưỡng an toàn.",
      "content": "Mỗi tầng nổi được giữ độ cao bằng lõi khí. Cầu treo và thang khí nối các tầng sẽ tự ngắt khi chênh lệch độ cao vượt ngưỡng an toàn, để tránh kéo hỏng kết cấu. Người ở mỗi tầng có thể thấy tín hiệu khóa tại đầu cầu.",
      "boundary": "Ngắt cầu chỉ chặn đường đi thông thường; không tạo lá chắn và không ngăn vật thể rơi qua khoảng trống. Muốn di chuyển lúc cầu khóa cần phương tiện khác đã được thiết lập.",
      "effective_from_chapter": 1,
      "visibility": "writer_safe",
      "writer_projection": "Lõi khí giữ các tầng nổi ở độ cao riêng. Khi chênh lệch vượt ngưỡng an toàn, cầu và thang nối tầng tự khóa, có tín hiệu tại đầu cầu. Khóa không ngăn vật thể rơi qua khoảng trống; không tự thêm phương tiện vượt khóa.",
      "author_only": {}
    }
  ]
}
```

Missing/conflict policy: không tự tạo ID tạm; backend chặn input thiếu hiệu lực; rule chỉ là màu nền thì không tách riêng.

### 4.5. `architect.foreshadow.v1`

Mục đích: tạo hoặc append Foreshadow payload, tách full truth khỏi surface instruction.

Ví dụ request (backend cấp input đã kiểm tra):

```json
{
  "action": "generate",
  "language": "vi",
  "base_idea_markdown": "# Thành phố nổi\nMột nữ pháp y điều tra án mạng trong thành phố nổi. Truyện tập trung vào suy luận vật lý và cái giá của an toàn tập thể. Nữ chính không tra tấn nghi phạm. Manh mối phải công bằng khi đọc lại; không giải án bằng bằng chứng đột ngột ở cuối. Không có twist toàn bộ chỉ là giấc mơ.",
  "premise": {
    "title": "Tầng Rơi Không Gió",
    "logline": "Một nữ pháp y điều tra các thi thể rơi từ những tầng không có điểm rơi trong thành phố nổi. Càng lần theo dấu vết, cô càng phát hiện luật vận hành của thành phố đang che giấu một tội ác được hợp pháp hóa.",
    "dramatic_question": "Cô có thể phơi bày sự thật mà không phá hủy nơi trú ẩn duy nhất của hàng triệu người không?",
    "themes": [
      "sự thật và trật tự",
      "cái giá của an toàn tập thể"
    ],
    "tone_contract": [
      "u tối vừa phải, tập trung suy luận và áp lực đạo đức",
      "không sa vào gore",
      "manh mối phải công bằng khi đọc lại"
    ],
    "hard_constraints": [
      "Nhân vật chính không tra tấn nghi phạm để lấy lời khai.",
      "Lời giải vụ án không dựa vào bằng chứng xuất hiện đột ngột ở cuối."
    ],
    "non_goals": [
      "Không biến truyện thành kinh dị sinh tồn thuần túy.",
      "Không dùng twist phủ nhận toàn bộ vụ án là giấc mơ."
    ]
  },
  "characters": [
    {
      "character_id": "char_0001",
      "display_name": "Lâm An",
      "aliases": [
        "Bác sĩ Lâm"
      ],
      "role": "protagonist",
      "tier": "core",
      "effective_from_chapter": 1,
      "status": "accepted",
      "public_profile": {
        "description": "Nữ pháp y tỉ mỉ, nổi tiếng vì tái dựng hiện trường từ dấu vết rất nhỏ.",
        "traits": [
          "kiên nhẫn",
          "khó tin người",
          "tôn trọng chứng cứ"
        ],
        "voice": "ngắn, chính xác, ít dùng cảm thán",
        "known_history": "Từng làm ở tầng đáy thành phố trước khi chuyển lên cơ quan pháp y trung tâm."
      },
      "writer_profile": {
        "dialogue_style": "đặt câu hỏi trực diện, hiếm khi giải thích cảm xúc của mình",
        "behavior_notes": [
          "quan sát tay và giày của người đối diện",
          "ghi chú bằng ký hiệu riêng"
        ],
        "do_not_write": [
          "không hành động bốc đồng nếu chưa có áp lực đủ lớn"
        ]
      },
      "author_only": {
        "secret": "Ký ức về một vụ rơi tầng cũ của cô đã bị chỉnh sửa."
      },
      "future_direction": {
        "arc_hint": "Từ người chỉ tin chứng cứ vật lý sang người dám tin lời khai đau đớn của nạn nhân sống sót."
      }
    },
    {
      "character_id": "char_0002",
      "display_name": "Tạ Minh",
      "aliases": [],
      "role": "ally",
      "tier": "major",
      "effective_from_chapter": 1,
      "status": "accepted",
      "public_profile": {
        "description": "Kỹ sư bảo trì khí cầu hiểu rõ các đường gió cấm và thói quen vận hành tầng nổi.",
        "traits": [
          "thực tế",
          "nói nhiều khi căng thẳng",
          "trung thành với đội của mình"
        ],
        "voice": "bình dân, hay ví von bằng máy móc",
        "known_history": "Lớn lên ở khu bảo trì, quen với tầng thấp và các đường đi không chính thức."
      },
      "writer_profile": {
        "dialogue_style": "nói vòng qua chuyện kỹ thuật rồi mới vào ý chính",
        "behavior_notes": [
          "kiểm tra móc dây an toàn theo thói quen"
        ],
        "do_not_write": [
          "không biến anh thành comic relief đơn thuần"
        ]
      },
      "author_only": {},
      "future_direction": {
        "relationship_with_protagonist": "Từ người cung cấp thông tin sang đồng minh dám phản lại hệ thống bảo trì."
      }
    }
  ],
  "world_rules": [
    {
      "world_rule_id": "rule_0001",
      "category": "city_physics",
      "summary": "Cầu và thang giữa các tầng tự khóa khi chênh lệch độ cao vượt ngưỡng an toàn.",
      "content": "Mỗi tầng nổi được giữ độ cao bằng lõi khí. Cầu treo và thang khí nối các tầng sẽ tự ngắt khi chênh lệch độ cao vượt ngưỡng an toàn, để tránh kéo hỏng kết cấu. Người ở mỗi tầng có thể thấy tín hiệu khóa tại đầu cầu.",
      "boundary": "Ngắt cầu chỉ chặn đường đi thông thường; không tạo lá chắn và không ngăn vật thể rơi qua khoảng trống. Muốn di chuyển lúc cầu khóa cần phương tiện khác đã được thiết lập.",
      "effective_from_chapter": 1,
      "visibility": "writer_safe",
      "writer_projection": "Lõi khí giữ các tầng nổi ở độ cao riêng. Khi chênh lệch vượt ngưỡng an toàn, cầu và thang nối tầng tự khóa, có tín hiệu tại đầu cầu. Khóa không ngăn vật thể rơi qua khoảng trống; không tự thêm phương tiện vượt khóa.",
      "author_only": {}
    }
  ],
  "genre_prompt": "Genre Prompt: Suspense...",
  "existing_foreshadows": [],
  "assigned_foreshadow_ids": [
    "fs_0001"
  ],
  "effective_from_chapter": 1,
  "user_instruction": "Gieo một manh mối liên quan ký ức nữ chính."
}
```

Output ví dụ:

```json
{
  "foreshadows": [
    {
      "foreshadow_id": "fs_0001",
      "label": "Ký hiệu trong sổ pháp y cũ",
      "truth_author_only": {
        "truth": "Lâm An từng ghi ký hiệu này sau vụ rơi tầng cũ, thuộc phần ký ức đã bị chỉnh sửa.",
        "surface_candidate": "Một ký hiệu trong sổ pháp y cũ khiến Lâm An dừng tay; cô chưa gọi tên được cảm giác quen thuộc.",
        "payoff_intent": "Khi truyện cho phép đối chiếu sổ cũ với quá khứ, chi tiết này góp phần cho thấy cô từng tiếp xúc vụ việc; riêng cảm giác quen không đủ làm bằng chứng."
      },
      "planned_planting": [],
      "planned_payoff": null,
      "effective_from_chapter": 1,
      "writer_visibility": "skeleton_only",
      "status": "active"
    }
  ]
}
```

Missing/conflict policy: chưa có planning IDs nên `planned_planting: []`, `planned_payoff: null`; ý định gieo/trả nằm trong object `truth_author_only`. Surface không reveal truth; yêu cầu trái hard constraint thì không tạo entry đó.

## 5. Kiểm tra thủ công cho T04

Checklist khi rà prompt/catalog:

1. Output field khớp `schemas.md` cho Co-create, Premise, Characters, World Rules và Foreshadow.
2. Không prompt nào yêu cầu model tự đọc file, tự lưu state, tự accept artifact, tự gọi bước tiếp theo hoặc tự sửa upstream.
3. Prompt Architect không trả envelope metadata: `schema_version`, `artifact_id`, `artifact_type`, `accepted_revision`, `candidate_revision`, `dependency_pins`, `history_refs`, file path.
4. Character/World/Foreshadow tách rõ writer-safe projection khỏi `author_only`, `truth_author_only` và future direction.
5. Append luôn có `effective_from_chapter`; entry tương lai không được mô tả như đã xuất hiện ở chương trước.
6. Genre prompt nào cũng ghi rõ Base Idea/user constraint thắng genre guidance.


## 6. Quy ước tích hợp bản nâng cấp

### 6.1. Đóng gói và schema

Các tên trong cột “Template variables” là field của input JSON, không phải marker thay chuỗi trong file Markdown. T10 đóng gói prompt chính + nội dung genre đã chọn + input được serialize bằng JSON; không tự nội suy dấu ngoặc của các ví dụ. Không nạp toàn bộ catalog, ví dụ hoặc reference vào mỗi request. Việc có đủ field input không thay thế kiểm tra accepted/freshness ở service.

Giữ nguyên prompt ID v1 và schema payload T02; đây là thay đổi chất lượng nội dung, chưa có runtime cần migration. Prompt output blocks mô tả shape/type; ví dụ request/response cụ thể ở mục 4 và [ca nhiều lượt](examples/foundation_prompt_cases.json) phục vụ rà soát offline. Không sao chép tên/chi tiết ví dụ vào truyện khác.

### 6.2. Co-create theo mẫu đối thoại tác giả cung cấp

Chắt lọc từ mẫu tham khảo: phản hồi ngắn trước, hỏi 1–2 câu, brief Markdown được tích lũy và xuất đầy đủ mỗi lượt, gợi ý 1–3 câu ở ngôi người dùng. Brief và các gợi ý nằm trong chuỗi `message`; `idea_state` là working state đầy đủ. Chỉ dẫn JSON yêu cầu escape xuống dòng trong chuỗi, không xuất XML hoặc Markdown ngoài object.

Không thêm `ready`, `suggestions`, `draft` vào schema. Khi đủ thông tin, message gợi ý xem brief và dùng Finalize Idea; không tuyên bố hoàn tất action, không nhắc Ctrl+S. Bản brief Markdown có thể hiển thị từ message; việc lưu Base Idea vẫn theo action hiện hữu. Đây là cách áp dụng mẫu trong contract T02, chưa triển khai giao diện/nút gợi ý riêng.

Mẫu stage co-create là tham khảo cho T05: hướng tương lai phải tôn trọng actual state đã xảy ra. T04 không đăng ký action stage-co-create, không đưa actual state vào initial Co-create và không tự tạo future Short Plan. Nếu sau này cần UI/field ready/suggestions riêng, phải cập nhật contract có chủ đích trước khi code.

### 6.3. Action và phạm vi candidate

- Premise trả toàn bộ payload; edit giữ phần không được yêu cầu sửa, regenerate đề xuất phương án theo yêu cầu trong ràng buộc đã chốt.
- Collections: `assigned_*_ids` là tập ID mục tiêu. Generate/append dùng ID mới không trùng existing; edit/regenerate dùng ID có trong existing. Trả entry hoàn chỉnh thuộc phạm vi, không trả patch field và không trả toàn bộ collection ngoài phạm vi. Backend ghép candidate theo ID, không diễn giải entry vắng mặt là delete.
- Entry mới dùng hiệu lực từ input. Entry edit/regenerate giữ hiệu lực và status cũ nếu không có yêu cầu revise rõ ràng. Backend chịu trách nhiệm authorize/validate revise, snapshot và stale; prompt không thực hiện chúng.
- T02 cho phép preallocation hoặc mapping ID tạm. Foundation T04 chọn **preallocation**. Long Plan/Skeleton dùng pool và mapping ID cục bộ theo mục 7.7; không áp fallback này cho foundation. T10/T13 cần kiểm tra ID đầu vào và đối chiếu ID trả về.
- Input thiếu Base Idea accepted, ID/hiệu lực hợp lệ hoặc đích edit: service phải chặn trước LLM. Collection rỗng/thiếu ID trong response không tự đồng nghĩa thành công, accept hoặc xóa dữ liệu; backend báo phần thiếu theo validation hiện hữu. Không tạo schema lỗi mới hay nhét lỗi kỹ thuật vào nội dung truyện.
- Foreshadow foundation không có chapter/arc IDs trong input nên để lịch rỗng/null. `truth_author_only` dạng object dùng các khóa minh họa `truth`, `surface_candidate`, `payoff_intent` (T02 cho phép object, không bắt parser phụ thuộc các khóa này). Planner/Skeleton dùng intent này về sau; không tự tạo foreign key.

### 6.4. Kiểm tra chất lượng nội dung

| Nhóm | Dấu hiệu đạt | Dấu hiệu cần sửa |
|---|---|---|
| Co-create | Giữ toàn bộ ý đã chọn; phản hồi đúng yêu cầu lượt mới; gợi ý có lựa chọn thực | Tự nhập twist mới vào state; hỏi lại điều đã trả lời; brief chỉ có delta |
| Premise | Mục tiêu, lực cản, cái giá và nét riêng có quan hệ nhân quả | Từ quảng cáo chung; tự thêm hard constraint; chốt ending không được yêu cầu |
| Characters | Động cơ dẫn đến hành vi; giọng/ranh giới dùng được; phần kín tách riêng | CV dài; mọi người cùng giọng; bí mật trong câu “đừng tiết lộ…” gửi Writer |
| World Rules | Điều kiện, giới hạn và hệ quả rõ; projection vẫn giữ giới hạn | Lore trang trí; ngoại lệ giải mọi vấn đề; projection bỏ chi phí |
| Foreshadow | Dấu hiệu tự nhiên khi đọc đầu, có căn cứ khi đọc lại | Bí ẩn không có truth; thay upstream để có twist; bịa lịch/ID |
| Genre | Có trục lựa chọn riêng và hệ quả thiết kế, nhường tác giả | Checklist motif hoặc khuôn truyện bắt buộc |

### 6.5. Nguồn chắt lọc

- `docs/references/differentiation.md`: khác biệt ở động cơ, thế giới và quan hệ; bỏ yêu cầu áp số lượng phản mẫu cứng.
- `docs/references/character-building.md`: động cơ, phản ứng dưới áp lực và giọng nói; không bắt mọi người có trauma/khuyết điểm chí mạng.
- `docs/prompts/architect-short.md`: hạt nhân tập trung, kiểm soát quy mô; không chuyển giao thức công cụ, outline cũ hoặc schema cũ.
- Mẫu Co-create do tác giả cung cấp trong phiên nâng cấp: tích lũy brief và gợi ý câu nói tiếp; áp dụng qua JSON hiện hữu như mục 6.2.

Các nguồn trên chỉ được đọc khi biên soạn; không phải dependency runtime cần model tự mở. Không sửa tài liệu reference cũ.

## 7. Planning và Skeleton — T05, 2026-09-20

Bốn prompt mới đăng ký trong mục 2: Long Plan tạo Volume/Arc, Short Plan tạo ChapterPlan trong một arc, Rolling review/patch future plan, Skeleton chuyển một chapter plan thành section instructions. Chúng giữ cấp authority và chỉ trả payload; T06 bổ sung manifest máy đọc, prompt loader thuộc T10. Không nạp catalog hoặc example vào prompt runtime mặc định.

### 7.1. Input và phạm vi tích hợp

Các biến ở mục 2 là **field input JSON**, không marker nội suy trong Markdown; mọi field phải có, mảng rỗng/null dùng đúng mô tả của prompt. Action generate/edit/regenerate luôn tạo candidate riêng. Foundation/chapter selectors dùng ID đã cấp; Long Plan/Skeleton có protocol ID cục bộ cho entry mới theo mục 7.7. Long Plan trả toàn payload trong scope; Short Plan trả các chapter hoàn chỉnh thuộc assigned_chapters để backend ghép theo ID; Skeleton trả toàn một chương. Không diễn giải thiếu entry là delete. Giữ stable ID cho section cũ còn tương ứng; section mới dùng pool hoặc ID cục bộ theo mục 7.7. Regenerate là replacement candidate, có thể đổi số section.

- `planning_scope`: `{start, end}` là range chương được giao cho Long Plan; `assigned_volume_ids`/`assigned_arc_ids` là pool ID khả dụng, không buộc dùng hết; thiếu thì map ID cục bộ theo mục 7.7. Backend cung cấp previous payload phù hợp cùng scope khi sửa; không tự xóa phần ngoài scope.
- `assigned_chapters`: array `{chapter_id, chapter_number}` nằm trong selected arc; có thể chỉ là một phần arc như fixture T02 có hai chương của arc 1–7. `chapter_constraints`: array `{chapter_id, language, pov, length_guidance}`, ba field nội dung là string user đã chọn; lưu vào một object trong outline, chuyển sang global_constraints ở Skeleton. Không thêm project config/schema ChapterPlan top-level.
- `timeline_as_of`: array TimelineEntry; `relationships_as_of`: array RelationshipState đúng thời điểm. `recent_finalized_summaries`: array `{chapter_id, chapter_number, summary}` từ final/accepted state, tối đa ba mặc định. Skeleton chỉ có previous_final_summary cùng shape hoặc null. Chương 1 baseline rỗng; mode actual phải đủ state trước target. Mode provisional cho Short Plan/Skeleton chuẩn bị trước giữ actual đã có và planned_bridge riêng; chưa được accept tới khi review với actual theo mục 7.7.
- Short Plan chọn từ ID current arc và user selection accepted. Skeleton chỉ nhận ChapterPlan selectors hiệu lực <= N; current_arc là summary liên quan đã lọc, không đưa future character/lore qua summary. Short Plan nhiều chương được nhận entity hiệu lực trong range, nhưng từng chapter chỉ dùng từ thời điểm hiệu lực. Long Plan range 1–7 không nhận rule_0100.
- Rolling `eligible_chapters` là array `{chapter_id, chapter_number}` được backend tính từ lifecycle và latest_consistent_chapter, không suy từ file tồn tại. Thiếu eligible thì có thể chỉ report/ok; không bịa chương. Config false chặn cả relationship_plan_changes và thay đổi quan hệ gián tiếp trong narrative fields.
- Freshness/pins, full validation và snapshot/accept là trách nhiệm T08–T15, chưa có runtime trong T05. Không prompt nào tự unlock Writer, finalize prose hay khẳng định đã commit.

### 7.2. Schema và xử lý lỗi

Theo [schemas](schemas.md) mục 3, 4, 6.5 và [context](context.md). T05 cụ thể hóa object RollingPatchPayload vốn chưa có field chi tiết và quy ước outline/writer_context_policy vốn cho phép object. Không thay product spec, không đổi top-level schema cũ. T08/T14 cần dùng allowlist field patch, kiểm tra scope/config rồi validate lại candidate ghép; T10 dùng registry input, T12/T15 dùng projection/contract chương. T06 đọc Skeleton global_constraints và các field writer-safe, không gửi nguyên object Skeleton.

Backend chặn thiếu dependency accepted/fresh, scope/context_basis hợp lệ hoặc yêu cầu viết bắt buộc trước gọi. Thiếu actual ngay trước target chỉ chặn mode actual; có thể chuẩn bị mode provisional theo mục 7.7. Output thiếu/invalid/no-op không được coi là đã thực hiện yêu cầu; giữ raw và accepted cũ. Prompt không có error envelope mới. Xung đột không giải được trong authority phải đưa user review; Rolling có blocked_by_authority để report, các action generate không được accept nội dung lờ conflict. Validation cấu trúc không chứng minh tuân thủ ngữ nghĩa.

### 7.3. Request/response và dry review hai chương

[planning_prompt_cases.json](examples/planning_prompt_cases.json) chứa **8 cặp request/response đầy đủ do người biên soạn tạo**, không phải response model:

| Ca | Kết quả mong đợi |
|---|---|
| `long_plan` | Một volume, arc 1–7; character hiệu lực 2 có thể thuộc arc nhưng lore 100 bị loại; không tạo chapter outline. |
| `short_plan` | Hai chapter từ T02, selector char_0001 ở chương 1, thêm char_0002 ở chương 2; contract vi/POV/độ dài trong outline; quan hệ mới chỉ là ý định. |
| `skeleton_ch1` | Baseline rỗng; đủ chuyển cảnh cấp cứu → tỉnh dậy → nồi → lệnh cưỡng chế → ending vách đá dự kiến. Hint không chứa lời giải; không nhắc tên nhân vật hiệu lực 2. Đây là thiết kế trước final, không rewrite final hiện hữu. |
| `rolling_adjust` | Final/T02 timeline chương 1 dừng khi lên sống núi, chưa xuống vách đá như plan. Chỉ đổi outline chương 2 để nối sự kiện, giữ actual chương 1 và hướng quan hệ. |
| `skeleton_ch2` | Giả định user accept rolling_adjust trước đó; state đầu chương lấy actual chương 1, chưa gặp nhau; mở ở sống núi rồi nối thoát vách đá, gặp dè chừng, ending bị theo dõi. |
| `rolling_relationship_blocked` | Config false: không patch quan hệ, không lách qua outline, report blocked. |
| `rolling_relationship_allowed` | Config true: chỉ chỉnh nhịp dè chừng chương 2 trong đích arc đồng minh miễn cưỡng, không sửa current. |
| `rolling_ok` | Kịch bản riêng có kế hoạch đã nối actual: mảng change rỗng, status ok. |

Fixture T02 là minh họa rút gọn: Skeleton chương 1 chỉ có hai section; planned_payoff nhắc arc_0003 chưa có trong Long Plan ví dụ. T05 **không sửa fixture gốc**: ví dụ mới bổ sung đủ section; projection planning giới hạn range để planned_payoff null, vẫn giữ truth và surface chương 1. Không dùng FK chưa resolve để bịa thêm arc hay cập nhật foundation. T02 final chương 1 cũng là đoạn minh họa, không phải bằng chứng mọi beat kế hoạch đã xảy ra.

Rà secret: truth cái nồi chỉ ở input author và author_only_notes. instruction/notes/beats/forbidden_moves/global_constraints/surface/purpose writer_safe không giải thích cổ vật/nhận chủ/nguyên chủ. Lời cấm dùng “không giải thích nguồn gốc cái nồi”, không nhắc đáp án. Đây là rà ví dụ, chưa chứng minh model hoặc backend chống leak.

### 7.4. Kiểm tra có thể chạy lại

Từ root repo, PowerShell:

```powershell
python -X utf8 docs/design/examples/check_planning_examples.py
rg -n "novel_context|save_foundation|dispatch|audit_foundation" docs/prompts/v1/long_plan.md docs/prompts/v1/short_plan.md docs/prompts/v1/rolling_plan.md docs/prompts/v1/skeleton.md
```

Script dùng standard library, đọc fixture/JSON blocks/catalog, đối chiếu field request, output shape, ID/hiệu lực, ví dụ projection và ba negative probes: patch chương quá khứ, config false có relationship change, field ngoài allowlist. Không dùng nó làm validator runtime hoặc kiểm chứng chất lượng LLM. Rà thủ công vẫn cần cho continuity, authority và secret ngữ nghĩa.

### 7.5. Reference chắt lọc

- [longform-planning](../references/longform-planning.md): mục tiêu–lực cản–đánh đổi cấp volume/arc, thay loại vấn đề thay vì chỉ nâng sức mạnh.
- [chapter-guide](../references/chapter-guide.md): chuỗi áp lực–lựa chọn–hệ quả, bỏ tỷ lệ mở đầu bắt buộc.
- [hook-techniques](../references/hook-techniques.md): nhiều dạng hook, không bắt chương nào cũng cliffhanger/reveal.
- [dialogue-writing](../references/dialogue-writing.md): mục tiêu, né tránh và giọng nhân vật; Skeleton mô tả chức năng đối thoại, không sinh prose.

Reference chỉ dùng khi biên soạn; không có dependency runtime hoặc lệnh model mở chúng. Không thay prompt/reference cũ.

### 7.6. Nâng cấp hướng dẫn sáng tác từ prompt gốc — 2026-09-20

Theo phản hồi tác giả rằng bản đầu chưa đủ hướng dẫn AI viết, bốn prompt T05 đã được mở rộng phần phương pháp sáng tác. Giữ input registry, prompt ID, phạm vi action và output schema; không thay T04 hoặc thực hiện T06.

| Nguồn đã đọc trong docs/prompts | Phần chắt lọc | Cách áp dụng trong v1 |
|---|---|---|
| [architect-long.md](../prompts/architect-long.md) | Lời hứa độc giả, động cơ duy trì truyện, chức năng volume/arc, đổi loại xung đột, mật độ theo độ dài, tránh kết quá sớm/kéo dài | Long Plan có phương pháp thiết kế 6 bước, mapping vào field hiện hữu; Short Plan phân phối tải sự kiện và phản ứng. Không mang quy định hai volume/tối thiểu tám chương, compass hoặc tự kết sách. |
| [architect-short.md](../prompts/architect-short.md) | Tập trung xung đột, kiểm soát cast/lore, nhân quả và hồi đáp | Dùng để làm rõ phạm vi range/chapter và chống phình kế hoạch. Không biến Short Plan thành chế độ truyện ngắn, không dùng outline phẳng cũ. |
| [writer.md](../prompts/writer.md) | Chapter contract, không nén mất phần đệm, độ dài là đầu vào thiết kế, tiêu đề cụ thể, continuity | Chuyển trách nhiệm thiết kế sang Short Plan/Skeleton; phân biệt việc chốt intent với khoảng tự do câu chữ của Writer. Không mang quyền Writer bỏ beat/đổi intent, tự plan hoặc commit. |
| [editor.md](../prompts/editor.md) | Kiểm tra động cơ, pacing, continuity, quan hệ không nhảy bậc, không phạt chương chuyển tiếp máy móc | Rà chất lượng candidate một lượt trong prompt; không thêm bảng điểm, review service hoặc audit loop. |
| [revision-analyze.md](../prompts/revision-analyze.md) | Bằng chứng actual, chỉ báo impact khi có ảnh hưởng, không khôi phục sự kiện đã bị bỏ | Rolling phân loại sai lệch, thiếu bằng chứng, điểm phụ thuộc đầu tiên và patch tối thiểu nhưng đủ. Không mang schema facts/retcon hoặc đảo authority. |
| [arbiter-plan-start.md](../prompts/arbiter-plan-start.md) | Đã đối chiếu giao thức và cách chọn planner | Không áp router theo truyện dài/ngắn hoặc lệnh công cụ vào pipeline v1. |

Mỗi prompt hiện có một JSON ví dụ ngay trong file: ArcPlan, ChapterPlan, RollingPatchPayload hoặc SkeletonSection; các ví dụ fragment ghi rõ vị trí trong payload. Chúng là mẫu dạy cách diễn đạt cụ thể, không phải input truyện thật, không phải quyền thay accepted plan. Short Plan/Rolling đã nêu đầy đủ field RelationshipDirection ngay trong prompt để model không cần đọc prompt khác. Catalog/fixture vẫn dành cho tích hợp và kiểm tra, không phải dependency model phải tự mở.

Skeleton hướng dẫn riêng cảnh hành động, đối thoại, mô tả/phát hiện, phản ứng, chuyển cảnh và kết chương; mỗi cảnh có mục tiêu, trở ngại và điểm ra. Writer notes cho phép chi tiết nhỏ; major events, information reveal và ending vẫn do plan/Skeleton khóa. Truth trong ví dụ chỉ ở author_only_notes; projection phải bỏ nó và purpose kín.

Kiểm tra refresh: script mục 7.4 mở rộng kiểm tra shape bốn ví dụ inline, patch Rolling và nội dung secret trong projection mẫu. Tám ca request/response cũ giữ nguyên để kiểm tra tương thích. Chất lượng phương pháp được rà thủ công, không khẳng định kiểm tra chuỗi/JSON chứng minh LLM viết hay hoặc tuân thủ tuyệt đối.

### 7.7. Hiệu chỉnh theo spec — thay thế quy định T05 trước nếu khác

- Short Plan chốt mục tiêu/sự kiện chính/hướng quan hệ/ending; Skeleton thiết kế cách thực hiện qua cảnh, hành động, nhịp cảm xúc và information reveal. Short Plan không bắt buộc viết trước mọi chi tiết surface; Skeleton được thiết kế chúng trong giới hạn author instruction. Writer chỉ chuyển Skeleton accepted thành prose, có invention nhỏ theo spec mục 16.
- Short Plan/Skeleton thêm input `context_basis` theo [schemas mục 4.1](schemas.md). Mode actual dùng state sát target; mode provisional cho phép chuẩn bị trước bằng actual đã biết và planned_bridge tách riêng. Backend giữ preparation_context/pins trong candidate metadata, không gửi giả định như actual. Provisional không auto-accept hoặc unlock Writer; cần review với actual/pins mới trước accept. Đây là cách hoàn thiện contract cho quyền chuẩn bị trước trong spec, không đổi spec.
- Long Plan/Skeleton không bị ép số entry theo ID reserve. Pool được để dư; entry mới thiếu ID dùng ID cục bộ theo protocol mapping ở schemas mục 4.1. ID tạm chỉ cho volume/arc/section mới, không phải quyền tạo foundation mới. Giữ ID entry cũ tương ứng; replacement candidate được thay số section mà không sửa accepted trước accept. T08/T09/T10/T12/T14/T15 phải đọc protocol này; các quy định preallocation-only của foundation T04 vẫn giữ nguyên.
- Fixture `skeleton_ch2_prepared_before_final` chuẩn bị N=2 từ baseline actual 0 và bridge dự kiến chương 1, không có timeline/relationship actual giả. Output dùng ID cục bộ khi pool rỗng. Các ca actual cũ được bổ sung context_basis; pool thêm ID dư để kiểm tra không phải quota. Đây là dữ liệu kỳ vọng thủ công, không có runtime guard đã implement.
- Skeleton có ví dụ tự thiết kế hint từ intent khái quát và ví dụ reveal giới hạn trong chính prompt. Information reveal được phép không đồng nghĩa gửi toàn truth; chỉ phần surface được approved scope cho phép mới tới Writer.

## 8. Writer, Review, Rewrite, Reconcile và Retcon Impact — T06

### 8.1. Manifest và đóng gói

[manifest.json](../prompts/v1/manifest.json) là allowlist runtime máy đọc. `manifest_version: 1`, path tính từ repository root; T10 resolve root độc lập cwd, từ chối path vượt root/ID không đăng ký. `template_variables` là **các field bắt buộc trong input JSON**, không marker thay chuỗi. Mảng rỗng/null dùng theo prompt. Không nội suy ví dụ trong Markdown.

`output_kind` là json hoặc markdown. `schema_ref` là tên contract tài liệu, **không phải JSON Schema executable**; T08 map tới model/validator, T10/T11 mới dùng schema có thể thực thi. IdeaStateResponse là `{message: string, idea_state: IdeaState}`; các alias LongPlanPayload/ShortPlanPayload chỉ đặt tên cho payload tại schemas mục 3. MarkdownProse là string draft theo mục 5.1, không lifecycle record.

`reference_selection` chỉ cho phép backend chọn đúng một entry từ registry được chỉ định, nạp nội dung vào input_field tương ứng. Genre lấy project.genre_prompt_id; style lấy project.writing_style_id. Chỉ đăng ký `default` ở T06; thiếu cấu hình có thể chọn mặc định này, ID lạ phải báo lỗi, không fallback âm thầm. Không ghép style/genre lần nữa vào prompt nếu đã có trong input JSON. Không nạp catalog, fixture hoặc toàn docs/references. Reference_selection rỗng nghĩa là không nạp reference ngoài template, dù input có story context do service chọn.

### 8.2. Input/guard bàn giao

- Writer: các constraints Base Idea/Premise là array string writer-safe P0; title từ chapter. skeleton chỉ `{sections, global_constraints}`; section có section_id, instruction, writer_notes, required_beats, forbidden_moves, foreshadow_surfaces là array surface string, thêm purpose chỉ khi writer_safe. Không gửi author notes hoặc reveal answer qua lời cấm. Characters/world cũng lọc ở field/string, không chỉ bỏ tên field secret. previous_final_summary là `{chapter_id, chapter_number, summary}` hoặc null, tối đa N−1. Service giữ pins/snapshot và actual preparation_context ngoài model input; gate accepted/fresh, actual, N−1 final_reconciled trước gọi vẫn bắt buộc.
- Review: constraint_sources cung cấp định danh và content của từng nguồn thực dùng. Các character/world/Skeleton/state projections vẫn theo chương. Source nội tại prose dùng authority_kind `prose`, artifact_id của chapter, revision đúng draft; style source dùng registry ID và revision nội dung backend quản lý. Không bịa ID/nguồn khi prompt nhận thiếu. Kiểm tra quote thuộc đúng prose revision; field_path và section_id phải resolve nếu có. Report gắn revision, không thay Human Review, không tự làm prose accepted.
- Rewrite: backend resolve target từ section/selected text trên đúng revision và pins, không dùng tìm-thay toàn chương mơ hồ. Surrounding chỉ đọc; apply thay đúng target bằng replacement, tạo revision mới và vô hiệu review cũ. JSON response **không** cho phép auto-apply prose. changed_intent true hoặc replacement giữ nguyên được đưa ra cho người dùng xử lý; false không chứng minh an toàn ngữ nghĩa. Không chuyển nguyên AI Review report có secret vào Writer/Rewrite.
- Reconcile: source_final_candidate `{prose_revision, markdown_ref}` là binding opaque, không lệnh đọc file. Input prose do backend đọc từ bản đóng băng; prior state là N−1, kể cả retcon. known_characters chỉ ID/tên/aliases đã có hiệu lực. Cặp mới được đề xuất bằng hai ID đã biết; backend cấp relationship_id sau validation. Không lập foundation từ prose. T02 fixture có update cho char_0002 hiệu lực chương 2 trong reconciliation chương 1: T06 không dùng output đó làm ví dụ đúng và không sửa fixture lịch sử; T08/T12/T17 phải reject cập nhật entity chưa hiệu lực, không suy danh tính từ ánh lửa xa.
- Impact: input before/after đầy đủ trong phạm vi, downstream metadata/summaries được backend chọn; AI không quyết định toàn bộ phạm vi stale. Thiếu bằng chứng thì report mức chưa chắc chắn, không rewrite. Backend stale rules chạy độc lập kể cả report rỗng.

Thiếu input bắt buộc phải chặn trước LLM. Writer trả rỗng khi không thể prose hóa an toàn: service ghi run incomplete/failed, không mở review/finalize. Response chỉ có thông báo lỗi cũng không phải prose complete. Stream đứt giữ partial; regenerate giữ accepted cũ. Review/impact có thể report giới hạn khi context mềm thiếu; không diễn giải report rỗng là đã được user review. Không thêm error envelope vào story payload.

### 8.3. Style và nguồn tham khảo

[default](../styles/v1/default.md) chắt lọc voice chung, đối thoại, nhịp và anti-ai-tone thành hướng dẫn độc lập. Đã rà các style tiếng Việt cũ: default chủ yếu bút pháp; suspense/fantasy/romance/wuxia còn yêu cầu thiết kế plot, manh mối hoặc quan hệ nên **không đăng ký runtime nguyên bản**. Các bản zh cũng không được đăng ký. Không triển khai style imitation/import.

| Reference đã đọc | Áp dụng | Phần loại bỏ |
|---|---|---|
| writer.md | Continuity, giọng, beat, khoảng phản ứng, chi tiết cụ thể | Tự plan, kiểm tra loop, commit, memory/cast tracking |
| editor.md | Bằng chứng nguyên văn, logic và gu thẩm mỹ tách biệt, không phạt chương chuyển tiếp | Điểm/verdict accept, tự lưu, hàng đợi rewrite, thống kê memory không có |
| revision-analyze.md, import-analyze.md | Facts dựa toàn prose sửa, không suy từ delta hoặc plan | Trích foundation/style profile, import pipeline |
| architect-long/short.md | Quy mô và tải cảnh thuộc planner; Writer giữ intent đã giao | Công cụ, tự điều phối và tự kết sách |
| arbiter-*.md, import-range/segment/synthesize.md, simulation-*.md | Chỉ rà để xác định ranh giới reference | Không đăng ký Arbiter LLM, import hoặc imitation trong MVP |
| anti-ai-tone.md, dialogue-writing.md, quality-checklist.md | Chi tiết cảm giác có mục đích, thoại phân biệt, nguồn chứng cứ | working_memory, cam kết checker tự động, quota hook/điểm số, cấm từ máy móc |

### 8.4. Ví dụ và kiểm tra

[writing_prompt_cases.json](examples/writing_prompt_cases.json) có 10 request/response **do người biên soạn tạo**: Writer chương 1/2; Review conflict/no issue; Rewrite cục bộ/đổi intent; Reconcile người gõ cửa chưa rõ danh tính/quan hệ từ hành động thật; Impact đổi vị trí vật/chỉ đổi câu chữ. Các ca độc lập: final chương 1 là bản user edit, không khẳng định Writer draft đã tự final. Metadata secret/lore chương 100 nằm ngoài requests để rà leak.

Chạy từ root PowerShell:

```powershell
python -X utf8 docs/design/examples/check_writing_examples.py
python -X utf8 docs/design/examples/check_planning_examples.py
```

Script rà manifest/catalog/schema section/link, mọi JSON example v1/catalog và fixture JSON, shape/binding của T06, quote/source, selector, prior state, forbidden secret mẫu, cùng negative probes. Đây là kiểm tra tài liệu offline, không phải validator/backend hoặc chứng minh LLM tuân thủ. Rà thủ công vẫn cần cho chất lượng prose, quan hệ/actual và quyền hạn. T07 là task todo có dependency done nhỏ nhất tiếp theo; T10 đọc manifest này khi đến lượt.

### 8.5. Nâng cấp Writer theo phản hồi tác giả — 2026-09-20

Đọc lại 14 prompt gốc trực tiếp trong docs/prompts (không dùng v1 làm nguồn tham khảo thay bản gốc), đặc biệt writer.md và editor.md; đối chiếu quyền Writer trong spec cùng SkeletonPayload/projection hiện hành. Writer v1 được mở rộng phần hướng dẫn sáng tác, giữ nguyên input registry, output Markdown, schema và manifest.

| Phần trong prompt gốc | Cách chuyển sang Writer phụ thuộc Skeleton |
|---|---|
| Chapter Contract, required_beats/forbidden_moves | Bảng cách dùng từng field projection. Emotion/pacing/hook nằm trong instruction, writer_notes, purpose an toàn và global_constraints; không yêu cầu field contract cũ không có. |
| Đọc context trước khi viết, continuity | Đọc actual N−1/profile đã đóng gói để nối cảnh; không tự đọc file, suy latest state hoặc bịa quá khứ thiếu trong summary. |
| Voice và user preferences | Hướng dẫn thoại/POV/nhịp câu và áp style/user_instruction trong authority; không placeholder voice hoặc nguồn memory tự tìm. |
| Word Count, không nén mất phần đệm | Phân bố dung lượng theo trọng lượng section, giữ nhân quả và phản ứng. Bỏ quyền xóa/gộp sự kiện để ép số từ và khuôn 2–3 cảnh; cho phép nối văn của section cùng cảnh. |
| Tiêu đề cụ thể | Giữ title đã cấp khi có heading; chưa có output sửa metadata nên không tự chọn lại title như app cũ. |
| Nhân vật phụ tái xuất | Giữ tên/giọng/đặc điểm từ profile đã cấp; không cast tracking tự động, không coi thiếu context là nhân vật mới. Chi tiết nền nhỏ được phép trong intent, nhân vật có chức năng plot cần upstream. |
| Tự kiểm duyệt trước nộp | Rà prose một lượt về beat, nhân quả, giọng, reveal, độ dài và ending; không tool loop, tự chấm điểm hay tự accept. |
| Viết lại/mài giũa | Phân biệt draft toàn chương từ Writer với target của Rewrite Section. Không tự suy Continue/retcon khi input không có bản prose hoặc partial prefix. |
| Editor: continuity, character, aesthetic, hook | Hướng dẫn hành động có điều kiện, thoại có mục đích, cảm xúc có tác nhân, chọn chi tiết theo POV và kết đúng mức; không áp quota cao trào hoặc thẩm mỹ thành canon. |

[writer_skeleton_projection_cases.json](examples/writer_skeleton_projection_cases.json) bổ sung hai ca biên soạn từ chính hai section JSON trong Skeleton v1: hint cái nồi và partial reveal dấu niêm. Fixture giữ source_section để đối chiếu nhưng request chỉ chứa projection; source author notes/purpose kín không gửi Writer. ID tmp_section_1 của ví dụ thiết kế được giả định backend map thành section_0003 trước accepted/Writer. Không thêm field type/index/reveal_policy vào request Writer hoặc thay schema để chứa ví dụ.

Kiểm tra tự động đối chiếu source section, field instruction/notes/beats/forbidden_moves, surface strings, ID stable và inline examples; không test bằng cách đếm heading/độ dài prompt. Rà thủ công prose: thao tác cọ–gõ–kiểm tra lại thực hiện hint mà không giải thích; cảnh dấu niêm thực hiện kết luận được phép mà không nêu thủ phạm/động cơ. Hai output là minh họa do người biên soạn viết, chưa gọi LLM. Bộ kiểm tra T06 hiện có 10 ca gốc + 2 ca projection, 6 fixture JSON files, 33 JSON blocks và 10 negative probes; regression T05 giữ 9 ca và 5 negative probes.
