import { z } from 'zod';
import type { DashboardMetrics, SystemCapabilities } from '@/types';

const readinessStatusSchema = z.enum(['green', 'amber', 'red']);

export const readinessResponseSchema = z.looseObject({
  status: readinessStatusSchema,
  checks: z.array(z.looseObject({
    name: z.string(),
    status: readinessStatusSchema,
    detail: z.string(),
    remediation: z.string().optional(),
  })),
});

const storageSectionSchema = z.looseObject({
  bytes_used: z.number().nullable(),
  error: z.string().nullable(),
});

export const systemStorageResponseSchema = z.looseObject({
  ollama_models: storageSectionSchema,
  postgres: storageSectionSchema,
  qdrant: storageSectionSchema,
  qdrant_collections: z.array(z.looseObject({
    name: z.string(),
    points_count: z.number().nullable(),
  })),
  hf_cache: storageSectionSchema,
  pressure: z.boolean(),
});

export const modelFitDetailSchema = z.looseObject({
  default: z.enum(['fits', 'partial', 'unfit', 'cloud', 'unknown']),
  at_num_ctx: z.number(),
  required_vram_gb: z.number().nullable(),
  base_vram_gb: z.number().nullable(),
  base_num_ctx: z.number(),
  default_num_ctx: z.number(),
  max_num_ctx: z.number(),
  kv_cache_bytes_per_token: z.number().nullable(),
});

export const modelCatalogEntrySchema = z.looseObject({
  id: z.string(),
  name: z.string(),
  provider: z.string(),
  ollama_tag: z.string().nullable(),
  roles: z.array(z.string()),
  vram_gb: z.number(),
  disk_gb: z.number(),
  context_tokens: z.number(),
  license: z.string(),
  tier: z.number(),
  description: z.string(),
  notes: z.string(),
  last_reviewed: z.string(),
  embedding_dimension: z.number().nullable(),
  phase: z.string(),
  assignable: z.boolean(),
  min_vram_gb_at_default_ctx: z.number().nullable(),
  kv_cache_bytes_per_token: z.number().nullable(),
  default_num_ctx: z.number().nullable(),
  max_num_ctx: z.number().nullable(),
  supports_thinking: z.boolean(),
  active: z.boolean(),
  pulled: z.boolean(),
  provider_key_present: z.boolean().nullable(),
  fit: z.enum(['recommended', 'stretch', 'available', 'key_required', 'unfit']),
  status: z.enum([
    'active',
    'pulled',
    'downloadable',
    'unfit',
    'cloud_active',
    'cloud_required',
  ]),
  can_assign: z.boolean(),
  assign_blocker: z.string().nullable(),
  fit_detail: modelFitDetailSchema,
  size: z.number().optional(),
  quantization: z.string().optional(),
  source: z.enum(['catalog', 'provider']).optional(),
  fetched_at: z.string().nullable().optional(),
  input_price_per_million: z.string().nullable().optional(),
  output_price_per_million: z.string().nullable().optional(),
  price_source: z.string().nullable().optional(),
});

const hardwareRecommendationSchema = z.looseObject({
  vram_mb: z.number().nullable(),
  bucket: z.enum(['CPU_ONLY', 'ENTRY', 'MID', 'MID_HIGH', 'HIGH']),
  summary: z.string(),
  aliases: z.array(z.looseObject({
    alias: z.enum(['smart', 'fast', 'embed']),
    model: z.string(),
    confirm_on_target: z.boolean(),
    notes: z.string(),
  })),
});

const providerModelListStatusSchema = z.looseObject({
  model_count: z.number(),
  fetched_at: z.string().nullable(),
  error: z.string().nullable(),
  truncated: z.boolean(),
  excluded: z.record(z.string(), z.number()),
});

