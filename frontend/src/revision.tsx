import { useState } from 'react'
import { errorText, projectPath } from './api'
import { useQuery } from './useQuery'
import { JsonEditor } from './editor'
import { artifactScope, chapterScope, clearDraft, readDraft, useSessionState, writeDraft, type WorkspaceProps } from './ui'

type Blocker = { kind: string; artifact_id?: string; chapter_id?: string; chapter_number?: number; reason: string; suggested_action: string }
type Version = { chapter_id: string; chapter_number: number; title: string; status: string; note: string; final_revision: number | null; retcon: { status: string; retcon_draft_revision: number; replaces_final_revision: number; final_still_canon: boolean } | null }
type Artifact = { artifact_id: string; artifact_type: string; status: string; accepted_revision: number | null; candidate_revision: number | null; stale_reasons: string[] }
type RevisionData = { blockers: Blocker[]; chapters: Version[]; artifacts: Artifact[]; snapshots: { snapshot_id: string; for_chapter_number: number; created_from_action: string; dependency_pins: number }[]; history: { operation_id: string; operation_type: string; status: string; created_at: string; applied_count: number; before_count: number }[] }
type FoundationData = { base_idea_markdown: string; base_idea_metadata: { revision: number; status: string } | null }
type PremiseData = { fingerprint: string; accepted: { revision: number; payload: Record<string, unknown> } | null; candidate: { revision: number; payload: Record<string, unknown> } | null }
type Props = WorkspaceProps & { readOnly: boolean; needsRecovery: boolean; onNavigate: (workspace: 'review' | 'reconcile', chapterId: string) => void }

const impactKinds = ['chapter', 'premise', 'characters', 'world_rules', 'foreshadow', 'long_plan', 'short_plan', 'skeleton']

