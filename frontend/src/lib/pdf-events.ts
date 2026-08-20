/**
 * The contract between the evidence anchors and the in-page PDF reader.
 *
 * The reader is lazy-loaded (it pulls in pdf.js), so the anchor side must not
 * import the component. It can safely import this module: it is a few bytes of
 * constant and type, and sharing it means a rename cannot silently break the
 * anchor the way two hand-typed strings could.
 */

export const PDF_GOTO_EVENT = 'jarvis:pdf-goto';

export interface PdfGotoDetail {
  page: number;
  quote: string | null;
}
