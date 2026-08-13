import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiPayloadError, fetchConfig, getFirstRunStatus, getSmtpConfig } from '@/lib/api';

describe('settings API runtime decoding', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('accepts bounded recursive configuration JSON and additive fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify([
      {
        key: 'pulse.filters',
        value: { enabled: true, thresholds: [0.2, 0.8], fallback: null },
        future_metadata: 'additive',
      },
    ])));

    const result = await fetchConfig();

    expect(result[0]?.value).toEqual({ enabled: true, thresholds: [0.2, 0.8], fallback: null });
    expect(result[0]?.future_metadata).toBe('additive');
  });

  it('rejects a malformed config key at the decoded boundary', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify([
      { key: 42, value: true },
    ])));

    await expect(fetchConfig()).rejects.toMatchObject({
      endpoint: '/api/config',
      fields: ['0.key'],
    });
  });

  it('rejects malformed pre-auth setup status without exposing its body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      configured: 'yes',
      diagnostic: 'must-not-appear',
    })));

    const result = getFirstRunStatus();

    await expect(result).rejects.toBeInstanceOf(ApiPayloadError);
    await expect(result).rejects.toMatchObject({
      endpoint: '/api/setup/status',
      fields: ['configured'],
    });
    await expect(result).rejects.not.toThrow(/must-not-appear/);
  });

  it('rejects a malformed SMTP port instead of passing it to the settings UI', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      host: 'smtp.example.test',
      port: '587',
      user: null,
      from_email: 'researcher@example.test',
      reply_to: null,
      from_name: null,
      has_password: true,
    })));

    await expect(getSmtpConfig()).rejects.toMatchObject({
      endpoint: '/api/setup/smtp',
      fields: ['port'],
    });
  });
});
