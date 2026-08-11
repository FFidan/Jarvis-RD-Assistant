// Zotero integration: connectivity test, push, linkage lookup, resync, poll.
import { apiFetchJson } from './core';
import { zoteroJobSchema, zoteroLinkageSchema, zoteroTestSchema } from './schemas/zotero';

export function zoteroDesktopHref(
  itemKey: string,
  libraryType?: 'user' | 'group',
  groupId?: string | null,
): string {
  const encodedItemKey = encodeURIComponent(itemKey);
  return libraryType === 'group' && groupId
    ? `zotero://select/groups/${encodeURIComponent(groupId)}/items/${encodedItemKey}`
    : `zotero://select/library/items/${encodedItemKey}`;
}

export async function zoteroTest(): Promise<{ success: boolean; error?: string }> {
  const r = await apiFetchJson('/api/zotero/test', zoteroTestSchema, { method: 'POST' });
  return { success: r.ok, error: r.detail };
}

export async function zoteroPushPaper(paperId: number): Promise<{ job_id: string; status: string }> {
  return apiFetchJson(`/api/papers/${paperId}/zotero`, zoteroJobSchema, { method: 'POST' });
}

export async function zoteroGetLinkage(paperId: number): Promise<{
  zotero_item_key: string | null;
  zotero_citation_key: string | null;
  zotero_last_pushed_at: string | null;
  zotero_library_type?: 'user' | 'group';
  zotero_group_id?: string | null;
}> {
  return apiFetchJson(`/api/papers/${paperId}/zotero`, zoteroLinkageSchema);
}

export async function zoteroResync(paperId: number): Promise<{ job_id: string; status: string }> {
  return apiFetchJson(`/api/zotero/resync/${paperId}`, zoteroJobSchema, { method: 'POST' });
}

export async function zoteroPushHighlights(paperId: number): Promise<{ job_id: string; status: string }> {
  return apiFetchJson(`/api/zotero/push-highlights/${paperId}`, zoteroJobSchema, { method: 'POST' });
}

export async function zoteroPollNow(): Promise<{ job_id: string; status: string }> {
  return apiFetchJson('/api/zotero/poll', zoteroJobSchema, { method: 'POST' });
}
