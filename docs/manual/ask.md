<!-- verified-against-UI: 2026-05-18 | routes: /ask -->

# Ask — Cross-paper RAG

The **Ask** workspace at `/ask` lets you pose questions that are answered from your entire library at once, rather than from a single paper. It uses a retrieval-augmented generation (RAG) pipeline that searches across all your embedded paper chunks, re-ranks results, and streams a grounded answer with citations.

<!-- screenshot: /ask — chat interface with a question, a streaming answer containing a ConfidenceBadge, and an open SourcesAccordion showing three chunk citations -->

---

## The chat interface

### Composing a question

Type your question in the **input box** at the bottom of the workspace and press Enter or click **Ask**. There is no limit on question length, but concise, focused questions produce better results.

Questions can be:

- Factual — *"What datasets does the literature use to evaluate X?"*
- Comparative — *"How do methods A and B differ in their treatment of Y?"*
- Exploratory — *"What are the open problems in Z according to the papers in my library?"*

### Streaming answers

Answers stream token-by-token via SSE (server-sent events). A **loading indicator** is shown while the system is retrieving and ranking chunks; the answer text then appears progressively as the model generates it.

Each answer message contains:

**ConfidenceBadge** — an indicator of how well the retrieved chunks support the answer. A high-confidence answer is grounded in several closely matching chunks; a low-confidence answer means the library may not contain strong evidence for the question.

**SourcesAccordion** — collapsed by default; click to expand. Lists the paper chunks that were retrieved and used to construct the answer. Each source shows the paper title, chunk excerpt, and a link to the [Paper Detail](paper-detail.md) page for that paper.

**FeedbackButtons** — thumbs-up and thumbs-down buttons for rating the answer quality. Your ratings are recorded and can inform future improvements to the retrieval pipeline.

---

## Message history

Conversation messages persist across navigation within the same session using the **chat store**. If you navigate away to another page and return to `/ask`, your previous messages are still visible. The history is cleared when you sign out or start a new session.

---

## Differences from single-paper Ask

The Ask workspace at `/ask` searches across **all embedded papers** in your library. The in-paper Ask section on the [Paper Detail](paper-detail.md) page is scoped to a single paper's chunks. Use the cross-paper Ask workspace when you want an answer synthesised from multiple sources.

---

## Offline behaviour

The Ask workspace requires a live connection to the LLM backend. It is **not available offline**. If you are offline, the input is disabled and a notice explains that cross-paper RAG is unavailable.

---

## Related pages

- [Paper Detail](paper-detail.md) — single-paper Ask tab for questions scoped to one document.
- [Research Feed & Library](research-feed.md) — add more papers to your library to broaden the evidence base for answers.
- [Settings](settings.md) — configure the LLM models used for answering.
