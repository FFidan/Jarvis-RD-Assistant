<!-- verified-against-UI: 2026-07-25 | routes: /citations -->

# Citation Graph

The **Citation Graph** page at `/citations` displays an interactive paper-level citation network rendered using Cytoscape.js. Unlike the [Knowledge Graph](knowledge-graph.md), which maps concepts and entities, the Citation Graph maps the relationships between papers themselves.

<!-- screenshot: /citations — Cytoscape graph with two seed papers, their citation neighbours at depth 1, an "influential" edge label, and the GraphControls panel -->

---

## Building the graph

### CitationPaperSelector

Use the **CitationPaperSelector** at the top of the page to choose one or more **seed papers** from your library. The control is a multi-select search field — type a title or author name to find papers. The graph is built starting from the papers you select here.

### Depth slider

The **depth slider** controls how many hops of citation relationships to include:

- **1 hop** — shows the seed papers and all papers they directly cite or are cited by.
- **2 hops** — extends the graph to include papers cited by (or citing) the first-hop papers.

Deeper graphs can become dense; start with 1 hop and extend to 2 when you want a broader view.

### FetchCitationsButton

Click **Fetch citations** to build the graph from the selected seeds and depth. If citation data for a seed paper has not yet been fetched, this button triggers the backend citation-fetch job. Depending on the size of the network, fetching may take a few seconds.

---

## Graph layout

### Layout options — GraphControls

The **GraphControls** panel provides four layout algorithms:

| Layout | Best for |
|--------|---------|
| **cose** | General-purpose force-directed layout; good for medium-sized networks |
| **breadthfirst** | Tree-like radial layout; highlights citation chains clearly |
| **circle** | Nodes evenly spaced on a circle; useful for small, dense networks |
| **concentric** | Concentric rings; useful for showing structural layers in the citation network |

Switch between layouts without re-fetching the citation data.

Pan and zoom are available via mouse/trackpad directly on the canvas.

---

## Node types

| Node style | Meaning |
|-----------|---------|
| **Blue** | A paper that is in your library |
| **Grey** (stub) | A cited paper that has been referenced in citation data but has not been fetched or saved to your library |

Click a node to open it: a full paper navigates to its [Paper Detail](paper-detail.md) page; a
stub opens an in-page panel listing its title, citation count, and published date. Nodes are also
reachable from a keyboard-only hidden list on the page, activating the same handler as a click.

---

## Edge labels

Edges represent citation relationships. An edge labelled **"influential"** indicates that the citing paper has been flagged as making a significant intellectual contribution to the cited work, as determined by the Semantic Scholar `is_influential` signal.

---

## Graph statistics

**GraphStats** — a summary panel showing the number of nodes and edges currently visible in the graph.

---

## Related pages

- [Knowledge Graph](knowledge-graph.md) — concept-level entity graph.
- [Paper Detail](paper-detail.md) — cross-references section shows direct citation neighbours for a single paper.
- [Research Feed & Library](research-feed.md) — where you search for and save papers, including ones you noticed while browsing the graph.
