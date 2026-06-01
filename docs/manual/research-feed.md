<!-- verified-against-UI: 2026-05-18 | routes: /feed, /feed?surface=inbox, /feed?surface=library, /feed?surface=search, /feed?surface=trash -->

# Research Feed & Library

The **Research Feed** at `/feed` is your primary paper management surface. It uses a three-pane layout: a **FacetRail** on the left for filtering, a **paper list** in the centre, and a detail preview or action panel on the right. The active surface is selected via the `?surface=` query parameter; the sidebar also provides direct navigation links for each surface.

<!-- screenshot: /feed?surface=inbox — three-pane layout showing FacetRail, paper list with source filter chips, and a paper preview -->

---

## Surfaces

### Inbox — `?surface=inbox`

The Inbox shows **unread** papers that have arrived since your last visit. It is the default view when you navigate to `/feed` without a surface parameter.

**Source filter chips** at the top of the list let you narrow papers by origin: **arXiv**, **Semantic Scholar**, **OpenAlex**, **PubMed**, and any other configured sources. Chips are multi-select; selecting none shows all sources.

**Upload PDF** — a button in the Inbox toolbar lets you upload a local PDF file directly into your library. The uploaded paper is queued for processing (text extraction, summarisation, embedding) in the same way as any other ingested paper.

Marking a paper as read (or opening its detail view) removes it from the Inbox; it moves to the Library.

---

### Library — `?surface=library`

The Library contains all **saved** papers — those you have explicitly kept or that have been moved out of the Inbox.

**Scope selector** at the top of the list toggles between:

- **My library** — papers you have personally saved or interacted with.
- **All discovered** — every paper the system has seen, including those saved by other users on your instance.

**State filter chips** allow you to narrow by reading state:

| Chip | Papers shown |
|------|-------------|
| ★ Starred | Papers you have starred |
| Reading | Papers with state `reading` |
| To read | Papers with state `to_read` |
| Done | Papers with state `done` |

If your library is empty, a **Discover CTA** prompt appears in the list area inviting you to run a search or trigger a Pulse deck to populate your library.

---

### Discover / Search — `?surface=search`

The Search surface combines full-text keyword search with cross-source discovery.

**SearchBar** — a text input at the top of the list. Results update as you type (with debouncing). Results are shown as **PreviewResults** cards; each preview card has a **Save to library** button that adds the paper to your Library without navigating away.

**Source checkboxes** below the search bar let you restrict results to specific sources (arXiv, Semantic Scholar, OpenAlex, PubMed). By default all sources are included.

**PDF upload zone** — a drag-and-drop area for uploading PDFs directly from the search surface, in addition to the Inbox upload button.

> **Offline:** The Search surface requires an internet connection and is **disabled offline**. If you are offline, the surface shows a notice that search is unavailable and invites you to browse your locally cached Library instead.

---

### Trash — `?surface=trash`

The Trash surface lists papers you have soft-deleted. Each paper has a **Restore** button that returns it to the Library. Papers in the Trash are not shown in any other surface.

**Hard-delete** — a confirmation modal lets you permanently delete individual papers from the Trash. This action is irreversible: all associated chunks, embeddings, and user state are removed.

---

## Bulk selection

In all surfaces except Trash, you can select multiple papers using the checkbox that appears on hover for each list item. Once one or more papers are selected:

- A **bulk action toolbar** appears above the list with actions appropriate to the current surface (e.g. Save, Mark as Done, Move to Trash).
- Selecting all visible papers uses the header checkbox.

---

## Pagination

The paper list paginates when the result set is large. Navigation controls appear at the bottom of the list. The current page is preserved in the URL so you can bookmark or share a specific page.

---

## Related pages

- [Paper Detail](paper-detail.md) — open any paper for full metadata, RAG chat, and analysis actions.
- [Settings](settings.md) — configure sources, topics, and automation schedules that feed papers into this surface.
