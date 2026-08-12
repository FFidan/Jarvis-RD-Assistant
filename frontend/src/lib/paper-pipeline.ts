/**
 * Derive a paper's pipeline status from persisted state.
 *
 * Both the navigation rail and the actions panel render this. They previously
 * derived it separately, so a paper whose processing had failed appeared failed
 * in one rail and merely pending in the other after a reload.
 */
export function derivePipelineStatus(input: {
  pdfDownloaded: boolean;
  hasChunks: boolean;
  hasSummary: boolean;
  processingFailed: boolean;
}): 'new' | 'downloaded' | 'processed' | 'summarized' | 'failed' {
  const { pdfDownloaded, hasChunks, hasSummary, processingFailed } = input;
  if (processingFailed && !hasChunks) return 'failed';
  if (hasSummary) return 'summarized';
  if (hasChunks) return 'processed';
  if (pdfDownloaded) return 'downloaded';
  return 'new';
}
