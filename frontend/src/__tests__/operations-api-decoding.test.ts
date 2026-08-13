import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiPayloadError,
  fetchActiveFocusSession,
  fetchMyDay,
  fetchProjects,
  fetchThreads,
  getJournalEntry,
  getBackupStatus,
  getJob,
  getRestoreStatus,
} from '@/lib/api';

function respondWith(payload: unknown): Response {
  return new Response(JSON.stringify(payload));
}

describe('operations API runtime decoding', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('accepts an additive backup-status payload', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      backup_dir_available: true,
      archive_count: 3,
      last_run_at: '2026-08-09T12:00:00Z',
      trigger_pending: false,
      last_attempt_at: '2026-08-09T12:00:00Z',
      last_run_succeeded: true,
      last_run_vectors_captured: true,
      last_run_s3_complete: null,
      future_field: 'preserved',
    }));

    const status = await getBackupStatus();
    expect(status.archive_count).toBe(3);
  });

  it('rejects an unknown restore transition discriminator', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      state: 'complete',
      current_step: null,
      steps: [],
      safety_backup_ts: null,
      started_at: null,
      finished_at: null,
      error: null,
      manual_steps_required: false,
      phase: null,
      restore_id: null,
      source: null,
      quarantine: 'none',
    }));

    await expect(getRestoreStatus()).rejects.toMatchObject({
      endpoint: '/api/admin/backups/restore/status',
      fields: ['state'],
    });
  });

  it('rejects a malformed nested restore step', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      state: 'running',
      current_step: 'Database',
      steps: [{ name: 'Database', status: 'finished' }],
      safety_backup_ts: null,
      started_at: '2026-08-09T12:00:00Z',
      finished_at: null,
      error: null,
      manual_steps_required: false,
      phase: 'database',
      restore_id: null,
      source: 'local',
      quarantine: 'none',
    }));

    await expect(getRestoreStatus()).rejects.toMatchObject({ fields: ['steps.0.status'] });
  });

  it('accepts an additive job payload with nullable backend fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      id: 'job-1',
      kind: 'pulse.generate',
      status: 'queued',
      progress: null,
      progress_message: null,
      payload: { force: false },
      result: null,
      error: null,
      created_at: null,
      started_at: null,
      finished_at: null,
      server_extension: 1,
    }));

    const job = await getJob('job-1');
    expect(job.status).toBe('queued');
  });

  it('rejects an unknown job state before it reaches the store', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      id: 'job-1',
      kind: 'pulse.generate',
      status: 'waiting',
      progress: 0,
      progress_message: null,
      result: null,
      error: null,
      created_at: '2026-08-09T12:00:00Z',
      started_at: null,
      finished_at: null,
    }));

    await expect(getJob('job-1')).rejects.toMatchObject({ fields: ['status'] });
  });

  it.each([
    ['cards_created', 'zero'],
    ['coverage', 'complete'],
    ['passes', -1],
    ['status', 7],
  ])('rejects a wrong-type consumed job result field: %s', async (field, value) => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      id: 'job-result-malformed',
      kind: 'card.generate',
      status: 'succeeded',
      progress: 1,
      progress_message: null,
      payload: {},
      result: { [field]: value },
      error: null,
      created_at: '2026-08-09T12:00:00Z',
      started_at: '2026-08-09T12:00:00Z',
      finished_at: '2026-08-09T12:00:01Z',
    }));

    await expect(getJob('job-result-malformed')).rejects.toMatchObject({
      fields: [`result.${field}`],
    });
  });

  it('decodes the My Day aggregate and tolerates additive fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      tasks: [],
      cards_due: 2,
      recommendations: [],
      today_focus_hours: 1.25,
      focus_streak_days: 4,
      project_pulse: [],
      future_summary: 'additive',
    }));

    const result = await fetchMyDay();
    expect(result.cards_due).toBe(2);
  });

  it('decodes the durable cross-client focus state', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      id: 41,
      state: 'paused',
      source: 'telegram',
      duration_seconds: 1500,
      remaining_seconds: 900,
      started_at: '2026-08-09T12:00:00+00:00',
      paused_at: '2026-08-09T12:10:00+00:00',
      paused_seconds: 0,
      completed_at: null,
      recorded_seconds: 0,
      task_id: null,
      paper_id: null,
      future_field: true,
    }));

    const session = await fetchActiveFocusSession();
    expect(session).toMatchObject({ id: 41, state: 'paused', source: 'telegram' });
  });

  it('rejects an unknown durable focus state before it reaches the timer store', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      id: 41,
      state: 'sleeping',
      source: 'telegram',
      duration_seconds: 1500,
      remaining_seconds: 900,
      started_at: '2026-08-09T12:00:00+00:00',
      paused_at: null,
      paused_seconds: 0,
      completed_at: null,
      recorded_seconds: 0,
      task_id: null,
      paper_id: null,
    }));

    await expect(fetchActiveFocusSession()).rejects.toMatchObject({ fields: ['state'] });
  });

  it('accepts the documented empty journal response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith(null));

    await expect(getJournalEntry('2026-08-09')).resolves.toBeNull();
  });

  it('rejects an unknown thread state', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith([{
      id: 7,
      title: 'Resume analysis',
      anchor: null,
      progress: 0.5,
      last_at: '2026-08-09T12:00:00Z',
      status: 'paused',
      created_at: '2026-08-09T10:00:00Z',
    }]));

    await expect(fetchThreads()).rejects.toMatchObject({ fields: ['0.status'] });
  });

  it('decodes project identifiers and additive fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith([{
      id: 12,
      name: 'Neural ODE survey',
      description: null,
      status: 'active',
      deadline: null,
      color: null,
      created_at: '2026-08-09T10:00:00Z',
      updated_at: '2026-08-09T11:00:00Z',
      future_field: true,
    }]));

    const projects = await fetchProjects();
    expect(projects[0]?.id).toBe(12);
  });

  it('rejects a malformed project identifier without exposing the payload', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith([{
      id: 'secret-project-id',
      name: 'Neural ODE survey',
      description: null,
      status: 'active',
      deadline: null,
      color: null,
      created_at: '2026-08-09T10:00:00Z',
      updated_at: '2026-08-09T11:00:00Z',
    }]));

    const result = fetchProjects();
    await expect(result).rejects.toBeInstanceOf(ApiPayloadError);
    await expect(result).rejects.toMatchObject({ fields: ['0.id'] });
    await expect(result).rejects.not.toThrow(/secret-project-id/);
  });
});
