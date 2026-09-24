import { useEffect, useRef, useState } from 'react'
import { ApiError, command, errorText, generation, newOperationId, projectPath, type CommandResult } from './api'
import { CoCreate, Architect } from './foundation'
import { Planning } from './planning'
import { Chapters } from './chapters'
import { Revision } from './revision'
import { useQuery } from './useQuery'
import { workspaceLabels, type ActionOptions, type Perform, type Transcript, type TreeNode, type Workspace } from './ui'
import type { Frame } from './generation'

type ProjectList = { projects: { project_id: string; title: string; current_chapter: number }[]; skipped_count: number }
type ProjectData = { title: string; current_chapter: number; default_pov: string; default_length_guidance: string; auto_accept_structured: boolean; fingerprint: string; pending_operation_ids: string[] }
type ArbiterData = { summary: string; report: { suggestions?: { label: string; reason: string; workspace: Workspace; chapter_id: string | null; blocking: boolean }[]; stale_artifact_ids?: string[] }; status_rows: unknown[] }
type StatusData = { chapter_label?: string; prose_revision_label?: string; llm_label?: string; config_warnings?: string[] }
const workspaces = Object.keys(workspaceLabels) as Workspace[]

function Tree({ nodes, onPick }: { nodes: TreeNode[]; onPick: (node: TreeNode) => void }) {
  return <ul className="tree">{nodes.map(node => <li key={node.key}><button className="tree-node" title={node.detail} onClick={() => onPick(node)}>
    <span>{node.label}</span><small>{node.badge}</small></button>{node.children?.length > 0 && <Tree nodes={node.children} onPick={onPick} />}</li>)}</ul>
}

function workspaceFor(node: TreeNode): Workspace {
  if (node.kind === 'chapter') return 'skeleton'
  if (node.artifact_id === 'base_idea') return 'co_create'
  if (node.artifact_id === 'long_plan') return 'long_plan'
  if (node.artifact_id === 'short_plan' || node.artifact_id?.startsWith('rolling_patch')) return 'short_plan'
  if (node.artifact_id?.startsWith('skeleton_')) return 'skeleton'
  if (node.artifact_id?.startsWith('reconciliation_')) return 'reconcile'
  return 'architect'
}

function transcriptLabel(frame: Frame): string {
  if (frame.event === 'generation') return `${frame.data.status}${frame.data.detail ? ` · ${frame.data.detail}` : ''}`
  if (frame.event === 'error') return `Lỗi: ${frame.data.message ?? frame.data.code}`
  return `Kết quả: ${frame.data.status ?? ''}`
}

