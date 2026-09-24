import { useState } from 'react'
import { projectPath } from './api'
import { JsonEditor } from './editor'
import { useQuery } from './useQuery'
import { chapterScope, clearDraft, readDraft, useSessionState, writeDraft, type TreeNode, type WorkspaceProps } from './ui'

type ChapterData = {
  chapter_id: string; chapter_number: number; title: string; status: string; fingerprint: string;
  current_draft_revision: number | null; draft_complete: boolean | null; draft_text: string;
  human_review: { prose_revision: number; valid_for_current_revision: boolean; notes: string } | null;
  final_candidate: { prose_revision: number; reconciliation_status: string } | null;
  final_revision: number | null; final_text: string; ai_review_reports: { artifact_id: string; revision: number }[];
  retcon_open: boolean;
}
type ArtifactData = {
  status: string; fingerprint: string; accepted: { revision: number; payload: Record<string, unknown> } | null;
  candidate: { revision: number; payload: Record<string, unknown>; validation_state: string } | null;
  stale_reasons: { reason: string }[];
}
type Props = WorkspaceProps & { workspace: 'skeleton' | 'writer' | 'review' | 'reconcile'; tree: TreeNode[] }

function chapterNodes(nodes: TreeNode[]): { id: string; label: string; status: string }[] {
  const result: { id: string; label: string; status: string }[] = []
  for (const node of nodes) {
    if (node.kind === 'chapter' && node.chapter_id && !result.some(item => item.id === node.chapter_id)) result.push({ id: node.chapter_id, label: node.label, status: node.status })
    result.push(...chapterNodes(node.children ?? []).filter(item => !result.some(old => old.id === item.id)))
  }
  return result
}

export function Chapters(props: Props) {
  const chapters = chapterNodes(props.tree)
  const selected = chapters.some(row => row.id === props.selectedChapter) ? props.selectedChapter : chapters[0]?.id ?? ''
  const chapter = useQuery<ChapterData>(selected ? projectPath(props.projectId, `/chapters/${selected}`) : null, props.epoch)
  return <div className="workspace"><header><h2>{props.workspace === 'reconcile' ? 'Finalize / Reconcile' : props.workspace}</h2><p>Chương tiếp theo chỉ mở sau khi chương trước final_reconciled.</p></header>
    {chapters.length ? <label className="field">Chương<select value={selected} onChange={event => props.setSelectedChapter(event.target.value)}>{chapters.map(row => <option key={row.id} value={row.id}>{row.label} · {row.status}</option>)}</select></label> : <div className="card">Chưa có chapter. Accept Short Plan để tạo metadata chương.</div>}
    {chapter.error && <p role="alert" className="error">{chapter.error}</p>}
    {chapter.value?.data && <><div className="chapter-head"><span className="badge">{chapter.value.data.status}</span><strong>Chương {chapter.value.data.chapter_number} · {chapter.value.data.title}</strong><span>prose r{chapter.value.data.current_draft_revision ?? '—'}</span></div>
      {props.workspace === 'skeleton' && <Skeleton key={selected} {...props} chapterId={selected} />}
      {props.workspace === 'writer' && <Writer key={selected} {...props} chapter={chapter.value.data} />}
      {props.workspace === 'review' && <Review key={selected} {...props} chapter={chapter.value.data} />}
      {props.workspace === 'reconcile' && <Reconcile key={selected} {...props} chapter={chapter.value.data} />}
    </>}
  </div>
}