export const systemModelsResponseSchema = z.looseObject({
  status: z.enum(['ok', 'degraded']),
  installed: z.array(z.looseObject({
    name: z.string(),
    size: z.number(),
    parameter_size: z.string(),
    quantization: z.string(),
  })),
  hardware: z.looseObject({
    vram_gb: z.number().optional(),
    vram_source: z.string().optional(),
    vram_source_detail: z.string().optional(),
    tier: z.number().optional(),
    detected_at: z.string().optional(),
    machine_id: z.string().optional(),
    host_gpu_divergence: z.boolean().optional(),
    vendor: z.string().optional(),
    ollama_running: z.number().optional(),
  }),
  current: z.record(z.string(), z.string()),
  issues: z.record(z.string(), z.string()),
  catalog: z.array(modelCatalogEntrySchema),
  recommendations: z.record(z.string(), z.array(modelCatalogEntrySchema)),
  hardware_recommendation: hardwareRecommendationSchema,
  delivery: z.record(z.string(), z.enum(['pending_restart', 'applied'])),
  routing: z.record(z.string(), z.string()),
  consistent: z.boolean(),
  provider_lists: z.record(z.string(), providerModelListStatusSchema),
  embedding_contract: z.looseObject({
    model: z.string(),
    dimension: z.number(),
    change_requires_reindex: z.boolean(),
  }).optional(),
});

export const dashboardMetricsSchema: z.ZodType<DashboardMetrics> = z.looseObject({
  total_papers: z.number(),
  unread_papers: z.number(),
  pending_papers: z.number(),
  due_cards: z.number(),
  active_projects: z.number(),
  topic_count: z.number(),
  nudge_count: z.number(),
  chunked_papers: z.number().optional(),
  onboarding_stage: z.enum(['needs_topics', 'needs_papers', 'needs_processing', 'complete']).optional(),
});

export const systemCapabilitiesSchema: z.ZodType<SystemCapabilities> = z.looseObject({
  networkx: z.boolean(),
  scikit_learn: z.boolean(),
  structured_output_enforced: z.boolean(),
});

const aiBackendCandidateSchema = z.looseObject({
  backend: z.enum(['ollama', 'vllm']),
  model: z.string(),
  catalog_id: z.string().nullable().optional(),
  source: z.enum(['catalog', 'tier-candidates']).optional(),
  rank: z.number(),
  score: z.number().nullable().optional(),
  reasoning: z.string().optional(),
  evidence: z.enum([
    'bench',
    'sim-bench',
    'static-benchmark',
    'pending-bench',
    'catalog',
  ]).nullable().optional(),
});

export const aiSettingsSchema = z.looseObject({
  hw_tier: z.string(),
  recommended_backend: z.string(),
  recommended_model: z.string(),
  observed_backend: z.string().nullable(),
  observed_recent_share: z.number(),
  candidates_for_tier: z.array(aiBackendCandidateSchema),
  candidate_issues: z.array(z.string()),
  eval_report_date: z.string().nullable(),
});

export type ReadinessCheck = z.infer<typeof readinessResponseSchema>['checks'][number];
export type ReadinessResponse = z.infer<typeof readinessResponseSchema>;
export type StorageSection = z.infer<typeof storageSectionSchema>;
export type QdrantCollectionUsage = z.infer<
  typeof systemStorageResponseSchema
>['qdrant_collections'][number];
export type SystemStorageResponse = z.infer<typeof systemStorageResponseSchema>;
export type ModelFitDetailApi = z.infer<typeof modelFitDetailSchema>;
export type ModelCatalogEntry = z.infer<typeof modelCatalogEntrySchema>;
export type HardwareRecommendation = z.infer<typeof hardwareRecommendationSchema>;
export type HardwareRecommendationAlias = HardwareRecommendation['aliases'][number];
export type ProviderModelListStatus = z.infer<typeof providerModelListStatusSchema>;
export type SystemModelsResponse = z.infer<typeof systemModelsResponseSchema>;
export type AIBackendCandidate = z.infer<typeof aiBackendCandidateSchema>;
export type AISettings = z.infer<typeof aiSettingsSchema>;
