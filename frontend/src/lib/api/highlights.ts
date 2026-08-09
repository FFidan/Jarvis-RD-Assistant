// Spatial PDF highlights for the in-PDF annotation reader.
import { apiFetchJson, apiFetchVoid } from './core';
import { highlightSchema } from './schemas/highlights';
import type { Highlight, HighlightCreate, HighlightUpdate } from '@/types';

export const listHighlights = (paperId: number): Promise<Highlight[]> =>
  apiFetchJson(`/api/papers/${paperId}/highlights`, highlightSchema.array());

export const createHighlight = (paperId: number, data: HighlightCreate): Promise<Highlight> =>
  apiFetchJson(`/api/papers/${paperId}/highlights`, highlightSchema, {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const updateHighlight = (id: number, data: HighlightUpdate): Promise<Highlight> =>
  apiFetchJson(`/api/highlights/${id}`, highlightSchema, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });

export const deleteHighlight = (id: number) =>
  apiFetchVoid(`/api/highlights/${id}`, { method: 'DELETE' });
