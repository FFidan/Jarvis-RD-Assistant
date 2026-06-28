// Spatial PDF highlights for the in-PDF annotation reader.
import { apiFetch } from './core';
import type { Highlight, HighlightCreate, HighlightUpdate } from '@/types';

export const listHighlights = (paperId: number) =>
  apiFetch<Highlight[]>(`/api/papers/${paperId}/highlights`);

export const createHighlight = (paperId: number, data: HighlightCreate) =>
  apiFetch<Highlight>(`/api/papers/${paperId}/highlights`, {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const updateHighlight = (id: number, data: HighlightUpdate) =>
  apiFetch<Highlight>(`/api/highlights/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });

export const deleteHighlight = (id: number) =>
  apiFetch<void>(`/api/highlights/${id}`, { method: 'DELETE' });
