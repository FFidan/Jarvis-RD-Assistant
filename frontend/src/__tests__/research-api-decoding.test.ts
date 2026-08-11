import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiPayloadError,
  deleteNote,
  fetchAnalyticsActivity,
  fetchCards,
  fetchExtractionTable,
  fetchPaperDetail,
  fetchPulseToday,
  getKnowledgeGraph,
  listHighlights,
  processLibrary,
  queryKnowledgeGraph,
  searchPreview,
  zoteroGetLinkage,
} from '@/lib/api';

function respondWith(payload: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(payload), { status });
}

const paper = {
  id: 7,
  external_id: 'paper-7',
  source_type: 'zotero',
  title: 'Contract Paper',
  authors: ['A. Researcher'],
  abstract: null,
  published_date: null,
  url: 'https://example.test/paper-7',
  pdf_url: null,
  pdf_local_path: null,
  pdf_downloaded: false,
  citation_count: 0,
  priority_score: null,
  metadata: { nested: ['bounded', 1, true, null] },
  discovered_at: null,
  created_at: '2026-08-09T12:00:00Z',
  discovery_origin: 'user_initiated',
};

describe('research API runtime decoding', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('accepts Zotero papers, NONE confidence, and additive detail fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      paper,
      summary: {
        id: 3,
        paper_id: 7,
        summary_brief: 'Brief',
        summary_detailed: 'Detailed',
        tldr: null,
        key_findings: [],
        methodology: null,
        limitations: null,
        relevance_notes: null,
        confidence: 'NONE',
        cross_references: [],
        llm_model: null,
        summary_verified: false,
        created_at: '2026-08-09T12:00:00Z',
      },
      chunks: [],
      user_state: null,
      has_project_links: false,
      processing_failed: false,
      future_field: 'preserved',
    }));

    const detail = await fetchPaperDetail(7);
    expect(detail.paper.source_type).toBe('zotero');
    expect(detail.summary?.confidence).toBe('NONE');
  });

  it('rejects an unknown nested paper confidence value', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      paper,
      summary: {
        id: 3,
        paper_id: 7,
        summary_brief: 'Brief',
        summary_detailed: 'Detailed',
        tldr: null,
        key_findings: [],
        methodology: null,
        limitations: null,
        relevance_notes: null,
        confidence: 'CERTAIN',
        cross_references: [],
        llm_model: null,
        summary_verified: false,
        created_at: '2026-08-09T12:00:00Z',
      },
      chunks: [],
      user_state: null,
    }));

    await expect(fetchPaperDetail(7)).rejects.toMatchObject({ fields: ['summary.confidence'] });
  });

  it('rejects a malformed nested search result without retaining its content', async () => {
    const secret = 'sensitive-search-payload';
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      results: [{
        external_id: 'x',
        source_type: 'invalid-source',
        title: secret,
        authors: [],
        abstract: null,
        published_date: null,
        url: 'https://example.test/x',
        pdf_url: null,
        citation_count: 0,
        metadata: {},
        library_match: null,
      }],
      total: 1,
      per_source_counts: {},
      degraded_sources: [],
      source_errors: {},
    }));

    const result = searchPreview('query');
    await expect(result).rejects.toBeInstanceOf(ApiPayloadError);
    await expect(result).rejects.toMatchObject({ fields: ['results.0.source_type'] });
    await expect(result).rejects.not.toThrow(new RegExp(secret));
  });

  it('preserves bounded number, list, and object extraction values', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith([{
      paper_id: 7,
      paper_title: 'Contract Paper',
      extractions: {
        sample_count: { value: 42, quote: null, verified: false, confidence: 0.7, chunk_id: null, page_number: null },
        methods: { value: ['A', { nested: true }], quote: 'Evidence', verified: true, confidence: 1, chunk_id: 2, page_number: 3 },
      },
    }]));

    const rows = await fetchExtractionTable(1, [7]);
    expect(rows[0]?.extractions.sample_count?.value).toBe(42);
  });

  it('uses the status-only path for the note deletion 204 contract', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith(null, 204));
    await expect(deleteNote(9)).resolves.toBeUndefined();
  });

  it('rejects an impossible queued-library envelope with a null job id', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      job_id: null,
      status: 'queued',
      reason: null,
    }));

    await expect(processLibrary()).rejects.toMatchObject({ fields: ['job_id'] });
  });

  it('accepts nullable knowledge-graph timestamps and relationship pages', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      entities: [{
        id: 1,
        name: 'Method',
        canonical_name: 'method',
        entity_type: 'method',
        description: null,
        metadata: {},
        paper_count: 1,
        created_at: null,
        display_size: 20,
      }],
      relationships: [{
        id: 2,
        source_entity_id: 1,
        target_entity_id: 1,
        relationship_type: 'uses',
        paper_id: 7,
        page_number: 4,
        evidence_quote: null,
        confidence: 0.5,
        created_at: null,
      }],
      entity_type_counts: { method: 1 },
    }));

    const graph = await getKnowledgeGraph();
    expect(graph.entities[0]?.created_at).toBeNull();
  });

  it('rejects non-object knowledge-query rows', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({ results: ['raw'], query: 'q' }));
    await expect(queryKnowledgeGraph('q')).rejects.toMatchObject({ fields: ['results.0'] });
  });

  it('accepts the backend 200 null Pulse empty state', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith(null));
    await expect(fetchPulseToday()).resolves.toBeNull();
  });

  it('rejects malformed Pulse signal values inside a card', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith({
      deck_id: 1,
      deck_date: '2026-08-09',
      card_count: 1,
      generated_at: '2026-08-09T12:00:00Z',
      cards: [{
        card_id: 1,
        paper_id: 7,
        paper_title: 'Contract Paper',
        paper_authors: [],
        paper_url: null,
        rank: 1,
        score: 0.8,
        llm_relevance: null,
        llm_novelty: null,
        reasoning: null,
        reasoning_verified: null,
        reasoning_confidence: null,
        signals: { semantic: 'high' },
      }],
      stats: {},
    }));

    await expect(fetchPulseToday()).rejects.toMatchObject({ fields: ['cards.0.signals.semantic'] });
  });

  it('rejects unknown card discriminators', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(respondWith([{
      id: 1,
      deck_id: 1,
      paper_id: null,
      card_type: 'essay',
      front: 'Front',
      back: 'Back',
      evidence: null,
      fsrs_state: {},
      due_at: null,
      stale: false,
      created_at: '2026-08-09T12:00:00Z',
      updated_at: '2026-08-09T12:00:00Z',
    }]));

    await expect(fetchCards()).rejects.toMatchObject({ fields: ['0.card_type'] });
  });

  it('rejects malformed highlight geometry, Zotero linkage, and analytics counters', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(respondWith([{
        id: 1,
        paper_id: 7,
        page: 1,
        rect: { boundingRect: { x0: 'left', y0: 0, x1: 1, y1: 1 }, rects: [] },
        note: null,
        color: null,
        quote: null,
        created_at: '2026-08-09T12:00:00Z',
        stale: false,
      }]))
      .mockResolvedValueOnce(respondWith({
        paper_id: '7',
        zotero_item_key: null,
        zotero_citation_key: null,
        zotero_last_pushed_at: null,
      }))
      .mockResolvedValueOnce(respondWith([{
        log_date: '2026-08-09',
        tasks_completed: 'one',
        cards_reviewed: 0,
        papers_read: 0,
        focus_hours: 0,
      }]));

    await expect(listHighlights(7)).rejects.toMatchObject({ fields: ['0.rect.boundingRect.x0'] });
    await expect(zoteroGetLinkage(7)).rejects.toMatchObject({ fields: ['paper_id'] });
    await expect(fetchAnalyticsActivity()).rejects.toMatchObject({ fields: ['0.tasks_completed'] });
  });

  it('accepts additive non-secret Zotero library context', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(respondWith({
      paper_id: 7,
      zotero_item_key: 'ITEM-7',
      zotero_citation_key: null,
      zotero_last_pushed_at: null,
      zotero_library_type: 'group',
      zotero_group_id: '987654',
    }));

    await expect(zoteroGetLinkage(7)).resolves.toMatchObject({
      zotero_library_type: 'group',
      zotero_group_id: '987654',
    });
  });
});
