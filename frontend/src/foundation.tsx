import { useState } from 'react'
import { errorText, projectPath } from './api'
import { JsonEditor } from './editor'
import { useQuery } from './useQuery'
import { artifactScope, clearDraft, readDraft, writeDraft, type WorkspaceProps } from './ui'

type IdeaState = { genre: string; core_concept: string; tone: string; protagonist: string; setting: string; conflict: string; constraints: string[]; open_questions: string[] }
type FoundationData = {
  co_create: { status: string; idea_state: IdeaState | null; messages: { role: string; content: string }[] };
  base_idea_markdown: string; base_idea_metadata: { status: string; revision: number } | null;
}
type ArtifactData = {
  artifact_id: string; status: string; fingerprint: string; accepted: { revision: number; payload: Record<string, unknown> } | null;
  candidate: { revision: number; payload: Record<string, unknown>; validation_state: string } | null;
  stale_reasons: { reason: string }[];
}
const artifactTypes = ['premise', 'characters', 'world_rules', 'foreshadow'] as const
const collectionByType: Record<(typeof artifactTypes)[number], string> = {
  premise: '', characters: 'characters', world_rules: 'world_rules', foreshadow: 'foreshadows',
}
const blankIdea: IdeaState = { genre: '', core_concept: '', tone: '', protagonist: '', setting: '', conflict: '', constraints: [], open_questions: [] }

function IdeaFields({ value, onChange }: { value: IdeaState; onChange: (value: IdeaState) => void }) {
  return <div className="field-grid">{(['genre', 'core_concept', 'tone', 'protagonist', 'setting', 'conflict'] as const).map(name =>
    <label className="field" key={name}>{name}<textarea value={value[name]} onChange={event => onChange({ ...value, [name]: event.target.value })} rows={name === 'core_concept' ? 3 : 2} /></label>)}
    {(['constraints', 'open_questions'] as const).map(name => <label className="field" key={name}>{name} · mỗi dòng một ý
      <textarea value={value[name].join('\n')} onChange={event => onChange({ ...value, [name]: event.target.value.split('\n').map(s => s.trim()).filter(Boolean) })} rows={4} /></label>)}
  </div>
}

export function CoCreate({ projectId, epoch, perform, disabled }: WorkspaceProps) {
  const { value, error, loading } = useQuery<FoundationData>(projectPath(projectId, '/foundation'), epoch)
  const saved = value?.data
  const [message, setMessage] = useState(() => readDraft(`${projectId}:co_create:message`))
  const [markdown, setMarkdown] = useState(() => readDraft(`${projectId}:co_create:markdown`))
  const [idea, setIdea] = useState<IdeaState | null>(null)
  const [busy, setBusy] = useState(false)
  const ideaKey = `${projectId}:co_create:idea`
  const currentIdea = idea ?? (() => { try { return JSON.parse(readDraft(ideaKey, '')) as IdeaState } catch { return saved?.co_create.idea_state ?? blankIdea } })()
  async function run(action: () => Promise<unknown>, after?: () => void) { setBusy(true); try { await action(); after?.() } catch { /* perform renders the error */ } finally { setBusy(false) } }
  return <div className="workspace"><header><h2>Co-create</h2><p>Working state chưa là canon. Finalize Idea mới chốt Base Idea.</p></header>
    {error && <p role="alert" className="error">{error}</p>}{loading && !saved && <p>Đang tải…</p>}
    <div className="card"><div className="row"><h3>Hội thoại</h3><span className="badge">{saved?.co_create.status ?? 'working'}</span></div>
      <div className="messages">{saved?.co_create.messages?.length ? saved.co_create.messages.map((item, index) =>
        <p key={index}><strong>{item.role === 'assistant' ? 'AI' : 'Bạn'}:</strong> {item.content}</p>) : <p className="hint">Chưa có hội thoại.</p>}</div>
      <label className="field">Tin nhắn<textarea value={message} rows={3} onChange={event => { setMessage(event.target.value); writeDraft(`${projectId}:co_create:message`, event.target.value) }} /></label>
      <button disabled={disabled || busy || !message.trim()} onClick={() => run(() => perform('co_create_turn', { user_message: message.trim() }, { generation: true, scope: `${projectId}:co_create` }), () => { setMessage(''); clearDraft(`${projectId}:co_create:message`) })}>Gửi lượt</button>
    </div>
    <div className="card"><h3>Working idea state</h3><IdeaFields value={currentIdea} onChange={next => { setIdea(next); writeDraft(ideaKey, JSON.stringify(next)) }} />
      <div className="row"><button disabled={disabled || busy} onClick={() => run(() => perform('save_idea_state', { idea_state: currentIdea }), () => { setIdea(null); clearDraft(ideaKey) })}>Lưu working state</button>
        <button className="subtle" onClick={() => { setIdea(saved?.co_create.idea_state ?? blankIdea); clearDraft(ideaKey) }}>Nạp lại</button></div>
    </div>
    <div className="card"><h3>Finalize Base Idea</h3><label className="field">Markdown tự viết (để trống để dùng working state)
      <textarea value={markdown} rows={7} onChange={event => { setMarkdown(event.target.value); writeDraft(`${projectId}:co_create:markdown`, event.target.value) }} /></label>
      <button disabled={disabled || busy} onClick={() => run(() => perform('finalize_base_idea', { markdown: markdown.trim() || null }), () => { setMarkdown(''); clearDraft(`${projectId}:co_create:markdown`) })}>Finalize Idea</button>
      {saved?.base_idea_metadata && <p className="hint">{saved.base_idea_metadata.status} · r{saved.base_idea_metadata.revision}</p>}
      {saved?.base_idea_markdown && <pre className="prose-preview">{saved.base_idea_markdown}</pre>}
    </div>
  </div>
}

