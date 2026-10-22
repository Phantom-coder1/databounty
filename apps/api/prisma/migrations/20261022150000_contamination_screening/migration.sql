-- Contamination screening against public benchmarks (services/contamination.ts).
--
-- The benchmark tables already exist, unused. This adds an import status to
-- each corpus version, so a version is screened against only once its import
-- has finished, and benchmark_shingles: the inverted index screening looks
-- problems up by, one row per shingle per problem, keyed (corpus, shingle).
--
-- Additive only: three columns (status has a default) on a table with no rows
-- yet, and one new table.
ALTER TABLE "benchmark_corpora"
  ADD COLUMN "status" TEXT NOT NULL DEFAULT 'ready',
  ADD COLUMN "error" TEXT,
  ADD COLUMN "imported_at" TIMESTAMP(3);

CREATE TABLE "benchmark_shingles" (
    "corpus_id" TEXT NOT NULL,
    "shingle" TEXT NOT NULL,
    "item_id" TEXT NOT NULL,

    CONSTRAINT "benchmark_shingles_pkey" PRIMARY KEY ("corpus_id","shingle","item_id")
);

ALTER TABLE "benchmark_shingles" ADD CONSTRAINT "benchmark_shingles_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "benchmark_corpora"("id") ON DELETE CASCADE ON UPDATE CASCADE;
