/**
 * StorageCard — admin-facing disk-usage snapshot.
 *
 * Admin-only (GET /api/system/storage requires admin or API key). Shows
 * approximate disk usage for the backing stores this instance can measure
 * without new privileged mounts: Ollama models, Postgres, and the
 * HuggingFace cache (Docling layout/table models). Qdrant has no byte-size
 * API, so its row shows a point-count proxy instead. Surfaces a "running
 * low" notice when free space on the mounted cache volume drops below a
 * safe floor.
 *
 * GET /api/system/storage → getSystemStorage()
 */
import { useQuery } from '@tanstack/react-query';
import { getSystemStorage } from '@/lib/api';
import type { StorageSection } from '@/lib/api';

function formatBytes(bytes: number | null): string {
  if (bytes === null) return '—';
  if (bytes === 0) return '0 GB';
  const gb = bytes / 1e9;
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / 1e6).toFixed(0)} MB`;
}

function sectionText(section: StorageSection): string {
  return section.error ? `Unavailable (${section.error})` : formatBytes(section.bytes_used);
}

export function StorageCard() {
  const { data, isLoading, error } = useQuery({
    // Owned-file scope keeps this out of the shared QUERY_KEYS registry
    // (frontend/src/lib/query-keys.ts) — not part of this lane's edit set.
    queryKey: ['admin', 'system-storage'] as const,
    queryFn: getSystemStorage,
    staleTime: 30_000,
  });

  const totalQdrantPoints = data?.qdrant_collections.reduce(
    (sum, c) => sum + (c.points_count ?? 0),
    0,
  );

  return (
    <section aria-labelledby="storage-heading" data-testid="storage-card">
      <div className="mb-3">
        <h2 id="storage-heading" className="text-base font-semibold">
          Disk usage
        </h2>
        <p className="text-sm text-muted-foreground mt-1">
          Approximate storage consumed by the backing stores this instance can measure.
        </p>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading disk usage…</p>}

      {error && (
        <p className="text-sm text-destructive">
          Couldn&apos;t load disk usage: {error instanceof Error ? error.message : 'unknown error'}
        </p>
      )}

      {data && (
        <div className="space-y-4">
          {data.pressure && (
            <div
              role="alert"
              className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-300"
            >
              Free disk space is running low. Remove unused models or free up space soon.
            </div>
          )}
          <div className="rounded-md border overflow-x-auto">
            <table className="w-full text-sm">
              <tbody>
                <tr className="border-b last:border-0">
                  <th scope="row" className="px-4 py-3 text-left font-medium w-56">
                    Ollama models
                  </th>
                  <td className="px-4 py-3 font-mono">{sectionText(data.ollama_models)}</td>
                </tr>
                <tr className="border-b last:border-0">
                  <th scope="row" className="px-4 py-3 text-left font-medium">
                    Database
                  </th>
                  <td className="px-4 py-3 font-mono">{sectionText(data.postgres)}</td>
                </tr>
                <tr className="border-b last:border-0">
                  <th scope="row" className="px-4 py-3 text-left font-medium">
                    HuggingFace cache
                  </th>
                  <td className="px-4 py-3 font-mono">{sectionText(data.hf_cache)}</td>
                </tr>
                <tr className="border-b last:border-0">
                  <th scope="row" className="px-4 py-3 text-left font-medium">
                    Search index (Qdrant)
                  </th>
                  <td className="px-4 py-3 font-mono">
                    {data.qdrant.error
                      ? `Unavailable (${data.qdrant.error})`
                      : `${(totalQdrantPoints ?? 0).toLocaleString()} points across ${
                          data.qdrant_collections.length
                        } collection${data.qdrant_collections.length === 1 ? '' : 's'}`}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
