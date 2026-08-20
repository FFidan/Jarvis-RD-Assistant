<!-- verified-against-UI: 2026-08-19 | routes: /paper/:paperId -->

# Paper Detail

Opening any paper — from [Papers & Discover](research-feed.md), from a Pulse card, from a citation row — brings you to `/paper/:paperId`. The page is built for reading. A single column of the paper's sections scrolls at a comfortable measure, and everything else is either in the toolbar above it or in a panel you open when you want it.

<!-- screenshot: /paper/:paperId — three-pane layout showing the table of contents on the left, content sections in the centre, and the actions rail on the right -->

---

## The toolbar

The toolbar stays put no matter how far you have scrolled, so it is reachable at any reading depth. It carries:

- **Back** — returns where you came from.
- The paper's **running title**, once the heading itself has scrolled away.
- **Reading state and star** — move the paper through Inbox, To Read, Reading, Done, or Trash, and star it.
- **Citation export** — Copy BibTeX, Copy RIS, or **Export Markdown**, which downloads the paper's summaries, your notes, its cards, its extractions, and BibTeX metadata. Spatial PDF highlights are not in the export: their positions only mean something on the rendered PDF canvas.
- **Contents** and **Actions** — the two panel toggles.

Offline, the toolbar also shows how old the cached copy is when that timestamp is known.

### Docked panels, not overlays

On a wide screen, **Contents** and **Actions** dock beside the reading column. Opening one narrows the column instead of covering it, so scrolling continues and the Contents list keeps highlighting the section you are actually looking at. On a narrow screen there is no room to dock, so the same panels open as sheets from the left and right edges; picking a section closes the sheet first and then scrolls, so the jump is not cancelled halfway.

---

## Contents

Contents lists every section, with a count beside the ones that have things in them, and highlights the section currently on screen. It also shows the processing state for this paper:

| Indicator | Meaning |
|-----------|---------|
| PDF downloaded | The source PDF has been fetched and stored |
| Source Passages | The PDF text has been split into passages for retrieval |
| Summary | The summary has been generated |
| Failed | A step ran into an error |

Contents and the Actions step tracker read the same stored failure signal, so a reload cannot leave one saying "failed" and the other "pending".

---

## The reading column

Sections appear in this order, each with its own anchor.

**Header** — title, authors, source, publication date, citation count, and a link to the original. Above them sit any honesty labels the summary earned: that it is AI-generated and only quoted findings were checked against the PDF; that the summary read only the first part of the paper, or could not be verified at all and fell back to the abstract; or that the paper was analyzed in several passes and fully covered.

**Brief** — the short summary. Until the paper has been analyzed this says so, rather than sitting empty.

**Detailed Summary** — the structured summary covering contributions, methods, and results.

**Methodology** and **Limitations** — the paper's methods and its caveats, drawn out during processing.

**Evidence / Key Findings** — the findings extracted from the paper, each labelled **Verified** or **Unverified** and carrying its supporting quote. This is where the anchors matter: every finding offers a way back to where it came from.

- **Page N →** scrolls to the PDF reader, turns to that page, and flashes the quote. A page snapshot thumbnail, where one exists, does the same when clicked.
- **Passage N of M →** reveals and scrolls to the exact source passage, expanding the Source Passages section first if it is still collapsed.
- If the PDF has not been downloaded, the page badge says so instead of promising a jump it cannot make.

**PDF Reader** — the source PDF rendered in the page, once it has been downloaded. Until then the section invites you to download it.

**Related work** — the paper's citation neighbourhood, at read time, in two parts that answer different questions. **References** and **Cited by** come from citation data and are the paper's actual scholarly graph; papers known only from a bibliography entry appear here too. **Similar in your library** is kept separate on purpose: it relates papers that do *not* cite each other, which citations alone can never surface.

**Contradictions** — claims the detection pipeline flagged as inconsistent with other papers in your library.

**Your Notes** — write, edit, and delete notes here; this is the section that owns them. Zotero highlights appear alongside them when Zotero is connected. Note editing needs a connection.

**Source Passages** — the passages the PDF was split into. Collapsed by default, expanding one at a time, and useful for checking exactly what was processed and what a citation actually points at.

**Ask This Paper** — a chat scoped to this paper alone: it retrieves from this paper's passages and answers with citations. It needs a connection to the model backend.

### Reading the PDF and annotating it

Scroll it as you would any PDF. To highlight, select text: a popup offers an optional note and four colors — Yellow (the default), Green, Blue, and Pink — with **Save** and **Cancel**. Clicking an existing highlight opens an editor below the PDF controls showing the quote read-only alongside the note and color; **Save** updates it and **Delete** removes it after a confirmation.

**Sync highlights to Zotero** pushes this paper's not-yet-synced highlights across as annotations, skipping any already sent. It works only once the paper itself has been sent to Zotero.

The PDF loads over an authenticated request, so both reading it and editing highlights need a connection.

---

## Actions

The Actions panel is where work gets queued. Offline the whole panel is disabled behind one banner rather than each button failing separately, and cached status stays visible.

### Analyze

One **Analyze Paper** button runs three steps in order: download the PDF, extract and chunk its text, then generate the summaries. A step tracker below it shows which step is complete, running, failed, or skipped, and names the reason for a skip. A failed step offers a **Retry** for that step alone.

Once a paper is fully analyzed, the button is replaced by a status line — *Analyzed — passages extracted (N), summary ready* — because a loud call to action on finished work reads as something still left to do.

**Show advanced** reveals the individual steps, and only the ones that still apply: **Download PDF** while there is no PDF, **Process PDF** while there are no passages, **Generate Summary** while there is no summary, and **Regenerate Summary** once there is one.

### Generate Cards

Choose a target deck and a maximum number of cards, then **Generate Cards** turns this paper's passages into spaced-repetition cards. Progress is reported inline. See [Learning Cards](learning-cards.md).

### Recommendation Feedback

For papers JARVIS recommended rather than ones you went and found, thumbs up or down tells the recommender whether it was on target, with an optional reason. It does not appear for papers you added yourself, or for trashed papers.

### Zotero

Shows the sync state and offers **Send to Zotero**, then a link through to the item. Configure the connection in [Settings](settings.md).

### Contradictions

Lists contradictions between this paper and others in your library, naming the conflicting claim on both sides. **Scan for contradictions** queues a fresh scan; a scan that fails says so and reports why.

> Rating and flagging a paper are no longer part of Paper Detail. Reading state and starring live in the toolbar, and notes live in Your Notes.

---

## Related pages

- [Papers & Discover](research-feed.md) — find papers to open here.
- [Ask (Cross-paper RAG)](ask.md) — ask questions spanning your whole library rather than one paper.
- [Learning Cards](learning-cards.md) — review the cards generated here.
- [Knowledge Graph](knowledge-graph.md) — explore entities extracted from this paper.
- [Citation Graph](citation-graph.md) — the full citation network behind Related work.