function Skeleton({ projectId, chapterId, epoch, perform, disabled }: Props & { chapterId: string }) {
  const id = `skeleton_${chapterId}`
  const artifact = useQuery<ArtifactData>(projectPath(projectId, `/artifacts/${id}`), epoch)
  const [action, setAction] = useSessionState(`${projectId}:skeleton:${chapterId}:action`, 'generate')
  const [instruction, setInstruction] = useSessionState(`${projectId}:skeleton:${chapterId}:instruction`, '')
  const [assigned, setAssigned] = useState<string[]>([])
  const value = artifact.value?.data
  return <><div className="card"><h3>Skeleton · {value?.status ?? 'missing'}</h3>
    {value?.stale_reasons?.map((item, i) => <p className="warning" key={i}>{item.reason}</p>)}
    <label className="field">Action<select value={action} onChange={event => setAction(event.target.value)}><option>generate</option><option>regenerate</option><option>edit</option></select></label>
    <label className="field">Yêu cầu thêm<textarea value={instruction} onChange={event => setInstruction(event.target.value)} /></label>
    <div className="row"><button className="subtle" disabled={disabled} onClick={() => void perform('reserve_section_ids', { chapter_id: chapterId, count: 5 }).then(result => setAssigned(result.data.value as string[])).catch(() => undefined)}>Reserve Section ID</button><code>{assigned.join(', ')}</code></div>
    <button disabled={disabled} onClick={() => void perform('generate_skeleton', { chapter_id: chapterId, action, user_instruction: instruction }, { generation: true, scope: `${projectId}:skeleton:${chapterId}` }).catch(() => undefined)}>Generate Skeleton</button></div>
    {value?.candidate && <div className="card"><h3>Candidate r{value.candidate.revision}</h3><p>{value.candidate.validation_state}</p>
      <JsonEditor key={`${projectId}:${id}:${value.fingerprint}`} scope={`${projectId}:${id}:${value.fingerprint}`} payload={value.candidate.payload} disabled={disabled}
        onSave={payload => perform('edit_skeleton_candidate', { chapter_id: chapterId, payload }, { base_ref: `artifact:${id}`, expected_revision: value.candidate?.revision, expected_fingerprint: value.fingerprint }).then(() => undefined)} />
      <div className="row"><button disabled={disabled} onClick={() => void perform('accept_skeleton', { chapter_id: chapterId }).catch(() => undefined)}>Accept Skeleton</button><button className="danger" disabled={disabled} onClick={() => void perform('reject_skeleton', { chapter_id: chapterId }).catch(() => undefined)}>Reject</button></div></div>}
    {value?.accepted && <details className="card"><summary>Accepted r{value.accepted.revision}</summary><pre className="json-view">{JSON.stringify(value.accepted.payload, null, 2)}</pre></details>}
  </>
}

function Writer({ projectId, chapter, epoch, perform, disabled }: Props & { chapter: ChapterData }) {
  const key = chapterScope(projectId, chapter.chapter_id, chapter.current_draft_revision, chapter.fingerprint)
  const [text, setText] = useState<string | null>(null)
  const [complete, setComplete] = useSessionState(`${projectId}:writer:${chapter.chapter_id}:complete`, true)
  const [mode, setMode] = useSessionState<'generate' | 'regenerate' | 'continue'>(`${projectId}:writer:${chapter.chapter_id}:mode`, 'generate')
  const operation = useQuery<{ operation: Record<string, unknown> | null }>(projectPath(projectId, `/chapters/${chapter.chapter_id}/latest-writer-operation`), epoch)
  const current = text ?? readDraft(key, chapter.draft_text ?? '')
  return <><div className="card"><h3>Writer draft</h3><p className="hint">Writer dùng Skeleton accepted/fresh và state actual. Backend chặn nếu chapter trước chưa final_reconciled.</p>
    <div className="row"><label className="field">Mode<select value={mode} onChange={event => setMode(event.target.value as typeof mode)}><option>generate</option><option>regenerate</option><option>continue</option></select></label>
      <button disabled={disabled} onClick={() => void perform('write_draft', { chapter_id: chapter.chapter_id, mode }, { generation: true, scope: `${projectId}:writer:${chapter.chapter_id}` }).catch(() => undefined)}>Chạy Writer</button></div>
    {operation.value?.data.operation && <p className="hint">Operation gần nhất: {String(operation.value.data.operation.stream_status ?? '')} · {String(operation.value.data.operation.operation_id ?? '')}</p>}
    <label className="field">Working copy prose<textarea className="prose-editor" value={current} onChange={event => { setText(event.target.value); writeDraft(key, event.target.value) }} rows={22} /></label>
    <label className="check"><input type="checkbox" checked={complete} onChange={event => setComplete(event.target.checked)} />Bản này đã hoàn chỉnh</label>
    <div className="row"><button disabled={disabled || !current.trim()} onClick={() => void perform('save_draft', { chapter_id: chapter.chapter_id, text: current, source_type: 'user', is_complete: complete }, { base_ref: `chapter:${chapter.chapter_id}`, expected_revision: chapter.current_draft_revision ?? undefined, expected_fingerprint: chapter.fingerprint }).then(() => { setText(null); clearDraft(key) }).catch(() => undefined)}>Save draft</button>
      <button className="danger" disabled={disabled || !chapter.current_draft_revision} onClick={() => void perform('discard_draft', { chapter_id: chapter.chapter_id, revision: chapter.current_draft_revision }).catch(() => undefined)}>Discard current draft</button></div>
    <p className="hint">Draft hiện tại: {chapter.draft_complete ? 'complete' : 'partial/chưa complete'} · r{chapter.current_draft_revision ?? '—'}. Partial không thể Review/Finalize.</p>
  </div></>
}

