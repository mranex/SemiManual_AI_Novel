import { useEffect, useState } from 'react'
import { errorText, getQuery, type Query } from './api'

export function useQuery<T = Record<string, unknown>>(path: string | null, epoch: number): { value: Query<T> | null; error: string; loading: boolean } {
  const [result, setResult] = useState<{ path: string; value: Query<T> } | null>(null)
  const [failure, setFailure] = useState<{ path: string; error: string } | null>(null)
  const [loadingPath, setLoadingPath] = useState<string | null>(null)
  useEffect(() => {
    if (!path) return
    let active = true
    setLoadingPath(path)
    getQuery<T>(path).then(value => { if (active) { setResult({ path, value }); setFailure(null) } })
      .catch(caught => { if (active) setFailure({ path, error: errorText(caught) }) })
      .finally(() => { if (active) setLoadingPath(null) })
    return () => { active = false }
  }, [path, epoch])
  return { value: result?.path === path ? result.value : null, error: failure?.path === path ? failure.error : '', loading: loadingPath === path }
}
