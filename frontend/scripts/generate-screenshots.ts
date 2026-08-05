/**
 * README screenshot generator — fully mocked, no live backend required.
 *
 * Produces 7 hero screenshots for docs/screenshots/:
 *   01-home.png        → HomePage         /
 *   02-my-day.png      → MyDayPage        /my-day
 *   03-pulse.png       → PulseDeckPage    /pulse
 *   04-library.png     → ResearchFeedPage /feed?surface=library (Library view)
 *   05-discover.png    → ResearchFeedPage /feed (Search/Discover tab)
 *   06-knowledge-graph.png → KnowledgeGraphPage /knowledge
 *   07-ask.png         → AskPage          /ask (cross-paper RAG Q&A)
 *
 * Viewport: 1440×900 (above-the-fold, not fullPage).
 * All API calls intercepted via page.route().
 *
 * This is documentation tooling, not a test: it asserts nothing and it
 * overwrites the tracked PNGs under docs/screenshots/. It therefore lives
 * outside the Playwright testDir so that `npm run test:e2e` cannot rewrite
 * those tracked files as a side effect, and runs only when invoked on purpose:
 *
 *   npm run build
 *   npm run preview -- --port 3001 &
 *   npm run screenshots:generate
 *
 * The Playwright runner still drives it, via playwright.screenshots.config.ts.
 */

import { test, type Page, type Route } from '@playwright/test';
import path from 'path';
import { fileURLToPath } from 'url';
import { seedAuthedSession } from '../e2e/helpers/setup';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const SCREENSHOTS_DIR = path.resolve(__dirname, '../../docs/screenshots');
const VIEWPORT = { width: 1440, height: 900 };

// ─────────────────────────────────────────────────────────────────────────────
// Shared mock data fixtures
// ─────────────────────────────────────────────────────────────────────────────

const SETUP_STATUS = {
  configured: true,
  setup_completed: true,
  setup_mode: 'single',
};

const DASHBOARD_METRICS = {
  total_papers: 347,
  unread_papers: 28,
  pending_papers: 12,
  due_cards: 14,
  active_projects: 3,
  topic_count: 8,
  nudge_count: 2,
  onboarding_stage: 'complete',
};

const PULSE_DECK = {
  deck_id: 1,
  deck_date: '2026-06-04',
  card_count: 7,
  generated_at: '2026-06-04T06:00:00Z',
  is_stale: false,
  stale_age_days: null,
  empty_reason: null,
  stats: {},
  cards: [
    {
      card_id: 1,
      paper_id: 101,
      paper_title: 'Sparse Mixture-of-Experts for Efficient Long-Context Inference',
      paper_authors: ['Zhuang Liu', 'Barret Zoph', 'Ekin Dogus Cubuk'],
      paper_url: 'https://arxiv.org/abs/2501.00101',
      rank: 1,
      score: 0.94,
      llm_relevance: 0.96,
      llm_novelty: 0.88,
      reasoning:
        'Directly addresses your sparse-attention research thread. Novel mixture gating improves 128k-token throughput by 2.4×.',
      reasoning_verified: true,
      reasoning_confidence: 'HIGH',
      signals: { semantic_sim: 0.91, recency: 0.95, citation_velocity: 0.82 },
      user_state: 'inbox',
      tags: ['transformers', 'efficiency', 'MoE'],
    },
    {
      card_id: 2,
      paper_id: 102,
      paper_title: 'Test-Time Compute Scaling via Iterative Self-Refinement',
      paper_authors: ['Sewon Min', 'Hannaneh Hajishirzi'],
      paper_url: 'https://arxiv.org/abs/2501.00202',
      rank: 2,
      score: 0.91,
      llm_relevance: 0.92,
      llm_novelty: 0.85,
      reasoning:
        'Explores compute-optimal inference; synergizes with your diffusion-LM notes from last week.',
      reasoning_verified: true,
      reasoning_confidence: 'HIGH',
      signals: { semantic_sim: 0.87, recency: 0.93, citation_velocity: 0.76 },
      user_state: 'inbox',
      tags: ['inference', 'self-refinement'],
    },
    {
      card_id: 3,
      paper_id: 103,
      paper_title: 'Scaling Laws for Reward Model Overoptimization in RLHF',
      paper_authors: ['Leo Gao', 'John Schulman', 'Jacob Hilton'],
      paper_url: 'https://arxiv.org/abs/2210.10760',
      rank: 3,
      score: 0.88,
      llm_relevance: 0.89,
      llm_novelty: 0.79,
      reasoning: 'Empirical reward-hacking curves; foundational for your alignment chapter.',
      reasoning_verified: true,
      reasoning_confidence: 'HIGH',
      signals: { semantic_sim: 0.85, recency: 0.71, citation_velocity: 0.88 },
      user_state: 'inbox',
      tags: ['RLHF', 'alignment', 'scaling'],
    },
    {
      card_id: 4,
      paper_id: 104,
      paper_title: 'FlashAttention-3: Fast and Accurate Attention with Asynchrony and Low-precision',
      paper_authors: ['Jay Shah', 'Ganesh Bikshandi', 'Ying Zhang', 'Tri Dao'],
      paper_url: 'https://arxiv.org/abs/2407.08608',
      rank: 4,
      score: 0.85,
      llm_relevance: 0.86,
      llm_novelty: 0.80,
      reasoning: 'H100 async attention; 1.5–2.0× faster than FA2 on your hardware tier.',
      reasoning_verified: false,
      reasoning_confidence: 'MEDIUM',
      signals: { semantic_sim: 0.82, recency: 0.88, citation_velocity: 0.79 },
      user_state: 'inbox',
      tags: ['attention', 'GPU', 'performance'],
    },
    {
      card_id: 5,
      paper_id: 105,
      paper_title: 'Mechanistic Interpretability of Chain-of-Thought Reasoning',
      paper_authors: ['Atticus Geiger', 'Zhengxuan Wu', 'Christopher Potts'],
      paper_url: 'https://arxiv.org/abs/2501.00505',
      rank: 5,
      score: 0.82,
      llm_relevance: 0.83,
      llm_novelty: 0.77,
      reasoning: 'Circuit-level analysis of CoT; complements your interpretability reading list.',
      reasoning_verified: true,
      reasoning_confidence: 'HIGH',
      signals: { semantic_sim: 0.80, recency: 0.85, citation_velocity: 0.68 },
      user_state: 'inbox',
      tags: ['interpretability', 'CoT', 'circuits'],
    },
    {
      card_id: 6,
      paper_id: 106,
      paper_title: 'Beyond Chinchilla-Optimal: Accounting for Inference in LLM Cost Modelling',
      paper_authors: ['Nikhil Sardana', 'Jonathan Frankle'],
      paper_url: 'https://arxiv.org/abs/2401.00448',
      rank: 6,
      score: 0.79,
      llm_relevance: 0.80,
      llm_novelty: 0.74,
      reasoning: 'Revisits Chinchilla trade-offs with deployment costs; matches your infra thread.',
      reasoning_verified: true,
      reasoning_confidence: 'MEDIUM',
      signals: { semantic_sim: 0.77, recency: 0.79, citation_velocity: 0.72 },
      user_state: 'inbox',
      tags: ['scaling', 'cost', 'inference'],
    },
    {
      card_id: 7,
      paper_id: 107,
      paper_title: 'Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks',
      paper_authors: ['Patrick Lewis', 'Ethan Perez', 'Aleksandra Piktus'],
      paper_url: 'https://arxiv.org/abs/2005.11401',
      rank: 7,
      score: 0.76,
      llm_relevance: 0.77,
      llm_novelty: 0.65,
      reasoning: 'Seminal RAG paper; high citation velocity spike suggests renewed community interest.',
      reasoning_verified: true,
      reasoning_confidence: 'HIGH',
      signals: { semantic_sim: 0.74, recency: 0.55, citation_velocity: 0.91 },
      user_state: 'inbox',
      tags: ['RAG', 'retrieval', 'NLP'],
    },
  ],
};

