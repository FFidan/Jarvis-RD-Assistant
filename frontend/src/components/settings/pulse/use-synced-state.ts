/**
 * Keeps local state in sync when a server value changes (e.g. after a refetch).
 * Collapses the useEffect(() => setLocal(server), [server]) pattern repeated in PulseSection.
 */
import { useEffect, useState } from 'react';

export function useSyncedState<T>(serverValue: T): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [local, setLocal] = useState<T>(serverValue);

  useEffect(() => {
    setLocal(serverValue);
     
  }, [serverValue]);

  return [local, setLocal];
}
