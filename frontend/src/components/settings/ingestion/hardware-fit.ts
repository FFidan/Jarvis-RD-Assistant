import type { ModelFitDetail } from '@/types';
import type { SystemModelsResponse } from '@/lib/api';

/** Power-of-2 snap steps for the num_ctx slider. */
export const NUM_CTX_STOPS = [2048, 4096, 8192, 16384, 32768, 65536] as const;
export type NumCtx = (typeof NUM_CTX_STOPS)[number];
export const isNumCtx = (n: number): n is NumCtx => (NUM_CTX_STOPS as readonly number[]).includes(n);

export interface HardwareInfoApi {
  vram_gb?: number;
  vram_source?: string;
  /** Human-readable detail string for how VRAM was detected (e.g. "nvidia-smi (GPU 0: RTX 4090)"). */
  vram_source_detail?: string;
  tier?: number;
  detected_at?: string;
  machine_id?: string;
  /** True when host GPU differs from the active GPU overlay — host VRAM reported, overlay not active. */
  host_gpu_divergence?: boolean;
}

export interface ModelCatalogEntryApi {
  id: string;
  name: string;
  provider: string;
  roles: string[];
  fit_detail?: ModelFitDetail;
  supports_thinking?: boolean;
}

/**
 * Local refinement of `SystemModelsResponse` that narrows `hardware` and `catalog`
 * to the concrete typed shapes used by this component.
 * Derived from the canonical type so it remains structurally consistent.
 */
export type SystemModelsApi = Pick<SystemModelsResponse, 'hardware_recommendation'> & {
  hardware?: HardwareInfoApi;
  catalog?: ModelCatalogEntryApi[];
  /** Per-role LiteLLM delivery state — absent on older backends. */
  delivery?: Record<string, 'pending_restart' | 'applied'>;
  /** Committed per-role model intent (what autoconfigure / Settings stored). */
  current?: { smart_model?: string; fast_model?: string; embed_model?: string };
};

export type FitDetailWithBaseline = ModelFitDetail & {
  base_vram_gb?: number | null;
  base_num_ctx?: number | null;
};

export function hasFitBaseline(
  fitDetail: ModelFitDetail,
): fitDetail is ModelFitDetail & { base_vram_gb: number; base_num_ctx: number } {
  const detail: FitDetailWithBaseline = fitDetail;
  return (
    typeof detail.base_vram_gb === 'number' &&
    Number.isFinite(detail.base_vram_gb) &&
    typeof detail.base_num_ctx === 'number' &&
    Number.isFinite(detail.base_num_ctx)
  );
}

/**
 * Compute required VRAM for a model at a given num_ctx.
 * Uses the backend-provided baseline fields; older fit_detail payloads return null.
 */
export function computeRequiredVram(
  fitDetail: ModelFitDetail,
  numCtx: number,
): number | null {
  if (!hasFitBaseline(fitDetail)) return null;
  const kvBytes = fitDetail.kv_cache_bytes_per_token ?? 1024;
  const extraTokens = Math.max(0, numCtx - fitDetail.base_num_ctx);
  return fitDetail.base_vram_gb + (extraTokens * kvBytes) / 1e9;
}

/**
 * Determine fit status for a given required VRAM vs available VRAM.
 * Hardware fit thresholds.
 */
export function fitStatus(
  requiredVramGb: number,
  availableVramGb: number,
): 'fits' | 'partial' | 'unfit' {
  if (requiredVramGb <= availableVramGb * 0.85) return 'fits';
  if (requiredVramGb <= availableVramGb * 1.2) return 'partial';
  return 'unfit';
}

/**
 * Find the highest snap-step that produces 'fits' (≤ 85% VRAM threshold).
 * Falls back to the lowest stop if nothing fits.
 */
export function largestFittingStop(
  fitDetail: ModelFitDetail,
  vramGb: number,
  stops: readonly number[],
): number {
  let best: number = stops[0] ?? 2048;
  for (const stop of stops) {
    if (stop > fitDetail.max_num_ctx) break;
    const req = computeRequiredVram(fitDetail, stop);
    if (req !== null && req <= vramGb * 0.85) best = stop;
  }
  return best;
}

/**
 * Clamp a slider value to the highest non-unfit stop (fits or partial).
 * Partial (up to 120%) is allowed; only unfit is blocked.
 */
export function clampToNonUnfit(
  value: number,
  fitDetail: ModelFitDetail,
  vramGb: number,
  stops: readonly number[],
): number {
  const allowed = stops.filter((s) => {
    const req = computeRequiredVram(fitDetail, s);
    return s <= fitDetail.max_num_ctx && req !== null && fitStatus(req, vramGb) !== 'unfit';
  });
  if (allowed.length === 0) return stops[0] ?? 2048;
  // If current value is allowed, keep it; otherwise clamp to max allowed
  if (allowed.includes(value)) return value;
  const sorted = [...allowed].sort((a, b) => a - b);
  return sorted[sorted.length - 1] ?? 2048;
}