export function Architect({ projectId, epoch, perform, disabled }: WorkspaceProps) {
  const [artifactType, setArtifactType] = useState<(typeof artifactTypes)[number]>(() => (sessionStorage.getItem(`${projectId}:architect:selected`) as (typeof artifactTypes)[number]) || 'premise')
  const [action, setAction] = useState('generate')
  const [instruction, setInstruction] = useState('')
  const [chapterNumber, setChapterNumber] = useState(1)
  const [assigned, setAssigned] = useState<string[]>([])
  const [append, setAppend] = useState(() => JSON.stringify({ [collectionByType[artifactType] || 'characters']: [] }, null, 2))
  const [busy, setBusy] = useState(false)
  const [localError, setLocalError] = useState('')
  const { value, error } = useQuery<ArtifactData>(projectPath(projectId, `/artifacts/${artifactType}`), epoch)
  const artifact = value?.data
  async function run(fn: () => Promise<unknown>) { setBusy(true); setLocalError(''); try { await fn() } catch (caught) { setLocalError(errorText(caught)) } finally { setBusy(false) } }
  const candidate = artifact?.candidate
  return <div className="workspace"><header><h2>Architect</h2><p>Premise, nhân vật, world rules và foreshadow. AI tạo candidate; bạn quyết định Accept.</p></header>
    <div className="tabs" role="tablist" aria-label="Architect artifact">{artifactTypes.map(type => <button role="tab" aria-selected={artifactType === type} key={type} onClick={() => { setArtifactType(type); sessionStorage.setItem(`${projectId}:architect:selected`, type); setAssigned([]); setAppend(JSON.stringify({ [collectionByType[type]]: [] }, null, 2)) }}>{type}</button>)}</div>
    {(error || localError) && <p className="error" role="alert">{error || localError}</p>}
    <div className="card"><div className="row"><h3>{artifactType}</h3><span className="badge">{artifact?.status ?? 'missing'}</span></div>
      {artifact?.stale_reasons?.map((item, index) => <p className="warning" key={index}>{item.reason}</p>)}
      <div className="field-grid"><label className="field">Action<select value={action} onChange={event => setAction(event.target.value)}><option>generate</option><option>regenerate</option><option>edit</option></select></label>
      <label className="field">Hiệu lực từ chương<input type="number" min={1} value={chapterNumber} onChange={event => setChapterNumber(Number(event.target.value))} /></label></div>
      <label className="field">Yêu cầu thêm<textarea value={instruction} onChange={event => setInstruction(event.target.value)} /></label>
      {artifactType !== 'premise' && <div className="row"><button className="subtle" disabled={disabled || busy} onClick={() => run(async () => { const result = await perform('reserve_foundation_ids', { artifact_type: artifactType, count: 3 }); setAssigned(result.data.value as string[]) })}>Reserve 3 ID</button><code>{assigned.join(', ')}</code></div>}
      <button disabled={disabled || busy} onClick={() => run(() => perform('generate_foundation', { artifact_type: artifactType, action, chapter_number: chapterNumber, assigned_ids: assigned.length ? assigned : null, user_instruction: instruction }, { generation: true, scope: `${projectId}:architect:${artifactType}` }))}>Generate candidate</button>
    </div>
    {candidate && <div className="card"><div className="row"><h3>Candidate r{candidate.revision}</h3><span className="badge">{candidate.validation_state}</span></div>
      <JsonEditor key={artifactScope(projectId, artifactType, candidate.revision, artifact?.fingerprint ?? '')} scope={artifactScope(projectId, artifactType, candidate.revision, artifact?.fingerprint ?? '')} payload={candidate.payload}
        disabled={disabled || busy} onSave={payload => perform('edit_foundation_candidate', { artifact_type: artifactType, payload }, { base_ref: `artifact:${artifactType}`, expected_revision: candidate.revision, expected_fingerprint: artifact?.fingerprint }).then(() => undefined)} />
      <div className="row"><button disabled={disabled || busy} onClick={() => run(() => perform('accept_foundation', { artifact_type: artifactType }))}>Accept</button><button className="danger" disabled={disabled || busy} onClick={() => run(() => perform('reject_foundation', { artifact_type: artifactType }))}>Reject</button></div>
    </div>}
    {artifact?.accepted && <div className="card"><h3>Accepted r{artifact.accepted.revision}</h3><pre className="json-view">{JSON.stringify(artifact.accepted.payload, null, 2)}</pre></div>}
    {artifactType !== 'premise' && artifact?.accepted && <div className="card"><h3>Append entry</h3><p className="hint">Entry mới phải có effective_from_chapter. ID do backend cấp.</p>
      <label className="field">Payload JSON<textarea className="raw-editor" value={append} onChange={event => setAppend(event.target.value)} rows={8} /></label>
      <button disabled={disabled || busy} onClick={() => run(() => perform('append_foundation_entries', { artifact_type: artifactType, payload: JSON.parse(append), effective_from_chapter: chapterNumber }))}>Append</button>
    </div>}
  </div>
}
