<!-- verified-against-UI: 2026-06-13 | routes: /, /my-day, /pulse, /feed, /feed?surface=search, /projects, /knowledge, /citations, /extractions, /cards, /analytics, /ask, /admin/users, /admin/system-health, /admin/audit-log, /logs, /settings -->

# Navigation

JARVIS RD Assistant uses a consistent application shell across all pages.

<!-- screenshot: AppShell — full-width layout showing the sidebar nav groups on the left and TopBar across the top -->

---

## AppShell layout

The application is divided into three regions:

- **Sidebar** (left) — primary navigation, grouped by function.
- **TopBar** (top) — global controls: quick search, jobs indicator, Pomodoro timer, and theme toggle.
- **Main content area** — the active page.

On narrow viewports (below the `md` breakpoint) the sidebar is hidden and replaced by a **hamburger menu button** (☰) on the left of the TopBar; tapping it slides the full sidebar in from the left as a drawer (a Sheet overlay). Tapping outside the drawer or selecting a destination closes it.

---

## Sidebar navigation groups

The sidebar is divided into five labelled groups identified by Roman numerals. Each group has a short sub-label describing its purpose. The **Ⅴ Admin** group is only rendered for users with the **admin** role; it is hidden entirely from regular users.

| Group | Items (route) |
|-------|---------------|
| **Ⅰ Today** — *What needs your attention right now.* | [Home](home-my-day.md) (`/`) · [My Day](home-my-day.md) (`/my-day`) · [Pulse Deck](pulse.md) (`/pulse`) · [Library](research-feed.md) (`/feed`) · [Discover](research-feed.md) (`/feed?surface=search`) |
| **Ⅱ Read** — *Your library, projects, and the graph that connects them.* | [Projects](projects.md) (`/projects`) · [Knowledge Graph](knowledge-graph.md) (`/knowledge`) · [Citation Graph](citation-graph.md) (`/citations`) · [Extraction Table](extraction-table.md) (`/extractions`) |
| **Ⅲ Learn** — *Cards, analytics, and how your knowledge grows.* | [Learning Cards](learning-cards.md) (`/cards`) · [Analytics](analytics.md) (`/analytics`) |
| **Ⅳ Ask** — *Cross-paper reasoning and workspace.* | [Ask](ask.md) (`/ask`) |
| **Ⅴ Admin** — *Users, health, and audit trail.* (**admin role only**) | [User Management](admin.md) (`/admin/users`) · [System Health](admin.md) (`/admin/system-health`) · [Audit Log](admin.md) (`/admin/audit-log`) · [System Logs](admin.md) (`/logs`) |

### Density modes — simple and full

The sidebar has two density modes, remembered per device:

- **Simple** — a short rail of the daily essentials (**Home · My Day · Library · Ask · Cards**). Everything else is one **"Show all features"** toggle away in the sidebar footer. This is the default the first time you use JARVIS, so the app isn't overwhelming.
- **Full** — the complete grouped layout shown above. This is the default for returning users.

Switch modes any time with the **"Show all features" / "Simple view"** toggle in the sidebar footer. Your choice is stored on your device and survives logout; it does not change which pages exist — every destination is always reachable.

### Sidebar footer

Below a separator, the sidebar footer is visible to **all users**:

- **Density toggle** (**"Show all features" / "Simple view"**) — switches the sidebar between simple and full modes (see above).
- **Settings** link (`/settings`) — navigates to the Settings page.
- **HealthDots pill** — shows live service health indicators. Admin users can click the pill to navigate to `/admin/system-health`; non-admin users see an in-place popover.
- **Logout** button — ends the current session.

---

## TopBar controls

| Control | Function |
|---------|----------|
| **BrandMark** | Application logo / home anchor |
| **⌘K / Ctrl+K** | Open the command-palette search — search papers, navigate to pages, or run actions |
| **Error pill** | Appears only when the application has logged errors in the last 24 hours; shows the count of error/critical events and, when clicked, opens the System Logs error view. Hidden when there are no recent errors. |
| **Jobs indicator** | Shows the count of running background jobs; click to expand the jobs panel |
| **Pomodoro timer** | A focus timer with configurable work and break intervals (configure in [Settings](settings.md) §IV System → Timer) |
| **Keyboard shortcuts** | Opens the keyboard-shortcuts reference panel |
| **Theme toggle** | Switch between light and dark mode |
| **User avatar menu** | A round avatar button; opens a menu with two items — **Settings** (goes to `/settings`) and **Logout** (ends the session and returns to the sign-in page). |

---

## Related pages

- [Getting Started](getting-started.md) — signing in and the setup wizard that precedes first use.
- [Settings](settings.md) — configure the timer interval and appearance preferences.