function makeFeedPaper(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 1,
    external_id: 'arxiv:2501.00001',
    source_type: 'arxiv',
    title: 'Sparse Mixture-of-Experts for Efficient Long-Context Inference',
    authors: ['Zhuang Liu', 'Barret Zoph'],
    abstract:
      'We propose a sparse gating mechanism for mixture-of-experts models that reduces memory bandwidth by routing only the top-k experts per token. On standard LLM benchmarks we achieve 2.4× throughput improvement with <1% quality degradation.',
    published_date: '2026-01-15',
    url: 'https://arxiv.org/abs/2501.00001',
    pdf_url: null,
    pdf_local_path: null,
    pdf_downloaded: false,
    discovered_at: '2026-06-01T10:00:00Z',
    priority_score: 0.94,
    citation_count: 142,
    metadata: {},
    created_at: '2026-06-01T10:00:00Z',
    discovery_origin: 'recommender',
    user_state: null,
    recent_feedback: null,
    state: 'inbox',
    state_before_trash: null,
    starred: false,
    rating: null,
    summary_brief: 'Sparse MoE gating for efficient long-context inference with 2.4× throughput gains.',
    tldr: '2.4× throughput at <1% quality loss via top-k sparse MoE gating.',
    confidence: 'HIGH',
    priority_level: 'must-read',
    has_chunks: true,
    has_summary: true,
    recommendation_score: 0.94,
    recommendation_reason: 'Matches your sparse-attention and MoE research threads',
    ...overrides,
  };
}

const INBOX_PAPERS = [
  makeFeedPaper({ id: 101, external_id: 'arxiv:2501.00101', title: 'Sparse Mixture-of-Experts for Efficient Long-Context Inference', authors: ['Zhuang Liu', 'Barret Zoph', 'Ekin Dogus Cubuk'], recommendation_score: 0.94, state: 'inbox', priority_level: 'must-read' }),
  makeFeedPaper({ id: 102, external_id: 'arxiv:2501.00202', title: 'Test-Time Compute Scaling via Iterative Self-Refinement', authors: ['Sewon Min', 'Hannaneh Hajishirzi'], recommendation_score: 0.91, state: 'inbox', abstract: 'We study how iterative self-refinement at test time scales with compute, showing consistent improvements across reasoning benchmarks.', tldr: 'More test-time compute → better reasoning via self-refinement.' }),
  makeFeedPaper({ id: 103, external_id: 'arxiv:2210.10760', title: 'Scaling Laws for Reward Model Overoptimization in RLHF', authors: ['Leo Gao', 'John Schulman', 'Jacob Hilton'], recommendation_score: 0.88, state: 'inbox', abstract: 'We study overoptimization of proxy reward models in RLHF and derive empirical scaling laws for reward hacking.', tldr: 'Empirical scaling laws for reward model overoptimization in RLHF.' }),
  makeFeedPaper({ id: 104, external_id: 'arxiv:2407.08608', title: 'FlashAttention-3: Fast and Accurate Attention with Asynchrony and Low-precision', authors: ['Jay Shah', 'Ganesh Bikshandi', 'Ying Zhang', 'Tri Dao'], recommendation_score: 0.85, state: 'inbox', abstract: 'FlashAttention-3 exploits H100 asynchrony to achieve 1.5–2.0× speedups over FA2.', tldr: 'H100-optimized attention: 1.5–2.0× speedup over FlashAttention-2.' }),
  makeFeedPaper({ id: 105, external_id: 'arxiv:2501.00505', title: 'Mechanistic Interpretability of Chain-of-Thought Reasoning', authors: ['Atticus Geiger', 'Zhengxuan Wu', 'Christopher Potts'], recommendation_score: 0.82, state: 'inbox', abstract: 'We identify the circuits responsible for chain-of-thought reasoning in transformer models through causal intervention experiments.', tldr: 'Circuit-level analysis of CoT reveals sparse, compositional reasoning structures.' }),
];

const LIBRARY_PAPERS = [
  makeFeedPaper({ id: 201, external_id: 'arxiv:1706.03762', title: 'Attention Is All You Need', authors: ['Ashish Vaswani', 'Noam Shazeer', 'Niki Parmar', 'Jakob Uszkoreit'], recommendation_score: null, state: 'to_read', starred: true, priority_level: 'must-read', published_date: '2017-06-12', citation_count: 98432, abstract: 'We propose the Transformer, a model architecture based solely on attention mechanisms, dispensing with recurrence and convolutions.', tldr: 'Pure-attention Transformer architecture; 98k citations.' }),
  makeFeedPaper({ id: 202, external_id: 'arxiv:2005.11401', title: 'Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks', authors: ['Patrick Lewis', 'Ethan Perez', 'Aleksandra Piktus'], recommendation_score: null, state: 'reading', starred: true, priority_level: 'must-read', published_date: '2020-05-22', citation_count: 12741, abstract: 'We explore a general-purpose fine-tuning recipe for retrieval-augmented generation, combining parametric memory with non-parametric memory.', tldr: 'RAG combines retrieval with generative models for knowledge-intensive tasks.' }),
  makeFeedPaper({ id: 203, external_id: 'arxiv:2005.14165', title: 'Language Models are Few-Shot Learners', authors: ['Tom B. Brown', 'Benjamin Mann', 'Nick Ryder'], recommendation_score: null, state: 'done', starred: false, priority_level: 'must-read', published_date: '2020-05-28', citation_count: 42890, abstract: 'We train GPT-3, an autoregressive language model with 175B parameters, and study its few-shot performance.', tldr: 'GPT-3: 175B parameter language model with strong few-shot capabilities.' }),
  makeFeedPaper({ id: 204, external_id: 'arxiv:2210.11416', title: 'Scaling Instruction-Finetuned Language Models', authors: ['Hyung Won Chung', 'Le Hou', 'Shayne Longpre'], recommendation_score: null, state: 'to_read', starred: false, priority_level: 'recommended', published_date: '2022-10-20', citation_count: 5621, abstract: 'We explore instruction finetuning across model scales and show that it consistently improves performance and usability.', tldr: 'Instruction finetuning scales well and improves model usability.' }),
  makeFeedPaper({ id: 205, external_id: 'arxiv:2307.09288', title: 'Llama 2: Open Foundation and Fine-Tuned Chat Models', authors: ['Hugo Touvron', 'Louis Martin', 'Kevin Stone'], recommendation_score: null, state: 'to_read', starred: true, priority_level: 'recommended', published_date: '2023-07-18', citation_count: 9812, abstract: 'We develop and release Llama 2, a collection of pretrained and fine-tuned large language models ranging from 7B to 70B parameters.', tldr: 'Open-source 7B–70B LLMs with chat fine-tuning.' }),
];

