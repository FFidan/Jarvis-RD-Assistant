import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { getRetention, putRetention, type RetentionConfig } from '@/lib/api/backups';
import { QUERY_KEYS } from '@/lib/query-keys';

function parseRetentionLimit(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const value = Number(trimmed);
  return Number.isFinite(value) && value >= 1 ? Math.floor(value) : null;
}

/** Own retention hydration and saving without ever sending uninitialized form state. */
export function useRetentionForm() {
  const queryClient = useQueryClient();
  const [keepLastN, setKeepLastNValue] = useState('');
  const [maxAgeDays, setMaxAgeDaysValue] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [dirty, setDirty] = useState(false);
  const query = useQuery({
    queryKey: QUERY_KEYS.admin.backupRetention(),
    queryFn: getRetention,
  });

  useEffect(() => {
    if (!query.data || loaded) return;
    setKeepLastNValue(query.data.keep_last_n?.toString() ?? '');
    setMaxAgeDaysValue(query.data.max_age_days?.toString() ?? '');
    setDirty(false);
    setLoaded(true);
  }, [loaded, query.data]);

  const mutation = useMutation({
    mutationFn: putRetention,
    onSuccess: (data) => {
      toast.success('Retention policy saved.');
      queryClient.setQueryData(QUERY_KEYS.admin.backupRetention(), data);
      setKeepLastNValue(data.keep_last_n?.toString() ?? '');
      setMaxAgeDaysValue(data.max_age_days?.toString() ?? '');
      setDirty(false);
    },
    onError: (error: unknown) => {
      toast.error(error instanceof Error ? error.message : 'Could not save the retention policy.');
    },
  });

  const save = () => {
    if (!loaded) return;
    const config: RetentionConfig = {
      keep_last_n: parseRetentionLimit(keepLastN),
      max_age_days: parseRetentionLimit(maxAgeDays),
    };
    mutation.mutate(config);
  };

  return {
    dirty,
    isError: query.isError,
    isPending: mutation.isPending,
    keepLastN,
    loaded,
    maxAgeDays,
    refetch: query.refetch,
    save,
    setKeepLastN: (value: string) => {
      setKeepLastNValue(value);
      setDirty(true);
    },
    setMaxAgeDays: (value: string) => {
      setMaxAgeDaysValue(value);
      setDirty(true);
    },
  };
}
