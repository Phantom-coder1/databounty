-- Contamination screening against public benchmarks (services/contamination.ts).
--
-- The benchmark tables already exist, unused. This adds an import status to
-- each corpus version, so a version is screened against only once its import
-- has finished, and a GIN index on the shingle hashes, which is how screening
-- finds every benchmark problem sharing a shingle with an item.
--
-- Additive only: three columns (status has a default) and one index on a table
-- with no rows yet.
ALTER TABLE "benchmark_corpora"
  ADD COLUMN "status" TEXT NOT NULL DEFAULT 'ready',
  ADD COLUMN "error" TEXT,
  ADD COLUMN "imported_at" TIMESTAMP(3);

CREATE INDEX "benchmark_items_shingles_idx" ON "benchmark_items" USING GIN ("shingles");