const FEED_COUNTS = {
  inbox: 28,
  library: 319,
  reading_list: 47,
  reading: 8,
  done: 124,
  starred: 31,
  trash: 3,
  active: 347,
  kept: 347,
  all_non_trash: 344,
  by_source: { arxiv: 189, semantic_scholar: 112, openalex: 34, pubmed: 12 },
  by_topic: [
    { topic_id: 1, name: 'Large Language Models', count: 98 },
    { topic_id: 2, name: 'Efficient Inference', count: 64 },
    { topic_id: 3, name: 'RLHF & Alignment', count: 52 },
    { topic_id: 4, name: 'Retrieval-Augmented Generation', count: 41 },
    { topic_id: 5, name: 'Mechanistic Interpretability', count: 38 },
    { topic_id: 6, name: 'Multimodal Models', count: 29 },
    { topic_id: 7, name: 'Neural Architecture Search', count: 22 },
    { topic_id: 8, name: 'Continual Learning', count: 21 },
  ],
  untagged: 22,
};

const SOURCES = [
  { source_type: 'arxiv', enabled: true, config: {}, priority: 1, display_order: 1, created_at: '2026-01-01T00:00:00Z' },
  { source_type: 'semantic_scholar', enabled: true, config: {}, priority: 2, display_order: 2, created_at: '2026-01-01T00:00:00Z' },
  { source_type: 'openalex', enabled: true, config: {}, priority: 3, display_order: 3, created_at: '2026-01-01T00:00:00Z' },
  { source_type: 'pubmed', enabled: true, config: {}, priority: 4, display_order: 4, created_at: '2026-01-01T00:00:00Z' },
  { source_type: 'local', enabled: true, config: {}, priority: 5, display_order: 5, created_at: '2026-01-01T00:00:00Z' },
];

const MY_DAY_RESPONSE = {
  tasks: [
    { id: 1, project_id: 1, title: 'Write §3 — sparse attention analysis', priority: 1, deadline: '2026-06-05', status: 'in_progress', completed_at: null, project_name: 'Thesis Chapter 3', project_color: '#6366f1' },
    { id: 2, project_id: 1, title: 'Read FlashAttention-3 paper', priority: 2, deadline: null, status: 'todo', completed_at: null, project_name: 'Thesis Chapter 3', project_color: '#6366f1' },
    { id: 3, project_id: 2, title: 'Review RLHF baseline experiments', priority: 2, deadline: '2026-06-07', status: 'todo', completed_at: null, project_name: 'Alignment Research', project_color: '#f59e0b' },
    { id: 4, project_id: null, title: 'Prepare weekly lab meeting slides', priority: 3, deadline: '2026-06-06', status: 'todo', completed_at: null, project_name: null, project_color: null },
  ],
  cards_due: 14,
  recommendations: [
    { recommendation_id: 1, paper_id: 101, score: 0.94, title: 'Sparse Mixture-of-Experts for Efficient Long-Context Inference', authors: ['Zhuang Liu', 'Barret Zoph'] },
    { recommendation_id: 2, paper_id: 102, score: 0.91, title: 'Test-Time Compute Scaling via Iterative Self-Refinement', authors: ['Sewon Min', 'Hannaneh Hajishirzi'] },
  ],
  today_focus_hours: 1.5,
  focus_streak_days: 7,
  project_pulse: [
    { id: 1, name: 'Thesis Chapter 3', color: '#6366f1', total_tasks: 12, done_tasks: 4, next_milestone: 'Draft submission', next_milestone_deadline: '2026-07-01' },
    { id: 2, name: 'Alignment Research', color: '#f59e0b', total_tasks: 8, done_tasks: 3, next_milestone: 'Baseline run', next_milestone_deadline: '2026-06-20' },
    { id: 3, name: 'Infra & Tooling', color: '#10b981', total_tasks: 5, done_tasks: 5, next_milestone: null, next_milestone_deadline: null },
  ],
};

// WeeklyDigestResponse — per-topic theme clusters with verified/unverified badges.
// Shape grounded against WeeklyDigestResponse in src/types/index.ts:1222.
const WEEKLY_DIGEST = {
  total_papers: 11,
  period_start: '2026-05-29',
  period_end: '2026-06-04',
  topics: [
    {
      name: 'Efficient Inference',
      paper_count: 5,
      summary:
        'Sparse routing and asynchronous attention dominate this week — the shared thread is reducing memory bandwidth without quality loss.',
      themes: [
        { theme: 'Top-k sparse MoE gating yields 2.4× long-context throughput at <1% quality loss.', supporting_papers: [101], notes: null, verified: true, verification_reason: null },
        { theme: 'FlashAttention-3 exploits H100 asynchrony for a 1.5–2.0× speedup over FA2.', supporting_papers: [104], notes: null, verified: true, verification_reason: null },
        { theme: 'Inference-aware cost modelling revisits Chinchilla-optimal scaling.', supporting_papers: [106], notes: null, verified: false, verification_reason: 'Claim paraphrases two papers; exact supporting quote not located in corpus.' },
      ],
      top_papers: [
        { paper_id: 101, title: 'Sparse Mixture-of-Experts for Efficient Long-Context Inference', url: 'https://arxiv.org/abs/2501.00101', confidence: 'HIGH', relevance_score: 0.94 },
        { paper_id: 104, title: 'FlashAttention-3: Fast and Accurate Attention with Asynchrony', url: 'https://arxiv.org/abs/2407.08608', confidence: 'HIGH', relevance_score: 0.85 },
      ],
    },
    {
      name: 'RLHF & Alignment',
      paper_count: 3,
      summary:
        'Reward-model overoptimization remains the open question — empirical scaling curves are now available to bound it.',
      themes: [
        { theme: 'Proxy-reward overoptimization follows a predictable scaling law in RLHF.', supporting_papers: [103], notes: null, verified: true, verification_reason: null },
        { theme: 'Reward collapse correlates with out-of-distribution prompts more than raw overfit.', supporting_papers: [103], notes: null, verified: true, verification_reason: null },
      ],
      top_papers: [
        { paper_id: 103, title: 'Scaling Laws for Reward Model Overoptimization in RLHF', url: 'https://arxiv.org/abs/2210.10760', confidence: 'HIGH', relevance_score: 0.88 },
      ],
    },
  ],
};