export default function App() {
  const [projectId, setProjectId] = useState(() => sessionStorage.getItem('selectedProject') ?? '')
  const [workspace, setWorkspace] = useState<Workspace>(() => (sessionStorage.getItem('selectedWorkspace') as Workspace) || 'co_create')
  const [selectedChapter, setSelectedChapter] = useState(() => sessionStorage.getItem('selectedChapter') ?? '')
  const [epoch, setEpoch] = useState(0)
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [errorCode, setErrorCode] = useState('')
  const [notice, setNotice] = useState('')
  const [transcript, setTranscript] = useState<Transcript | null>(null)
  const [arbiterOpen, setArbiterOpen] = useState(false)
  const errorRef = useRef<HTMLDivElement>(null)
  const list = useQuery<ProjectList>('/api/v1/projects', epoch)
  const project = useQuery<ProjectData>(projectId ? projectPath(projectId) : null, epoch)
  const tree = useQuery<{ nodes: TreeNode[] }>(projectId ? projectPath(projectId, '/tree') : null, epoch)
  const arbiter = useQuery<ArbiterData>(projectId ? projectPath(projectId, '/arbiter') : null, epoch)
  const status = useQuery<StatusData>(projectId ? projectPath(projectId, `/status?workspace=${workspace}${selectedChapter ? `&chapter_id=${encodeURIComponent(selectedChapter)}` : ''}`) : null, epoch)
  const disabled = busy || Boolean(project.value?.write_blocked)
  useEffect(() => { if (error) errorRef.current?.focus() }, [error])
  useEffect(() => {
    const key = sessionStorage.getItem(`lastTranscript:${projectId}:${workspace}`)
    if (!key) { setTranscript(null); return }
    try { setTranscript(JSON.parse(sessionStorage.getItem(`transcript:${key}`) ?? 'null') as Transcript | null) }
    catch { setTranscript(null) }
  }, [projectId, workspace])
  function refresh() { setEpoch(value => value + 1) }
  function selectProject(id: string) { setProjectId(id); sessionStorage.setItem('selectedProject', id); setSelectedChapter(''); sessionStorage.removeItem('selectedChapter'); setTranscript(null); setError(''); setNotice('') }
  function selectWorkspace(next: Workspace) { setWorkspace(next); sessionStorage.setItem('selectedWorkspace', next) }
  function selectChapter(id: string) { setSelectedChapter(id); sessionStorage.setItem('selectedChapter', id) }
  const perform: Perform = async (name, params = {}, options: ActionOptions = {}): Promise<CommandResult> => {
    setBusy(true); setError(''); setErrorCode(''); setNotice('')
    const operation_id = newOperationId()
    const body = { operation_id, project_id: projectId || undefined, params, ...options }
    delete (body as Record<string, unknown>).generation
    delete (body as Record<string, unknown>).scope
    try {
      let result: CommandResult
      if (options.generation) {
        const scope = options.scope ?? `${projectId}:${workspace}:${name}`
        sessionStorage.setItem(`lastTranscript:${projectId}:${workspace}`, scope)
        const frames: Frame[] = []
        setTranscript({ scope, frames: [], operationId: operation_id })
        result = await generation(name, { ...body, stream: options.stream ?? true }, frame => {
          frames.push(frame)
          const next = { scope, frames: [...frames], operationId: operation_id }
          setTranscript(next)
          sessionStorage.setItem(`transcript:${scope}`, JSON.stringify(next))
        })
      } else result = await command(name, body)
      setNotice((result.message || `${name}: ${result.status ?? 'hoàn tất'}`).replaceAll('**', '').replaceAll('`', ''))
      refresh()
      return result
    } catch (caught) { setError(errorText(caught)); setErrorCode(caught instanceof ApiError ? caught.body.code : ''); throw caught }
    finally { setBusy(false) }
  }
  async function createProject() {
    if (!title.trim()) return
    try {
      const result = await perform('create_project', { title: title.trim() })
      if (result.project_id) selectProject(result.project_id)
      setTitle('')
    } catch { /* displayed by perform */ }
  }
  const blocker = project.value?.write_blocked || Boolean(arbiter.value?.data.report.suggestions?.some(item => item.blocking)) || Boolean(arbiter.value?.data.report.stale_artifact_ids?.length)
  const props = { projectId, epoch, perform, disabled, selectedChapter, setSelectedChapter: selectChapter }
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark">✦</span><div><strong>Manual AI Novel</strong><small>Potato, but effective.</small></div></div>
      <label className="field">Project<select value={projectId} onChange={event => selectProject(event.target.value)}><option value="">Chọn project</option>{list.value?.data.projects.map(item => <option value={item.project_id} key={item.project_id}>{item.title}</option>)}</select></label>
      <div className="create"><input aria-label="Tên project mới" placeholder="Tên project mới" value={title} onChange={event => setTitle(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void createProject() }} /><button disabled={busy || !title.trim()} onClick={createProject}>Tạo</button></div>
      {list.error && <p className="error" role="alert">{list.error}</p>}
      <nav aria-label="Workspace">{workspaces.map(item => <button className={workspace === item ? 'active' : ''} key={item} aria-current={workspace === item ? 'page' : undefined} onClick={() => selectWorkspace(item)}>{workspaceLabels[item]}</button>)}</nav>
      {projectId && <details className="tree-wrap" open><summary>Project tree</summary>{tree.value?.data.nodes && <Tree nodes={tree.value.data.nodes} onPick={node => { if (node.chapter_id) selectChapter(node.chapter_id); selectWorkspace(workspaceFor(node)) }} />}{tree.error && <p className="error">{tree.error}</p>}</details>}
    </aside>
    <main className="main"><div className="topbar"><div><span className="eyebrow">{project.value?.data.title ?? 'Chọn project'}</span><h1>{workspaceLabels[workspace]}</h1></div><div className="row"><span className={`status-pill ${blocker ? 'blocked' : ''}`}>{blocker ? 'Có blocker' : project.value?.data.auto_accept_structured ? 'Auto Accept bật' : 'Manual Accept'}</span>{projectId && <button className="subtle" disabled={busy} onClick={() => void perform('probe_llm').catch(() => undefined)}>Kiểm tra LLM</button>}<button className="subtle" onClick={refresh}>Làm mới</button></div></div>
      {project.error && <p className="error" role="alert">{project.error}</p>}
      {project.value?.write_blocked && <div className="banner danger-banner" role="alert">Project cần recovery hoặc đang read-only. Các lệnh ghi bị khóa. Pending: {project.value.data.pending_operation_ids?.join(', ') || 'cần kiểm tra'}. <button onClick={() => { selectWorkspace('revision') }}>Mở Recovery</button></div>}
      {blocker && !project.value?.write_blocked && <div className="banner warning" role="status">Có blocker{arbiter.value?.data.report.stale_artifact_ids?.length ? `: ${arbiter.value.data.report.stale_artifact_ids.join(', ')}` : ''}. <button className="subtle" onClick={() => selectWorkspace('revision')}>Mở Revision</button></div>}
      {error && <div ref={errorRef} tabIndex={-1} className="banner danger-banner" role="alert">{error} {['stale_candidate', 'stale_dependency', 'operation_conflict'].includes(errorCode) && <button className="subtle" onClick={refresh}>Tải lại state để so sánh</button>}{errorCode === 'recovery_required' && <button className="subtle" onClick={() => selectWorkspace('revision')}>Mở Recovery</button>}</div>}{notice && <div className="banner success" role="status">{notice}</div>}
      {projectId ? <>
        {workspace === 'co_create' && <CoCreate key={projectId} {...props} />}
        {workspace === 'architect' && <Architect key={projectId} {...props} />}
        {(workspace === 'long_plan' || workspace === 'short_plan') && <Planning key={`${projectId}:${workspace}`} {...props} workspace={workspace} project={project.value?.data ?? null} />}
        {(workspace === 'skeleton' || workspace === 'writer' || workspace === 'review' || workspace === 'reconcile') && <Chapters key={`${projectId}:${workspace}`} {...props} workspace={workspace} tree={tree.value?.data.nodes ?? []} />}
        {workspace === 'revision' && <Revision key={projectId} {...props} readOnly={Boolean(project.value?.read_only)} needsRecovery={Boolean(project.value?.needs_recovery)} onNavigate={(next, chapterId) => { selectChapter(chapterId); selectWorkspace(next) }} />}
      </> : <section className="empty"><h2>Bắt đầu với một project</h2><p>Chọn project hiện có hoặc tạo project mới trong thanh bên.</p></section>}
      {transcript && <details className="card transcript" open><summary>Generation · {transcript.operationId}</summary><p className="hint">Preview raw chỉ để theo dõi; candidate hoàn chỉnh phải có saved + result.</p>
        <ol>{transcript.frames.filter(frame => frame.event !== 'generation' || frame.data.status !== 'streaming').map((frame, index) => <li key={index}>{transcriptLabel(frame)}</li>)}</ol>
        <pre>{transcript.frames.filter(frame => frame.event === 'generation').map(frame => frame.event === 'generation' ? frame.data.text_delta : '').join('')}</pre></details>}
      {projectId && <section className="status-panel"><button className="subtle" aria-expanded={arbiterOpen} onClick={() => setArbiterOpen(value => !value)}>Arbiter {arbiterOpen ? '▴' : '▾'}</button><span>{arbiter.value?.data.summary}</span>
        {arbiterOpen && <div className="card"><h3>Gợi ý theo state</h3><ul className="suggestions">{arbiter.value?.data.report.suggestions?.map((item, index) => <li key={index}><div><strong>{item.label}</strong>{item.blocking && <span className="badge">blocker</span>}<p>{item.reason}</p></div><button className="subtle" onClick={() => { if (item.chapter_id) selectChapter(item.chapter_id); selectWorkspace(item.workspace) }}>Mở</button></li>)}</ul>
          {arbiter.value?.data.report.stale_artifact_ids?.length ? <p className="warning">Stale: {arbiter.value.data.report.stale_artifact_ids.join(', ')}</p> : null}</div>}</section>}
      {projectId && <footer className="statusbar"><span>{status.value?.data.chapter_label || 'Chưa chọn chương'}</span><span>{status.value?.data.prose_revision_label || ''}</span><span>{status.value?.data.llm_label || ''}</span>{status.value?.data.config_warnings?.map((item, index) => <span className="warning" key={index}>{item}</span>)}</footer>}
    </main>
  </div>
}
