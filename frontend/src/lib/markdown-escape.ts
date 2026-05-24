export function escapeMarkdownInline(text: string): string {
  return text.replace(/([*_[\]()\\`#>!])/g, '\\$1');
}
