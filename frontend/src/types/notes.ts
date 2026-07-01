export interface Note {
  id: number;
  paper_id: number;
  user_note: string;
  highlight_text: string | null;
  page_number: number | null;
  source: 'user' | 'zotero';
  zotero_annotation_key: string | null;
  verification_status: 'unverified' | 'verified' | 'failed';
  verified_quote: string | null;
  verified_page_number: number | null;
  promoted_at: string | null;
  created_at: string;
}

// --- Spatial PDF highlights (in-PDF annotation reader) ---

/** Normalized, top-origin rectangle on a PDF page (coordinates in [0, 1]). */
export interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** react-pdf-highlighter geometry: union box plus its per-line rectangles. */
export interface HighlightRect {
  boundingRect: Rect;
  rects: Rect[];
}

export interface Highlight {
  id: number;
  paper_id: number;
  page: number;
  rect: HighlightRect;
  note: string | null;
  color: string | null;
  quote: string | null;
  created_at: string;
}

export interface HighlightCreate {
  page: number;
  rect: HighlightRect;
  note?: string | null;
  color?: string | null;
  quote?: string | null;
}

export interface HighlightUpdate {
  note?: string | null;
  color?: string | null;
}
