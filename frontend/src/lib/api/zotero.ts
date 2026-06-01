// Zotero integration: connectivity test, push, linkage lookup, resync, poll.
import { apiFetch } from './core';

export async function zoteroTest(): Promise<{ success: boolean; error?: string }> {
  return apiFetch('/api/zotero/test', { method: 'POST' });
}

export async function zoteroPushPaper(paperId: number): Promise<{ job_id: string; status: string }> {
  return apiFetch(`/api/papers/${paperId}/zotero`, { method: 'POST' });
}

export async function zoteroGetLinkage(paperId: number): Promise<{
  zotero_item_key: string | null;
  zotero_citation_key: string | null;
  zotero_last_pushed_at: string | null;
}> {
  return apiFetch(`/api/papers/${paperId}/zotero`);
}

export async function zoteroResync(paperId: number): Promise<{ job_id: string; status: string }> {
  return apiFetch(`/api/zotero/resync/${paperId}`, { method: 'POST' });
}

export async function zoteroPollNow(): Promise<{ job_id: string; status: string }> {
  return apiFetch('/api/zotero/poll', { method: 'POST' });
}
