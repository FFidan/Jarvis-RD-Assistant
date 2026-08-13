/**
 * Whether a paper's passage extraction should read as failed.
 *
 * The navigation rail and the actions panel both render this step and each used
 * to decide it independently, so a paper whose processing had failed appeared
 * failed in one rail and merely pending in the other after a reload. A run that
 * failed but still produced passages is not surfaced as a failure: the work the
 * reader cares about survived.
 */
export function isProcessingFailed(input: {
  processingFailed: boolean;
  hasChunks: boolean;
}): boolean {
  return input.processingFailed && !input.hasChunks;
}
