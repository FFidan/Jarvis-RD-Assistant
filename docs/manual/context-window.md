<!-- verified-against-UI: 2026-07-11 | routes: /settings?section=models -->

# Context window (reading window)

The **reading window** — technically `num_ctx` — is how much of a paper the AI reads in a single pass, measured in tokens. There is one such number per model role (main model, quick model). You set it on the **Settings → Models** page using the slider next to each model; no environment-variable editing is needed.

---

## Full coverage regardless of window size

A larger reading window does **not** mean more of the paper gets read. Long papers are read in **full** via a map-reduce pipeline regardless of the window size:

1. The paper's text is split into windows sized to fit the reading window.
2. The AI summarises each window in sequence ("map" pass).
3. A final "reduce" pass synthesises the per-window digests into the structured summary you see.

This means: a smaller window → more passes, never less paper covered. Coverage is always 1.0 (full) on the normal path.

When a paper needed more than one pass, the Paper Detail page shows a quiet note — "Read in N passes — full paper covered". A coverage warning only appears on the rare degraded-fallback path (abstract-only summary when the PDF could not be processed), not because the reading window was small.

---

## When a larger reading window helps, and when it does not

**On a GPU:** a larger window means fewer passes and therefore faster first-analysis. JARVIS bounds the slider to a KV-cache-safe maximum for the model and the VRAM available, so you cannot set a value that causes out-of-memory errors.

**On CPU:** raising the reading window does not make passes faster and can make each pass slower. Coverage is already full at any window size.

**Cloud models:** the reading window follows the cloud model's own context limit with a bounded input ceiling. The slider is informational for cloud models; you cannot exceed what the provider supports.

---

## Where to change it

Open **Settings → Models** (admin only). Each model role shows a **Configure** disclosure with the reading-window slider. Your change applies immediately — no restart required.

---

## Related pages

- [Settings](settings.md) — the Models section with the reading-window slider.
- [Paper Detail](paper-detail.md) — where the "Read in N passes" note appears after a multi-pass analysis.
- [Ask — Cross-paper RAG](ask.md) — the Ask feature uses retrieved passages rather than the full-paper pipeline; the reading window does not directly affect Ask answers.
- [What your hardware gets you](hardware-and-models.md) — how VRAM determines the recommended model and the upper bound on the reading window.
