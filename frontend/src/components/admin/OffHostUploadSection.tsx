/**
 * Off-host recovery upload: mint a one-time upload grant, then stream a backup
 * archive set (plus the one-time operator key) from the browser straight into
 * this server's restore inbox via the dedicated restore-uploader sidecar. The
 * app never sees the bytes; the sidecar enforces the grant and the filename
 * allowlist — the client-side checks here only fail fast with a friendlier
 * message before a multi-GB PUT is wasted.
 */

import { useEffect, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { createUploadGrant, uploadRestoreFile } from '@/lib/api/backups';

// Mirrors services/restore_uploader/uploader.py _FILENAME_RE — the sidecar is
// the enforcing copy; keep the shapes identical when either side changes.
const TS = String.raw`\d{8}_\d{6}`;
const ALLOWED_FILENAME_RE = new RegExp(
  `^(?:jarvis_${TS}\\.sql\\.gz(?:\\.enc)?` +
    `|litellm_${TS}\\.sql\\.gz(?:\\.enc)?` +
    `|secrets_${TS}\\.tar\\.gz(?:\\.enc)?` +
    `|qdrant_[A-Za-z0-9_-]+_${TS}\\.snapshot(?:\\.enc)?` +
    `|manifest_${TS}\\.json` +
    `|operator_key)$`,
);

/** The uploader stores the one-time key under exactly this name, whatever the picked file is called. */
const OPERATOR_KEY_NAME = 'operator_key';

interface UploadItem {
  id: number;
  file: File;
  /** Name PUT to the uploader — the picked file's own name, or the literal operator_key. */
  targetName: string;
  status: 'queued' | 'uploading' | 'done' | 'failed';
  progress: number;
  error: string | null;
}

interface Grant {
  token: string;
  expiresAt: number; // epoch ms
}

function formatCountdown(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export function OffHostUploadSection({ onUploaded }: { onUploaded: () => void }) {
  const [grant, setGrant] = useState<Grant | null>(null);
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [rejected, setRejected] = useState<string[]>([]);
  const [uploading, setUploading] = useState(false);
  const nextId = useRef(0);

  const grantMutation = useMutation({
    mutationFn: () => createUploadGrant(),
    onSuccess: (data) => {
      setGrant({
        token: data.grant_token,
        expiresAt: Date.now() + data.expires_in_seconds * 1000,
      });
    },
  });

  // Live countdown; when the grant expires the controls lock until a new one is minted.
  useEffect(() => {
    if (!grant) return;
    const tick = () => {
      const left = Math.max(0, Math.ceil((grant.expiresAt - Date.now()) / 1000));
      setSecondsLeft(left);
      if (left === 0) setGrant(null);
    };
    tick();
    const intervalId = setInterval(tick, 1000);
    return () => clearInterval(intervalId);
  }, [grant]);

  const addFiles = (files: FileList | null, targetName?: string) => {
    if (!files) return;
    const bad: string[] = [];
    const good: UploadItem[] = [];
    for (const file of Array.from(files)) {
      const name = targetName ?? file.name;
      if (!ALLOWED_FILENAME_RE.test(name)) {
        bad.push(file.name);
        continue;
      }
      good.push({
        id: nextId.current++,
        file,
        targetName: name,
        status: 'queued',
        progress: 0,
        error: null,
      });
    }
    setRejected(bad);
    if (good.length > 0) {
      // Re-picking a file replaces its queued row instead of duplicating it.
      setItems((prev) => [
        ...prev.filter((item) => !good.some((g) => g.targetName === item.targetName)),
        ...good,
      ]);
    }
  };

  const uploadOne = async (item: UploadItem, token: string) => {
    const update = (patch: Partial<UploadItem>) =>
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, ...patch } : i)));
    update({ status: 'uploading', progress: 0, error: null });
    try {
      await uploadRestoreFile(item.targetName, item.file, token, (percent) =>
        update({ progress: percent }),
      );
      update({ status: 'done', progress: 100 });
      onUploaded();
    } catch (e) {
      update({ status: 'failed', error: e instanceof Error ? e.message : 'Upload failed.' });
    }
  };

  const startUploads = async () => {
    if (!grant) return;
    setUploading(true);
    try {
      for (const item of items) {
        if (item.status === 'queued' || item.status === 'failed') {
          await uploadOne(item, grant.token);
        }
      }
    } finally {
      setUploading(false);
    }
  };

  const hasUploadable = items.some((i) => i.status === 'queued' || i.status === 'failed');

  return (
    <div className="rounded-md border p-4 space-y-3" data-testid="offhost-upload-section">
      <div>
        <h2 className="text-sm font-medium">Upload a backup from another JARVIS</h2>
        <p className="text-xs text-muted-foreground">
          Stage an off-host backup in this server&apos;s restore inbox without shell access:
          generate a one-time upload grant, then upload the backup&apos;s archive set and its
          one-time key. Files stream straight to the restore inbox — the app never sees them.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className="rounded-md border px-3 py-1.5 text-sm font-medium disabled:opacity-50"
          data-testid="generate-upload-grant"
          onClick={() => grantMutation.mutate()}
          disabled={grantMutation.isPending}
        >
          {grant ? 'Regenerate upload grant' : 'Generate upload grant'}
        </button>
        {grant && (
          <span className="text-xs text-muted-foreground" data-testid="upload-grant-countdown">
            Grant expires in {formatCountdown(secondsLeft)}
          </span>
        )}
        {grantMutation.isError && (
          <span className="text-xs text-destructive">
            {grantMutation.error instanceof Error
              ? grantMutation.error.message
              : 'Could not generate an upload grant.'}
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium">Backup archives</span>
          <input
            type="file"
            multiple
            data-testid="upload-file-input"
            aria-label="Backup archive files"
            className="text-sm"
            disabled={!grant}
            onChange={(e) => {
              addFiles(e.target.files);
              e.target.value = '';
            }}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium">One-time key (stored as operator_key)</span>
          <input
            type="file"
            data-testid="operator-key-input"
            aria-label="One-time operator key"
            className="text-sm"
            disabled={!grant}
            onChange={(e) => {
              addFiles(e.target.files, OPERATOR_KEY_NAME);
              e.target.value = '';
            }}
          />
        </label>
      </div>

      {rejected.length > 0 && (
        <p className="text-xs text-destructive" data-testid="upload-rejected">
          Not part of a backup set (skipped): {rejected.join(', ')}. Upload the archive files
          exactly as the backup produced them.
        </p>
      )}

      {items.length > 0 && (
        <ul className="space-y-2">
          {items.map((item) => (
            <li
              key={item.id}
              data-testid="upload-file-row"
              className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3"
            >
              <div className="min-w-0 space-y-1">
                <div className="font-mono text-xs break-all">{item.targetName}</div>
                {item.status === 'uploading' && (
                  <div className="h-1.5 w-48 overflow-hidden rounded-full bg-muted">
                    <div className="h-full bg-primary" style={{ width: `${item.progress}%` }} />
                  </div>
                )}
                {item.error && <p className="text-xs text-destructive">{item.error}</p>}
              </div>
              {item.status === 'done' ? (
                <span className="inline-flex rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/30 dark:text-green-400">
                  Uploaded
                </span>
              ) : item.status === 'uploading' ? (
                <span className="text-xs text-muted-foreground">{item.progress}%</span>
              ) : item.status === 'failed' ? (
                <button
                  type="button"
                  className="rounded-md border px-3 py-1 text-xs font-medium disabled:opacity-50"
                  disabled={!grant || uploading}
                  onClick={() => {
                    if (grant) void uploadOne(item, grant.token);
                  }}
                >
                  Retry
                </button>
              ) : (
                <span className="text-xs text-muted-foreground">Queued</span>
              )}
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
        data-testid="upload-start"
        onClick={() => void startUploads()}
        disabled={!grant || uploading || !hasUploadable}
      >
        {uploading ? 'Uploading…' : 'Upload files'}
      </button>
    </div>
  );
}
