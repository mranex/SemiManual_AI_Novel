"""Rà tài liệu/fixture T05 offline; không thay validator hoặc context builder runtime."""
import copy
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EX = Path(__file__).resolve().parent
data = json.loads((EX / 'planning_prompt_cases.json').read_text(encoding='utf-8'))
cases = {c['case_id']: c for c in data['cases']}
catalog = (ROOT / 'docs/design/prompt-catalog.md').read_text(encoding='utf-8')
CHAPTER = set('chapter_id chapter_number title summary hook outline character_ids world_rule_ids foreshadow_ids threads relationship_changes chapter_goal planned_ending'.split())
SECTION = set('section_id index type instruction purpose purpose_visibility writer_notes author_only_notes required_beats forbidden_moves character_ids world_rule_ids foreshadow_ids foreshadow_surfaces'.split())
DIRECTION = set('character_ids arc_direction target_state notes'.split())
ROLLING = set('status reviewed_chapter_range deviations short_plan_changes relationship_plan_changes blocked_by_authority'.split())
FIELDS = set('title summary hook outline threads chapter_goal planned_ending'.split())

def exact(obj, keys):
    assert set(obj) == keys, (set(obj) - keys, keys - set(obj))

def direction(d):
    assert DIRECTION <= set(d) <= DIRECTION | {'relationship_id'}
    assert len(d['character_ids']) == len(set(d['character_ids'])) == 2

def chapter(c):
    exact(c, CHAPTER)
    contract = [o for o in c['outline'] if isinstance(o, dict)]
    assert len(contract) == 1
    exact(contract[0], {'language', 'pov', 'length_guidance'})
    assert all(isinstance(v, str) and v for v in contract[0].values())
    for d in c['relationship_changes']:
        direction(d)
        assert set(d['character_ids']) <= set(c['character_ids'])

def basis(req, target):
    b=req['context_basis']
    exact(b, {'mode','actual_through_chapter','planned_bridge'})
    assert 0 <= b['actual_through_chapter'] <= target-1
    assert all(t['chapter_number'] <= b['actual_through_chapter'] for t in req['timeline_as_of'])
    assert all(r['last_updated_chapter'] <= b['actual_through_chapter'] for r in req['relationships_as_of'])
    if b['mode']=='actual':
        assert b['actual_through_chapter']==target-1 and b['planned_bridge']==[]
    else:
        assert b['mode']=='provisional' and b['actual_through_chapter']<target-1
        assert [x['chapter_number'] for x in b['planned_bridge']]==list(range(b['actual_through_chapter']+1,target))

def section_ids(req,res):
    ids=[s['section_id'] for s in res['sections']]
    assert len(ids)==len(set(ids))
    assert all(i in req['assigned_section_ids'] or re.fullmatch(r'tmp_section_[1-9][0-9]*',i) for i in ids)

def rolling(req, res):
    exact(res, ROLLING)
    assert res['reviewed_chapter_range'] == req['reviewed_chapter_range']
    assert res['status'] == ('adjust' if any(res[k] for k in ROLLING - {'status','reviewed_chapter_range'}) else 'ok')
    eligible = {c['chapter_id'] for c in req['eligible_chapters'] if c['chapter_number'] > req['latest_consistent_chapter']}
    current = {c['chapter_id']: c for c in req['current_short_plan']['chapters']}
    for kind in ('short_plan_changes','relationship_plan_changes'):
        ids = [c['chapter_id'] for c in res[kind]]
        assert len(ids) == len(set(ids)) and set(ids) <= eligible
    if not req['allow_relationship_replan']:
        assert res['relationship_plan_changes'] == []
    for change in res['short_plan_changes']:
        exact(change, {'chapter_id','changes','reason'})
        assert change['changes'] and set(change['changes']) <= FIELDS
        merged = copy.deepcopy(current[change['chapter_id']])
        merged.update(change['changes'])
        chapter(merged)
        # Outline replacement retains the language/POV/length contract.
        assert [o for o in merged['outline'] if isinstance(o,dict)] == [o for o in current[change['chapter_id']]['outline'] if isinstance(o,dict)]
    for change in res['relationship_plan_changes']:
        exact(change, {'chapter_id','relationship_changes','reason'})
        for d in change['relationship_changes']:
            direction(d)
            assert set(d['character_ids']) <= set(current[change['chapter_id']]['character_ids'])
    for d in res['deviations']:
        exact(d, {'chapter_id','planned','actual','evidence'})
    for b in res['blocked_by_authority']:
        exact(b, {'chapter_id','authority','reason'})

