/**
 * Links from the product UI into the published documentation site (see
 * `site_url` and `docs_dir` in mkdocs.yml).
 */
const DOCS_BASE_URL = 'https://limitcycle-oss.github.io/jarvis-rd-assistant/';

/**
 * Builds a URL to a page on the published documentation site.
 *
 * `docPath` is the path of a Markdown file under `docs/` (e.g.
 * `manual/backup-and-restore.md`), matching the `docs_dir`-relative paths
 * used in mkdocs.yml's nav. mkdocs serves each page as a directory, so the
 * `.md` suffix is dropped and a trailing slash is added.
 *
 * A path that resolves against the local `docs/` tree can still 404 on the
 * published site, which lags behind by one documentation deployment.
 */
export function docsUrl(docPath: string): string {
  return `${DOCS_BASE_URL}${docPath.replace(/\.md$/, '')}/`;
}
