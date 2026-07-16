/**
 * Transport-level tests for the REAL off-host upload client in lib/api/backups:
 * uploadRestoreFile's XHR PUT (status branching, UploadError mapping, network
 * error, abort, progress) and createUploadGrant's POST. Every other suite mocks
 * '@/lib/api/backups'; this one imports the actual module and fakes the
 * transport (XMLHttpRequest / fetch) instead.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createUploadGrant, uploadRestoreFile, UploadError } from '@/lib/api/backups';

type ProgressHandler = (e: { lengthComputable: boolean; loaded: number; total: number }) => void;

/** Minimal XHR fake: records open/setRequestHeader/send, lets tests drive the events. */
class FakeXHR {
  static instances: FakeXHR[] = [];
  method = '';
  url = '';
  headers: Record<string, string> = {};
  status = 0;
  sentBody: unknown = null;
  aborted = false;
  upload: { onprogress: ProgressHandler | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  onloadend: (() => void) | null = null;

  constructor() {
    FakeXHR.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }

  send(body: unknown) {
    this.sentBody = body;
  }

  abort() {
    this.aborted = true;
    this.onabort?.();
    this.onloadend?.();
  }

  /** Complete the request with the given HTTP status (fires onload → onloadend). */
  respond(status: number) {
    this.status = status;
    this.onload?.();
    this.onloadend?.();
  }

  /** Fail at the network layer (fires onerror → onloadend). */
  networkError() {
    this.onerror?.();
    this.onloadend?.();
  }
}

/** Start an upload against the fake transport and return the promise + driving XHR. */
function startUpload(opts?: { onProgress?: (p: number) => void; signal?: AbortSignal }) {
  const promise = uploadRestoreFile(
    'jarvis_20260101_000000.sql.gz',
    new Blob(['dump']),
    'grant-tok-1',
    opts?.onProgress,
    opts?.signal,
  );
  const xhr = FakeXHR.instances[FakeXHR.instances.length - 1];
  if (!xhr) throw new Error('uploadRestoreFile did not construct an XMLHttpRequest');
  return { promise, xhr };
}

/** Await the rejection of an upload promise and return the thrown value. */
const rejectionOf = (promise: Promise<void>): Promise<unknown> =>
  promise.then(
    () => {
      throw new Error('expected the upload to reject');
    },
    (err: unknown) => err,
  );

/** Await the rejection and assert (with type narrowing) that it is an UploadError. */
async function uploadErrorOf(promise: Promise<void>): Promise<UploadError> {
  const err = await rejectionOf(promise);
  expect(err).toBeInstanceOf(UploadError);
  if (!(err instanceof UploadError)) throw new Error('expected an UploadError');
  return err;
}

describe('uploadRestoreFile (real transport)', () => {
  beforeEach(() => {
    FakeXHR.instances = [];
    vi.stubGlobal('XMLHttpRequest', FakeXHR);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('PUTs the file to /restore-upload/<name> with the grant header and resolves on 201', async () => {
    const { promise, xhr } = startUpload();
    expect(xhr.method).toBe('PUT');
    expect(xhr.url).toBe('/restore-upload/jarvis_20260101_000000.sql.gz');
    expect(xhr.headers['X-Upload-Grant']).toBe('grant-tok-1');
    expect(xhr.sentBody).toBeInstanceOf(Blob);
    xhr.respond(201);
    await expect(promise).resolves.toBeUndefined();
  });

  it.each([
    [401, /grant missing/i],
    [403, /grant invalid or expired/i],
    [413, /exceeds the server upload size limit/i],
    [507, /not enough free disk space/i],
    [500, /Upload failed \(HTTP 500\)/],
  ])('maps a non-201 status %i to a typed UploadError', async (status, message) => {
    const { promise, xhr } = startUpload();
    xhr.respond(status);
    const err = await uploadErrorOf(promise);
    expect(err.status).toBe(status);
    expect(err.message).toMatch(message);
  });

  it('maps a network error to UploadError(0) with the network message', async () => {
    const { promise, xhr } = startUpload();
    xhr.networkError();
    const err = await uploadErrorOf(promise);
    expect(err.status).toBe(0);
    expect(err.message).toMatch(/network error/i);
  });

  it('aborts the XHR when the signal fires and rejects with an AbortError', async () => {
    const controller = new AbortController();
    const { promise, xhr } = startUpload({ signal: controller.signal });
    controller.abort();
    expect(xhr.aborted).toBe(true);
    const err = await rejectionOf(promise);
    expect(err).toBeInstanceOf(DOMException);
    if (err instanceof DOMException) expect(err.name).toBe('AbortError');
  });

  it('reports rounded percentages from computable upload.onprogress events only', async () => {
    const onProgress = vi.fn();
    const { promise, xhr } = startUpload({ onProgress });
    xhr.upload.onprogress?.({ lengthComputable: false, loaded: 1, total: 0 });
    expect(onProgress).not.toHaveBeenCalled();
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 333, total: 1000 });
    expect(onProgress).toHaveBeenCalledWith(33);
    xhr.respond(201);
    await promise;
  });
});

describe('createUploadGrant', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('POSTs the grant endpoint and returns the grant payload', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ grant_token: 'tok-abc', expires_in_seconds: 1800 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const grant = await createUploadGrant();

    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/admin/backups/upload-grant',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(grant).toEqual({ grant_token: 'tok-abc', expires_in_seconds: 1800 });
  });
});
