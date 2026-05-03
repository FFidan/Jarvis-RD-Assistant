# B.3 — mxbai-rerank-base-v2 Drop-in
**Status:** COMPLETE
**Scope:** Replace cross-encoder/ms-marco-MiniLM-L-6-v2 default in Reranker.__init__
**Change:** reranker.py line ~35, one-line default param swap
**Why:** mxbai-rerank-base-v2 outperforms ms-marco on BEIR benchmark at same latency;
same CrossEncoder API (sentence-transformers compatible). Model is ~280 MB vs ~22 MB —
first-run download spike expected; noted in REQUIREMENTS.md.
