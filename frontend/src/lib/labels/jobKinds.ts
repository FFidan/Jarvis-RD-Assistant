export const JOB_KIND_LABELS: Record<string, string> = {
  'pulse.generate': 'Generating Pulse',
  'pulse.train_classifier': 'Training Pulse',
  'paper.process': 'Processing PDF',
  'paper.analyze': 'Analyzing Paper',
  'paper.download': 'Downloading PDF',
  'paper.summarize': 'Summarizing',
  'papers.batch_summarize': 'Batch Summarize',
  'papers.batch_process': 'Batch Process',
  'papers.process_library': 'Whole-library processing',
  'papers.scan_local': 'Scanning Local PDFs',
  'extraction.single': 'Extracting',
  'extraction.batch': 'Batch Extraction',
  'citations.batch_fetch': 'Fetching Citations',
  'contradictions.scan': 'Scanning Contradictions',
  'digest.weekly': 'Weekly Digest',
  'model.pull': 'Pulling Model',
  'card.generate': 'Generating Cards',
  'card.generate_batch': 'Batch Card Generation',
  'zotero.push': 'Pushing to Zotero',
  'zotero.resync': 'Resyncing Zotero',
  'zotero.poll': 'Syncing Zotero',
  'zotero.sync_from_zotero': 'Syncing Zotero',
  'zotero.sync_annotations': 'Syncing Highlights',
  'zotero.push_highlights': 'Exporting Highlights to Zotero',
};

/**
 * Overrides `JOB_KIND_LABELS` when a job is scoped to a single paper rather
 * than the whole library — e.g. `contradictions.scan` is submitted both by
 * the library-wide Consensus scan and a single paper's Contradictions scan,
 * and researchers need to tell the two apart at a glance.
 */
const PAPER_SCOPED_LABELS: Partial<Record<string, string>> = {
  'contradictions.scan': 'Scanning Paper Contradictions',
};

export function kindLabel(kind: string, options?: { paperScoped?: boolean }): string {
  if (options?.paperScoped) {
    const scopedLabel = PAPER_SCOPED_LABELS[kind];
    if (scopedLabel) return scopedLabel;
  }
  return JOB_KIND_LABELS[kind] ?? kind;
}
