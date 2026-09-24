import { useState } from 'react'
import { projectPath } from './api'
import { JsonEditor } from './editor'
import { useQuery } from './useQuery'
import { artifactScope, useSessionState, type WorkspaceProps } from './ui'

type ArtifactData = {
  status: string; fingerprint: string; stale_reasons: { reason: string }[];
  accepted: { revision: number; payload: Record<string, unknown>; planning_scope: { start: number; end: number } | null } | null;
  candidate: { revision: number; payload: Record<string, unknown>; planning_scope: { start: number; end: number } | null; validation_state: string } | null;
}
type Arc = { arc_id: string; label: string; chapter_start: number; chapter_end: number }
type Assigned = { chapter_id: string; chapter_number: number; title?: string }
type PlanningData = {
  planning_scope: { start: number; end: number } | null; arcs: Arc[];
  assigned_chapters: Record<string, Assigned[]>;
  writing_defaults: { language: string; pov: string; length_guidance: string };
  eligible_rolling: Assigned[]; allow_relationship_replan: boolean;
}
type Props = WorkspaceProps & { workspace: 'long_plan' | 'short_plan'; project: { fingerprint: string; default_pov: string; default_length_guidance: string } | null }

export function Planning(props: Props) {
  return props.workspace === 'long_plan' ? <LongPlan {...props} /> : <ShortPlan {...props} />
}

function ArtifactPanel({ projectId, id, data, perform, disabled, editAction, acceptAction, rejectAction, editParams = {} }: {
  projectId: string; id: string; data: ArtifactData | undefined; perform: WorkspaceProps['perform']; disabled: boolean;
  editAction: string; acceptAction: string; rejectAction: string; editParams?: Record<string, unknown>;
}) {
  if (!data) return null
  return <><div className="card"><div className="row"><h3>{id}</h3><span className="badge">{data.status}</span></div>
    {data.stale_reasons?.map((item, index) => <p className="warning" key={index}>{item.reason}</p>)}
    {data.candidate && <><p>Candidate r{data.candidate.revision} · {data.candidate.validation_state}</p>
      {id === 'long_plan' && <p className="hint">Horizon candidate: {data.candidate.planning_scope ? `${data.candidate.planning_scope.start}–${data.candidate.planning_scope.end}` : 'legacy chưa xác nhận'}</p>}
      <JsonEditor key={artifactScope(projectId, id, data.candidate.revision, data.fingerprint)} scope={artifactScope(projectId, id, data.candidate.revision, data.fingerprint)} payload={data.candidate.payload}
        disabled={disabled} onSave={payload => perform(editAction, { ...editParams, payload, ...(id === 'long_plan' ? { planning_scope: data.candidate?.planning_scope } : {}) }, { base_ref: `artifact:${id}`, expected_revision: data.candidate?.revision, expected_fingerprint: data.fingerprint }).then(() => undefined)} />
      <div className="row"><button disabled={disabled} onClick={() => void perform(acceptAction).catch(() => undefined)}>Accept</button><button className="danger" disabled={disabled} onClick={() => void perform(rejectAction).catch(() => undefined)}>Reject</button></div></>}
    {data.accepted && <details><summary>Accepted r{data.accepted.revision}</summary><pre className="json-view">{JSON.stringify(data.accepted.payload, null, 2)}</pre></details>}
  </div></>
}

