/**
 * RestoreRunbook — read-only guided restore procedure.
 *
 * Restore is a destructive, cross-tenant host operation (DROPs both DBs,
 * overwrites live secrets) and the app container cannot run pg_restore or reach
 * the live secrets dir — so this panel only SHOWS the host commands from
 * docs/DEPLOYMENT.md. It never executes anything.
 */

const STEPS: { title: string; note?: string; code: string }[] = [
  {
    title: 'Step 0 — decrypt (only for .enc archives)',
    note: 'Applies to any encrypted artifact, including Qdrant snapshots (e.g. qdrant_kg_entities_YYYYMMDD_HHMMSS.snapshot.enc). Decrypt to the plain filename (drop the .enc suffix) before using it below.',
    code: `openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -d \\
  -kfile ./secrets/backup_encrypt_key.txt \\
  -in <archive>.enc -out <archive>`,
  },
  {
    title: 'Step 1 — restore secrets/ (before starting the stack)',
    note: 'Restore the matching secrets archive first so the encryption keys are in place; a DB dump restored without its key leaves provider credentials undecryptable.',
    code: `tar -tzf secrets_YYYYMMDD_HHMMSS.tar.gz
tar -xzf secrets_YYYYMMDD_HHMMSS.tar.gz -C ./secrets`,
  },
  {
    title: 'Step 2 — restore Postgres (jarvis + litellm)',
    code: `docker compose stop paper_ingestion learning_engine
docker compose exec postgres psql -U jarvis -d postgres \\
  -c 'DROP DATABASE jarvis;' -c 'CREATE DATABASE jarvis OWNER jarvis;'
gunzip -c /path/to/jarvis_YYYYMMDD_HHMMSS.sql.gz | \\
  docker compose exec -T postgres psql -U jarvis -d jarvis
docker compose exec postgres psql -U jarvis -d postgres \\
  -c 'DROP DATABASE litellm;' -c 'CREATE DATABASE litellm OWNER jarvis;'
gunzip -c /path/to/litellm_YYYYMMDD_HHMMSS.sql.gz | \\
  docker compose exec -T postgres psql -U jarvis -d litellm
docker compose up -d paper_ingestion learning_engine`,
  },
  {
    title: 'Step 3 — restore Qdrant vectors',
    note: 'One snapshot per collection. The collection list is whatever qdrant_<name>_YYYYMMDD_HHMMSS.snapshot files exist for the chosen restore point — currently kg_entities and paper_chunks. Restore each collection independently; repeat the block per collection. Encrypted snapshots (.snapshot.enc) must be decrypted first (Step 0).',
    code: `# Restore kg_entities
docker compose stop qdrant
docker compose cp qdrant_kg_entities_YYYYMMDD_HHMMSS.snapshot qdrant:/qdrant/snapshots/restore.snapshot
docker compose start qdrant
docker compose exec qdrant sh -c 'curl -s -X PUT \\
  -H "api-key: $(cat /run/secrets/qdrant_api_key)" \\
  "http://localhost:6333/collections/kg_entities/snapshots/recover" \\
  -H "Content-Type: application/json" \\
  -d "{\\"location\\":\\"file:///qdrant/snapshots/restore.snapshot\\"}"'

# Restore paper_chunks
docker compose stop qdrant
docker compose cp qdrant_paper_chunks_YYYYMMDD_HHMMSS.snapshot qdrant:/qdrant/snapshots/restore.snapshot
docker compose start qdrant
docker compose exec qdrant sh -c 'curl -s -X PUT \\
  -H "api-key: $(cat /run/secrets/qdrant_api_key)" \\
  "http://localhost:6333/collections/paper_chunks/snapshots/recover" \\
  -H "Content-Type: application/json" \\
  -d "{\\"location\\":\\"file:///qdrant/snapshots/restore.snapshot\\"}"'`,
  },
];

export function RestoreRunbook() {
  return (
    <section aria-labelledby="restore-runbook-heading" className="space-y-4">
      <div>
        <h2 id="restore-runbook-heading" className="text-base font-semibold">
          Manual restore (advanced)
        </h2>
        <p className="text-sm text-muted-foreground mt-1">
          Most restores are handled by the one-click flow above. This manual host procedure is the
          documented fallback: it is destructive and runs on the host, not in the app. Download the
          archives above, then run these commands on the deployment host. See the{' '}
          <a
            href="https://ffidan.github.io/Jarvis-RD-Assistant/DEPLOYMENT/#restore"
            className="underline"
            target="_blank"
            rel="noopener noreferrer"
          >
            Deployment Guide → Restore
          </a>{' '}
          for the full procedure.
        </p>
      </div>
      {STEPS.map((step) => (
        <div key={step.title} className="rounded-md border p-4 space-y-2">
          <h3 className="text-sm font-medium">{step.title}</h3>
          {step.note && <p className="text-xs text-muted-foreground">{step.note}</p>}
          <pre className="overflow-x-auto rounded bg-muted p-3 text-xs">
            <code>{step.code}</code>
          </pre>
        </div>
      ))}
    </section>
  );
}
