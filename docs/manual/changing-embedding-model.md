# Changing the embedding model

The embedding model turns paper chunks and search questions into vectors for
semantic search. It is different from the **Main** and **Quick** models: those
roles can be changed in **Settings → Models**, but the embedding model is tied
to every vector in the search index. Changing it means rebuilding that index.

Consider a change when a candidate model improves retrieval for your research
questions, supports a language or subject area better, or is a better fit for
available hardware. Do not change it just to alter summary style; use the Main
or Quick model for that.

This is an administrator operation. Read [Backup and restore](backup-and-restore.md)
and [Deployment](../DEPLOYMENT.md) first. The model and hardware background is
in [What your hardware gets you](hardware-and-models.md).

## Before you change anything

Record the current configuration and index state. The embedding route is the
`embed` entry in [`litellm/config.yaml`](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/litellm/config.yaml).
Its provider model and `dimensions` must agree with `EMBEDDING_MODEL_NAME` and
`EMBEDDING_DIMENSION` in `.env`. The default is also listed in
[What your hardware gets you](hardware-and-models.md).

The dimension is the length of every vector stored in Qdrant. A candidate with
the same dimension can use the existing collection shape, but it still needs a
full re-embedding because vectors from different models are not comparable. A
candidate with a different dimension requires collection recreation; existing
vectors cannot be mixed with it.

Before proceeding, inspect:

- the `embed` model and `dimensions` in `litellm/config.yaml`;
- `EMBEDDING_MODEL_NAME` and `EMBEDDING_DIMENSION` in `.env`;
- the current Qdrant collection dimension and point count; and
- a few representative semantic-search questions and their useful results.

Take a complete restore point in **Admin → Backups** and confirm that it is
complete before recreating a collection. Restore points include the Qdrant
snapshot, database, PDFs, and data keys. Keep the encryption key separately
from the downloaded archive set as described in [Backup and restore](backup-and-restore.md).

## Evaluate the candidate

Benchmark the candidate against representative questions before migrating the
library. Record retrieval quality as well as speed; throughput alone does not
show whether search results are better.

`scripts/reembed.py` also has a read-only benchmark mode. It samples existing
chunk text, calls the configured embedding backend, and reports latency,
throughput, and returned dimension without writing to PostgreSQL or Qdrant:

```bash
uv run python -m scripts.reembed --benchmark
```

Run this command from the repository checkout on the host, where its database,
Qdrant, and LiteLLM connection settings are available. It is not a command to
run inside the application container. The candidate configuration must already
be in the LiteLLM/environment contract for the benchmark to exercise it.

## Configure and pull the model

Make one coherent configuration change:

1. Update the YAML-seeded `embed` route in `litellm/config.yaml`, including its
   `dimensions` value.
2. Set `.env` `EMBEDDING_MODEL_NAME` and `EMBEDDING_DIMENSION` to the same
   provider model and dimension. Keep `EMBEDDING_MODEL=embed` unless the
   deployment contract deliberately uses a different alias.
3. For a local Ollama model, pull it through the deployed container:

   ```bash
   docker compose exec ollama ollama pull <embedding-model>
   ```

4. Apply the changed LiteLLM configuration through the deployment:

   ```bash
   docker compose restart litellm
   ```

Do not put provider credentials in commands, shell history, or documentation.
Configure provider credentials through the supported deployment settings.

The `docker compose` commands above operate on containers. The re-embedding
tool below is run from the host checkout; it needs the same reachable services
and configuration, but it is not included in the running `paper_ingestion`
image.

## Rebuild the index

With the backup confirmed and the candidate benchmarked, run the existing tool:

```bash
uv run python -m scripts.reembed
```

The tool first warms up the embedding backend, checks every returned vector's
dimension, then processes papers in batches and logs paper and batch progress.
It updates a paper only after checking its chunks under a per-paper lock.

If the configured dimension differs from the existing Qdrant collection, the
tool stops. Collection recreation is deliberately destructive and requires both
of these explicit confirmations, after you have taken the snapshot:

```bash
REEMBED_RECREATE_COLLECTION=true REEMBED_SNAPSHOT_CONFIRMED=true \
  uv run python -m scripts.reembed
```

Do not use those flags for a routine same-dimension migration. They delete and
recreate the configured collection only when its dimension is wrong.

## Progress, interruption, and failure

Watch the command output for warm-up time, per-paper progress, batch progress,
and the final postcondition result. The default behavior stops on a failed
paper or stale-point cleanup failure; `--continue-on-error` is a debug option,
not the normal migration path.

The operation is restartable. A subsequent ordinary run finds papers whose
target-model vectors or metadata are missing or inconsistent and processes
them again; papers already embedded consistently with the target are skipped.
If the command fails, keep the configuration and restore point, read the first
reported error, correct its cause, and run the ordinary command again. Do not
claim completion from partial progress.

After a collection recreation, the prior vectors are no longer present in that
collection. The pre-change restore point remains the rollback boundary. A
failed run does not replace that restore point.

## Verify and roll back

On a successful run, the tool verifies that the database count for the target
embedding model equals the Qdrant point count for that model and that each
current paper has a matching visible vector. Then verify the active `embed`
route and its dimension in the deployed configuration, check the collection
dimension and point count, and repeat the representative semantic-search
questions recorded before the change.

If the candidate is not suitable, restore the previous `embed` YAML and `.env`
values, restart LiteLLM, and restore the pre-change restore point from
**Admin → Backups**. Use the guided restore procedure in
[Backup and restore](backup-and-restore.md); it restores the database and
search-index snapshot together. Do not try to combine vectors from the old and
new embedding models.
