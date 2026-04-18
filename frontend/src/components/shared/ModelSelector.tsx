import { useQuery } from '@tanstack/react-query';
import { Cpu } from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { apiFetch } from '@/lib/api';

interface SystemModels {
  status: 'ok' | 'degraded';
  installed: Array<{
    name: string;
    size: number;
    parameter_size: string;
    quantization: string;
  }>;
  hardware: Record<string, unknown>;
  current: Record<string, string>;
  issues: Record<string, string>;
}

interface ModelSelectorProps {
  value: string;
  onChange: (value: string) => void;
  configKey?: string;
}

function filterModelsForRole(
  models: SystemModels['installed'],
  role?: string,
): SystemModels['installed'] {
  if (!role) return models;
  const r = role.replace('llm.', '');
  if (r === 'embed_model') {
    return models.filter((m) => /embed/i.test(m.name));
  }
  if (r === 'fast_model') {
    return models.filter((m) => {
      if (/embed/i.test(m.name)) return false;
      const match = m.parameter_size.match(/([\d.]+)/);
      return match ? parseFloat(match[1]) <= 4.5 : false;
    });
  }
  if (r === 'smart_model') {
    return models.filter((m) => {
      if (/embed/i.test(m.name)) return false;
      const match = m.parameter_size.match(/([\d.]+)/);
      return match ? parseFloat(match[1]) >= 7 : false;
    });
  }
  return models;
}

export function ModelSelector({ value, onChange, configKey: role }: ModelSelectorProps) {
  const { data, error } = useQuery<SystemModels>({
    queryKey: ['system-models'],
    queryFn: () => apiFetch<SystemModels>('/api/system/models'),
    staleTime: 60_000,
  });

  const models = data?.installed ?? [];
  const filteredModels = filterModelsForRole(models, role);
  const currentRole = role?.replace('llm.', '');
  const systemDefault = currentRole ? data?.current?.[currentRole] : undefined;
  // If value doesn't match any installed model (e.g. it's an alias like "fast"),
  // fall back to the system default which uses actual model names.
  // Normalize by stripping `:latest` suffix so "model" matches "model:latest".
  const normalize = (n: string) => n.replace(/:latest$/, '');
  const matchedName = filteredModels.find(
    (m) => m.name === value || normalize(m.name) === normalize(value)
  )?.name;
  const effectiveValue = matchedName || systemDefault || '';
  const emptyStateMessage = error
    ? 'Could not load models. Check the API and Ollama status.'
    : data?.issues.installed ?? 'No models found. Is Ollama running?';

  const formatSize = (bytes: number) => {
    if (bytes > 1e9) return `${(bytes / 1e9).toFixed(1)}GB`;
    if (bytes > 1e6) return `${(bytes / 1e6).toFixed(0)}MB`;
    return `${bytes}B`;
  };

  return (
    <Select value={effectiveValue} onValueChange={onChange}>
      <SelectTrigger>
        <SelectValue placeholder="Select a model" />
      </SelectTrigger>
      <SelectContent>
        {data?.issues.current && (
          <div className="px-2 pb-2 text-xs text-amber-700">
            {data.issues.current}
          </div>
        )}
        {filteredModels.length === 0 ? (
          <div className="px-2 py-4 text-center text-sm text-muted-foreground">
            <Cpu className="mx-auto mb-2 h-5 w-5" />
            {models.length > 0
              ? 'No compatible models installed for this role'
              : emptyStateMessage}
          </div>
        ) : (
          filteredModels.map((m) => {
            const isCurrent = data?.current?.[currentRole ?? ''] === m.name;
            return (
              <SelectItem key={m.name} value={m.name}>
                <div className="flex items-center gap-2">
                  <span>{m.name}</span>
                  {m.parameter_size && (
                    <span className="text-xs text-muted-foreground">
                      {m.parameter_size}
                    </span>
                  )}
                  {m.quantization && (
                    <span className="text-xs text-muted-foreground">
                      {m.quantization}
                    </span>
                  )}
                  {m.size > 0 && (
                    <span className="text-xs text-muted-foreground">
                      ({formatSize(m.size)})
                    </span>
                  )}
                  {isCurrent && (
                    <span className="text-xs font-medium text-green-600">
                      current
                    </span>
                  )}
                </div>
              </SelectItem>
            );
          })
        )}
      </SelectContent>
    </Select>
  );
}