json_blocks = 0
for path in [*(ROOT/'docs/prompts/v1').rglob('*.md'), ROOT/'docs/design/prompt-catalog.md']:
    content = path.read_text(encoding='utf-8')
    for block in re.findall(r'```json\s*\n(.*?)\n```',content,re.S):
        json.loads(block)
        json_blocks += 1

for c in cases.values():
    req, res = c['request'], c['response']
    row = next(line for line in catalog.splitlines() if line.startswith(f"| `{c['prompt_id']}` |"))
    fields = set(re.findall(r'`([^`]+)`',row.split('|')[5]))
    exact(req, fields)
    path = ROOT / re.search(r'`([^`]+)`',row.split('|')[2]).group(1)
    prompt = path.read_text(encoding='utf-8')
    assert all(f'`{field}`' in prompt for field in fields)
    assert not re.search(r'novel_context|save_foundation|dispatch|audit_foundation',prompt)
    if c['prompt_id'] == 'long_plan.v1':
        exact(res, {'volumes','global_threads'})
        for v in res['volumes']:
            exact(v,set('volume_id title theme goal arcs'.split()))
            assert v['volume_id'] in req['assigned_volume_ids']
            for a in v['arcs']:
                exact(a,set('arc_id title chapter_range goal core_conflict start_state end_state major_reveals character_ids world_rule_ids foreshadow_ids relationship_directions'.split()))
                assert a['arc_id'] in req['assigned_arc_ids']
                assert req['planning_scope']['start'] <= a['chapter_range']['start'] <= a['chapter_range']['end'] <= req['planning_scope']['end']
                for d in a['relationship_directions']:
                    direction(d)
    elif c['prompt_id'] == 'short_plan.v1':
        basis(req,min(ch['chapter_number'] for ch in res['chapters']))
        exact(res, {'arc_id','chapters'})
        assert res['arc_id'] == req['current_arc']['arc_id']
        assert [{k:c[k] for k in ('chapter_id','chapter_number')} for c in res['chapters']] == req['assigned_chapters']
        for ch in res['chapters']:
            chapter(ch)
            for source, key, selector in [('characters','character_id','character_ids'),('world_rules','world_rule_id','world_rule_ids'),('foreshadows','foreshadow_id','foreshadow_ids')]:
                allowed = {e[key] for e in req[source] if e['effective_from_chapter'] <= ch['chapter_number']}
                assert set(ch[selector]) <= allowed
    elif c['prompt_id'] == 'rolling_plan.v1':
        rolling(req,res)
    else:
        exact(res, set('chapter_id chapter_number sections global_constraints writer_context_policy'.split()))
        assert res['chapter_id'] == req['chapter_plan']['chapter_id']
        assert res['writer_context_policy'] == {'include_author_only':False,'include_future_plan':False}
        basis(req,res['chapter_number'])
        section_ids(req,res)
        assert [s['index'] for s in res['sections']] == list(range(1,len(res['sections'])+1))
        visible = {'global_constraints':res['global_constraints'], 'sections':[]}
        for s in res['sections']:
            exact(s,SECTION)
            for selector in ('character_ids','world_rule_ids','foreshadow_ids'):
                assert set(s[selector]) <= set(req['chapter_plan'][selector])
            safe = {k:s[k] for k in ('instruction','writer_notes','required_beats','forbidden_moves')}
            if s['purpose_visibility'] == 'writer_safe':
                safe['purpose'] = s['purpose']
            safe['surfaces'] = [f['surface_instruction'] for f in s['foreshadow_surfaces']]
            for f in s['foreshadow_surfaces']:
                exact(f, {'foreshadow_id','surface_instruction','reveal_policy'})
                assert f['foreshadow_id'] in s['foreshadow_ids']
            visible['sections'].append(safe)
        rendered = json.dumps(visible,ensure_ascii=False)
        for secret in ('mảnh vật phẩm cổ','nhận chủ sai người','thân xác nguyên chủ','rule_0100','Cơ quan phòng chống'):
            assert secret not in rendered
        if res['chapter_number'] == 1:
            assert req['timeline_as_of'] == [] and req['relationships_as_of'] == []
            assert 'Tiêu Như Ngọc' not in json.dumps(req,ensure_ascii=False)
        elif req['context_basis']['mode']=='actual':
            assert all(t['chapter_number'] == 1 for t in req['timeline_as_of'])
            assert req['relationships_as_of'][0]['current'].startswith('Chưa gặp')
        for source in ('characters','world_rules','foreshadows'):
            assert all(e['effective_from_chapter'] <= res['chapter_number'] for e in req[source])

