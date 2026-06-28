import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const css = readFileSync(path.resolve(__dirname, '../index.css'), 'utf8');

describe('index.css touch-device hover fallback', () => {
  it('does not contain the invalid escaped selector that drops the whole rule', () => {
    expect(css).not.toContain('.group:hover\\:opacity-100');
  });
  it('keeps the valid group-hover opacity fallback', () => {
    expect(css).toContain('.group-hover\\:opacity-100');
  });
});
