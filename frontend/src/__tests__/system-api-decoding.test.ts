import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiPayloadError, fetchSystemModels } from '@/lib/api';

const SYSTEM_MODELS_PAYLOAD = {
  status: 'ok',
  installed: [],
  hardware: { vram_gb: 24, tier: 3 },
  current: { smart_model: 'qwen3:14b' },
  issues: {},
  catalog: [],
  recommendations: {},
  reviewed_choices: {},
  hardware_recommendation: {
    vram_mb: 24576,
    bucket: 'MID_HIGH',
    summary: 'Test recommendation',
    aliases: [],
  },
  delivery: { smart: 'applied' },
  routing: { smart: 'qwen3:14b' },
  consistent: true,
  provider_lists: {
    anthropic: {
      model_count: 0,
      fetched_at: null,
      error: null,
      truncated: false,
      excluded: {},
    },
  },
};

describe('system API runtime decoding', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('accepts the canonical models payload and preserves additive fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      ...SYSTEM_MODELS_PAYLOAD,
      future_status_detail: 'additive',
    })));

    const result = await fetchSystemModels();

    expect(result.current.smart_model).toBe('qwen3:14b');
    expect(result.future_status_detail).toBe('additive');
  });

  it('rejects a malformed nested provider status without leaking the payload', async () => {
    const malformed = {
      ...SYSTEM_MODELS_PAYLOAD,
      provider_lists: {
        anthropic: {
          ...SYSTEM_MODELS_PAYLOAD.provider_lists.anthropic,
          error: { secret: 'must-not-appear' },
        },
      },
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(malformed)));

    const result = fetchSystemModels();

    await expect(result).rejects.toBeInstanceOf(ApiPayloadError);
    await expect(result).rejects.toMatchObject({
      endpoint: '/api/system/models',
      fields: ['provider_lists.anthropic.error'],
    });
    await expect(result).rejects.not.toThrow(/must-not-appear/);
  });
});
