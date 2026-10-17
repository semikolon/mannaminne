-- mannaminne: put the split table in place of the old one (after split_mail.sql has finished).
-- Writes wait while the counts are compared; searches keep running until the rename itself,
-- which waits for running searches and then takes milliseconds. The old table stays as
-- `chunks_old` until it is dropped by hand after the new one has been checked.
-- Back out: ALTER TABLE chunks RENAME TO chunks_part; ALTER TABLE chunks_old RENAME TO chunks;
-- (and the code change that goes with it).
\set ON_ERROR_STOP on
\timing on
SET lock_timeout = '20s';
BEGIN;
LOCK TABLE chunks IN EXCLUSIVE MODE;
SELECT (SELECT count(*) FROM chunks) AS old_rows, (SELECT count(*) FROM chunks_part) AS new_rows,
       (SELECT count(embedding) FROM chunks) AS old_embedded,
       (SELECT count(embedding) FROM chunks_part) AS new_embedded \gset
SELECT :old_rows = :new_rows AND :old_embedded = :new_embedded AS counts_match \gset
\echo old :old_rows / :old_embedded embedded, new :new_rows / :new_embedded embedded
\if :counts_match
  ALTER TABLE chunks RENAME TO chunks_old;
  ALTER TABLE chunks_part RENAME TO chunks;
  COMMIT;
  \echo swapped
\else
  ROLLBACK;
  \echo counts differ: a write reached the old table during the build; copy the difference first
\endif
