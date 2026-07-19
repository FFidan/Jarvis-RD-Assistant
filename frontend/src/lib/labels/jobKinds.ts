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
  'zotero.sync_from_zotero': 'Syncing Zotero',
  'zotero.sync_annotations': 'Syncing Highlights',
  'zotero.push_highlights': 'Exporting Highlights to Zotero',
};

export function kindLabel(kind: string): string {
  return JOB_KIND_LABELS[kind] ?? kind;
}
