# Methods and limitations

JARVIS RD Assistant is a research-support tool, not an autonomous scientific authority. It combines literature discovery, document extraction, retrieval, language-model generation, and learning workflows in a self-hosted application.

## What “verified” means

Verification checks whether generated text can be matched to passages retrieved from the user's corpus. For verbatim evidence, the system compares quotes with extracted paper text. For synthesized answers, it checks sentence-level support against retrieved passages.

A verified result is traceable to retrieved text. It does **not** establish that:

- the source paper is correct;
- the generated interpretation is complete or scientifically valid;
- the relevant literature was exhaustively retrieved;
- a result has been independently reproduced; or
- the output is suitable for clinical, legal, safety-critical, or other high-stakes decisions.

## Sources of error

Results depend on the papers available to the system, metadata quality, PDF extraction, chunking and retrieval, model choice, prompts, and configuration. Scanned documents, tables, equations, unusual layouts, and inaccessible full text can reduce evidence quality. Language models can still omit qualifications, combine incompatible findings, or produce plausible unsupported text.

Verification badges should therefore be treated as provenance signals. Researchers should open the cited passage and inspect the original paper before relying on a claim.

## Consensus and evidence extraction

Consensus views summarize model-interpreted relationships among the available papers. Extraction tables organize model-extracted fields for comparison. Neither feature implements a systematic-review protocol, risk-of-bias assessment, statistical meta-analysis, or comprehensive literature search.

## Data flow and privacy

The application and its databases are self-hosted. With Ollama, model inference can remain on infrastructure controlled by the operator. When a cloud model is configured through LiteLLM, prompts and relevant source excerpts are transmitted to that provider under its terms and retention policies.

Application-level user isolation does not remove the infrastructure trust boundary. Operators with access to the host, database, backups, logs, or configured model providers may be able to access research data.

## Evaluation status

Repository tests exercise software behavior, retrieval contracts, tenant isolation, and user-interface flows. They are not a peer-reviewed evaluation of scientific accuracy. Performance notes in the repository document engineering measurements in their stated environments and should not be generalized beyond those conditions.