// MyDayBundle — single round-trip that primes per-section caches (MyDayPage.tsx).
// Shape grounded against MyDayBundle in src/types/index.ts:1295.
const MY_DAY_BUNDLE = {
  tasks: MY_DAY_RESPONSE.tasks,
  intent: { intent: 'Deep work: finish §3.2 sparse-routing analysis + write FlashAttention comparison table.', updated_at: '2026-06-04T07:30:00Z' },
  threads: [
    { id: 1, title: 'Sparse attention vs dense: when does routing overhead pay off?', anchor: 'Re-read §4 of the MoE paper before benchmarking.', progress: 0.45, last_at: '2026-06-03T09:15:00Z', status: 'open', created_at: '2026-06-02T14:00:00Z' },
    { id: 2, title: 'RLHF reward collapse — relationship to overfit or OOD?', anchor: null, progress: 0.2, last_at: '2026-06-03T16:00:00Z', status: 'open', created_at: '2026-06-01T10:00:00Z' },
  ],
  yesterday: { date: '2026-06-03', focused_hours: 3.5, cards_reviewed: 18, tasks_done: 2, completed: [{ id: 10, title: 'Submit preprint revision', status: 'done' }], deferred: [] },
  journal: null,
};

const RETENTION_STATS = {
  total_cards: 284,
  due_now: 14,
  reviewed_today: 6,
  average_retention: 87.4,
  reviews_by_rating: { '1': 4, '2': 8, '3': 42, '4': 89 },
  streak_days: 12,
};

const KNOWLEDGE_GRAPH = {
  entities: [
    { id: 1, name: 'Transformer', canonical_name: 'Transformer', entity_type: 'method', description: 'Sequence-to-sequence architecture using self-attention', paper_count: 87, created_at: '2026-01-01T00:00:00Z', metadata: {} },
    { id: 2, name: 'BERT', canonical_name: 'BERT', entity_type: 'method', description: 'Bidirectional encoder representations from transformers', paper_count: 54, created_at: '2026-01-01T00:00:00Z', metadata: {} },
    { id: 3, name: 'GPT-4', canonical_name: 'GPT-4', entity_type: 'method', description: 'Large multimodal language model by OpenAI', paper_count: 41, created_at: '2026-01-01T00:00:00Z', metadata: {} },
    { id: 4, name: 'ImageNet', canonical_name: 'ImageNet', entity_type: 'dataset', description: 'Large-scale visual recognition benchmark', paper_count: 38, created_at: '2026-01-01T00:00:00Z', metadata: {} },
    { id: 5, name: 'RLHF', canonical_name: 'RLHF', entity_type: 'concept', description: 'Reinforcement learning from human feedback for LLM alignment', paper_count: 31, created_at: '2026-01-01T00:00:00Z', metadata: {} },
    { id: 6, name: 'Attention Mechanism', canonical_name: 'Attention Mechanism', entity_type: 'concept', description: 'Core building block of modern neural networks', paper_count: 29, created_at: '2026-01-01T00:00:00Z', metadata: {} },
    { id: 7, name: 'LoRA', canonical_name: 'LoRA', entity_type: 'method', description: 'Low-rank adaptation for parameter-efficient fine-tuning', paper_count: 26, created_at: '2026-01-01T00:00:00Z', metadata: {} },
    { id: 8, name: 'RAG', canonical_name: 'Retrieval-Augmented Generation', entity_type: 'method', description: 'Hybrid retrieval + generation for knowledge-intensive tasks', paper_count: 22, created_at: '2026-01-01T00:00:00Z', metadata: {} },
    { id: 9, name: 'Chain-of-Thought', canonical_name: 'Chain-of-Thought Prompting', entity_type: 'concept', description: 'Prompting technique eliciting step-by-step reasoning', paper_count: 19, created_at: '2026-01-01T00:00:00Z', metadata: {} },
  ],
  // Hub-and-spoke topology: "Transformer" (id 1) is the sole high-degree hub,
  // every other entity a degree-1/2 leaf. The "Concentric" layout keys on node
  // degree, so this seats Transformer alone at the centre and fans the eight
  // leaves into one evenly-spaced outer ring — a clean, legible README graph
  // (a flat degree distribution instead makes concentric pile everything in the
  // middle and the labels collide).
  relationships: [
    { id: 1, source_entity_id: 1, target_entity_id: 2, relationship_type: 'basis_for', paper_id: 201, evidence_quote: null, confidence: 0.95, created_at: '2026-01-01T00:00:00Z' },
    { id: 2, source_entity_id: 1, target_entity_id: 3, relationship_type: 'basis_for', paper_id: 201, evidence_quote: null, confidence: 0.95, created_at: '2026-01-01T00:00:00Z' },
    { id: 3, source_entity_id: 1, target_entity_id: 6, relationship_type: 'uses', paper_id: 201, evidence_quote: null, confidence: 0.90, created_at: '2026-01-01T00:00:00Z' },
    { id: 4, source_entity_id: 1, target_entity_id: 5, relationship_type: 'aligned_by', paper_id: 103, evidence_quote: null, confidence: 0.88, created_at: '2026-01-01T00:00:00Z' },
    { id: 5, source_entity_id: 1, target_entity_id: 7, relationship_type: 'fine_tuned_by', paper_id: 202, evidence_quote: null, confidence: 0.85, created_at: '2026-01-01T00:00:00Z' },
    { id: 6, source_entity_id: 1, target_entity_id: 8, relationship_type: 'extended_by', paper_id: 202, evidence_quote: null, confidence: 0.82, created_at: '2026-01-01T00:00:00Z' },
    { id: 7, source_entity_id: 1, target_entity_id: 9, relationship_type: 'prompted_by', paper_id: 204, evidence_quote: null, confidence: 0.79, created_at: '2026-01-01T00:00:00Z' },
    { id: 8, source_entity_id: 1, target_entity_id: 4, relationship_type: 'evaluated_on', paper_id: 201, evidence_quote: null, confidence: 0.78, created_at: '2026-01-01T00:00:00Z' },
  ],
  entity_type_counts: { method: 5, concept: 3, dataset: 1 },
};

// ─────────────────────────────────────────────────────────────────────────────
// Ask (cross-paper RAG) — completed Q&A fixture
//
// The Ask answer is rendered by streaming a complete, well-formed SSE response
// from POST /api/ask/stream. The shape is grounded against StreamEvent in
// src/lib/sse.ts:27 (token | sources | confidence | done). The answer carries
// inline [n] citations, a HIGH-confidence "Verified" badge, per-sentence
// verification (one sentence intentionally unverified → yellow <mark>), and a
// sources list with page numbers — the anti-hallucination design on display.
// ─────────────────────────────────────────────────────────────────────────────

const ASK_QUESTION =
  'How do sparse mixture-of-experts methods compare to dense transformers for long-context inference, and what are the throughput/quality trade-offs?';

// The answer text. Inline [n] markers reference the sources list below.
const ASK_ANSWER = [
  'For long-context inference, sparse mixture-of-experts (MoE) models route each token to only the top-k experts, so the activated parameter count — and therefore the memory bandwidth per token — stays far below a dense transformer of equivalent total size [1].',
  ' Empirically this yields a 2.4× throughput improvement at 128k-token contexts with under 1% quality degradation on standard LLM benchmarks [1].',
  ' Dense transformers, by contrast, activate every parameter for every token, so their long-context cost is dominated by the quadratic attention term and the full feed-forward pass [3].',
  ' Kernel-level work narrows part of that gap: FlashAttention-3 exploits H100 asynchrony to deliver a 1.5–2.0× attention speedup over FlashAttention-2, which benefits dense and sparse models alike [2].',
  ' The principal trade-off is that MoE routing adds load-balancing overhead and can underperform dense models on tasks where expert specialization fragments rare-token coverage.',
  ' Inference-aware cost modelling suggests the sparse advantage widens as deployment scale grows, since the per-token compute savings compound across long sequences [3].',
].join('');

