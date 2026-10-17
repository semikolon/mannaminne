-- mannaminne: build the email / everything-else split beside the live table.
-- Started 2026-10-17 on Fredrik's go. The live table `chunks` keeps serving every
-- search until a separate, short swap step renames the tables. Nothing here
-- changes a row in `chunks`; only its index names change, so the new ones can
-- take the canonical names.
\set ON_ERROR_STOP on
\timing on
SET statement_timeout = 0;
SET lock_timeout = '30s';
SET maintenance_work_mem = '2GB';
-- /dev/shm in the container is 64 MB, so a parallel HNSW build would fail.
SET max_parallel_maintenance_workers = 0;
SET synchronous_commit = off;

SELECT clock_timestamp() AS t, 'start' AS phase;
SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables WHERE relname = 'chunks';

-- Free the canonical names for the new table.
ALTER TABLE chunks RENAME CONSTRAINT chunks_pkey TO chunks_old_pkey;
ALTER INDEX chunks_tsv_gin     RENAME TO chunks_old_tsv_gin;
ALTER INDEX chunks_trgm_gin    RENAME TO chunks_old_trgm_gin;
ALTER INDEX chunks_kind        RENAME TO chunks_old_kind;
ALTER INDEX chunks_src         RENAME TO chunks_old_src;
ALTER INDEX chunks_emb_hnsw    RENAME TO chunks_old_emb_hnsw;
ALTER INDEX chunks_embed_queue RENAME TO chunks_old_embed_queue;
SET lock_timeout = 0;

CREATE TABLE chunks_part (
  id text NOT NULL,
  source_kind text NOT NULL,
  source_id text NOT NULL,
  chunk_idx integer NOT NULL,
  project text,
  title text,
  text text NOT NULL,
  created text,
  content_hash text NOT NULL,
  embedding vector(1024),
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, ((COALESCE(title, ''::text) || ' '::text) || text))) STORED,
  updated text,
  embed_priority smallint
) PARTITION BY LIST (source_kind);
CREATE TABLE chunks_mail PARTITION OF chunks_part FOR VALUES IN ('email');
CREATE TABLE chunks_rest PARTITION OF chunks_part DEFAULT;

-- 1. Everything but email: copy and index first, the part default searches will read.
SELECT clock_timestamp() AS t, 'copy rest' AS phase;
INSERT INTO chunks_rest (id, source_kind, source_id, chunk_idx, project, title, text, created,
                         content_hash, embedding, updated, embed_priority)
SELECT id, source_kind, source_id, chunk_idx, project, title, text, created,
       content_hash, embedding, updated, embed_priority
FROM chunks WHERE source_kind <> 'email';

SELECT clock_timestamp() AS t, 'index rest' AS phase;
CREATE INDEX chunks_rest_emb_hnsw ON chunks_rest USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops);
CREATE INDEX chunks_rest_tsv_gin  ON chunks_rest USING gin (tsv);
CREATE INDEX chunks_rest_trgm_gin ON chunks_rest USING gin (text gin_trgm_ops);
CREATE INDEX chunks_rest_kind     ON chunks_rest (source_kind);
CREATE INDEX chunks_rest_src      ON chunks_rest (source_id);
CREATE INDEX chunks_rest_embed_queue ON chunks_rest (embed_priority) WHERE embedding IS NULL;
VACUUM (ANALYZE) chunks_rest;

-- 2. Email.
SELECT clock_timestamp() AS t, 'copy mail' AS phase;
INSERT INTO chunks_mail (id, source_kind, source_id, chunk_idx, project, title, text, created,
                         content_hash, embedding, updated, embed_priority)
SELECT id, source_kind, source_id, chunk_idx, project, title, text, created,
       content_hash, embedding, updated, embed_priority
FROM chunks WHERE source_kind = 'email';

SELECT clock_timestamp() AS t, 'index mail' AS phase;
CREATE INDEX chunks_mail_tsv_gin  ON chunks_mail USING gin (tsv);
CREATE INDEX chunks_mail_trgm_gin ON chunks_mail USING gin (text gin_trgm_ops);
CREATE INDEX chunks_mail_kind     ON chunks_mail (source_kind);
CREATE INDEX chunks_mail_src      ON chunks_mail (source_id);
CREATE INDEX chunks_mail_embed_queue ON chunks_mail (embed_priority) WHERE embedding IS NULL;
CREATE INDEX chunks_mail_emb_hnsw ON chunks_mail USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops);
ANALYZE chunks_mail;

-- 3. Parent-level indexes: these attach the partition indexes above, no rebuild.
SELECT clock_timestamp() AS t, 'parent indexes' AS phase;
CREATE INDEX chunks_emb_hnsw ON chunks_part USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops);
CREATE INDEX chunks_tsv_gin  ON chunks_part USING gin (tsv);
CREATE INDEX chunks_trgm_gin ON chunks_part USING gin (text gin_trgm_ops);
CREATE INDEX chunks_kind     ON chunks_part (source_kind);
CREATE INDEX chunks_src      ON chunks_part (source_id);
CREATE INDEX chunks_embed_queue ON chunks_part (embed_priority) WHERE embedding IS NULL;
-- The key must hold the partition column. Every id carries its kind's prefix and
-- no email id is shared with another kind (checked 2026-10-17: 0 and 0), so
-- (id, source_kind) is as unique as id alone was.
ALTER TABLE chunks_part ADD CONSTRAINT chunks_pkey PRIMARY KEY (id, source_kind);

-- 4. Check against the live table.
SELECT clock_timestamp() AS t, 'verify' AS phase;
SELECT (SELECT count(*) FROM chunks) AS old_rows, (SELECT count(*) FROM chunks_part) AS new_rows,
       (SELECT count(embedding) FROM chunks) AS old_embedded, (SELECT count(embedding) FROM chunks_part) AS new_embedded;
SELECT c.relname, pg_size_pretty(pg_total_relation_size(c.oid)) FROM pg_class c
 WHERE c.relname IN ('chunks', 'chunks_mail', 'chunks_rest');
SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables WHERE relname = 'chunks';
SELECT clock_timestamp() AS t, 'done' AS phase;
