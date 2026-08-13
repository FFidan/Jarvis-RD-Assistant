import { beforeEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';
import {
  ApiPayloadError,
  apiFetchJson,
  apiFetchVoid,
} from '@/lib/api/core';

const payloadSchema = z.looseObject({
  id: z.number().int(),
  nested: z.looseObject({ label: z.string() }),
});

describe('decoded API boundary', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('returns a schema-decoded payload and preserves additive fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ id: 7, nested: { label: 'ok' }, added: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const result = await apiFetchJson('/api/decoded', payloadSchema);

    expect(result).toEqual({ id: 7, nested: { label: 'ok' }, added: true });
  });

  it('reports endpoint and failing field without retaining the payload', async () => {
    const secret = 'must-not-appear';
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ id: 7, nested: { label: 42 }, secret }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const error = await apiFetchJson('/api/decoded', payloadSchema).catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiPayloadError);
    expect(error).toMatchObject({ endpoint: '/api/decoded', fields: ['nested.label'] });
    expect(String(error)).not.toContain(secret);
    expect(JSON.stringify(error)).not.toContain(secret);
  });

  it('rejects invalid JSON without echoing the response body', async () => {
    const body = 'not-json-sensitive-body';
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(body, { status: 200 }));

    const error = await apiFetchJson('/api/decoded', payloadSchema).catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiPayloadError);
    expect(error).toMatchObject({ endpoint: '/api/decoded', fields: ['response'] });
    expect(String(error)).not.toContain(body);
  });

  it('rejects 204 on the JSON path', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

    await expect(apiFetchJson('/api/decoded', payloadSchema)).rejects.toMatchObject({
      endpoint: '/api/decoded',
      fields: ['response'],
    });
  });

  it.each([
    new Response(null, { status: 204 }),
    new Response('ignored-success-body', { status: 200 }),
  ])('accepts successful status-only responses without parsing them', async (response) => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(response);

    await expect(apiFetchVoid('/api/status-only', { method: 'POST' })).resolves.toBeUndefined();
  });
});