// Per-sentence verification. Five of six sentences verified against retrieved
// source chunks; the routing-overhead caveat is left unverified to surface the
// per-sentence highlight (yellow <mark>) without tripping the amber low-confidence
// banner (which only shows when confidence !== 'HIGH').
const ASK_PER_SENTENCE = [
  { text: 'For long-context inference, sparse mixture-of-experts (MoE) models route each token to only the top-k experts, so the activated parameter count — and therefore the memory bandwidth per token — stays far below a dense transformer of equivalent total size [1].', verified: true },
  { text: 'Empirically this yields a 2.4× throughput improvement at 128k-token contexts with under 1% quality degradation on standard LLM benchmarks [1].', verified: true },
  { text: 'Dense transformers, by contrast, activate every parameter for every token, so their long-context cost is dominated by the quadratic attention term and the full feed-forward pass [3].', verified: true },
  { text: 'Kernel-level work narrows part of that gap: FlashAttention-3 exploits H100 asynchrony to deliver a 1.5–2.0× attention speedup over FlashAttention-2, which benefits dense and sparse models alike [2].', verified: true },
  { text: 'The principal trade-off is that MoE routing adds load-balancing overhead and can underperform dense models on tasks where expert specialization fragments rare-token coverage.', verified: false },
  { text: 'Inference-aware cost modelling suggests the sparse advantage widens as deployment scale grows, since the per-token compute savings compound across long sequences [3].', verified: true },
];

const ASK_SOURCES = [
  {
    chunk_id: 9101,
    paper_id: 101,
    paper_title: '[1] Sparse Mixture-of-Experts for Efficient Long-Context Inference (Liu et al., 2026)',
    text: 'Routing only the top-k experts per token reduces activated memory bandwidth and yields a 2.4× throughput improvement at 128k context length with <1% quality degradation across standard LLM benchmarks.',
    page_number: 4,
    score: 0.912,
  },
  {
    chunk_id: 9104,
    paper_id: 104,
    paper_title: '[2] FlashAttention-3: Fast and Accurate Attention with Asynchrony (Shah et al., 2024)',
    text: 'By overlapping computation and memory movement on H100 GPUs, FlashAttention-3 attains a 1.5–2.0× speedup over FlashAttention-2 while preserving numerical accuracy.',
    page_number: 7,
    score: 0.864,
  },
  {
    chunk_id: 9106,
    paper_id: 106,
    paper_title: '[3] Beyond Chinchilla-Optimal: Accounting for Inference in LLM Cost Modelling (Sardana & Frankle, 2024)',
    text: 'When inference cost is folded into the scaling objective, compute-per-token savings dominate at deployment scale, favouring architectures that activate fewer parameters per token.',
    page_number: 3,
    score: 0.831,
  },
];

/**
 * Build a complete SSE body: token frames (so the answer renders progressively),
 * then a single sources frame, a confidence frame, and a terminal done frame.
 * Frames follow the `data: {json}\n` shape parsed by readSSEFrames (sse.ts:85).
 */
function buildAskStreamBody(): string {
  const frames: string[] = [];
  // Stream the answer as fixed-width character chunks so it arrives
  // progressively yet reassembles to the EXACT text (sentence-boundary
  // splitting would fragment the decimal points in "2.4×"/"1.5–2.0×" and drop
  // characters, corrupting both the answer and the per-sentence highlight match).
  const CHUNK = 64;
  for (let i = 0; i < ASK_ANSWER.length; i += CHUNK) {
    frames.push(`data: ${JSON.stringify({ type: 'token', content: ASK_ANSWER.slice(i, i + CHUNK) })}\n`);
  }
  frames.push(`data: ${JSON.stringify({ type: 'sources', sources: ASK_SOURCES })}\n`);
  frames.push(
    `data: ${JSON.stringify({ type: 'confidence', confidence: 'HIGH', verified_fraction: 5 / 6, per_sentence: ASK_PER_SENTENCE })}\n`,
  );
  frames.push(`data: ${JSON.stringify({ type: 'done', model_used: null })}\n`);
  frames.push('data: [DONE]\n');
  return frames.join('');
}

async function installAskRoutes(page: Page): Promise<void> {
  await installCommonRoutes(page);

  // POST /api/ask/stream → complete mocked SSE response.
  await page.route('**/api/ask/stream**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: buildAskStreamBody() });
  });

  // OnboardingTour eligibility check — non-empty so the tour stays hidden.
  await page.route('**/api/papers/feed**', async (route: Route) => {
    if (route.request().url().includes('/counts')) { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers: INBOX_PAPERS.slice(0, 1), total: 1 }) });
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Route-install helpers
// ─────────────────────────────────────────────────────────────────────────────

async function installCommonRoutes(page: Page): Promise<void> {
  // Setup status — must return setup_completed=true or OnboardingWizard renders
  await page.route('**/api/setup/status**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SETUP_STATUS) });
  });

  // Jobs SSE + listing — return empty so no job banners appear.
  //
  // NOTE: this is a RegExp, not a `**/api/jobs**` glob, on purpose. Under
  // `vite dev` the source module `/src/lib/api/jobs.ts` is served verbatim, and
  // a `**/api/jobs**` glob ALSO matches that module path — intercepting the JS
  // module and returning JSON, which blanks the whole app ("Failed to load
  // module script: …MIME type application/json"). Anchoring to a real API path
  // (`/api/jobs` followed by end, `?`, or `/`) avoids the dev-server collision
  // while still matching `/api/jobs`, `/api/jobs?…`, and `/api/jobs/stream`.
  await page.route(/\/api\/jobs(\?|\/|$)/, async (route: Route) => {
    const url = route.request().url();
    if (url.includes('/stream') || url.includes('stream')) {
      // SSE streams: return a minimal well-formed event-stream with no events
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });

  // Sources (used by multiple pages)
  await page.route('**/api/sources**', async (route: Route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SOURCES) });
  });

  // ── Defect 1: kill the "N down" red health badge in the sidebar footer ──
  // fetchStackHealth() calls these 3 endpoints. Mock all as healthy so
  // the pill renders "All healthy" (green) instead of "2 down" (red).
  //
  // /health/paper_ingestion/internal — per-dependency breakdown (postgres/qdrant/litellm/ollama/vector)
  await page.route('**/health/paper_ingestion/internal**', async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'ok',
        checks: {
          postgres: 'ok',
          qdrant: 'ok',
          litellm: 'ok',
          ollama: 'ok',
          vector: 'ok',
        },
      }),
    });
  });
  // /health/paper_ingestion — public service-level check
  await page.route('**/health/paper_ingestion**', async (route: Route) => {
    // Don't re-match the /internal sub-path handled above
    if (route.request().url().includes('/internal')) { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) });
  });
  // /health/learning_engine — public service-level check
  await page.route('**/health/learning_engine**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) });
  });

  // Suppress post-auth system-status (fetched by AIPanel/AccessModeSection) — keeps screenshots
  // free of red banners without mocking the full settings page.
  await page.route('**/api/system/setup-status**', async (route: Route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ configured: true, setup_completed: true, setup_mode: 'single', hw_tier_changed: false }) });
  });

  // Suppress logs/summary endpoint (sidebar log badge) — silences 403s.
  await page.route('**/api/logs/summary**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ error_count: 0, warn_count: 0, last_updated: null }) });
  });

  // Topics — return non-empty so OnboardingTour eligibility (zeroTopics&&zeroPapers) is false
  // This prevents the Joyride "Connect a Source" overlay from appearing.
  await page.route('**/api/topics**', async (route: Route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        { id: 1, name: 'Large Language Models', query_terms: ['LLM', 'transformer'], category: null, description: null, enabled: true, created_at: '2026-01-01T00:00:00Z' },
        { id: 2, name: 'Efficient Inference', query_terms: ['inference', 'quantization'], category: null, description: null, enabled: true, created_at: '2026-01-01T00:00:00Z' },
      ]),
    });
  });
}

