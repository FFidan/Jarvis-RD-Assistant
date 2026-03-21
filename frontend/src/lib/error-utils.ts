import { ApiError } from './api';

export function getErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.name === 'AbortError' ? 'Request cancelled' : error.message;
  return 'An unexpected error occurred';
}
