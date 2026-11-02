import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { checkNormalized, normalizers, type NormalizerName } from '../src/normalize.js';

/** CONTRACTS §4: the cases both services run (apps/worker/tests/test_normalize_ids.py loads the same file). */
interface Case {
  normalizer: NormalizerName;
  input: unknown;
  output?: string;
  error?: boolean;
  note?: string;
}
const file = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'contracts', 'normalize-cases.json');
const cases = (JSON.parse(readFileSync(file, 'utf8')) as { cases: Case[] }).cases;
const label = (c: Case) => `${c.normalizer}(${JSON.stringify(c.input)})${c.note ? ` — ${c.note}` : ''}`;

describe('shared normalizer cases (contracts/normalize-cases.json)', () => {
  it('cover every normalizer', () => {
    expect(cases.length).toBeGreaterThanOrEqual(60);
    expect(new Set(cases.map((c) => c.normalizer))).toEqual(new Set(Object.keys(normalizers)));
  });

  it.each(cases.filter((c) => !c.error).map((c) => [label(c), c] as const))('%s', (_, c) => {
    const norm = normalizers[c.normalizer];
    expect(norm(c.input)).toBe(c.output);
    expect(norm(c.output)).toBe(c.output); // a fixed point
    expect(checkNormalized(c.normalizer, c.output)).toBeNull(); // the writer accepts what the worker sends
    if (typeof c.input === 'string' && c.input !== c.output) {
      expect(checkNormalized(c.normalizer, c.input)).toContain(`expected ${JSON.stringify(c.output)}`);
    }
  });

  it.each(cases.filter((c) => c.error).map((c) => [label(c), c] as const))('%s is an error', (_, c) => {
    expect(normalizers[c.normalizer](c.input)).toBeNull();
    expect(checkNormalized(c.normalizer, c.input)).not.toBeNull();
  });
});

describe('normalizers', () => {
  it('are idempotent on random inputs', () => {
    let seed = 12;
    const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
    const alphabet = [...'wW.:/@?#[]-_ aZ09\t', 'www.', 'http://', 'https://', 'x.com/', '\u00a0', '\u00dc', '%', '\ufeff', 'q1'];
    const unstable: string[][] = [];
    for (let n = 0; n < 20000; n++) {
      let raw = '';
      const len = 1 + Math.floor(rnd() * 12);
      for (let i = 0; i < len; i++) raw += alphabet[Math.floor(rnd() * alphabet.length)];
      for (const [name, norm] of Object.entries(normalizers)) {
        const once = norm(raw);
        if (once !== null && norm(once) !== once) unstable.push([name, raw, once]);
      }
    }
    expect(unstable).toEqual([]);
  });

  it('checkNormalized requires a string', () => {
    expect(checkNormalized('exact', 41234567)).toBe('must be a string (normalizer exact)');
  });
});