async function installMyDayRoutes(page: Page): Promise<void> {
  await installCommonRoutes(page);

  await page.route('**/api/executive/my-day**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MY_DAY_RESPONSE) });
  });

  await page.route('**/api/executive/intent/today**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ intent: 'Deep work: finish §3.2 sparse-routing analysis + write FlashAttention comparison table.', updated_at: '2026-06-04T07:30:00Z' }) });
  });

  await page.route('**/api/pulse/today**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PULSE_DECK) });
  });

  await page.route('**/api/papers/feed**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers: [], total: 0 }) });
  });

  await page.route('**/api/analytics/missing-foundational**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });

  await page.route('**/api/stats**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(RETENTION_STATS) });
  });

  await page.route('**/api/my-day/yesterday**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ date: '2026-06-03', focused_hours: 3.5, cards_reviewed: 18, tasks_done: 2, completed: [{ id: 10, title: 'Submit preprint revision' }], deferred: [] }) });
  });

  await page.route('**/api/my-day/threads**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MY_DAY_BUNDLE.threads) });
  });

  await page.route('**/api/my-day/journal**', async (route: Route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });

  await page.route('**/api/executive/tasks**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MY_DAY_RESPONSE.tasks) });
  });

  // GET /api/digest/weekly → WeeklyDigestResponse. The response MUST carry a
  // `topics` array: WeeklyDigestSection reads `data.topics.length`, so a
  // shapeless `{summary, generated_at}` body throws "Cannot read properties of
  // undefined (reading 'length')" and trips the ErrorBoundary.
  await page.route('**/api/digest/weekly**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(WEEKLY_DIGEST) });
  });

  // GET /api/executive/my-day-bundle → MyDayBundle. MyDayPage primes per-section
  // caches from this single round-trip; without it apiFetch receives the SPA
  // index.html and throws a JSON-parse console error.
  await page.route('**/api/executive/my-day-bundle**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MY_DAY_BUNDLE) });
  });
}

async function installPulseRoutes(page: Page): Promise<void> {
  await installCommonRoutes(page);

  await page.route('**/api/pulse/today**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PULSE_DECK) });
  });

  await page.route('**/api/pulse/history**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });

  await page.route('**/api/pulse/stats**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      window_days: 30,
      decks_generated: 21,
      avg_candidates: 45.2,
      avg_llm_calls: 7.1,
      avg_duration_s: 12.4,
      last_run_at: '2026-06-04T06:00:00Z',
      last_error: null,
      degraded_reason: null,
    }) });
  });

  // Suppress OnboardingTour feed check — return non-empty so zeroPapers=false.
  await page.route('**/api/papers/feed**', async (route: Route) => {
    if (route.request().url().includes('/counts')) { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers: INBOX_PAPERS.slice(0, 1), total: 1 }) });
  });
}

async function installFeedRoutes(page: Page): Promise<void> {
  await installCommonRoutes(page);

  // ── Defect 3: fix route ordering so /counts sub-path is never swallowed ──
  // Register the broader glob FIRST — Playwright matches in reverse-registration
  // order (last registered = highest priority). So the /counts-specific handler
  // registered after this one will win for counts URLs.
  await page.route('**/api/papers/feed**', async (route: Route) => {
    // Guard: delegate /counts sub-path to the more-specific handler below.
    if (route.request().url().includes('/counts')) { await route.continue(); return; }
    const url = new URL(route.request().url());
    // The real fetchFeed sends ?view= (not ?surface=) per the papers.ts SSOT.
    const view = url.searchParams.get('view') ?? url.searchParams.get('surface');
    const papers = view === 'library' ? LIBRARY_PAPERS : INBOX_PAPERS;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers, total: papers.length }) });
  });

  // Registered after the feed glob → higher priority in Playwright's reverse order.
  await page.route('**/api/papers/feed/counts**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FEED_COUNTS) });
  });

  // topics already mocked in installCommonRoutes; skip duplicate registration.

  await page.route('**/api/pulse/today**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PULSE_DECK) });
  });

  await page.route('**/api/pulse/history**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });
}

async function installKnowledgeGraphRoutes(page: Page): Promise<void> {
  await installCommonRoutes(page);

  await page.route('**/api/knowledge-graph**', async (route: Route) => {
    // Handle both /api/knowledge-graph and /api/knowledge-graph?*
    if (route.request().url().includes('/query')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ results: [] }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(KNOWLEDGE_GRAPH) });
  });

  // Suppress OnboardingTour feed check — return non-empty so zeroPapers=false.
  await page.route('**/api/papers/feed**', async (route: Route) => {
    if (route.request().url().includes('/counts')) { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers: INBOX_PAPERS.slice(0, 1), total: 1 }) });
  });
}

async function installHomeRoutes(page: Page): Promise<void> {
  await installCommonRoutes(page);

  await page.route('**/api/dashboard/metrics**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(DASHBOARD_METRICS) });
  });

  await page.route('**/api/system/capabilities**', async (route: Route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      has_ollama: true,
      ollama_running: true,
      has_gpu: true,
      gpu_vram_gb: 24,
    }) });
  });

  // Mock feed for OnboardingTour eligibility check (fetchFeed({limit:1})).
  // Must return non-empty papers so zeroPapers=false and the tour stays hidden.
  await page.route('**/api/papers/feed**', async (route: Route) => {
    if (route.request().url().includes('/counts')) { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ papers: INBOX_PAPERS.slice(0, 1), total: 1 }) });
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Screenshot tests
// ─────────────────────────────────────────────────────────────────────────────

// Block service workers so page.route() mocks intercept API calls.
// The JARVIS SW (stale-while-revalidate for /api/dashboard/metrics,
// /api/papers/feed, etc.) intercepts fetches before Playwright can, making
// the route mocks unreachable. Blocking SW ensures all API traffic is
// observable and mockable.
test.use({ viewport: VIEWPORT, serviceWorkers: 'block' });

