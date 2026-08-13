export interface Deck {
  id: number;
  name: string;
  description: string | null;
  topic_id: number | null;
  card_count: number;
  due_count: number;
  created_at: string;
}

export interface Evidence {
  quote: string | null;
  page_number: number | null;
  chunk_id: number | null;
  snapshot_path: string | null;
  verified: boolean;
}

export interface Card {
  id: number;
  deck_id: number;
  paper_id: number | null;
  card_type: 'concept' | 'quote' | 'method' | 'comparison';
  front: string;
  back: string;
  evidence: Evidence | null;
  fsrs_state: Record<string, unknown>;
  due_at: string | null;
  stale: boolean;
  created_at: string;
  updated_at: string;
}

export interface ReviewResponse {
  card_id: number;
  rating: number;
  next_due_at: string;
  fsrs_state: Record<string, unknown>;
  review_log_id: number;
}

export interface RetentionStats {
  total_cards: number;
  due_now: number;
  reviewed_today: number;
  average_retention: number;
  reviews_by_rating: Record<string, number>;
  streak_days: number;
}

export interface GenerateCardsResponse {
  cards_created: number;
  cards: Card[];
  confidence: string;
}
