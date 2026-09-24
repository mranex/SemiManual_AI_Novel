export type GenerationEvent = {
  status: string; operation_id: string; action: string; attempt: number;
  transport: string; text_delta: string; detail: string;
}
export type TerminalResult = { action: string; operation_id: string; status: string | null; [key: string]: unknown }
export type TerminalError = { code: string; operation_id: string | null; retryable: boolean; message: string; [key: string]: unknown }
export type Frame =
  | { event: 'generation'; data: GenerationEvent }
  | { event: 'result'; data: TerminalResult }
  | { event: 'error'; data: TerminalError }

export class GenerationHttpError extends Error {
  constructor(public status: number, public body: Record<string, unknown> | null) {
    super(typeof body?.message === 'string' ? body.message : `HTTP ${status}`)
  }
}

export class SSEParser {
  private buffer = ''

  feed(chunk: string): Frame[] {
    this.buffer = (this.buffer + chunk).replace(/\r\n/g, '\n')
    const frames: Frame[] = []
    while (true) {
      const end = this.buffer.indexOf('\n\n')
      if (end < 0) break
      const block = this.buffer.slice(0, end)
      this.buffer = this.buffer.slice(end + 2)
      const lines = block.split('\n')
      const event = lines.find(line => line.startsWith('event: '))?.slice(7)
      const data = lines.filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n')
      if (event === 'generation' || event === 'result' || event === 'error') {
        frames.push({ event, data: JSON.parse(data) } as Frame)
      }
    }
    return frames
  }
}

export async function postGeneration(
  name: string, body: Record<string, unknown>, onFrame: (frame: Frame) => void,
  signal?: AbortSignal,
): Promise<Frame> {
  const operationId = body.operation_id
  if (typeof operationId !== 'string') throw new Error('operation_id is required')
  const response = await fetch(`/api/v1/generations/${encodeURIComponent(name)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body), signal,
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null) as Record<string, unknown> | null
    throw new GenerationHttpError(response.status, error)
  }
  if (!response.body) throw new Error('Generation response không có stream')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const parser = new SSEParser()
  let terminal: Frame | undefined
  let saved = false
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      for (const frame of parser.feed(decoder.decode(value, { stream: true }))) {
        if (frame.data.operation_id !== operationId) throw new Error('operation_id mismatch')
        onFrame(frame)
        if (frame.event === 'generation' && frame.data.status === 'saved') saved = true
        if (frame.event === 'result' || frame.event === 'error') terminal = frame
      }
    }
    for (const frame of parser.feed(decoder.decode())) {
      if (frame.data.operation_id !== operationId) throw new Error('operation_id mismatch')
      onFrame(frame)
      if (frame.event === 'generation' && frame.data.status === 'saved') saved = true
      if (frame.event === 'result' || frame.event === 'error') terminal = frame
    }
  } finally {
    reader.releaseLock()
  }
  if (!terminal) throw new Error('Generation stream ended without terminal result')
  if (terminal.event === 'result' && terminal.data.status === 'saved' && !saved) throw new Error('Generation thiếu event saved')
  return terminal
}
