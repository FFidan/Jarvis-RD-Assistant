/** Frontend test helpers shared across component suites. */

import { QueryClient, QueryClientProvider, type QueryClientConfig } from '@tanstack/react-query';
import {
  render,
  type RenderOptions,
  type RenderResult,
} from '@testing-library/react';
import type { PropsWithChildren, ReactNode } from 'react';

const DEFAULT_QUERY_CLIENT_CONFIG: QueryClientConfig = {
  defaultOptions: {
    queries: {
      retry: false,
    },
  },
};

export function createTestQueryClient(
  config: QueryClientConfig = DEFAULT_QUERY_CLIENT_CONFIG,
): QueryClient {
  return new QueryClient(config);
}

type RenderWithProvidersOptions = Omit<RenderOptions, 'wrapper'> & {
  queryClient?: QueryClient;
};

type RenderWithProvidersResult = RenderResult & {
  queryClient: QueryClient;
};

export function renderWithProviders(
  ui: ReactNode,
  options: RenderWithProvidersOptions = {},
): RenderWithProvidersResult {
  const { queryClient = createTestQueryClient(), ...renderOptions } = options;

  function QueryProvider({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }

  return {
    queryClient,
    ...render(ui, { wrapper: QueryProvider, ...renderOptions }),
  };
}
