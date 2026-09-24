import { useState, type Dispatch, type SetStateAction } from 'react'
import type { CommandResult, Envelope } from './api'
import type { Frame } from './generation'

export type Workspace = 'co_create' | 'architect' | 'long_plan' | 'short_plan' | 'skeleton' | 'writer' | 'review' | 'reconcile' | 'revision'
export const workspaceLabels: Record<Workspace, string> = {
  co_create: 'Co-create', architect: 'Architect', long_plan: 'Long Plan', short_plan: 'Short Plan',
  skeleton: 'Skeleton', writer: 'Writer', review: 'Review', reconcile: 'Finalize / Reconcile', revision: 'Revision / Recovery',
}
export type TreeNode = {
  key: string; label: string; kind: string; status: string; badge: string;
  artifact_id: string | null; chapter_id: string | null; detail: string; children: TreeNode[];
}
export type ActionOptions = Partial<Omit<Envelope, 'operation_id' | 'project_id' | 'params'>> & { generation?: boolean; scope?: string }
export type Perform = (name: string, params?: Record<string, unknown>, options?: ActionOptions) => Promise<CommandResult>
export type WorkspaceProps = {
  projectId: string; epoch: number; perform: Perform; disabled: boolean;
  selectedChapter: string; setSelectedChapter: (id: string) => void;
}
export type Transcript = { scope: string; frames: Frame[]; operationId: string }

export function readDraft(key: string, fallback = ''): string {
  return sessionStorage.getItem(`input:${key}`) ?? fallback
}
export function writeDraft(key: string, value: string): void { sessionStorage.setItem(`input:${key}`, value) }
export function clearDraft(key: string): void { sessionStorage.removeItem(`input:${key}`) }

export function useSessionState<T>(key: string, initial: T): [T, Dispatch<SetStateAction<T>>] {
  const storageKey = `state:${key}`
  const [value, setValue] = useState<T>(() => {
    try { const raw = sessionStorage.getItem(storageKey); return raw === null ? initial : JSON.parse(raw) as T }
    catch { return initial }
  })
  const update: Dispatch<SetStateAction<T>> = next => setValue(old => {
    const result = typeof next === 'function' ? (next as (old: T) => T)(old) : next
    sessionStorage.setItem(storageKey, JSON.stringify(result))
    return result
  })
  return [value, update]
}

export function artifactScope(projectId: string, artifactId: string, revision: number | null, fingerprint: string): string {
  return `${projectId}:artifact:${artifactId}:${revision ?? 'new'}:${fingerprint}`
}

export function chapterScope(projectId: string, chapterId: string, revision: number | null, fingerprint: string): string {
  return `${projectId}:chapter:${chapterId}:${revision ?? 'new'}:${fingerprint}`
}
