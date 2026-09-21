"""Kiểm tra tài liệu T06 offline; không phải runtime validator hoặc LLM test."""
import copy
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EX = Path(__file__).resolve().parent


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def exact(obj, fields):
    assert isinstance(obj, dict)
    assert set(obj) == set(fields.split()), (set(obj), fields)


def strings(items):
    assert isinstance(items, list) and all(isinstance(x, str) for x in items)


def safe(value):
    forbidden = {'author_only', 'truth_author_only', 'future_direction',
                 'author_only_notes', 'planned_payoff', 'long_plan', 'short_plan'}
    if isinstance(value, dict):
        assert not (set(value) & forbidden), set(value) & forbidden
        for item in value.values():
            safe(item)
    elif isinstance(value, list):
        for item in value:
            safe(item)
    text = json.dumps(value, ensure_ascii=False)
    for secret in ('Bình tráo thư', 'Hội gương bạc', 'rule_0100'):
        assert secret not in text, secret


manifest = read_json(ROOT / 'docs/prompts/v1/manifest.json')
registry = {p['prompt_id']: p for p in manifest['prompts']}
catalog = (ROOT / 'docs/design/prompt-catalog.md').read_text(encoding='utf-8')
assert len(registry) == len(manifest['prompts']) == 14
assert manifest['input_mode'] == 'json_message'
assert manifest['unknown_prompt_policy'] == 'error'
assert manifest['reference_policy'] == 'explicit_only'
paths = set()
for pid, p in registry.items():
    path = ROOT / p['template']
    assert path.is_file() and path.resolve().is_relative_to(ROOT)
    paths.add(path.resolve())
    row = next(line for line in catalog.splitlines() if line.startswith(f'| `{pid}` |'))
    assert re.findall(r'`([^`]+)`', row.split('|')[5]) == p['template_variables']
    assert len(p['template_variables']) == len(set(p['template_variables']))
    prompt = path.read_text(encoding='utf-8')
    assert all(re.search(r'\b' + re.escape(f) + r'\b', prompt) for f in p['template_variables']), pid
    assert not re.search(r'novel_context|save_foundation|commit_chapter|check_consistency|'
                         r'working_memory|episodic_memory|\{\{VOICE\}\}', prompt), pid
    schemas = (ROOT / p['schema_document']).read_text(encoding='utf-8')
    assert re.search(r'^#{2,3} ' + re.escape(p['schema_section']) + r'\.? ', schemas, re.M)
    assert p['output_kind'] == ('markdown' if pid == 'writer.v1' else 'json')
    for selection in p['reference_selection']:
        assert selection['input_field'] in p['template_variables']
        assert selection['registry'] in ('genres', 'styles')
        assert selection['selection'] == 'exactly_one'
assert paths == {p.resolve() for p in (ROOT / 'docs/prompts/v1').rglob('*.md')}
for kind in ('genres', 'styles'):
    for path in manifest[kind].values():
        assert (ROOT / path).is_file()
assert manifest['styles'] == {'default': 'docs/styles/v1/default.md'}

# Parse every JSON fixture and every JSON block in runtime prompts/catalog.
json_count = 0
for path in EX.glob('*.json'):
    read_json(path)
    json_count += 1
blocks = 0
for path in [*paths, ROOT / 'docs/design/prompt-catalog.md']:
    text = path.read_text(encoding='utf-8')
    for block in re.findall(r'```json\s*\n(.*?)\n```', text, re.S):
        json.loads(block)
        blocks += 1
for link in re.findall(r'\]\(([^)]+)\)', catalog):
    if '://' not in link and not link.startswith('#'):
        assert (ROOT / 'docs/design' / link.split('#')[0]).is_file(), link

data = read_json(EX / 'writing_prompt_cases.json')
cases = {c['case_id']: c for c in data['cases']}
assert len(cases) == len(data['cases']) == 10
SEVERITIES = {'info', 'minor', 'major', 'blocking'}


