import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { updateSource } from '@/lib/api';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { GripVertical, Key, Pencil, Check, X } from 'lucide-react';
import type { SourceConfig } from '@/types';

export const SOURCE_DISPLAY_NAMES: Record<string, string> = {
  arxiv: 'ArXiv',
  semantic_scholar: 'Semantic Scholar',
  openalex: 'OpenAlex',
  pubmed: 'PubMed',
  local: 'Local',
};

export const SOURCE_DESCRIPTIONS: Record<string, string> = {
  local: "PDFs you've uploaded directly. No API key needed; always enabled.",
  arxiv:
    'Open-access preprint server covering physics, math, CS, quant-bio and economics. No API key required.',
  semantic_scholar:
    'AI-powered academic search by Allen Institute for AI. Optional API key raises rate limits.',
  openalex:
    'Free, open catalog of the global research graph (250M+ works). Provide your email for the polite pool.',
  pubmed:
    'NCBI\'s biomedical literature database. No API key required but one increases rate limits.',
};

function getConfigString(
  config: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  if (!config) return null;
  const value = config[key];
  return typeof value === 'string' && value.length > 0 ? value : null;
}

interface SourceSectionProps {
  source: SourceConfig;
  displayIdx: number;
}

export function SourceSection({ source, displayIdx }: SourceSectionProps) {
  const queryClient = useQueryClient();
  const [editingKey, setEditingKey] = useState(false);
  const [apiKey, setApiKey] = useState('');

  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: source.source_type,
  });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<SourceConfig> }) =>
      updateSource(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] });
    },
  });

  const handleToggle = () => {
    updateMut.mutate({ id: source.id, data: { enabled: !source.enabled } });
  };

  const config = source.config as Record<string, unknown> | null | undefined;
  const keyEnv = getConfigString(config, 'key_env');
  const requiresKey = config?.requires_key !== false;
  const description = SOURCE_DESCRIPTIONS[source.source_type];

  return (
    <div ref={setNodeRef} style={style}>
      <Card className="rounded-md border-hair shadow-none">
        <CardContent className="flex flex-col gap-3 p-4">
          {/* Header row */}
          <div className="flex items-center gap-3">
            {/* Drag handle */}
            <button
              type="button"
              className="cursor-grab text-muted-foreground hover:text-foreground active:cursor-grabbing shrink-0"
              aria-label="Drag to reorder"
              {...attributes}
              {...listeners}
            >
              <GripVertical className="h-4 w-4" />
            </button>

            {/* Position pill */}
            <span className="text-xs font-mono text-muted-foreground w-6 shrink-0 text-center">
              #{displayIdx}
            </span>

            {/* Source name + info bubble */}
            <div className="flex items-center gap-1.5 flex-1 min-w-0">
              <span className="font-medium truncate">
                {SOURCE_DISPLAY_NAMES[source.source_type] ?? source.source_type}
              </span>
              {description && <InfoTooltip content={description} side="right" />}
            </div>

            {/* Status badges */}
            <div className="flex items-center gap-2 shrink-0">
              <Badge variant={source.enabled ? 'default' : 'outline'}>
                {source.enabled ? 'Enabled' : 'Disabled'}
              </Badge>
            </div>

            {/* Toggle button */}
            <Button
              size="sm"
              variant="ghost"
              onClick={handleToggle}
              disabled={updateMut.isPending}
              className="shrink-0"
            >
              {source.enabled ? 'Disable' : 'Enable'}
            </Button>
          </div>

          {/* API key row — shown for all cards; either edit UI or "No API key required" */}
          <div className="pl-9">
            {keyEnv ? (
              <div>
                {!editingKey ? (
                  <div className="flex items-center gap-2">
                    <Key className="h-4 w-4 text-muted-foreground" />
                    {source.config?.api_key ? (
                      <span className="text-xs text-[var(--status-ok)]">API key: configured</span>
                    ) : !requiresKey ? (
                      <span className="text-xs text-muted-foreground">API key: optional</span>
                    ) : source.enabled ? (
                      <span className="text-xs text-[var(--status-warn)]">API key: not set</span>
                    ) : (
                      <span className="text-xs text-muted-foreground">API key: not set</span>
                    )}
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-7 w-7"
                      onClick={() => {
                        setEditingKey(true);
                        setApiKey('');
                      }}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                ) : (
                  <div className="flex items-center gap-2">
                    <Input
                      type="password"
                      placeholder={`Enter ${keyEnv}`}
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="h-8 text-sm"
                    />
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-7 w-7"
                      onClick={() =>
                        updateMut.mutate(
                          {
                            id: source.id,
                            data: {
                              config: {
                                ...((source.config as Record<string, unknown>) || {}),
                                api_key: apiKey,
                              },
                            },
                          },
                          { onSuccess: () => setEditingKey(false) },
                        )
                      }
                      disabled={updateMut.isPending}
                    >
                      <Check className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-7 w-7"
                      onClick={() => setEditingKey(false)}
                    >
                      <X className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                )}
                <p className="text-xs text-muted-foreground mt-1">
                  {requiresKey
                    ? 'API key enables higher rate limits. Changes effective after service restart.'
                    : 'No API key required; without one this source uses its standard rate limit.'}
                </p>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">No API key required.</p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
