import type { ConfidenceLevel } from '@/lib/sse';

export interface Source {
  chunk_id?: number;
  paper_id?: number;
  paper_title?: string;
  content?: string;
  text?: string;
  page_number?: number | null;
  score: number;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  sources?: Source[];
  confidence?: ConfidenceLevel;
  verified_fraction?: number;
  per_sentence?: { text: string; verified: boolean }[];
}