function Review({ projectId, chapter, epoch, perform, disabled }: Props & { chapter: ChapterData }) {
  const [notes, setNotes] = useSessionState(`${projectId}:review:${chapter.chapter_id}:notes`, '')
  const [focus, setFocus] = useSessionState(`${projectId}:review:${chapter.chapter_id}:focus`, '')
  const [selectedText, setSelectedText] = useSessionState(`${projectId}:review:${chapter.chapter_id}:selected`, '')
  const [instruction, setInstruction] = useSessionState(`${projectId}:review:${chapter.chapter_id}:instruction`, '')
  const [replacement, setReplacement] = useSessionState(`${projectId}:review:${chapter.chapter_id}:replacement`, '')
  const reportId = `review_report_${chapter.chapter_id}`
  const report = useQuery<ArtifactData>(projectPath(projectId, `/artifacts/${reportId}`), epoch)
  return <><div className="card"><h3>Human Review</h3><p>Prose r{chapter.current_draft_revision ?? '—'} · {chapter.draft_complete ? 'complete' : 'partial'}</p>
    {chapter.human_review && <p className={chapter.human_review.valid_for_current_revision && chapter.human_review.prose_revision === chapter.current_draft_revision ? 'success-text' : 'warning'}>Review r{chapter.human_review.prose_revision} · {chapter.human_review.valid_for_current_revision ? 'còn hiệu lực' : 'hết hiệu lực'}</p>}
    <label className="field">Ghi chú review<textarea value={notes} onChange={event => setNotes(event.target.value)} /></label>
    <button disabled={disabled || !chapter.draft_complete || chapter.retcon_open} onClick={() => void perform('mark_reviewed', { chapter_id: chapter.chapter_id, notes }).catch(() => undefined)}>Đánh dấu đã review</button>
    {chapter.retcon_open && <p className="warning">Retcon đang mở. Đọc bản draft retcon, rồi xác nhận review trong action Finalize; final cũ vẫn canon cho tới khi commit.</p>}
    <details><summary>Xem prose hiện tại</summary><pre className="prose-preview">{chapter.draft_text}</pre></details></div>
    <div className="card"><h3>AI Review</h3><label className="field">Trọng tâm review<textarea value={focus} onChange={event => setFocus(event.target.value)} /></label>
      <button disabled={disabled || !chapter.draft_complete} onClick={() => void perform('run_ai_review', { chapter_id: chapter.chapter_id, review_focus: focus }, { generation: true, scope: `${projectId}:review:${chapter.chapter_id}` }).catch(() => undefined)}>Chạy AI Review</button>
      {report.value?.data.candidate && <pre className="json-view">{JSON.stringify(report.value.data.candidate.payload, null, 2)}</pre>}
      {report.value?.data.accepted && <pre className="json-view">{JSON.stringify(report.value.data.accepted.payload, null, 2)}</pre>}
    </div>
    <div className="card"><h3>Rewrite section</h3><p className="hint">Chọn đúng đoạn text trên prose revision hiện tại; replacement chỉ áp dụng khi bạn bấm Apply.</p>
      <label className="field">Selected text<textarea value={selectedText} onChange={event => setSelectedText(event.target.value)} rows={5} /></label>
      <label className="field">Chỉ dẫn rewrite<textarea value={instruction} onChange={event => setInstruction(event.target.value)} /></label>
      <button disabled={disabled || !chapter.draft_complete || !selectedText.trim() || !instruction.trim()} onClick={() => void perform('rewrite_section', { request: { chapter_id: chapter.chapter_id, prose_revision: chapter.current_draft_revision, selected_text: selectedText, instruction } }, { generation: true, scope: `${projectId}:rewrite:${chapter.chapter_id}` }).then(result => setReplacement(String((result.data.payload as { replacement_markdown?: string } | undefined)?.replacement_markdown ?? result.data.replacement_markdown ?? ''))).catch(() => undefined)}>Đề xuất rewrite</button>
      <label className="field">Replacement working copy<textarea value={replacement} onChange={event => setReplacement(event.target.value)} rows={8} /></label>
      <button disabled={disabled || !replacement.trim()} onClick={() => void perform('apply_rewrite', { chapter_id: chapter.chapter_id, replacement_markdown: replacement, target_text: selectedText }, { base_ref: `chapter:${chapter.chapter_id}`, expected_revision: chapter.current_draft_revision ?? undefined, expected_fingerprint: chapter.fingerprint }).catch(() => undefined)}>Apply rewrite</button>
    </div>
  </>
}