function LongPlan({ projectId, epoch, perform, disabled }: Props) {
  const planning = useQuery<PlanningData>(projectPath(projectId, '/planning'), epoch)
  const artifact = useQuery<ArtifactData>(projectPath(projectId, '/artifacts/long_plan'), epoch)
  const scope = planning.value?.data.planning_scope
  const [start, setStart] = useSessionState<number | null>(`${projectId}:long_plan:start`, null)
  const [end, setEnd] = useSessionState<number | null>(`${projectId}:long_plan:end`, null)
  const [action, setAction] = useSessionState(`${projectId}:long_plan:action`, 'generate')
  const [instruction, setInstruction] = useSessionState(`${projectId}:long_plan:instruction`, '')
  const [assigned, setAssigned] = useSessionState<{ volume: string[]; arc: string[] }>(`${projectId}:long_plan:assigned`, { volume: [], arc: [] })
  const currentStart = start ?? scope?.start ?? 0
  const currentEnd = end ?? scope?.end ?? 0
  const validScope = currentStart >= 1 && currentEnd >= currentStart
  const candidate = artifact.value?.data.candidate
  const accepted = artifact.value?.data.accepted
  return <div className="workspace"><header><h2>Long Plan</h2><p>Horizon bao phủ toàn bộ phạm vi chương bạn chọn. Candidate và edit window là hai khái niệm riêng.</p></header>
    {(planning.error || artifact.error) && <p className="error" role="alert">{planning.error || artifact.error}</p>}
    <div className="card"><h3>Generate Long Plan</h3><div className="field-grid"><label className="field">Horizon từ chương<input type="number" min={1} placeholder="Nhập chương bắt đầu" value={currentStart || ''} onChange={event => setStart(event.target.value ? Number(event.target.value) : null)} /></label>
      <label className="field">Đến chương<input type="number" min={currentStart || 1} placeholder="Nhập chương kết thúc" value={currentEnd || ''} onChange={event => setEnd(event.target.value ? Number(event.target.value) : null)} /></label>
      <label className="field">Action<select value={action} onChange={event => setAction(event.target.value)}><option>generate</option><option>regenerate</option><option>edit</option></select></label></div>
      <label className="field">Yêu cầu thêm<textarea value={instruction} onChange={event => setInstruction(event.target.value)} /></label>
      <div className="row"><button disabled={disabled} onClick={() => void perform('reserve_plan_ids', { volume_count: 4, arc_count: 12 }).then(result => {
        const value = result.data.value as { volume_ids?: string[]; arc_ids?: string[] }
        setAssigned({ volume: value?.volume_ids ?? [], arc: value?.arc_ids ?? [] })
      }).catch(() => undefined)}>Reserve Volume/Arc ID</button><code>{[...assigned.volume, ...assigned.arc].join(', ')}</code></div>
      <button disabled={disabled || !validScope} onClick={() => void perform('generate_long_plan', { planning_scope: { start: currentStart, end: currentEnd }, action, user_instruction: instruction }, { generation: true, scope: `${projectId}:long_plan` }).catch(() => undefined)}>Generate</button>
    </div>
    {accepted && !accepted.planning_scope && <div className="card warning"><h3>Legacy scope chưa xác nhận</h3><p>Không tự suy horizon từ range cũ. Nhập phạm vi đã chốt rồi xác nhận.</p><button disabled={disabled || !validScope} onClick={() => void perform('confirm_planning_scope', { start: currentStart, end: currentEnd }).catch(() => undefined)}>Xác nhận horizon</button></div>}
    <ArtifactPanel projectId={projectId} id="long_plan" data={artifact.value?.data} perform={perform} disabled={disabled} editAction="edit_long_plan_candidate" acceptAction="accept_long_plan" rejectAction="reject_long_plan" editParams={{ assigned_volume_ids: assigned.volume, assigned_arc_ids: assigned.arc }} />
    {candidate && <p className="hint">Coverage và scope được backend kiểm tra lúc Save/Accept.</p>}
  </div>
}

