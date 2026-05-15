import { QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { queryClient } from '@/lib/query-client';
import { registerQueryClient } from '@/stores/auth-store';

// Register the singleton QueryClient with the auth store so logout() can call
// queryClient.clear() without creating a circular module dependency.
registerQueryClient(queryClient);

interface AppProvidersProps {
  children: ReactNode;
}

export function AppProviders({ children }: AppProvidersProps) {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        {children}
      </BrowserRouter>
    </QueryClientProvider>
  );
}