def check(c):
    req, res, pid = c['request'], c['response'], c['prompt_id']
    assert set(req) == set(registry[pid]['template_variables'])
    if pid in {'writer.v1', 'rewrite_section.v1'}:
        safe(req)
    if pid == 'writer.v1':
        assert isinstance(res, str) and res.strip()
        assert all(t['chapter_number'] < req['chapter_number'] for t in req['timeline_as_of'])
        assert all(r['last_updated_chapter'] < req['chapter_number'] for r in req['relationships_as_of'])
        if req['chapter_number'] == 1:
            assert not req['timeline_as_of'] and req['previous_final_summary'] is None
        else:
            assert req['previous_final_summary']['chapter_number'] == req['chapter_number'] - 1
        assert req['skeleton']['sections'] and req['skeleton']['global_constraints']
        for section in req['skeleton']['sections']:
            strings(section['foreshadow_surfaces'])
    elif pid == 'review.v1':
        exact(res, 'chapter_id prose_revision issues summary')
        assert res['chapter_id'] == req['chapter_id']
        assert res['prose_revision'] == req['prose_revision']
        assert isinstance(res['summary'], str)
        sources = [{k: v for k, v in s.items() if k != 'content'} for s in req['constraint_sources']]
        ids = [i['issue_id'] for i in res['issues']]
        assert len(ids) == len(set(ids))
        for issue in res['issues']:
            exact(issue, 'issue_id severity category source evidence message suggested_action status')
            assert issue['source'] in sources
            assert issue['severity'] in SEVERITIES and issue['status'] == 'open'
            assert issue['category'] in {'base_idea_conflict', 'premise_conflict', 'character_conflict',
                'world_rule_conflict', 'skeleton_deviation', 'relationship_inconsistency',
                'timeline_inconsistency', 'foreshadow_issue', 'logic_issue', 'style_issue'}
            assert issue['evidence']['prose_revision'] == req['prose_revision']
            if 'quote' in issue['evidence']:
                assert issue['evidence']['quote'] in req['prose_markdown']
    elif pid == 'rewrite_section.v1':
        exact(res, 'replacement_markdown notes changed_intent')
        assert isinstance(res['replacement_markdown'], str)
        strings(res['notes'])
        assert isinstance(res['changed_intent'], bool)
        assert req['request']['selected_text'] == req['target_markdown']
        if res['changed_intent']:
            assert res['replacement_markdown'] == req['target_markdown']
    elif pid == 'reconcile.v1':
        exact(res, 'chapter_id chapter_number source_final_candidate timeline relationship_updates notes')
        for field in ('chapter_id', 'chapter_number', 'source_final_candidate'):
            assert res[field] == req[field]
        exact(res['source_final_candidate'], 'prose_revision markdown_ref')
        exact(res['timeline'], 'time location status')
        assert all(isinstance(v, str) and v for v in res['timeline'].values())
        strings(res['notes'])
        assert all(t['chapter_number'] < req['chapter_number'] for t in req['timeline_as_of'])
        known = {ch['character_id'] for ch in req['known_characters']}
        pairs = set()
        for update in res['relationship_updates']:
            assert {'character_ids', 'current'} <= set(update) <= {'relationship_id', 'character_ids', 'current'}
            pair = frozenset(update['character_ids'])
            assert len(update['character_ids']) == len(pair) == 2 and pair <= known and pair not in pairs
            pairs.add(pair)
            assert isinstance(update['current'], str) and update['current']
            if 'relationship_id' in update:
                assert any(r['relationship_id'] == update['relationship_id'] and
                           frozenset(r['character_ids']) == pair for r in req['relationships_as_of'])
    elif pid == 'retcon_impact.v1':
        exact(res, 'source_change affected_items risk_summary suggested_actions')
        assert res['source_change'] == req['source_change']
        strings(res['suggested_actions'])
        assert isinstance(res['risk_summary'], str)
        known = {(i['item_kind'], i['item_id']) for i in req['downstream_items']}
        for item in res['affected_items']:
            exact(item, 'item_kind item_id reason severity suggested_action')
            assert (item['item_kind'], item['item_id']) in known
            assert item['severity'] in SEVERITIES and item['reason']


for case in cases.values():
    check(case)

