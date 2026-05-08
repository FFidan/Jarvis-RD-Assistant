export const errorMessage = (e: unknown, fallback = 'Unknown error'): string =>
  e instanceof Error ? e.message : typeof e === 'string' ? e : fallback;