# Negative probes of the document checker, not tests of an unimplemented service.
for mutation in ('past','config','upstream'):
    c = copy.deepcopy(cases['rolling_relationship_allowed'])
    if mutation == 'past':
        c['response']['short_plan_changes'][0]['chapter_id'] = 'ch_0001'
    elif mutation == 'config':
        c['request']['allow_relationship_replan'] = False
    else:
        c['response']['short_plan_changes'][0]['changes']['world_rule_ids'] = ['rule_0100']
    try:
        rolling(c['request'],c['response'])
    except AssertionError:
        pass
    else:
        raise AssertionError(f'Negative probe not rejected: {mutation}')

# Inline teaching examples must be usable without loading the catalog at runtime.
inline = {}
for name in ('long_plan','short_plan','rolling_plan','skeleton'):
    content = (ROOT / f'docs/prompts/v1/{name}.md').read_text(encoding='utf-8')
    blocks = re.findall(r'```json\s*\n(.*?)\n```',content,re.S)
    assert len(blocks) == (2 if name=='skeleton' else 1), name
    inline[name] = json.loads(blocks[0])
chapter(inline['short_plan'])
exact(inline['long_plan'], set('arc_id title chapter_range goal core_conflict start_state end_state major_reveals character_ids world_rule_ids foreshadow_ids relationship_directions'.split()))
for d in inline['long_plan']['relationship_directions']:
    direction(d)
rolling(cases['rolling_adjust']['request'], inline['rolling_plan'])
exact(inline['skeleton'], SECTION)
assert inline['skeleton']['purpose_visibility'] == 'planner_only'
safe_section = {k:inline['skeleton'][k] for k in ('instruction','writer_notes','required_beats','forbidden_moves','foreshadow_surfaces')}
assert 'nhận chủ sai người' in json.dumps(inline['skeleton']['author_only_notes'],ensure_ascii=False)
assert all(secret not in json.dumps(safe_section,ensure_ascii=False) for secret in ('nhận chủ sai người','mảnh vật phẩm cổ'))

reveal=json.loads(re.findall(r'```json\s*\n(.*?)\n```',(ROOT/'docs/prompts/v1/skeleton.md').read_text(encoding='utf-8'),re.S)[1])
exact(reveal, SECTION)
safe_reveal=json.dumps({k:reveal[k] for k in ('instruction','writer_notes','required_beats','forbidden_moves','foreshadow_surfaces')},ensure_ascii=False)
assert 'thư từng bị mở' in safe_reveal
assert 'người đưa tin' not in safe_reveal and 'đổi lịch hẹn' not in safe_reveal
prepared=cases['skeleton_ch2_prepared_before_final']
assert prepared['request']['timeline_as_of']==[] and prepared['request']['relationships_as_of']==[]
assert prepared['request']['context_basis']['mode']=='provisional'
assert all(s['section_id'].startswith('tmp_section_') for s in prepared['response']['sections'])
for kind in ('false_actual','duplicate_id'):
    probe=copy.deepcopy(prepared)
    if kind=='false_actual':
        probe['request']['context_basis']['mode']='actual'
    else:
        probe['response']['sections'][1]['section_id']=probe['response']['sections'][0]['section_id']
    try:
        basis(probe['request'],2); section_ids(probe['request'],probe['response'])
    except AssertionError:
        pass
    else:
        raise AssertionError(kind)

for target in re.findall(r'\]\(([^)]+)\)',catalog):
    if '://' not in target:
        assert (ROOT/'docs/design'/target.split('#')[0]).exists(), target
print(f'PASS: {len(cases)} request/response cases; {json_blocks} JSON blocks; 5 inline examples; provisional/actual separation, ID pools/local IDs, hint/partial reveal; catalog fields/links; 5 negative probes. Document checks only, no runtime validation.')
