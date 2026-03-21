import { useState, useCallback } from 'react';

interface UsePaginationOptions {
  pageSize?: number;
}

export function usePagination({ pageSize = 20 }: UsePaginationOptions = {}) {
  const [page, setPage] = useState(0);

  const offset = page * pageSize;

  const nextPage = useCallback(() => {
    setPage((p) => p + 1);
  }, []);

  const prevPage = useCallback(() => {
    setPage((p) => Math.max(0, p - 1));
  }, []);

  const goToPage = useCallback((n: number) => {
    setPage(Math.max(0, n));
  }, []);

  const resetPage = useCallback(() => {
    setPage(0);
  }, []);

  return {
    page,
    pageSize,
    offset,
    nextPage,
    prevPage,
    goToPage,
    resetPage,
  };
}
