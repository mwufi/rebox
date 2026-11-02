import { describe, expect, it } from 'vitest';
import { ADMIN, mint, nameClaim, ref, setupApp, uid } from './helpers.js';

const t = setupApp();

async function claimCount(layer: string) {
  const [r] = await t.owner<{ n: number }[]>`select count(*)::int as n from claim where layer_id = ${layer}`;
  return r!.n;
}

describe('POST /internal/claims', () => {
  it('requires the ingest token', async () => {
    const body = { layer: 'public/test', claims: [nameClaim(uid('company'), 'X')] };
    expect((await t.post('/internal/claims', body)).status).toBe(401);
    expect((await t.post('/internal/claims', body, { authorization: 'Bearer nope' })).status).toBe(401);
    expect((await t.post('/internal/claims', body, { authorization: `Bearer ${ADMIN}` })).status).toBe(403);
  });

  it('mints entities, auto-creates the public layer, and is idempotent', async () => {
    const layer = `public/w${Date.now().toString(36)}`;
    const person = uid('person', 'ada');
    const company = uid('company', 'acme');
    const claims = [
      nameClaim(person, 'Ada Test'),
      nameClaim(company, 'Acme Test'),
      { subject: ref(person), predicate: 'worked_at', object: ref(company), value: { title: 'CTO' },
        valid_from: '2019-01-01', valid_to: '2021-06-30', source_ref: `t:${person}:worked_at` },
      { subject: ref(company), predicate: 'domain', value: 'acme-test.example', source_ref: `t:${company}:domain` },
    ];
    const first = await t.write(layer, claims);
    expect(first.status).toBe(200);
    expect(first.body).toEqual({ written: 4, minted: 2 });
    const [l] = await t.owner`select scope::text, source, owner from layer where id = ${layer}`;
    expect(l).toEqual({ scope: 'public', source: layer.slice(7), owner: null });
    const [e] = await t.owner`select kind, home_layer from entity where id = ${person}`;
    expect(e).toEqual({ kind: 'person', home_layer: layer });

    const seqBefore = await t.owner`select max(seq)::int as s from change`;
    const second = await t.write(layer, claims);
    expect(second.status).toBe(200);
    expect(second.body).toEqual({ written: 0, minted: 0 });
    expect(await claimCount(layer)).toBe(4);
    // Unchanged re-sends emit no change rows.
    expect(await t.owner`select max(seq)::int as s from change`).toEqual(seqBefore);

    // Same key, new value: updated in place (latest write wins).
    const upd = await t.write(layer, [{ ...claims[0]!, subject: ref(person), value: 'Ada Renamed' }]);
    expect(upd.body).toEqual({ written: 1, minted: 0 });
    expect(await claimCount(layer)).toBe(4);
    const [n] = await t.owner`select value from claim where layer_id = ${layer} and subject = ${person} and predicate = 'name'`;
    expect(n!.value).toBe('Ada Renamed');
  });

  it('keeps the last duplicate within one batch', async () => {
    const id = uid('company');
    const r = await t.write('public/test', [nameClaim(id, 'First'), nameClaim(id, 'Second')]);
    expect(r.body).toEqual({ written: 1, minted: 1 });
    const [n] = await t.owner`select value from claim where subject = ${id}`;
    expect(n!.value).toBe('Second');
  });

  describe('validation (400, nothing written)', () => {
    const layer = 'public/test';
    const cases: [string, () => { layer?: string; claims: unknown[] }, RegExp][] = [
      ['edge without object', () => {
        const p = uid('person');
        return { claims: [nameClaim(p, 'P'), { subject: ref(p), predicate: 'worked_at', source_ref: 'r1' }] };
      }, /object is required/],
      ['wrong subject kind', () => ({
        claims: [{ subject: mint(uid('place')), predicate: 'yc_batch', value: 'W24', source_ref: 'r1' }],
      }), /subject kind place not allowed/],
      ['wrong object kind', () => {
        const p = uid('person');
        return { claims: [{ subject: mint(p), predicate: 'worked_at', object: mint(uid('place')), source_ref: 'r1' }] };
      }, /object kind place must be company/],
      ['unknown predicate', () => ({
        claims: [{ subject: mint(uid('person')), predicate: 'favorite_color', value: 'red', source_ref: 'r1' }],
      }), /unknown predicate/],
      ['private predicate into public/*', () => ({
        claims: [{ subject: mint(uid('person')), predicate: 'email', value: 'a@b.example', source_ref: 'r1' }],
      }), /email is private/],
      ['bad identity normalization (domain)', () => ({
        claims: [{ subject: mint(uid('company')), predicate: 'domain', value: 'https://www.Stripe.com/about', source_ref: 'r1' }],
      }), /not normalized \(domain\): expected "stripe.com"/],
      ['bad identity normalization (qid)', () => ({
        claims: [{ subject: mint(uid('company')), predicate: 'wikidata', value: 'q42', source_ref: 'r1' }],
      }), /not normalized \(qid\)/],
      ['attribute with object', () => {
        const c = uid('company');
        return { claims: [{ subject: mint(c), predicate: 'yc_batch', value: 'W24', object: mint(uid('company')), source_ref: 'r1' }] };
      }, /attribute: object is not allowed/],
      ['wrong value type', () => ({
        claims: [{ subject: mint(uid('company')), predicate: 'headcount', value: '12', source_ref: 'r1' }],
      }), /must be an integer/],
      ['unknown subject id', () => ({
        claims: [{ subject: ref('rbx:person:does-not-exist'), predicate: 'name', value: 'Ghost', source_ref: 'r1' }],
      }), /does not exist or is not visible/],
      ['impossible date', () => ({
        claims: [{ subject: mint(uid('person')), predicate: 'name', value: 'P', valid_from: '2021-02-30', source_ref: 'r1' }],
      }), /valid ISO date/],
      ['mint kind mismatch', () => ({
        claims: [{ subject: { mint: { id: uid('person'), kind: 'company' } }, predicate: 'name', value: 'P', source_ref: 'r1' }],
      }), /id kind does not match/],
      ['private layer that does not exist', () => ({
        layer: 'user/nobody/gmail',
        claims: [{ subject: mint(uid('person')), predicate: 'name', value: 'P', source_ref: 'r1' }],
      }), /does not exist/],
    ];

    for (const [label, make, message] of cases) {
      it(label, async () => {
        const body = make();
        // A valid claim alongside proves the whole batch is rejected.
        const good = nameClaim(uid('company'), 'Should not exist');
        const before = await t.owner<{ c: number; e: number; ch: number }[]>`
          select (select count(*)::int from claim) as c, (select count(*)::int from entity) as e,
                 (select count(*)::int from change) as ch`;
        const res = await t.write(body.layer ?? layer, [good, ...body.claims]);
        expect(res.status).toBe(400);
        expect(res.body.error).toBe('validation');
        const messages = res.body.details.map((d: { message: string }) => d.message).join(' | ');
        expect(messages).toMatch(message);
        expect(res.body.details.every((d: { index: number }) => d.index >= 1 || d.index === -1)).toBe(true);
        const after = await t.owner<{ c: number; e: number; ch: number }[]>`
          select (select count(*)::int from claim) as c, (select count(*)::int from entity) as e,
                 (select count(*)::int from change) as ch`;
        expect(after).toEqual(before);
      });
    }

    describe('database-level rejections name the failing claim (S16)', () => {
      async function counts() {
        const [r] = await t.owner<{ c: number; e: number; ch: number }[]>`
          select (select count(*)::int from claim) as c, (select count(*)::int from entity) as e,
                 (select count(*)::int from change) as ch`;
        return r;
      }
      // Incompressible text: too large for the claim_value_text_idx btree (54000), which no
      // request validation checks.
      const huge = () => Array.from({ length: 12_000 }, () => Math.random().toString(36).slice(2, 3)).join('');
      const batch = (bad: Record<number, (id: string) => Record<string, unknown>>) =>
        Array.from({ length: 50 }, (_, i) => {
          const id = uid('company', `db${i}`);
          return bad[i] ? bad[i]!(id) : nameClaim(id, `Fine ${i}`);
        });

      it('a value too large for the index -> 400 with that index, nothing written', async () => {
        const before = await counts();
        const claims = batch({ 37: (id) => ({ subject: mint(id), predicate: 'name', value: huge(), source_ref: `t:${id}` }) });
        const res = await t.write(layer, claims);
        expect(res.status).toBe(400);
        expect(res.body.details).toHaveLength(1);
        expect(res.body.details[0].index).toBe(37);
        expect(res.body.details[0].message).toMatch(/^rejected by database: .*index row/);
        expect(await counts()).toEqual(before);
        // Without it the batch goes through (what the worker client does with the index).
        const ok = await t.write(layer, claims.filter((_, i) => i !== 37));
        expect(ok.status).toBe(200);
        expect(ok.body).toEqual({ written: 49, minted: 49 });
      });

      it('several bad claims (NUL in text, oversized value) -> each index', async () => {
        const before = await counts();
        const claims = batch({
          3: (id) => ({ subject: mint(id), predicate: 'name', value: 'Bad\u0000Name', source_ref: `t:${id}` }),
          41: (id) => ({ subject: mint(id), predicate: 'name', value: huge(), source_ref: `t:${id}` }),
        });
        const res = await t.write(layer, claims);
        expect(res.status).toBe(400);
        expect(res.body.details.map((d: { index: number }) => d.index)).toEqual([3, 41]);
        expect(res.body.details[0].message).toMatch(/^rejected by database: .*Unicode/);
        expect(await counts()).toEqual(before);
      });

      it('a duplicate key reports the occurrence that would have been written', async () => {
        const id = uid('company', 'dup');
        const claims = [
          { subject: mint(id), predicate: 'name', value: 'First', source_ref: 'dup' },
          ...batch({}).slice(0, 5),
          { subject: mint(id), predicate: 'name', value: huge(), source_ref: 'dup' },
        ];
        const res = await t.write(layer, claims);
        expect(res.status).toBe(400);
        expect(res.body.details.map((d: { index: number }) => d.index)).toEqual([6]);
      });
    });

    it('rejects batches over 1000 and malformed bodies', async () => {
      const claims = Array.from({ length: 1001 }, (_, i) => nameClaim(`rbx:company:big-${i}`, `B${i}`));
      expect((await t.write(layer, claims)).status).toBe(400);
      expect((await t.write(layer, [])).status).toBe(400);
      const bad = await t.post('/internal/claims', { claims: [] }, { authorization: 'Bearer test-ingest-token' });
      expect(bad.status).toBe(400);
    });
  });

  it('writes to a private layer that exists and lets ingest reference public entities', async () => {
    await t.owner`insert into layer (id, scope, owner, source) values ('user/wtest/gmail', 'user', 'wtest', 'gmail') on conflict do nothing`;
    const pub = uid('person');
    expect((await t.write('public/test', [nameClaim(pub, 'Public Person')])).status).toBe(200);
    const priv = uid('person');
    const res = await t.write('user/wtest/gmail', [
      nameClaim(priv, 'Private Contact'),
      { subject: ref(priv), predicate: 'email', value: 'contact@example.com', source_ref: 'g:1' },
      { subject: ref(pub), predicate: 'knows', object: ref(priv), value: { strength: 0.5 }, source_ref: 'g:2' },
    ]);
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ written: 3, minted: 1 });
  });
});
