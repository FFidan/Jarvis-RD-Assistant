import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchSources, updateSource } from '@/lib/api';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { EmptyState } from '@/components/EmptyState';
import { Plug, Key, Pencil, Check, X } from 'lucide-react';
import type { SourceConfig } from '@/types';

export function SourceSection() {
  const queryClient = useQueryClient();
  const [editingId, setEditingId] = useState<number | null>(null);
  const [apiKey, setApiKey] = useState('');

  const { data: sources = [], isLoading } = useQuery({
    queryKey: ['sources'],
    queryFn: fetchSources,
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<SourceConfig> }) =>
      updateSource(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] });
    },
  });

  const handleToggle = (source: SourceConfig) => {
    updateMut.mutate({ id: source.id, data: { enabled: !source.enabled } });
  };

  if (isLoading) {
    return <div className="py-8 text-center text-muted-foreground">Loading sources...</div>;
  }

  if (sources.length === 0) {
    return <EmptyState title="No sources" description="No paper sources configured." icon={Plug} />;
  }

  return (
    <div className="space-y-2">
      {sources.map((source) => (
        <Card key={source.id}>
          <CardContent className={source.source_type === 'semantic_scholar' ? 'flex flex-col gap-3 p-4' : 'flex items-center gap-4 p-4'}>
            <div className="flex items-center gap-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium capitalize">{source.source_type}</span>
                  <Badge variant={source.enabled ? 'default' : 'outline'}>
                    {source.enabled ? 'Enabled' : 'Disabled'}
                  </Badge>
                  <Badge variant="secondary">Priority: {source.priority}</Badge>
                </div>
              </div>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => handleToggle(source)}
                disabled={updateMut.isPending}
              >
                {source.enabled ? 'Disable' : 'Enable'}
              </Button>
            </div>
            {source.source_type === 'semantic_scholar' && (
              <div>
                {editingId !== source.id ? (
                  <div className="flex items-center gap-2">
                    <Key className="h-4 w-4 text-muted-foreground" />
                    <span className="text-sm text-muted-foreground">
                      {source.config?.api_key ? 'API key: ••••' : 'No API key'}
                    </span>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-7 w-7"
                      onClick={() => { setEditingId(source.id); setApiKey(''); }}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                ) : (
                  <div className="flex items-center gap-2">
                    <Input
                      type="password"
                      placeholder="Enter S2 API key"
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
                          { onSuccess: () => setEditingId(null) },
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
                      onClick={() => setEditingId(null)}
                    >
                      <X className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                )}
                <p className="text-xs text-muted-foreground mt-1">API key enables higher rate limits</p>
              </div>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