function ShortPlan({ projectId, epoch, perform, disabled, project }: Props) {
  const planning = useQuery<PlanningData>(projectPath(projectId, '/planning'), epoch)
  const artifact = useQuery<ArtifactData>(projectPath(projectId, '/artifacts/short_plan'), epoch)
  const [arcId, setArcId] = useState(() => sessionStorage.getItem(`${projectId}:short_plan:arc`) ?? '')
  const [defaults, setDefaults] = useSessionState<{ pov: string; length: string } | null>(`${projectId}:short_plan:defaults`, null)
  const [overrides, setOverrides] = useSessionState<Record<string, { language: string; pov: string; length_guidance: string }>>(`${projectId}:short_plan:overrides`, {})
  const [action, setAction] = useSessionState(`${projectId}:short_plan:action`, 'generate')
  const [instruction, setInstruction] = useSessionState(`${projectId}:short_plan:instruction`, '')
  const [rollingInstruction, setRollingInstruction] = useSessionState(`${projectId}:short_plan:rolling`, '')
  const [reserveCount, setReserveCount] = useSessionState(`${projectId}:short_plan:reserve_count`, 1)
  const [reservedChapters, setReservedChapters] = useState<unknown>(null)
  const arcs = planning.value?.data.arcs ?? []
  const selected = arcs.some(item => item.arc_id === arcId) ? arcId : arcs[0]?.arc_id || ''
  const rows = planning.value?.data.assigned_chapters[selected] ?? []
  const effectiveDefaults = planning.value?.data.writing_defaults
  const pov = defaults?.pov ?? effectiveDefaults?.pov ?? ''
  const length = defaults?.length ?? effectiveDefaults?.length_guidance ?? ''
  const rolling = useQuery<ArtifactData>(selected ? projectPath(projectId, `/artifacts/rolling_patch_${selected}`) : null, epoch)
  const constraints = rows.map(row => ({ chapter_id: row.chapter_id, language: overrides[row.chapter_id]?.language ?? effectiveDefaults?.language ?? 'vi', pov: overrides[row.chapter_id]?.pov ?? pov, length_guidance: overrides[row.chapter_id]?.length_guidance ?? length }))
  const missing = constraints.some(row => !row.language.trim() || !row.pov.trim() || !row.length_guidance.trim())
  return <div className="workspace"><header><h2>Short Plan & Rolling</h2><p>POV và độ dài lấy từ default project, override từng chương. Save default không sửa plan đã accepted.</p></header>
    {(planning.error || artifact.error) && <p className="error" role="alert">{planning.error || artifact.error}</p>}
    <div className="card"><h3>Default viết của project</h3><div className="field-grid"><label className="field">POV<input value={pov} onChange={event => setDefaults({ pov: event.target.value, length })} /></label><label className="field">Độ dài<input value={length} onChange={event => setDefaults({ pov, length: event.target.value })} /></label></div>
      <button disabled={disabled || !project?.fingerprint} onClick={() => void perform('save_writing_defaults', { default_pov: pov, default_length_guidance: length }, { base_ref: 'project', expected_fingerprint: project?.fingerprint }).then(() => setDefaults(null)).catch(() => undefined)}>Lưu default viết</button></div>
    <div className="card"><h3>Generate Short Plan</h3>{arcs.length ? <label className="field">Arc<select value={selected} onChange={event => { setArcId(event.target.value); sessionStorage.setItem(`${projectId}:short_plan:arc`, event.target.value) }}>{arcs.map(arc => <option key={arc.arc_id} value={arc.arc_id}>{arc.label}</option>)}</select></label> : <p>Chưa có Long Plan accepted.</p>}
      <div className="row"><label className="field">Reserve Chapter ID · số lượng<input type="number" min={1} max={100} value={reserveCount} onChange={event => setReserveCount(Number(event.target.value))} /></label><button className="subtle" disabled={disabled || reserveCount < 1 || reserveCount > 100} onClick={() => void perform('reserve_chapter_ids', { count: reserveCount }).then(result => setReservedChapters(result.data.value)).catch(() => undefined)}>Reserve ID</button></div>
      {reservedChapters !== null && <pre className="json-view">{JSON.stringify(reservedChapters, null, 2)}</pre>}
      <p className="hint">{rows.length ? `${rows.length} chương chưa có plan trong arc này.` : 'Không còn chương mới để lập trong arc.'}</p>
      {rows.map(row => { const current = constraints.find(item => item.chapter_id === row.chapter_id)!; return <fieldset className="nested" key={row.chapter_id}><legend>Chương {row.chapter_number} · {row.chapter_id}</legend><div className="field-grid">
        {(['language', 'pov', 'length_guidance'] as const).map(field => <label className="field" key={field}>{field}<input value={current[field]} onChange={event => setOverrides(old => ({ ...old, [row.chapter_id]: { ...current, [field]: event.target.value } }))} /></label>)}</div></fieldset> })}
      {missing && <p className="warning">Thiếu language/POV/length. Backend cũng chặn trước LLM.</p>}
      <label className="field">Action<select value={action} onChange={event => setAction(event.target.value)}><option>generate</option><option>regenerate</option><option>edit</option></select></label>
      <label className="field">Yêu cầu thêm<textarea value={instruction} onChange={event => setInstruction(event.target.value)} /></label>
      <button disabled={disabled || !selected || !rows.length || missing} onClick={() => void perform('generate_short_plan', { arc_id: selected, assigned_chapters: rows, chapter_constraints: constraints, action, user_instruction: instruction }, { generation: true, scope: `${projectId}:short_plan:${selected}` }).catch(() => undefined)}>Generate</button>
    </div>
    <ArtifactPanel projectId={projectId} id="short_plan" data={artifact.value?.data} perform={perform} disabled={disabled} editAction="edit_short_plan_candidate" acceptAction="accept_short_plan" rejectAction="reject_short_plan" editParams={{ arc_id: selected }} />
    <div className="card"><h3>Rolling proposal</h3><p>Eligible: {(planning.value?.data.eligible_rolling ?? []).map(row => row.chapter_id).join(', ') || 'chưa có'}</p><p className="hint">Chỉ áp vào future Short Plan. Authority và scope được kiểm tại backend.</p>
      <label className="field">Yêu cầu review<textarea value={rollingInstruction} onChange={event => setRollingInstruction(event.target.value)} /></label>
      <button disabled={disabled || !selected} onClick={() => void perform('generate_rolling', { arc_id: selected, user_instruction: rollingInstruction }, { generation: true, scope: `${projectId}:rolling:${selected}` }).catch(() => undefined)}>Generate Rolling</button>
      {rolling.value?.data.candidate && <><pre className="json-view">{JSON.stringify(rolling.value.data.candidate.payload, null, 2)}</pre><div className="row"><button disabled={disabled} onClick={() => void perform('accept_rolling').catch(() => undefined)}>Apply Rolling</button><button className="danger" disabled={disabled} onClick={() => void perform('reject_rolling').catch(() => undefined)}>Reject</button></div></>}
    </div>
  </div>
}