export function Revision({ projectId, epoch, perform, disabled, readOnly, needsRecovery, onNavigate }: Props) {
  const revision = useQuery<RevisionData>(projectPath(projectId, '/revision'), epoch)
  const recovery = useQuery<{ pending_operation_ids: string[]; needs_recovery: boolean; read_only: boolean; write_blocked: boolean }>(projectPath(projectId, '/recovery'), epoch)
  const foundation = useQuery<FoundationData>(projectPath(projectId, '/foundation'), epoch)
  const premise = useQuery<PremiseData>(projectPath(projectId, '/artifacts/premise'), epoch)
  const [localError, setLocalError] = useState('')
  const [recoveryResult, setRecoveryResult] = useState('')
  const [chapterId, setChapterId] = useSessionState(`${projectId}:revision:chapter`, '')
  const [baseText, setBaseText] = useSessionState<string | null>(`${projectId}:revision:base`, null)
  const [baseConfirm, setBaseConfirm] = useState(false)
  const [premiseConfirm, setPremiseConfirm] = useState(false)
  const [retconConfirm, setRetconConfirm] = useState(false)
  const [resetConfirm, setResetConfirm] = useState(false)
  const [impactKind, setImpactKind] = useSessionState(`${projectId}:revision:impact_kind`, 'chapter')
  const [impactId, setImpactId] = useSessionState(`${projectId}:revision:impact_id`, '')
  const [impactBefore, setImpactBefore] = useSessionState(`${projectId}:revision:impact_before`, '')
  const [impactAfter, setImpactAfter] = useSessionState(`${projectId}:revision:impact_after`, '')
  const [impactScope, setImpactScope] = useSessionState(`${projectId}:revision:impact_scope`, '')
  const [fromRevision, setFromRevision] = useSessionState(`${projectId}:revision:impact_from`, 0)
  const [toRevision, setToRevision] = useSessionState(`${projectId}:revision:impact_to`, 0)
  const report = useQuery<{ candidate: { revision: number; payload: Record<string, unknown> } | null; accepted: { revision: number; payload: Record<string, unknown> } | null }>(impactId.trim() ? projectPath(projectId, `/artifacts/impact_report_${encodeURIComponent(impactId.trim())}`) : null, epoch)
  const data = revision.value?.data
  const chapters = data?.chapters ?? []
  const selected = chapters.find(row => row.chapter_id === chapterId && row.final_revision !== null) ?? chapters.find(row => row.final_revision !== null)
  const downstream = (data?.blockers ?? []).filter(row => row.suggested_action === 'reconcile_downstream' && row.chapter_number !== undefined)
  const firstDownstream = downstream.reduce<number | null>((first, row) => first === null ? row.chapter_number! : Math.min(first, row.chapter_number!), null)
  async function run(name: string, params: Record<string, unknown> = {}, options: Parameters<typeof perform>[2] = {}) {
    setLocalError('')
    try { return await perform(name, params, options) }
    catch (caught) { setLocalError(errorText(caught)); return null }
  }
  const pending = recovery.value?.data.pending_operation_ids ?? []
  return <div className="workspace revision"><header><h2>Revision / Recovery</h2><p>Thay đổi upstream là action tường minh. Final Manuscript đã chốt không tự bị viết lại.</p></header>
    {(revision.error || recovery.error || foundation.error || premise.error || localError) && <p className="error" role="alert" tabIndex={-1}>{revision.error || recovery.error || foundation.error || premise.error || localError}</p>}
    <section className="card" aria-labelledby="recovery-title"><h3 id="recovery-title">Recovery</h3>
      {readOnly || recovery.value?.read_only ? <p className="danger-banner" role="alert">Project read-only; cần kiểm tra transaction pending và bản history. Giao diện không chạy action ghi.</p> :
        needsRecovery || recovery.value?.needs_recovery ? <><p className="warning">Có transaction chờ xử lý: {pending.join(', ') || 'đang kiểm tra'}. Mọi action ghi khác bị khóa cho tới khi recovery hoàn tất.</p>
          <button onClick={() => void run('recover_project').then(result => { if (result) setRecoveryResult(JSON.stringify(result.data)) })}>Chạy recovery</button></> : <p className="hint">Không có transaction chờ recovery.</p>}
      {recoveryResult && <pre className="json-view">{recoveryResult}</pre>}
    </section>
    {readOnly && <History data={data} />}
    {!readOnly && <>
      <section className="card"><h3>Stale chain và blocker</h3>
        {!data?.blockers.length && <p className="hint">Không có artifact/state stale.</p>}
        <ul className="action-list">{data?.blockers.map((item, index) => <li key={`${item.kind}:${item.artifact_id}:${item.chapter_id}:${index}`}><div><strong>{item.artifact_id || item.chapter_id || item.kind}</strong><p>{item.reason}</p></div>
          {item.suggested_action === 'reaccept_stale' && item.artifact_id && <button disabled={disabled} onClick={() => void run('reaccept_stale', { artifact_id: item.artifact_id })}>Review / Reaccept</button>}
          {item.suggested_action === 'reconcile_downstream' && item.chapter_id && <button disabled={disabled || item.chapter_number !== firstDownstream} title={item.chapter_number !== firstDownstream ? 'Reconcile chương trước theo thứ tự' : undefined} onClick={() => void run('reconcile_downstream', { chapter_id: item.chapter_id }, { generation: true, stream: false, scope: `${projectId}:revision:downstream:${item.chapter_id}` })}>Reconcile downstream</button>}
          {item.suggested_action === 'generate_reconciliation' && item.chapter_id && <button className="subtle" onClick={() => onNavigate('reconcile', item.chapter_id!)}>Mở Reconcile</button>}
          {item.suggested_action === 'finalize_chapter' && item.chapter_id && <button className="subtle" onClick={() => onNavigate('review', item.chapter_id!)}>Mở Review</button>}
        </li>)}</ul>
      </section>
      <section className="card"><h3>Retcon chapter đã final</h3><p className="hint">Final cũ vẫn là canon đến khi bản retcon được Finalize và Accept Reconciliation. State downstream chỉ stale sau action Reset consistency.</p>
        <label className="field">Chương<select value={selected?.chapter_id ?? ''} onChange={event => setChapterId(event.target.value)}><option value="" disabled>Chọn chương</option>{chapters.filter(row => row.final_revision !== null).map(row => <option key={row.chapter_id} value={row.chapter_id}>Chương {row.chapter_number} · {row.title}</option>)}</select></label>
        {selected?.retcon ? <p className="warning">Retcon mở: draft r{selected.retcon.retcon_draft_revision} thay final r{selected.retcon.replaces_final_revision}; final cũ vẫn canon.</p> : null}
        <label className="check"><input type="checkbox" checked={retconConfirm} onChange={event => setRetconConfirm(event.target.checked)} />Tôi hiểu retcon tạo draft mới và không sửa final prose của chương sau.</label>
        <button disabled={disabled || !selected?.final_revision || !!selected?.retcon || !retconConfirm} onClick={() => void run('start_retcon', { chapter_id: selected?.chapter_id ?? '' }).then(result => { if (result) setRetconConfirm(false) })}>Start Retcon</button>
        {selected?.retcon && <RetconEditor key={`${projectId}:${selected.chapter_id}`} projectId={projectId} chapterId={selected.chapter_id} epoch={epoch} perform={perform} disabled={disabled} />}
        {selected?.final_revision && <><p className="hint">Sau khi retcon được commit, reset consistency để đánh dấu state downstream stale, rồi reconcile lần lượt từng chương.</p><label className="check"><input type="checkbox" checked={resetConfirm} onChange={event => setResetConfirm(event.target.checked)} />Đánh dấu state sau chương {selected.chapter_number} stale.</label>
          <button className="danger" disabled={disabled || !resetConfirm} onClick={() => void run('reset_consistency', { chapter_number: selected.chapter_number }).then(result => { if (result) setResetConfirm(false) })}>Reset consistency sau retcon</button></>}
      </section>
      <section className="card"><h3>Revise Base Idea</h3><p className="hint">Accepted r{foundation.value?.data.base_idea_metadata?.revision ?? '—'}. Premise, planning, Skeleton và draft chưa final có thể stale. Final Manuscript giữ nguyên.</p>
        <label className="field">Markdown mới<textarea className="prose-editor" rows={12} value={baseText ?? foundation.value?.data.base_idea_markdown ?? ''} onChange={event => setBaseText(event.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={baseConfirm} onChange={event => setBaseConfirm(event.target.checked)} />Tôi xác nhận revise upstream và sẽ xử lý downstream stale.</label>
        <button disabled={disabled || !baseConfirm || !(baseText ?? foundation.value?.data.base_idea_markdown ?? '').trim()} onClick={() => void run('revise_base_idea', { markdown: baseText ?? foundation.value?.data.base_idea_markdown }).then(result => { if (result) { setBaseText(null); setBaseConfirm(false) } })}>Revise Base Idea</button>
      </section>
      <section className="card"><h3>Revise Premise</h3><p className="hint">Accepted r{premise.value?.data.accepted?.revision ?? '—'}. Planning, Skeleton và draft chưa final có thể stale; final prose giữ nguyên.</p>
        {premise.value?.data.accepted && <JsonEditor key={artifactScope(projectId, 'revision-premise', premise.value.data.accepted.revision, premise.value.data.fingerprint)} scope={artifactScope(projectId, 'revision-premise', premise.value.data.accepted.revision, premise.value.data.fingerprint)} payload={premise.value.data.accepted.payload} disabled={disabled || !premiseConfirm} saveLabel="Revise Premise" hint="Action này tạo accepted revision mới và đánh dấu downstream stale."
          onSave={payload => perform('revise_premise', { payload }, { base_ref: 'artifact:premise', expected_fingerprint: premise.value?.data.fingerprint }).then(() => { setPremiseConfirm(false) })} />}
        <label className="check"><input type="checkbox" checked={premiseConfirm} onChange={event => setPremiseConfirm(event.target.checked)} />Tôi xác nhận revise Premise accepted; Save ở trên là action revise trực tiếp.</label>
      </section>
      <section className="card"><h3>Impact report</h3><p className="hint">Report chỉ là candidate để đọc; không tự apply hay đánh dấu stale.</p>
        <div className="field-grid"><label className="field">Loại thay đổi<select value={impactKind} onChange={event => setImpactKind(event.target.value)}>{impactKinds.map(kind => <option key={kind}>{kind}</option>)}</select></label>
          <label className="field">Item ID<input value={impactId} onChange={event => setImpactId(event.target.value)} /></label>
          <label className="field">From revision (0 = không khai)<input type="number" min={0} value={fromRevision} onChange={event => setFromRevision(Number(event.target.value))} /></label>
          <label className="field">To candidate revision (0 = không khai)<input type="number" min={0} value={toRevision} onChange={event => setToRevision(Number(event.target.value))} /></label></div>
        <label className="field">Trước thay đổi<textarea value={impactBefore} onChange={event => setImpactBefore(event.target.value)} /></label>
        <label className="field">Sau thay đổi<textarea value={impactAfter} onChange={event => setImpactAfter(event.target.value)} /></label>
        <label className="field">Phạm vi phân tích<input value={impactScope} onChange={event => setImpactScope(event.target.value)} /></label>
        <button disabled={disabled || !impactId.trim()} onClick={() => void run('generate_impact_report', { source_change: { item_kind: impactKind, item_id: impactId.trim(), from_revision: fromRevision || null, to_candidate_revision: toRevision || null }, before_content: impactBefore, after_content: impactAfter, analysis_scope: impactScope }, { generation: true, scope: `${projectId}:revision:impact:${impactId.trim()}` })}>Chạy impact report</button>
        {report.value?.data.candidate && <details><summary>Report r{report.value.data.candidate.revision}</summary><pre className="json-view">{JSON.stringify(report.value.data.candidate.payload, null, 2)}</pre></details>}
      </section>
      <History data={data} />
    </>}
  </div>
}

function RetconEditor({ projectId, chapterId, epoch, perform, disabled }: { projectId: string; chapterId: string; epoch: number; perform: WorkspaceProps['perform']; disabled: boolean }) {
  const chapter = useQuery<{ fingerprint: string; current_draft_revision: number; draft_text: string; final_text: string }>(projectPath(projectId, `/chapters/${chapterId}`), epoch)
  const current = chapter.value?.data
  const scope = current ? chapterScope(projectId, chapterId, current.current_draft_revision, current.fingerprint) : ''
  const [text, setText] = useState<string | null>(null)
  const working = text ?? (scope ? readDraft(`retcon:${scope}`, current?.draft_text ?? '') : '')
  return <div className="editor"><h3>Sửa draft retcon</h3><p className="hint">Save tạo prose revision mới; final cũ vẫn canon đến khi Finalize Retcon và Accept Reconciliation.</p>
    {chapter.error && <p role="alert" className="error">{chapter.error}</p>}
    <label className="field">Prose working copy<textarea className="prose-editor" rows={16} value={working} onChange={event => { setText(event.target.value); if (scope) writeDraft(`retcon:${scope}`, event.target.value) }} /></label>
    <button disabled={disabled || !current || !working.trim()} onClick={() => void perform('edit_retcon_draft', { chapter_id: chapterId, text: working }, { base_ref: `chapter:${chapterId}`, expected_revision: current?.current_draft_revision, expected_fingerprint: current?.fingerprint }).then(() => { if (scope) clearDraft(`retcon:${scope}`); setText(null) }).catch(() => undefined)}>Save draft retcon</button>
  </div>
}

function History({ data }: { data: RevisionData | undefined }) {
  return <section className="card"><h3>History và snapshot</h3><p className="hint">Chỉ đọc để đối chiếu; không có restore tùy ý.</p>
    <details open><summary>Chapter versions ({data?.chapters.length ?? 0})</summary><ul>{data?.chapters.map(row => <li key={row.chapter_id}>Chương {row.chapter_number} · {row.chapter_id} — {row.note}</li>)}</ul></details>
    <details><summary>Artifact envelopes ({data?.artifacts.length ?? 0})</summary><ul>{data?.artifacts.map(row => <li key={row.artifact_id}>{row.artifact_id} · {row.status} · accepted r{row.accepted_revision ?? '—'} · candidate r{row.candidate_revision ?? '—'}{row.stale_reasons.map((reason, i) => <p className="warning" key={i}>{reason}</p>)}</li>)}</ul></details>
    <details><summary>Context snapshots ({data?.snapshots.length ?? 0})</summary><ul>{data?.snapshots.map(row => <li key={row.snapshot_id}>{row.snapshot_id} · chương {row.for_chapter_number} · {row.created_from_action} · {row.dependency_pins} pins</li>)}</ul></details>
    <details><summary>Operation history ({data?.history.length ?? 0})</summary><ul>{data?.history.map(row => <li key={row.operation_id}>{row.operation_id} · {row.operation_type} · {row.status} · {row.created_at} · {row.applied_count} applied / {row.before_count} before</li>)}</ul></details>
  </section>
}
