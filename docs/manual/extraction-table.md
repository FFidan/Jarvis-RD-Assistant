<!-- verified-against-UI: 2026-06-06 | routes: /extractions -->

# Extraction Table

The **Extraction Table** page at `/extractions` lets you run structured data extraction across multiple papers using LLM-powered templates, and then compare and export the results.

<!-- screenshot: /extractions — ExtractionDataTable with columns for three papers and rows for each template field, with an Export CSV button visible -->

---

## Workflow

### 1. Select a template

Templates define the fields to extract — for example a template might specify columns for "Sample size", "Evaluation metric", and "Baseline model". Choose a template from the **template selector** dropdown.

Templates are managed in [Settings → System → Extraction Templates](settings.md). Creating and editing templates is an admin function; using them to extract data is available to all users.

### 2. Select papers

Use the **PaperSearchSelect** control — a multi-select search field — to choose one or more papers from your library. Type a title or author name to find papers. You can select as many papers as you need.

### 3. Extract Selected

Click the **Extract Selected** button. The system submits an LLM extraction job for each selected paper using the chosen template. A progress indicator shows extraction status per paper. Depending on the number of papers and the complexity of the template, extraction may take from a few seconds to several minutes.

---

## ExtractionDataTable

Once extraction is complete, the results are displayed in the **ExtractionDataTable**:

- **Rows** correspond to the fields defined in the template.
- **Columns** correspond to the papers you selected.
- Each cell shows the extracted value for that field in that paper. Cells may be empty if the model could not find a relevant value.

The table helps compare how selected papers describe the same aspects of a research problem. It can support screening and evidence organization, but every extracted value should be checked against the source before use in a systematic review or meta-analysis.

---

## Export CSV

Click **Export CSV** to download the current table as a comma-separated values file. The CSV includes all visible rows and columns. You can open it in any spreadsheet application for further analysis.

---

## Related pages

- [Settings](settings.md) — manage extraction templates (§IV System → Extraction Templates; admin required).
- [Paper Detail](paper-detail.md) — run entity extraction for a single paper from the right-hand actions sidebar.
- [Knowledge Graph](knowledge-graph.md) — visualise entities extracted from papers.
