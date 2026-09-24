import { useEffect, useState } from 'react'

type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
type Props = {
  scope: string; payload: Record<string, unknown>; onSave: (payload: Record<string, unknown>) => Promise<void>;
  disabled?: boolean; saveLabel?: string; hint?: string;
}

const protectedFields = new Set(['id', 'chapter_id', 'chapter_number', 'arc_id', 'volume_id', 'section_id', 'character_id', 'rule_id', 'foreshadow_id', 'prose_revision', 'markdown_ref', 'chapters'])
const pretty = (value: unknown) => JSON.stringify(value, null, 2)

function Field({ name, value, onChange, depth = 0 }: {
  name: string; value: Json; onChange: (value: Json) => void; depth?: number;
}) {
  if (Array.isArray(value)) return <fieldset className="nested"><legend>{name}</legend>
    {value.map((item, index) => <div className="array-item" key={index}>
      {item !== null && typeof item === 'object' && !Array.isArray(item) ?
        <details className="array-detail" key={`${name}-${index}`}>
          <summary>{name} {index + 1}{typeof item.title === 'string' ? ` · ${item.title}` : ''}</summary>
          <Field name={`${name} ${index + 1}`} value={item} depth={depth + 1} onChange={next => onChange(value.map((old, i) => i === index ? next : old))} />
        </details> :
        <Field name={`${name} ${index + 1}`} value={item} depth={depth + 1} onChange={next => onChange(value.map((old, i) => i === index ? next : old))} />}
      {!protectedFields.has(name) && <button type="button" className="subtle" onClick={() => onChange(value.filter((_, i) => i !== index))}>Xóa dòng</button>}
    </div>)}
    {depth < 4 && !protectedFields.has(name) && <button type="button" className="subtle" onClick={() => onChange([...value, typeof value[0] === 'object' && value[0] !== null ? structuredClone(value[0]) : ''])}>Thêm dòng</button>}
  </fieldset>
  if (value !== null && typeof value === 'object') return <fieldset className="nested"><legend>{name}</legend>
    {Object.entries(value).map(([key, item]) => <Field key={key} name={key} value={item} depth={depth + 1}
      onChange={next => onChange({ ...value, [key]: next })} />)}
  </fieldset>
  if (protectedFields.has(name)) return <label className="field">{name}<input value={String(value ?? '')} readOnly aria-readonly="true" /></label>
  if (typeof value === 'boolean') return <label className="check"><input type="checkbox" checked={value} onChange={event => onChange(event.target.checked)} />{name}</label>
  if (typeof value === 'number') return <label className="field">{name}<input type="number" value={value} onChange={event => onChange(Number(event.target.value))} /></label>
  return <label className="field">{name}<textarea rows={String(value ?? '').length > 100 ? 5 : 2} value={String(value ?? '')} onChange={event => onChange(event.target.value)} /></label>
}

export function JsonEditor({ scope, payload, onSave, disabled = false, saveLabel = 'Lưu candidate', hint = 'Save chỉ cập nhật candidate. Accept là bước riêng.' }: Props) {
  const key = `draft:${scope}`
  const [raw, setRaw] = useState(() => sessionStorage.getItem(key) ?? pretty(payload))
  const [mode, setMode] = useState<'form' | 'raw'>('form')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { setRaw(sessionStorage.getItem(key) ?? pretty(payload)); setError('') }, [key])
  function update(next: string) { setRaw(next); sessionStorage.setItem(key, next) }
  function parsed(reportError = true): Record<string, unknown> | null {
    try {
      const value: unknown = JSON.parse(raw)
      if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('Cần một JSON object.')
      if (reportError) setError(''); return value as Record<string, unknown>
    } catch (caught) { if (reportError) setError(caught instanceof Error ? caught.message : String(caught)); return null }
  }
  const formPayload = mode === 'form' ? parsed(false) : null
  async function save() {
    const value = parsed(); if (!value) return
    setBusy(true)
    try { await onSave(value); sessionStorage.removeItem(key) }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }
  return <section className="editor">
    <div className="row"><strong>Working copy</strong><div className="segmented"><button type="button" aria-pressed={mode === 'form'} onClick={() => setMode('form')}>Form</button><button type="button" aria-pressed={mode === 'raw'} onClick={() => setMode('raw')}>JSON thô</button></div></div>
    {mode === 'form' && formPayload ? <div className="field-grid">{Object.entries(formPayload).map(([name, value]) => <Field key={name} name={name} value={value as Json} onChange={next => update(pretty({ ...formPayload, [name]: next }))} />)}</div> :
      <label className="field">Payload JSON<textarea className="raw-editor" value={raw} onChange={event => update(event.target.value)} rows={18} spellCheck={false} /></label>}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="row"><button type="button" disabled={disabled || busy} onClick={save}>{saveLabel}</button><button type="button" className="subtle" onClick={() => { sessionStorage.removeItem(key); update(pretty(payload)); setError('') }}>Nạp lại bản đã lưu</button></div>
    <p className="hint">{hint}</p>
  </section>
}
