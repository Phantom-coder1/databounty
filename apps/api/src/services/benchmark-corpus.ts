// SPDX-License-Identifier: Apache-2.0

import type { Prisma } from "@prisma/client";
import { prisma } from "../lib/prisma.js";
import { writeAuditLog } from "../lib/audit-log.js";
import { benchmarkFingerprint } from "./contamination.js";
import { enqueueJob } from "./jobs.js";

/**
 * The public benchmarks that contamination screening compares items against,
 * and how a corpus of them is imported.
 *
 * Rows come from the Hugging Face datasets-server API. Only fingerprints are
 * stored (see services/contamination.ts): hashes of 8-token shingles, never a
 * benchmark's text, so the corpus holds no copy of anyone's dataset.
 *
 * Each import builds a NEW corpus version, and screening switches to it only
 * once every row is in. A failed import leaves the version already in force
 * untouched.
 */

export const CORPUS_NAME = "public-benchmarks";
const DATASETS_SERVER = "https://datasets-server.huggingface.co";
const PAGE_SIZE = 100;
const INSERT_BATCH = 200;
const FETCH_TIMEOUT_MS = 30_000;
const FETCH_ATTEMPTS = 3;
/**
 * An import must end inside its job lease (HEAVY_LEASE_MS in worker.ts, 10
 * minutes). Past the lease the queue hands the job to another worker while
 * this one may still be writing, so an import that runs long fails instead.
 */
const IMPORT_BUDGET_MS = 8 * 60_000;

type Row = Record<string, unknown>;

interface BenchmarkPart {
  /** Shown after the problem's label, e.g. "problem" or "patch". */
  suffix: string;
  fields: string[];
}

export interface BenchmarkSource {
  id: string;
  title: string;
  dataset: string;
  config: string;
  splits: string[];
  /**
   * Parts fingerprinted separately. A long part (a SWE-bench patch) would
   * otherwise dilute a copied short one (its issue text); a part too short to
   * fingerprint on its own (an MBPP problem sentence) is combined with its code.
   */
  parts: BenchmarkPart[];
  label(row: Row, rowIndex: number, split: string): string;
}

export const BENCHMARK_SOURCES: BenchmarkSource[] = [
  {
    id: "humaneval",
    title: "HumanEval",
    dataset: "openai/openai_humaneval",
    config: "openai_humaneval",
    splits: ["test"],
    parts: [
      { suffix: "problem", fields: ["prompt"] },
      { suffix: "solution", fields: ["canonical_solution"] },
    ],
    label: (row) => String(row.task_id ?? ""),
  },
  {
    id: "mbpp",
    title: "MBPP",
    dataset: "google-research-datasets/mbpp",
    config: "full",
    splits: ["train", "test", "validation", "prompt"],
    parts: [{ suffix: "problem and solution", fields: ["text", "code"] }],
    label: (row) => `MBPP/${String(row.task_id ?? "")}`,
  },
  {
    id: "swe-bench-lite",
    title: "SWE-bench Lite",
    dataset: "princeton-nlp/SWE-bench_Lite",
    config: "default",
    splits: ["dev", "test"],
    parts: [
      { suffix: "issue", fields: ["problem_statement"] },
      { suffix: "patch", fields: ["patch"] },
    ],
    label: (row) => String(row.instance_id ?? ""),
  },
  {
    id: "gsm8k",
    title: "GSM8K",
    dataset: "openai/gsm8k",
    config: "main",
    splits: ["test"],
    parts: [
      { suffix: "question", fields: ["question"] },
      { suffix: "solution", fields: ["answer"] },
    ],
    label: (_row, rowIndex, split) => `GSM8K/${split}/${rowIndex}`,
  },
  {
    id: "ds-1000",
    title: "DS-1000",
    dataset: "xlangai/DS-1000",
    config: "default",
    splits: ["test"],
    parts: [
      { suffix: "problem", fields: ["prompt"] },
      { suffix: "solution", fields: ["reference_code"] },
    ],
    label: (row, rowIndex) => {
      const metadata = row.metadata as { problem_id?: unknown } | undefined;
      return `DS-1000/${String(metadata?.problem_id ?? rowIndex)}`;
    },
  },
];

export class BenchmarkImportError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function sourceById(id: string): BenchmarkSource | undefined {
  return BENCHMARK_SOURCES.find((source) => source.id === id);
}

