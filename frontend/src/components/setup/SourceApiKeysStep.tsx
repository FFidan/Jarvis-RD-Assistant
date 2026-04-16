import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, Pencil, Check, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { SetupStep } from '@/components/setup/SetupStep';
import { fetchSources, updateSource } from '@/lib/api';
import type { SourceConfig } from '@/types';

// Only these source types have API keys worth configuring in setup
const KEY_SOURCES = ['semantic_scholar', 'openalex', 'pubmed'] as const;

const SOURCE_LABELS: Record<string, { name: string; description: string }> = {
  semantic_scholar: {
    name: 'Semantic Scholar',
    description: 'Academic paper search (recommended)',
  },
  openalex: {
    name: 'OpenAlex',
    description: 'Open academic graph',
  },
  pubmed: {
    name: 'PubMed',
    description: 'Biomedical literature (NCBI)',
  },
};

const TOTAL_STEPS = 7;

function SourceKeyRow({ source }: { source: SourceConfig }) {
  const [editing, setEditing] = useState(false);
  const [apiKey, setApiKey] = useState('');
  const queryClient = useQueryClient();
  const meta = SOURCE_LABELS[source.source_type];

  const saveMut = useMutation({
    mutationFn: () =>
      updateSource(source.id, { config: { ...(source.config ?? {}), api_key: apiKey } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] });
      setEditing(false);
      setApiKey('');
    },
  });

  const hasKey = !!(source.config as Record<string, unknown>)?.api_key;

  return (
    <div className="rounded-md border p-3 space-y-2">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium">{meta?.name ?? source.source_type}</p>
          <p className="text-xs text-muted-foreground">{meta?.description}</p>
        </div>
        {!editing && (
          <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
            <Pencil className="h-3.5 w-3.5 mr-1" />
            {hasKey ? 'Edit key' : 'Add key'}
          </Button>
        )}
      </div>
      {editing && (
        <div className="flex items-center gap-2">
          <Input
            type="password"
            placeholder="Paste API key…"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            className="h-8 text-sm flex-1"
            autoFocus
          />
          <Button
            size="sm"
            variant="ghost"
            disabled={!apiKey || saveMut.isPending}
            onClick={() => saveMut.mutate()}
          >
            <Check className="h-4 w-4" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setEditing(false);
              setApiKey('');
            }}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      )}
      {hasKey && !editing && <p className="text-xs text-green-600">API key configured</p>}
    </div>
  );
}

interface SourceApiKeysStepProps {
  onNext: () => void;
  onBack: () => void;
}

export function SourceApiKeysStep({ onNext, onBack }: SourceApiKeysStepProps) {
  const { data: sources, isLoading } = useQuery({
    queryKey: ['sources'],
    queryFn: fetchSources,
  });

  const keyedSources = sources?.filter((s) =>
    KEY_SOURCES.includes(s.source_type as (typeof KEY_SOURCES)[number]),
  );

  return (
    <SetupStep
      stepNumber={5}
      totalSteps={TOTAL_STEPS}
      title="Configure API Keys"
      description="Optional — sources work without keys, but adding them increases rate limits for paper discovery."
      footer={
        <>
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>
            Skip for now
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading sources…</p>
        ) : (
          keyedSources?.map((source) => <SourceKeyRow key={source.id} source={source} />)
        )}
        <p className="text-xs text-muted-foreground">
          You can update these later in Settings → Sources.
        </p>
      </div>
    </SetupStep>
  );
}