test('01-home: HomePage at /', async ({ page }) => {
  await seedAuthedSession(page);
  await installHomeRoutes(page);

  // Dismiss checklist so hero is metrics-focused (onboarding_stage=complete).
  // Also suppress the Joyride onboarding tour (LOCAL_STORAGE_KEY = 'jarvis-onboarding-dismissed').
  await page.addInitScript(() => {
    const uiState = { state: { checklistDismissed: true, sidebarOpen: true }, version: 0 };
    localStorage.setItem('jarvis-ui', JSON.stringify(uiState));
    // Defect 4: prevent Joyride "Connect a Source" overlay
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  });

  const consoleErrors01: string[] = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors01.push(msg.text()); });
  page.on('response', (resp) => { if (!resp.ok() && resp.url().includes('/api/')) console.warn(`[01-home] non-2xx: ${resp.status()} ${resp.url()}`); });

  await page.goto('/');
  // Wait for the Dashboard heading and metric tiles to load
  await page.waitForSelector('h1', { timeout: 10000 });
  await page.waitForLoadState('networkidle');

  // Hide scrollbar
  await page.addStyleTag({ content: '::-webkit-scrollbar { display: none !important; }' });
  await page.waitForTimeout(400);

  if (consoleErrors01.length) console.warn('[01-home] console errors:', consoleErrors01);

  await page.screenshot({
    path: path.join(SCREENSHOTS_DIR, '01-home.png'),
    fullPage: false,
    clip: { x: 0, y: 0, width: VIEWPORT.width, height: VIEWPORT.height },
  });
});

test('02-my-day: MyDayPage at /my-day', async ({ page }) => {
  await seedAuthedSession(page);
  await installMyDayRoutes(page);

  // Pre-seed active Pomodoro so the hero tab is richer
  await page.addInitScript(() => {
    localStorage.setItem(
      'jarvis-pomodoro',
      JSON.stringify({
        state: {
          phase: 'work',
          secondsRemaining: 1247,
          phaseDurationMs: 1_500_000,
          totalPausedMs: 0,
          cyclesCompleted: 1,
          targetCycles: 4,
          workMinutes: 25,
          shortBreakMinutes: 5,
          longBreakMinutes: 15,
          startedAt: Date.now() - 253_000,
          pausedAt: null,
          attachedItem: { id: 1, title: 'Write §3 — sparse attention analysis', type: 'task' },
          completedSession: null,
        },
        version: 1,
      }),
    );
    const uiState = { state: { checklistDismissed: true, sidebarOpen: true }, version: 0 };
    localStorage.setItem('jarvis-ui', JSON.stringify(uiState));
    // Defect 4: prevent Joyride "Connect a Source" overlay
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  });

  const consoleErrors02: string[] = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors02.push(msg.text()); });
  page.on('response', (resp) => { if (!resp.ok() && resp.url().includes('/api/')) console.warn(`[02-my-day] non-2xx: ${resp.status()} ${resp.url()}`); });

  await page.goto('/my-day');
  // RESEARCH LOG header is the reliable ready-signal
  await page.waitForSelector('text=RESEARCH LOG', { timeout: 10000 });
  await page.waitForLoadState('networkidle');

  await page.addStyleTag({ content: '::-webkit-scrollbar { display: none !important; }' });
  await page.waitForTimeout(400);

  if (consoleErrors02.length) console.warn('[02-my-day] console errors:', consoleErrors02);

  await page.screenshot({
    path: path.join(SCREENSHOTS_DIR, '02-my-day.png'),
    fullPage: false,
    clip: { x: 0, y: 0, width: VIEWPORT.width, height: VIEWPORT.height },
  });
});

test('03-pulse: PulseDeckPage at /pulse', async ({ page }) => {
  await seedAuthedSession(page);
  await installPulseRoutes(page);

  await page.addInitScript(() => {
    const uiState = { state: { checklistDismissed: true, sidebarOpen: true }, version: 0 };
    localStorage.setItem('jarvis-ui', JSON.stringify(uiState));
    // Defect 4: prevent Joyride "Connect a Source" overlay
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  });

  const consoleErrors03: string[] = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors03.push(msg.text()); });
  page.on('response', (resp) => { if (!resp.ok() && resp.url().includes('/api/')) console.warn(`[03-pulse] non-2xx: ${resp.status()} ${resp.url()}`); });

  await page.goto('/pulse');
  await page.waitForLoadState('networkidle');
  // Wait for at least one pulse card to appear
  await page.waitForSelector('[data-testid="pulse-card"], .pulse-card, h2, h3', { timeout: 10000 });
  await page.waitForTimeout(600);

  await page.addStyleTag({ content: '::-webkit-scrollbar { display: none !important; }' });

  if (consoleErrors03.length) console.warn('[03-pulse] console errors:', consoleErrors03);

  await page.screenshot({
    path: path.join(SCREENSHOTS_DIR, '03-pulse.png'),
    fullPage: false,
    clip: { x: 0, y: 0, width: VIEWPORT.width, height: VIEWPORT.height },
  });
});

test('04-library: ResearchFeedPage library at /feed?surface=library', async ({ page }) => {
  await seedAuthedSession(page);
  await installFeedRoutes(page);

  await page.addInitScript(() => {
    const uiState = { state: { checklistDismissed: true, sidebarOpen: true }, version: 0 };
    localStorage.setItem('jarvis-ui', JSON.stringify(uiState));
    // Defect 4: prevent Joyride "Connect a Source" overlay
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  });

  const consoleErrors04: string[] = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors04.push(msg.text()); });
  page.on('response', (resp) => { if (!resp.ok() && resp.url().includes('/api/')) console.warn(`[04-library] non-2xx: ${resp.status()} ${resp.url()}`); });

  await page.goto('/feed?surface=library');
  await page.waitForLoadState('networkidle');
  // Wait for at least one paper row to appear
  await page.waitForTimeout(1000);

  await page.addStyleTag({ content: '::-webkit-scrollbar { display: none !important; }' });

  if (consoleErrors04.length) console.warn('[04-library] console errors:', consoleErrors04);

  await page.screenshot({
    path: path.join(SCREENSHOTS_DIR, '04-library.png'),
    fullPage: false,
    clip: { x: 0, y: 0, width: VIEWPORT.width, height: VIEWPORT.height },
  });
});