/** Start importing a new corpus version from the given sources (all by default). */
export async function startBenchmarkImport(
  sourceIds: string[] | undefined,
  actor: { userId: string; ip?: string; userAgent?: string | null; requestId?: string },
) {
  const ids = sourceIds?.length ? [...new Set(sourceIds)] : BENCHMARK_SOURCES.map((source) => source.id);
  const unknown = ids.filter((id) => !sourceById(id));
  if (unknown.length > 0) {
    throw new BenchmarkImportError(`Unknown benchmark source: ${unknown.join(", ")}.`, 400);
  }
  if (await prisma.benchmarkCorpus.findFirst({ where: { status: "importing" }, select: { id: true } })) {
    throw new BenchmarkImportError("An import is already running. Wait for it to finish.", 409);
  }
  const latest = await prisma.benchmarkCorpus.findFirst({
    where: { name: CORPUS_NAME },
    orderBy: { version: "desc" },
    select: { version: true },
  });
  return prisma.$transaction(async (tx) => {
    const corpus = await tx.benchmarkCorpus.create({
      data: { name: CORPUS_NAME, version: (latest?.version ?? 0) + 1, source: ids.join("+"), status: "importing", active: false },
    });
    await enqueueJob(
      "benchmark.corpus_import",
      { corpusId: corpus.id },
      { idempotencyKey: `benchmark.corpus_import:${corpus.id}`, maxAttempts: 1, tx },
    );
    await writeAuditLog(tx, {
      actorUserId: actor.userId,
      action: "benchmark_corpus.import_started",
      targetType: "benchmark_corpus",
      targetId: corpus.id,
      after: { version: corpus.version, sources: ids },
      ip: actor.ip,
      userAgent: actor.userAgent ?? null,
      requestId: actor.requestId,
    });
    return corpus;
  });
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(asText).join("\n");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

async function getJson(url: string, deadline: number): Promise<Record<string, unknown>> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= FETCH_ATTEMPTS; attempt += 1) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error(`The import ran past its ${IMPORT_BUDGET_MS / 60_000}-minute budget.`);
    try {
      const res = await fetch(url, {
        headers: { Accept: "application/json", "User-Agent": "databounty-contamination-corpus/1.0" },
        signal: AbortSignal.timeout(Math.min(FETCH_TIMEOUT_MS, remaining)),
      });
      if (res.ok) return (await res.json()) as Record<string, unknown>;
      lastError = new Error(`HTTP ${res.status}`);
      // Rate limiting and server errors are worth another try; anything else is not.
      if (res.status !== 429 && res.status < 500) break;
    } catch (error) {
      lastError = error;
    }
    if (attempt < FETCH_ATTEMPTS) await new Promise((resolve) => setTimeout(resolve, 1_000 * attempt));
  }
  throw new Error(`Couldn't read ${url}: ${lastError instanceof Error ? lastError.message : String(lastError)}`);
}

async function* benchmarkRows(
  source: BenchmarkSource,
  split: string,
  deadline: number,
): AsyncGenerator<{ row: Row; rowIndex: number }> {
  let offset = 0;
  let total = Number.POSITIVE_INFINITY;
  while (offset < total) {
    const query = new URLSearchParams({
      dataset: source.dataset,
      config: source.config,
      split,
      offset: String(offset),
      length: String(PAGE_SIZE),
    });
    const page = await getJson(`${DATASETS_SERVER}/rows?${query}`, deadline);
    total = Number(page.num_rows_total ?? 0);
    const rows = Array.isArray(page.rows) ? (page.rows as { row: Row; row_idx: number }[]) : [];
    if (rows.length === 0) break;
    for (const entry of rows) yield { row: entry.row, rowIndex: entry.row_idx };
    offset += rows.length;
  }
}

/**
 * Build a corpus version's inverted index (BenchmarkShingle) from its
 * problems' shingle arrays, in one statement: one row per shingle per problem.
 * Screening looks problems up through it (services/contamination.ts).
 */
export async function indexBenchmarkShingles(corpusId: string): Promise<number> {
  return prisma.$executeRaw`
    INSERT INTO benchmark_shingles (corpus_id, shingle, item_id)
    SELECT corpus_id, unnest(shingles), id FROM benchmark_items WHERE corpus_id = ${corpusId}`;
}

async function clearCorpusRows(corpusId: string) {
  await prisma.benchmarkShingle.deleteMany({ where: { corpusId } });
  await prisma.benchmarkItem.deleteMany({ where: { corpusId } });
}

/**
 * `benchmark.corpus_import` handler: fingerprint every row of every source,
 * then make this version the one screening uses. On any failure the version is
 * marked failed, its partial rows are removed, and the active version stays.
 */
export async function runBenchmarkCorpusImport(corpusId: string): Promise<void> {
  const corpus = await prisma.benchmarkCorpus.findUnique({ where: { id: corpusId } });
  if (!corpus || corpus.status !== "importing") return;
  const sources = corpus.source.split("+").map(sourceById).filter((source): source is BenchmarkSource => Boolean(source));

  try {
    const deadline = Date.now() + IMPORT_BUDGET_MS;
    // A retried run starts clean rather than doubling rows.
    await clearCorpusRows(corpusId);
    let itemCount = 0;
    let batch: Prisma.BenchmarkItemCreateManyInput[] = [];
    const flush = async () => {
      if (batch.length === 0) return;
      await prisma.benchmarkItem.createMany({ data: batch });
      itemCount += batch.length;
      batch = [];
    };

    for (const source of sources) {
      for (const split of source.splits) {
        for await (const { row, rowIndex } of benchmarkRows(source, split, deadline)) {
          const label = source.label(row, rowIndex, split);
          for (const part of source.parts) {
            const fingerprint = benchmarkFingerprint(part.fields.map((field) => asText(row[field])).join("\n"));
            if (!fingerprint) continue;
            batch.push({ corpusId, ...fingerprint, label: `${source.dataset} ${label} (${part.suffix})` });
            if (batch.length >= INSERT_BATCH) await flush();
          }
        }
      }
    }
    await flush();
    if (itemCount === 0) throw new Error("No benchmark problems could be fingerprinted.");
    await indexBenchmarkShingles(corpusId);

    await prisma.$transaction([
      prisma.benchmarkCorpus.updateMany({ where: { active: true, id: { not: corpusId } }, data: { active: false } }),
      prisma.benchmarkCorpus.update({
        where: { id: corpusId },
        data: { active: true, status: "ready", itemCount, importedAt: new Date(), error: null },
      }),
    ]);
  } catch (error) {
    await clearCorpusRows(corpusId);
    await prisma.benchmarkCorpus.update({
      where: { id: corpusId },
      data: { status: "failed", error: (error instanceof Error ? error.message : String(error)).slice(0, 500) },
    });
  }
}
