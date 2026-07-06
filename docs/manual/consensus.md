<!-- verified-against-UI: 2026-06-27 | routes: /consensus -->

# Consensus

Consensus is a model-assisted evidence map over the papers available to JARVIS. It is not a statistical consensus estimate, systematic review, or meta-analysis; missing papers, extraction errors, and model interpretation can change the result.

The **Consensus** page (at `/consensus`, in the **Ⅱ Read** sidebar group) shows where the papers in your library agree and disagree on shared claims extracted by the contradiction-detection pipeline.

<!-- screenshot: /consensus — page showing the "Agreement by claim" stacked bar chart with several claim rows -->

---

## Agreement by claim

The top card is titled **Agreement by claim** and contains a horizontal stacked bar chart — one row per shared claim topic. Each bar is split into two segments:

- **Supports** (green) — the number of cross-paper assessments where one paper supports the claim.
- **Opposes** (red) — the number where a paper contradicts it.

Hovering a bar shows the exact counts.

---

## Claim evidence

Below the chart, each claim appears as an expandable card. The card header shows the claim topic and a summary badge such as **2 support · 1 oppose**.

Click **Show evidence (N)** to expand the card and read the individual assessments. Each assessment shows:

- The **stance** (Supports or Opposes) in the corresponding color.
- The **title and quote** from the first paper, with a page reference where available.
- The **title and quote** from the second paper it was compared against, also with a page reference.

Click **Hide evidence** to collapse the card again.

---

## Empty state

When the pipeline has not yet run a consensus scan, the page shows:

> **No related-paper claims yet**
> Run a contradiction scan across related papers to see where they agree and disagree.

Click **Run consensus scan** to queue a full contradiction scan across your library. The button label changes to **Scanning…** while the job is queued or running, and the background-task menu shows progress. When the whole-library job succeeds, the Consensus page refreshes its data automatically.

A completed scan can still produce no chart. In that case the page distinguishes a true empty outcome from a failed scan: a successful zero-result scan says that no verified agreement or contradiction clusters are available yet, while a failed scan shows the failure message and leaves the retry action available. JARVIS does not lower quote-verification thresholds just to populate this view.

---

## Related pages

- [Paper Detail](paper-detail.md) — the **Contradictions** card in the right pane triggers a per-paper contradiction scan and lists contradictions for that paper.
- [Citation Graph](citation-graph.md) — explore citation relationships between the same papers.
- [Knowledge Graph](knowledge-graph.md) — visualise the entities and relationships extracted from your library.
