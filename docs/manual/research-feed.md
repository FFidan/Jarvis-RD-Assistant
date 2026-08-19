<!-- verified-against-UI: 2026-08-19 | routes: /feed, /feed?surface=inbox, /feed?surface=library, /feed?surface=search, /feed?surface=trash -->

# Papers & Discover

Both live at `/feed`, and the `surface` query parameter decides which one you see. The split is about ownership: **Papers** is what you already have, **Discover** is everything you do not.

<!-- screenshot: /feed?surface=inbox — three-pane layout showing FacetRail, paper list with source filter chips, and a paper preview -->

---

## Papers

Papers shows a facet rail on the left and the paper list on the right. The rail always describes **your own papers**, never the wider instance.

### The facet rail

| Group | Entries |
|-------|---------|
| **Status** | Inbox, Saved, Reading, Reading List, Done, Trash |
| **Star** | Starred |
| **Source** | One entry per source your papers came from |
| **Topic** | One entry per topic your papers are tagged with, plus Untagged |

Every entry carries a count. Source and Topic need a live connection and say so when you are offline; Status and Star keep working from the cache.

When a facet has nothing in it, the rail explains why rather than showing an empty heading — no papers saved yet, or no topic tags yet, with a pointer to add a topic in Settings and turn on **Auto-add matches**.

### The surfaces

**Inbox** (`?surface=inbox`) holds unread papers arriving from your configured sources, and it is where `/feed` lands by default when you are online. It needs a connection: offline it says so and points you at your saved papers.

**Saved** (`?surface=library`) is everything you own. Offline, this is the surface `/feed` lands on instead, reading from the local cache and labelled with how old that cache is when the timestamp is known. An empty library shows a **Discover papers** and **Upload PDF** prompt rather than a blank list.

The Reading, Reading List, and Done entries are the same surface with a reading-state filter applied.

**Trash** (`?surface=trash`) holds papers you removed. They stay until you delete them for good, and **Restore** puts a paper back where it was. Permanent deletion removes your notes, summaries, highlights, and other private activity for that paper and cannot be undone — the shared paper record and its processed search content may remain, because this is your library's delete, not the instance's.

### Filtering the list in front of you

The box above the list filters the **current** view by title, author, or abstract. It does not reach outside the facets you have selected and it does not navigate anywhere. **Upload PDF** sits beside it and takes you to Discover's upload zone.

---

## Discover

Discover has no facet rail: the rail counts your own papers, which is not what either Discover tab shows. Instead there are two tabs.

**Find new papers** searches external databases live. Tick the sources to include — a source that needs an API key you have not supplied is listed but cannot be ticked, marked *API key required* — type a query, and press **Search**. **Filters** adds a year range, an author, a sort order, and a result cap. Nothing runs as you type; the search is deliberate.

**Browse public corpus** lists the public papers this instance already holds, plus your own, so you can save from them without going out to the network.

Discover needs a connection. Offline it states that search is unavailable instead of failing quietly.

### What the results tell you

Results arrive with a count and a sort control (relevance, newest, title, most cited). Above them is one row per source searched, and this is where the honesty matters:

- A source that answered reports **N results**.
- A source that **failed** reports **not searched** — not "0 results", because it never looked. Its error message is printed underneath, along with the status code, any retry-after delay, and a settings hint when one applies.

Nothing is pre-selected. Tick the papers you want and choose **Save N selected**, or **Save all unsaved**. Results already in your library are marked and excluded from saving; when every result is already yours, the page says so instead of offering a button that would do nothing.

**Upload PDF** — a drag-and-drop zone at the foot of Discover takes local PDFs directly. Arriving from the Upload PDF button on Papers moves that zone to the top of the page.

---

## Which papers you can see

See [Source-aware paper visibility](../SECURITY.md#source-aware-paper-visibility) for the canonical matrix. In short: papers verified through the server's public scholarly sources are shared across the instance, and another user's private upload or personal integration never becomes visible to you merely because it exists on the same machine.

---

## Bulk selection and pagination

Outside Trash, a checkbox appears on each row on hover; selecting one or more raises a bulk-action toolbar carrying the actions that make sense for the current surface. The header checkbox selects everything visible. Changing surface or facet clears the selection.

Long result sets paginate, and the current page is carried in the URL so you can bookmark or share it.

---

## Related pages

- [Paper Detail](paper-detail.md) — open any paper for its full reading view.
- [Settings](settings.md) — the sources, topics, and schedules that fill these surfaces.
- [Navigation](navigation.md) — how Papers, Discover, and the command palette divide the job of searching.