function Reconcile({ projectId, chapter, epoch, perform, disabled }: Props & { chapter: ChapterData }) {
  const id = `reconciliation_${chapter.chapter_id}`
  const artifact = useQuery<ArtifactData>(projectPath(projectId, `/artifacts/${id}`), epoch)
  const data = artifact.value?.data
  const [retconReviewed, setRetconReviewed] = useState(false)
  return <><div className="card"><h3>Finalize chapter</h3><p className="hint">Human Review cho đúng prose revision là gate bắt buộc. Finalize đóng băng final candidate; chapter tiếp theo vẫn khóa đến khi Reconcile commit.</p>
    <p>Trạng thái: {chapter.status} · Human Review r{chapter.human_review?.prose_revision ?? '—'}</p>
    {chapter.final_candidate && <p>Final candidate prose r{chapter.final_candidate.prose_revision} · Reconciliation {chapter.final_candidate.reconciliation_status}</p>}
    {chapter.retcon_open && <label className="check"><input type="checkbox" checked={retconReviewed} onChange={event => setRetconReviewed(event.target.checked)} />Tôi đã đọc và xác nhận bản draft retcon r{chapter.current_draft_revision}.</label>}
    <button disabled={disabled || !chapter.draft_complete || chapter.status === 'finalizing' || (chapter.status === 'final_reconciled' && !chapter.retcon_open) || (chapter.retcon_open ? !retconReviewed : !chapter.human_review?.valid_for_current_revision || chapter.human_review.prose_revision !== chapter.current_draft_revision)} onClick={() => void perform('finalize_chapter', { chapter_id: chapter.chapter_id, confirm_review: true }).then(() => setRetconReviewed(false)).catch(() => undefined)}>{chapter.retcon_open ? 'Finalize Retcon' : 'Finalize Chapter'}</button>
    {chapter.status === 'finalizing' && <button className="subtle" disabled={disabled} onClick={() => void perform('cancel_finalizing', { chapter_id: chapter.chapter_id }).catch(() => undefined)}>Cancel finalizing</button>}
    {chapter.final_text && <details><summary>Final Manuscript</summary><pre className="prose-preview">{chapter.final_text}</pre></details>}
  </div>
    {chapter.status === 'finalizing' && <div className="card"><h3>Reconciliation · {data?.status ?? 'missing'}</h3>
      <div className="row"><button disabled={disabled} onClick={() => void perform('generate_reconciliation', { chapter_id: chapter.chapter_id }, { generation: true, scope: `${projectId}:reconcile:${chapter.chapter_id}` }).catch(() => undefined)}>Generate proposal</button>
        <button className="subtle" disabled={disabled} onClick={() => void perform('retry_reconcile', { chapter_id: chapter.chapter_id }, { generation: true, scope: `${projectId}:reconcile:${chapter.chapter_id}` }).catch(() => undefined)}>Retry</button></div>
      {data?.candidate && <><p>Candidate r{data.candidate.revision} · {data.candidate.validation_state}</p>
        <JsonEditor key={`${projectId}:${id}:${data.fingerprint}`} scope={`${projectId}:${id}:${data.fingerprint}`} payload={data.candidate.payload} disabled={disabled}
          onSave={payload => perform('edit_reconciliation_candidate', { chapter_id: chapter.chapter_id, payload }, { base_ref: `artifact:${id}`, expected_revision: data.candidate?.revision, expected_fingerprint: data.fingerprint }).then(() => undefined)} />
        <div className="row"><button disabled={disabled} onClick={() => void perform('accept_reconciliation', { chapter_id: chapter.chapter_id, expect_chapter_number: chapter.chapter_number }).catch(() => undefined)}>Accept & commit state</button><button className="danger" disabled={disabled} onClick={() => void perform('reject_reconciliation', { chapter_id: chapter.chapter_id }).catch(() => undefined)}>Reject</button></div></>}
    </div>}
  </>
}
