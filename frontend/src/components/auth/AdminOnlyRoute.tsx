/**
 * AdminOnlyRoute — route guard for admin-only pages.
 *
 * Renders children when the authenticated user has role === 'admin'.
 * Otherwise redirects to '/' so non-admin users are never stranded on a
 * 403 page caused by a direct URL entry or stale bookmark.
 *
 * API-key-only sessions (user === null) also redirect to '/' because there
 * is no role information available — those sessions are legacy single-tenant
 * and the admin pages require a proper magic-link session.
 *
 * Usage:
 *   <Route path="logs" element={<AdminOnlyRoute><LogsPage /></AdminOnlyRoute>} />
 */

import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';
import { useAuthStore } from '@/stores/auth-store';

interface AdminOnlyRouteProps {
  children: ReactNode;
}

export function AdminOnlyRoute({ children }: AdminOnlyRouteProps) {
  const user = useAuthStore((s) => s.user);

  if (user?.role !== 'admin') {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}