test('05-discover: ResearchFeedPage Search/Discover at /feed', async ({ page }) => {
  await seedAuthedSession(page);
  await installFeedRoutes(page);

  // Mock search-preview so discover shows results
  await page.route('**/api/search-preview**', async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        results: [
          { title: 'Sparse Mixture-of-Experts for Efficient Long-Context Inference', authors: ['Zhuang Liu', 'Barret Zoph'], abstract: 'We propose a sparse gating mechanism that achieves 2.4× throughput improvement.', published_date: '2026-01-15', source_type: 'arxiv', external_id: '2501.00101', citation_count: 142 },
          { title: 'Test-Time Compute Scaling via Iterative Self-Refinement', authors: ['Sewon Min', 'Hannaneh Hajishirzi'], abstract: 'We study how iterative self-refinement at test time scales with compute budgets.', published_date: '2026-01-22', source_type: 'arxiv', external_id: '2501.00202', citation_count: 87 },
          { title: 'Mechanistic Interpretability of Chain-of-Thought Reasoning', authors: ['Atticus Geiger', 'Zhengxuan Wu'], abstract: 'Circuit-level analysis of CoT reveals sparse, compositional reasoning structures.', published_date: '2026-01-10', source_type: 'semantic_scholar', external_id: '2501.00505', citation_count: 54 },
          { title: 'FlashAttention-3: Fast and Accurate Attention with Asynchrony', authors: ['Jay Shah', 'Tri Dao'], abstract: 'H100 asynchrony enables 1.5–2.0× speedup over FlashAttention-2.', published_date: '2024-07-12', source_type: 'arxiv', external_id: '2407.08608', citation_count: 321 },
        ],
        total: 4,
      }),
    });
  });

  await page.addInitScript(() => {
    const uiState = { state: { checklistDismissed: true, sidebarOpen: true }, version: 0 };
    localStorage.setItem('jarvis-ui', JSON.stringify(uiState));
    // Defect 4: prevent Joyride "Connect a Source" overlay
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  });

  const consoleErrors05: string[] = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors05.push(msg.text()); });
  page.on('response', (resp) => { if (!resp.ok() && resp.url().includes('/api/')) console.warn(`[05-discover] non-2xx: ${resp.status()} ${resp.url()}`); });

  await page.goto('/feed');
  await page.waitForLoadState('networkidle');

  // Click the Search tab to open the discover/search view
  const searchTab = page.getByRole('tab', { name: 'Search' });
  if (await searchTab.isVisible({ timeout: 3000 }).catch(() => false)) {
    await searchTab.click();
    await page.waitForTimeout(300);

    // Fill in a search query and fire it
    const searchInput = page.getByPlaceholder(/Search/i).first();
    if (await searchInput.isVisible({ timeout: 2000 }).catch(() => false)) {
      await searchInput.fill('sparse mixture of experts long context inference');
      const searchBtn = page.getByRole('button', { name: /^Search$/i });
      if (await searchBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
        await searchBtn.click();
        await page.waitForTimeout(800);
      }
    }
  }

  await page.waitForLoadState('networkidle');
  await page.addStyleTag({ content: '::-webkit-scrollbar { display: none !important; }' });
  await page.waitForTimeout(400);

  if (consoleErrors05.length) console.warn('[05-discover] console errors:', consoleErrors05);

  await page.screenshot({
    path: path.join(SCREENSHOTS_DIR, '05-discover.png'),
    fullPage: false,
    clip: { x: 0, y: 0, width: VIEWPORT.width, height: VIEWPORT.height },
  });
});

test('06-knowledge-graph: KnowledgeGraphPage at /knowledge', async ({ page }) => {
  await seedAuthedSession(page);
  await installKnowledgeGraphRoutes(page);

  await page.addInitScript(() => {
    const uiState = { state: { checklistDismissed: true, sidebarOpen: true }, version: 0 };
    localStorage.setItem('jarvis-ui', JSON.stringify(uiState));
    // Defect 4: prevent Joyride "Connect a Source / Step 1 of 4" overlay
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  });

  const consoleErrors06: string[] = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors06.push(msg.text()); });
  page.on('response', (resp) => { if (!resp.ok() && resp.url().includes('/api/')) console.warn(`[06-kg] non-2xx: ${resp.status()} ${resp.url()}`); });

  await page.goto('/knowledge');
  await page.waitForLoadState('networkidle');
  // Wait for Knowledge Graph heading
  await page.waitForSelector('h1, h2, h3', { timeout: 10000 });
  await page.waitForSelector('[data-testid="cytoscape-container"]', { timeout: 10000 });

  // The default force-directed (cose) layout clumps a small graph and lets
  // labels collide. Switch to the deterministic "Circle" layout — it spaces all
  // entities evenly around one ring, so the below-node labels never overlap and
  // the hub-and-spoke relationships read clearly across the middle. Driving it
  // through the GraphControls dropdown also remounts CytoscapeGraph with a fresh fit.
  const layoutTrigger = page.getByRole('combobox').filter({ hasText: /Force-directed|Concentric|Circle|Breadth/ }).first();
  if (await layoutTrigger.isVisible({ timeout: 3000 }).catch(() => false)) {
    await layoutTrigger.click();
    await page.getByRole('option', { name: 'Circle' }).click();
  }

  // Let the circle layout settle.
  await page.waitForTimeout(4000);

  // Scroll the graph to the top of the viewport so the full 500px canvas is
  // above the fold (the page chrome — title + filters + query bar — otherwise
  // pushes the graph's lower third below the 900px clip).
  await page.evaluate(() => {
    // Omit `behavior` → default 'auto' (instant) scroll; no animation to wait on.
    document.querySelector('[data-testid="cytoscape-container"]')?.scrollIntoView({ block: 'start' });
  });
  await page.waitForTimeout(600);

  await page.addStyleTag({ content: '::-webkit-scrollbar { display: none !important; }' });

  if (consoleErrors06.length) console.warn('[06-knowledge-graph] console errors:', consoleErrors06);

  await page.screenshot({
    path: path.join(SCREENSHOTS_DIR, '06-knowledge-graph.png'),
    fullPage: false,
    clip: { x: 0, y: 0, width: VIEWPORT.width, height: VIEWPORT.height },
  });
});

test('07-ask: AskPage cross-paper RAG at /ask', async ({ page }) => {
  await seedAuthedSession(page);
  await installAskRoutes(page);

  await page.addInitScript(() => {
    const uiState = { state: { checklistDismissed: true, sidebarOpen: true }, version: 0 };
    localStorage.setItem('jarvis-ui', JSON.stringify(uiState));
    localStorage.setItem('jarvis-onboarding-dismissed', 'true');
  });

  const consoleErrors07: string[] = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors07.push(msg.text()); });
  page.on('response', (resp) => { if (!resp.ok() && resp.url().includes('/api/')) console.warn(`[07-ask] non-2xx: ${resp.status()} ${resp.url()}`); });

  await page.goto('/ask');
  await page.waitForLoadState('networkidle');
  // Ask page header is the ready-signal
  await page.waitForSelector('[data-testid="ask-page"]', { timeout: 10000 });

  // Ask a research question — this fires POST /api/ask/stream (mocked SSE) and
  // the completed answer (inline [n] citations + Verified badge + per-sentence
  // verification) streams into the chat workspace.
  const input = page.getByPlaceholder('Ask a question...');
  await input.fill(ASK_QUESTION);
  await page.getByRole('button', { name: 'Send message' }).click();

  // Wait for the streamed answer to settle (last sentence + Verified badge).
  await page.getByText('Inference-aware cost modelling').waitFor({ timeout: 10000 });
  await page.getByText('Verified', { exact: true }).first().waitFor({ timeout: 10000 });

  // Expand the sources accordion so page-numbered citations are visible.
  const sourcesToggle = page.getByRole('button', { name: /\d+ sources?/ });
  if (await sourcesToggle.isVisible({ timeout: 3000 }).catch(() => false)) {
    await sourcesToggle.click();
    await page.waitForTimeout(300);
  }

  await page.addStyleTag({ content: '::-webkit-scrollbar { display: none !important; }' });
  await page.waitForTimeout(400);

  if (consoleErrors07.length) console.warn('[07-ask] console errors:', consoleErrors07);

  await page.screenshot({
    path: path.join(SCREENSHOTS_DIR, '07-ask.png'),
    fullPage: false,
    clip: { x: 0, y: 0, width: VIEWPORT.width, height: VIEWPORT.height },
  });
});
