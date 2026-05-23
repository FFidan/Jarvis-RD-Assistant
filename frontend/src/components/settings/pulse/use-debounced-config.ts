/**
 * Returns a debounced mutate function for committing config changes to the backend.
 * Collapses the setTimeout-debounce patterns repeated in PulseSection.
 */
import { useRef, useEffect } from 'react';

type MutateConfig = (args: { key: string; value: unknown }) => void;

export function useDebouncedConfig(mutate: MutateConfig, delay = 600): MutateConfig {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    },
    [],
  );

  return (args: { key: string; value: unknown }) => {
    if (timerRef.current !== null) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      mutate(args);
    }, delay);
  };
}