covered = {c['prompt_id'] for c in cases.values()}
for filename in ('foundation_prompt_cases.json', 'planning_prompt_cases.json'):
    for case in read_json(EX / filename)['cases']:
        pid = case['prompt_id']
        assert set(case['request']) == set(registry[pid]['template_variables']), pid
        covered.add(pid)
assert covered == set(registry)

# Đối chiếu hai ví dụ Writer với section thật trong prompt Skeleton hiện hành.
# Source chỉ là dữ liệu kiểm tra: không đưa cả Skeleton/author truth vào request.
projection_cases = read_json(EX / 'writer_skeleton_projection_cases.json')['cases']
skeleton_examples = [json.loads(block) for block in re.findall(
    r'```json\s*\n(.*?)\n```',
    (ROOT / 'docs/prompts/v1/skeleton.md').read_text(encoding='utf-8'), re.S)]
writer_examples = [json.loads(block) for block in re.findall(
    r'```json\s*\n(.*?)\n```',
    (ROOT / 'docs/prompts/v1/writer.md').read_text(encoding='utf-8'), re.S)]
for case in projection_cases:
    check(case)
    source = case['source_section']
    assert source in skeleton_examples
    projected = case['request']['skeleton']['sections'][0]
    assert projected in writer_examples
    for field in ('instruction', 'writer_notes', 'required_beats', 'forbidden_moves'):
        assert projected[field] == source[field]
    assert projected['foreshadow_surfaces'] == [s['surface_instruction'] for s in source['foreshadow_surfaces']]
    assert 'purpose' not in projected  # Cả hai source examples đều planner_only.
    assert not projected['section_id'].startswith('tmp_')
    for note in source['author_only_notes']:
        assert note not in json.dumps(case['request'], ensure_ascii=False)

assert cases['reconcile_unknown_knock']['response']['relationship_updates'] == []
assert cases['impact_wording_only']['response']['affected_items'] == []
assert cases['rewrite_local']['response']['replacement_markdown'] != cases['rewrite_local']['request']['target_markdown']

# Negative probes: deliberately corrupt the authored fixtures; static checks must fail.
probes = []
def probe(name, case_id, mutate):
    bad = copy.deepcopy(cases[case_id])
    mutate(bad)
    try:
        check(bad)
    except AssertionError:
        probes.append(name)
    else:
        raise AssertionError('Không chặn negative probe: ' + name)

probe('secret field', 'writer_ch1', lambda c: c['request']['characters'][0].update(author_only={'secret': 'ẩn'}))
probe('secret in safe text', 'writer_ch1', lambda c: c['request']['skeleton']['global_constraints'].append('Không lộ Bình tráo thư.'))
probe('future lore', 'writer_ch1', lambda c: c['request']['world_rules'].append(data['excluded_source_material']['future_rule']))
probe('future state', 'writer_ch2', lambda c: c['request']['timeline_as_of'][0].update(chapter_number=2))
probe('invented review quote', 'review_conflict', lambda c: c['response']['issues'][0]['evidence'].update(quote='Không có câu này.'))
probe('wrong review revision', 'review_conflict', lambda c: c['response'].update(prose_revision=99))
probe('wrong final binding', 'reconcile_unknown_knock', lambda c: c['response']['source_final_candidate'].update(prose_revision=99))
probe('unknown character', 'reconcile_actual_relationship', lambda c: c['response']['relationship_updates'][0].update(character_ids=['char_0001', 'char_9999']))
probe('impact outside scope', 'impact_object_location', lambda c: c['response']['affected_items'][0].update(item_id='skeleton_ch_0099'))
probe('rewrite exceeds blocked intent', 'rewrite_blocked', lambda c: c['response'].update(replacement_markdown='An mở thư.'))
print(f'PASS: {len(registry)} prompts, 6 genres, 1 style; {json_count} JSON files, {blocks} JSON blocks; '
      f'{len(cases)} authored cases, {len(projection_cases)} Skeleton projection cases; '
      f'{len(probes)} negative probes. Tài liệu offline, chưa test runtime/LLM.')
