import { GenerationHttpError, postGeneration, type Frame } from './generation'

export type Query<T = Record<string, unknown>> = {
  kind: string; project_id: string | null; data: T;
  needs_recovery: boolean; read_only: boolean; write_blocked: boolean;
}
export type ApiErrorBody = {
  code: string; message: string; operation_id: string | null;
  issues: { path?: string; code?: string; message?: string }[];
  retryable: boolean; recovery_required: boolean; current_revision: number | null;
}
export type CommandResult = {
  action: string; operation_id: string; project_id: string | null;
  artifact_id: string | null; chapter_id: string | null; revision: number | null;
  status: string | null; message: string; data: Record<string, unknown>;
  warnings: string[]; validation_issues: ApiErrorBody['issues']; recovery_required: boolean;
}
export type Envelope = {
  operation_id: string; project_id?: string; params: Record<string, unknown>;
  base_ref?: string; expected_revision?: number; expected_fingerprint?: string;
  attempt?: number; stream?: boolean;
}

export class ApiError extends Error {
  constructor(public body: ApiErrorBody, public status: number) { super(body.message) }
}

async function unpack<T>(response: Response): Promise<T> {
  const body: unknown = await response.json()
  if (!response.ok) throw new ApiError(body as ApiErrorBody, response.status)
  return body as T
}

export function projectPath(projectId: string, suffix = ''): string {
  return `/api/v1/projects/${encodeURIComponent(projectId)}${suffix}`
}

export async function getQuery<T = Record<string, unknown>>(path: string): Promise<Query<T>> {
  return unpack<Query<T>>(await fetch(path, { headers: { Accept: 'application/json' } }))
}

export async function command(name: string, body: Envelope): Promise<CommandResult> {
  return unpack<CommandResult>(await fetch(`/api/v1/commands/${encodeURIComponent(name)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }))
}

export async function generation(name: string, body: Envelope, onFrame: (frame: Frame) => void): Promise<CommandResult> {
  let terminal: Frame
  try { terminal = await postGeneration(name, body, onFrame) }
  catch (caught) {
    if (caught instanceof GenerationHttpError && caught.body) throw new ApiError(caught.body as ApiErrorBody, caught.status)
    throw caught
  }
  if (terminal.event === 'error') throw new ApiError(terminal.data as ApiErrorBody, 200)
  if (terminal.event !== 'result') throw new Error('Generation không có kết quả cuối.')
  if (terminal.data.status !== 'saved') throw new Error(`Generation chưa hoàn chỉnh: ${terminal.data.status ?? 'unknown'}`)
  return terminal.data as CommandResult
}

export function newOperationId(): string { return `ui_${crypto.randomUUID().replaceAll('-', '')}` }

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const issues = error.body.issues?.map(issue => `${issue.path ?? ''}: ${issue.message ?? issue.code ?? ''}`).join('; ')
    return `${error.body.message}${issues ? ` — ${issues}` : ''}`
  }
  return error instanceof Error ? error.message : String(error)
}
