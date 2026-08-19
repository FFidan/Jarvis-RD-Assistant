<!-- verified-against-UI: 2026-08-19 | routes: /, /my-day, /pulse, /feed?surface=library, /feed?surface=search, /projects, /ask, /extractions, /knowledge, /citations, /consensus, /cards, /analytics, /admin/users, /admin/system-health, /admin/audit-log, /admin/backups, /logs, /settings -->

# Navigation

JARVIS RD Assistant uses a consistent application shell across all pages.

<!-- screenshot: AppShell — full-width layout showing the sidebar nav groups on the left and TopBar across the top -->

---

## AppShell layout

The application is divided into three regions:

- **Sidebar** (left) — primary navigation.
- **TopBar** (top) — global controls: quick search, jobs indicator, focus timer, and theme toggle.
- **Main content area** — the active page.

On narrow viewports (below the `md` breakpoint) the sidebar is hidden and replaced by a **hamburger menu button** on the left of the TopBar; tapping it slides the full sidebar in from the left as a drawer. Tapping outside the drawer or selecting a destination closes it.

---

## Two rails: simple and full

The sidebar has two densities, and the choice follows your account rather than the browser you happen to be using.

**Simple** is what you start on. It is a short rail of the six places the daily research loop actually visits:

| Item | Route |
|------|-------|
| [My Day](home-my-day.md) | `/my-day` |
| [Papers](research-feed.md) | `/feed?surface=library` |
| [Discover](research-feed.md) | `/feed?surface=search` |
| [Projects](projects.md) | `/projects` |
| [Learning Cards](learning-cards.md) | `/cards` |
| [Ask](ask.md) | `/ask` |

**Full** is the grouped layout below. Switch with the **Show all features** / **Simple view** button in the sidebar footer. Nothing is hidden by the simple rail in the sense of being unreachable — every page still has a URL, and links from other pages still work.

Once you have saved or analyzed your first paper, a one-time tip appears above the toggle naming exactly what the full rail adds. Dismissing it or using the toggle retires it.

---

## Sidebar groups in full mode

Groups are labelled with a Roman numeral and a one-line description of what they are for. The **Ⅳ Admin** group renders only for accounts with the admin role; regular users do not see it at all.

| Group | Items (route) |
|-------|---------------|
| **Ⅰ Today** — *What needs your attention right now.* | [My Day](home-my-day.md) (`/my-day`) · [Home](home-my-day.md) (`/`) · [Pulse Deck](pulse.md) (`/pulse`) · [Papers](research-feed.md) (`/feed?surface=library`) · [Discover](research-feed.md) (`/feed?surface=search`) |
| **Ⅱ Workspace** — *Projects, questions, and the tools that connect your papers.* | [Projects](projects.md) (`/projects`) · [Ask](ask.md) (`/ask`) · [Extraction Table](extraction-table.md) (`/extractions`) · [Knowledge Graph](knowledge-graph.md) (`/knowledge`) · [Citation Graph](citation-graph.md) (`/citations`) · [Consensus](consensus.md) (`/consensus`) |
| **Ⅲ Learn** — *Cards, analytics, and how your knowledge grows.* | [Learning Cards](learning-cards.md) (`/cards`) · [Analytics](analytics.md) (`/analytics`) |
| **Ⅳ Admin** — *Users, health, and audit trail.* (**admin role only**) | [User Management](admin.md) (`/admin/users`) · [System Health](admin.md) (`/admin/system-health`) · [Audit Log](admin.md) (`/admin/audit-log`) · [Backups](admin.md) (`/admin/backups`) · [System Logs](admin.md) (`/logs`) |

**Papers** and **Discover** are two views of the same `/feed` route, told apart by the `surface` query parameter. Discover is highlighted only while `surface=search`; every other feed state — Inbox, Saved, Reading, Trash — highlights Papers. See [Papers & Discover](research-feed.md).

### Sidebar footer

Below a separator, and visible to everyone:

- The **Show all features** / **Simple view** toggle described above.
- **Settings** (`/settings`) — a footer utility link, not a member of any numbered group.
- The **health pill** — live service-health dots. Admins can click it to open System Health; everyone else gets the detail in a popover on the spot.
- The running version number.
- **Sign out**.

Collapsing the sidebar with the chevron in its header leaves icons only; each one keeps its label as a tooltip.

---

## TopBar controls

| Control | Function |
|---------|----------|
| **BrandMark** | Application logo and home anchor |
| **Search your papers** (⌘K / Ctrl+K) | Opens the command palette over the papers you have saved |
| **Error pill** | Appears only when the application has logged errors in the last 24 hours; shows the count and opens the System Logs error view |
| **Jobs indicator** | Queued, running, and recent background work for your account, including work started from Telegram or another browser |
| **Focus timer** | The shared per-user focus interval, startable from either Web or Telegram; see [Settings](settings.md) |
| **Keyboard shortcuts** | Opens the shortcut reference (also `?`) |
| **Theme toggle** | Light or dark |
| **User avatar menu** | Settings and Sign out |

### The three ways to search

The app has three search inputs and they never overlap, so a result is always where you expect it:

1. **⌘K in the TopBar** searches **the papers you have already saved** and opens one.
2. **The filter box on Papers, Inbox, or Trash** narrows **the list you are currently looking at** by title or author, inside whatever facets are selected. It does not leave the view.
3. **Discover** searches **external sources you do not have yet** — arXiv, Semantic Scholar, OpenAlex, PubMed — and its results have to be saved before they join your library.

---

## Related pages

- [Getting Started](getting-started.md) — signing in and the setup wizard that precedes first use.
- [Settings](settings.md) — appearance, timer, and account preferences.
