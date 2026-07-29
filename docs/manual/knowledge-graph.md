<!-- verified-against-UI: 2026-07-25 | routes: /knowledge -->

# Knowledge Graph

The **Knowledge Graph** page at `/knowledge` displays an interactive concept-level graph of entities extracted from papers visible to you — public papers plus any papers in your own library — rendered using Cytoscape.js.

<!-- screenshot: /knowledge — Cytoscape graph with EntityTypeFilter chips visible, coloured nodes, and GraphStats panel -->

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

### EntityTypeFilter

A row of filter chips along the top of the graph panel lets you show or hide entity types. Selecting **All** resets the filter to show every type. Deselecting a type removes those nodes (and their edges) from the visible graph.

### Min-paper-count slider

A slider with a range of **1–10** filters nodes by how many papers they appear in. Dragging the slider to the right hides entities that appear in fewer papers, reducing clutter and surfacing the most widely-attested concepts across the papers visible to you.

### GraphControls

A **Layout** dropdown selects one of four layout algorithms: **Force-directed** (cose), **Breadth-first**, **Circle**, or **Concentric**.

Pan and zoom are available via mouse or trackpad gestures directly on the graph canvas.

### KGQueryInput

A natural-language query box. Type a question such as "What methods are used on ImageNet?" and submit it to search the knowledge graph; matching relationships, comparisons, or entities come back as a list of result cards below the input, rather than as a filter or highlight applied to the graph itself. If nothing matches, a "No results found for this query" message is shown instead.

---

## Graph statistics

**GraphStats** — a summary panel showing the total number of nodes and edges currently visible, broken down by entity type.

**EntityBreakdown** — a list or chart showing how many entities of each type exist in the full (unfiltered) graph, helping you understand the composition of your knowledge graph.

---

## Batch extraction

If unprocessed papers exist, an admin-only **Batch Extract Entities** button appears on an otherwise empty graph. Clicking it processes up to 50 summarized papers per run that do not yet have extracted entities. The graph populates as extraction jobs complete.

You can also trigger extraction for individual papers from the [Paper Detail](paper-detail.md) page (the Actions rail → Extract Entities).

---

## Related pages

- [Paper Detail](paper-detail.md) — trigger entity extraction for a single paper; also shows cross-references and contradictions.
- [Citation Graph](citation-graph.md) — paper-level network as opposed to the concept-level graph here.
- [Extraction Table](extraction-table.md) — tabular view of the same extracted data.
