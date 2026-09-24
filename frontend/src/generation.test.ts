import { expect, test, vi } from 'vitest'
import { SSEParser, postGeneration } from './generation'

test('parses SSE blocks split across CRLF chunks', () => {
  const parser = new SSEParser()
  expect(parser.feed('event: generation\r\ndata: {"status":"connecting","operation_id":"op1"}\r')).toEqual([])
  expect(parser.feed('\n\r\nevent: result\ndata: {"operation_id":"op1","status":"saved"}\n\n')).toEqual([
    { event: 'generation', data: { status: 'connecting', operation_id: 'op1' } },
    { event: 'result', data: { operation_id: 'op1', status: 'saved' } },
  ])
})

test('correlates operation ID and requires terminal frame', async () => {
  const original = globalThis.fetch
  const responseFor = (wire: string) => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode(wire)); controller.close() },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
  try {
    globalThis.fetch = vi.fn(async () => responseFor('event: generation\ndata: {"operation_id":"op1","status":"saved"}\n\nevent: result\ndata: {"operation_id":"op1","status":"saved"}\n\n'))
    const seen: string[] = []
    const terminal = await postGeneration('co_create_turn', { operation_id: 'op1', params: {} }, f => seen.push(f.event))
    expect(terminal.event).toBe('result')
    expect(seen).toEqual(['generation', 'result'])
    globalThis.fetch = vi.fn(async () => responseFor('event: result\ndata: {"operation_id":"op1","status":"saved"}\n\n'))
    await expect(postGeneration('co_create_turn', { operation_id: 'op1' }, () => {})).rejects.toThrow('thiếu event saved')
    globalThis.fetch = vi.fn(async () => responseFor('event: result\ndata: {"operation_id":"other","status":"saved"}\n\n'))
    await expect(postGeneration('co_create_turn', { operation_id: 'op1' }, () => {})).rejects.toThrow('operation_id mismatch')
    globalThis.fetch = vi.fn(async () => responseFor('event: generation\ndata: {"operation_id":"op1","status":"connecting"}\n\n'))
    await expect(postGeneration('co_create_turn', { operation_id: 'op1' }, () => {})).rejects.toThrow('without terminal')
  } finally {
    globalThis.fetch = original
  }
})
