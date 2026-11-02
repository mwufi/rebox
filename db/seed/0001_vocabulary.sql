-- Seed: public layers and the v0 predicate vocabulary. Idempotent.
-- meta keys: label (UI), value_type ('text'|'url'|'date'|'int'|'number'|'bool'),
--            identity (true = hard key the resolver matches on), normalize (normalizer name),
--            filterable (usable in /v0/query attr filters), private (never written to public layers),
--            props (edge property types)

insert into layer (id, scope, owner, source) values
  ('public/manual',   'public', null, 'manual'),
  ('public/wikidata', 'public', null, 'wikidata'),
  ('public/hn',       'public', null, 'hn'),
  ('public/github',   'public', null, 'github'),
  ('public/x',        'public', null, 'x')
on conflict (id) do nothing;

insert into predicate (id, subject_kinds, object_kind, cardinality, meta) values
  -- attributes shared across kinds
  ('name',        '{person,company,launch,event,place}', null, 'one',  '{"label":"Name","value_type":"text","filterable":true}'),
  ('description', '{person,company,launch,event}',       null, 'one',  '{"label":"Description","value_type":"text"}'),
  ('website',     '{person,company,event}',              null, 'many', '{"label":"Website","value_type":"url","identity":true,"normalize":"url"}'),
  ('wikidata',    '{person,company,place}',              null, 'one',  '{"label":"Wikidata","value_type":"text","identity":true,"normalize":"qid"}'),
  ('x_handle',    '{person,company}',                    null, 'many', '{"label":"X","value_type":"text","identity":true,"normalize":"handle"}'),
  ('github',      '{person,company}',                    null, 'many', '{"label":"GitHub","value_type":"text","identity":true,"normalize":"handle"}'),
  -- people
  ('headline',    '{person}',  null, 'one',  '{"label":"Headline","value_type":"text"}'),
  ('location',    '{person}',  null, 'one',  '{"label":"Location","value_type":"text","filterable":true}'),
  ('hn_user',     '{person}',  null, 'one',  '{"label":"Hacker News","value_type":"text","identity":true,"normalize":"exact"}'),
  ('x_followers', '{person,company}', null, 'one', '{"label":"X followers","value_type":"int","filterable":true}'),
  ('email',       '{person}',  null, 'many', '{"label":"Email","value_type":"text","identity":true,"normalize":"email","private":true}'),
  -- companies
  ('domain',      '{company}', null, 'many', '{"label":"Domain","value_type":"text","identity":true,"normalize":"domain"}'),
  ('yc_batch',    '{company}', null, 'one',  '{"label":"YC batch","value_type":"text","filterable":true}'),
  ('founded_year','{company}', null, 'one',  '{"label":"Founded","value_type":"int","filterable":true}'),
  ('headcount',   '{company}', null, 'one',  '{"label":"Headcount","value_type":"int","filterable":true}'),
  -- launches and events
  ('url',         '{launch,event}', null, 'one', '{"label":"URL","value_type":"url","identity":true,"normalize":"url"}'),
  ('hn_story',    '{launch}',  null, 'one',  '{"label":"HN story","value_type":"text","identity":true,"normalize":"exact"}'),
  ('points',      '{launch}',  null, 'one',  '{"label":"Points","value_type":"int","filterable":true}'),
  -- X identity, profile and history (0005)
  ('x_id',        '{person,company}', null, 'one',  '{"label":"X id","value_type":"text","identity":true,"normalize":"exact"}'),
  ('avatar',      '{person,company}', null, 'one',  '{"label":"Avatar","value_type":"url"}'),
  ('milestone',   '{person,company,launch}', null, 'many', '{"label":"Milestone","value_type":"json","props":{"title":"text","kind":"text","url":"url"},"timeline":true}'),
  -- edges
  ('follows',     '{person,company}', '*', 'many', '{"label":"Follows","filterable":true}'),
  ('acquired',    '{company}', 'company', 'many', '{"label":"Acquired","filterable":true,"timeline":true}'),
  ('worked_at',   '{person}',  'company', 'many', '{"label":"Worked at","props":{"title":"text"},"filterable":true}'),
  ('founded',     '{person}',  'company', 'many', '{"label":"Founded","filterable":true}'),
  ('ceo_of',      '{person}',  'company', 'many', '{"label":"CEO of","filterable":true}'),
  ('invested_in', '{person,company}', 'company', 'many', '{"label":"Invested in","filterable":true}'),
  ('posted',      '{person}',  'launch',  'many', '{"label":"Posted","filterable":true}'),
  ('launched',    '{company}', 'launch',  'many', '{"label":"Launched","filterable":true}'),
  ('based_in',    '{person,company}', 'place', 'many', '{"label":"Based in","filterable":true}'),
  ('attended',    '{person}',  'event',   'many', '{"label":"Attended","props":{"role":"text"},"filterable":true}'),
  ('knows',       '{person}',  'person',  'many', '{"label":"Knows","props":{"strength":"number"},"private":true}'),
  ('emailed',     '{person}',  'person',  'many', '{"label":"Emailed","props":{"threads":"int","last_at":"date"},"private":true}')
on conflict (id) do update set subject_kinds = excluded.subject_kinds, object_kind = excluded.object_kind,
  cardinality = excluded.cardinality, meta = excluded.meta;
