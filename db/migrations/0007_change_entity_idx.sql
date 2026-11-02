-- Change-feed lookups by entity (S10: timelines and wiki staleness).
--
-- The change table is indexed only by seq, (layer_id, seq), (worker_id, at) and at. Two reads
-- now ask "what happened to this entity?":
--   - timeline "first seen in Rebox": the first visible insert whose subject is in the entity's
--     merge group (order by seq limit 1);
--   - wiki staleness: the newest visible change whose subject or object is in the merge group
--     (max(seq)).
-- Without an index both walk the change pkey: 18,104 rows / ~33 ms for Patrick Collison on a
-- copy of s4_live (30k changes), and linear in the size of the feed. With (subject, seq) and
-- (object_id, seq) they are index range scans of a handful of rows. `change` has no RLS, and
-- the layer filter (layer_id = any(rebox_visible())) is applied to the few rows returned.
create index if not exists change_subject_seq_idx on change (subject, seq) where subject is not null;
create index if not exists change_object_seq_idx on change (object_id, seq) where object_id is not null;
