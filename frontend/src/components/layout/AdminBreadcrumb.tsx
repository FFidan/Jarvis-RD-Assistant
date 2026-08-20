/**
 * AdminBreadcrumb — shared breadcrumb primitive for admin pages.
 *
 * Renders a breadcrumb row: "Ⅴ Admin  /  [Page Name]"
 * matching the handoff admin.jpg group tag "Ⅴ ADMIN · USERS" aesthetic.
 *
 * Usage:
 *   <AdminBreadcrumb page="Users" />
 *   <AdminBreadcrumb page="Audit log" />
 *   <AdminBreadcrumb page="System health" />
 *   <AdminBreadcrumb page="System logs" />
 */

import { Link } from 'react-router-dom';

interface AdminBreadcrumbProps {
  page: string;
}

export function AdminBreadcrumb({ page }: AdminBreadcrumbProps) {
  return (
    <nav
      aria-label="breadcrumb"
      className="flex items-center gap-1.5 text-xs font-mono uppercase tracking-widest text-muted-foreground mb-1"
      data-testid="admin-breadcrumb"
    >
      <Link to="/admin/system-health" className="text-foreground/60 hover:text-foreground hover:underline">
        Admin
      </Link>
      <span aria-hidden="true" className="opacity-40">/</span>
      <span>{page}</span>
    </nav>
  );
}
