<!-- verified-against-UI: 2026-07-25 | routes: /knowledge -->

# Knowledge Graph

The **Knowledge Graph** page at `/knowledge` displays an interactive concept-level graph of entities extracted from papers visible to you — public papers plus any papers in your own library — rendered using Cytoscape.js.

<!-- screenshot: /knowledge — Cytoscape graph with the Filters card (entity type dropdown and paper-count slider), coloured nodes, and GraphStats panel -->

---

## Entity types and colours

The graph visualises six categories of entity, each with a distinct node colour:

| Entity type | Node colour |
|-------------|-------------|
| Method | Blue |
| Dataset | Green |
| Metric | Orange |
| Concept | Purple |
| Institution | Red |
| Author | Brown |

---

## Controls

### Entity type

A dropdown in the Filters card selects one entity type at a time. The graph is re-fetched for the
type you pick, so only entities of that type and the edges between them are returned. Choosing
**All** removes the restriction and shows every type again.

### Min-paper-count slider

A slider with a range of **1–10** filters nodes by how many papers they appear in. Dragging the slider to the right hides entities that appear in fewer papers, reducing clutter and surfacing the most widely-attested concepts across the papers visible to you.

### GraphControls

A **Layout** dropdown selects one of four layout algorithms: **Force-directed** (cose), **Breadth-first**, **Circle**, or **Concentric**.

Pan and zoom are available via mouse or trackpad gestures directly on the graph canvas.

### Reset filters

A **Reset filters** button, beside the layout selector, clears the entity-type filter, the min-paper-count threshold, and the current node selection back to their defaults in one click.

### Node selection

Click a node to open a details panel beside the graph showing its name, type, canonical name (if different), how many papers it appears in, its description, and its relationships to other visible entities. A **Clear selection** button in the panel header closes the detail view. Selecting a node does not navigate away from the page.

### KGQueryInput

A natural-language query box. Type a question such as "What methods are used on ImageNet?" and submit it to search the knowledge graph; matching relationships, comparisons, or entities come back as a list of result cards below the input, rather than as a filter or highlight applied to the graph itself. If nothing matches, a "No results found for this query" message is shown instead.

---

## Graph statistics

**GraphStats** — a summary panel showing three totals for the graph currently on screen: the number of entities, the number of relationships, and how many distinct entity types are present.

**EntityBreakdown** — how many entities of each type are in the graph you are currently looking at. It follows the filters, so narrowing to a single entity type leaves one row; clear the filters to see the whole composition.

---

## Batch extraction

An admin-only **Batch Extract Entities** button appears whenever the graph comes back empty. Clicking it processes up to 50 summarized papers per run that do not yet have extracted entities, and reports how many it handled — if nothing has been summarized yet, that count is zero and the graph stays empty until summaries exist. The graph populates as extraction jobs complete.

You can also trigger extraction for individual papers from the [Paper Detail](paper-detail.md) page (the Actions rail → Extract Entities).

---

## Related pages

- [Paper Detail](paper-detail.md) — trigger entity extraction for a single paper; also shows cross-references and contradictions.
- [Citation Graph](citation-graph.md) — paper-level network as opposed to the concept-level graph here.
- [Extraction Table](extraction-table.md) — tabular view of the same extracted data.
