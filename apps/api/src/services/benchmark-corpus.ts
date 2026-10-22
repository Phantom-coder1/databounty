// SPDX-License-Identifier: Apache-2.0

import { createHash } from "node:crypto";
import { JobStatus, type Prisma } from "@prisma/client";
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
 * untouched. The import runs as a chain of short jobs (see
 * runBenchmarkCorpusImport), so it never holds the job queue for long.
 */

export const CORPUS_NAME = "public-benchmarks";
const DATASETS_SERVER = "https://datasets-server.huggingface.co";
const PAGE_SIZE = 100;
const INSERT_BATCH = 500;
const FETCH_TIMEOUT_MS = 30_000;
const FETCH_ATTEMPTS = 3;
const RETRY_PAUSE_MS = 1_000;
/**
 * Each job of an import reads for about this long, or this many pages, then
 * queues the rest. The worker runs one job at a time, so one long import would
 * hold back every validation queued behind it. Well inside the job lease
 * (HEAVY_LEASE_MS in worker.ts, 10 minutes).
 */
const CHUNK_BUDGET_MS = 45_000;
const PAGES_PER_CHUNK = 40;
/**
 * Every statement must finish inside the database's statement timeout
 * (DB_STATEMENT_TIMEOUT_MS, 30 s by default), and SWE-bench alone yields about a
 * million shingles. So a part also stops after about this many shingles, and
 * saves its index rows this many at a time.
 */
const PART_SHINGLE_BUDGET = 250_000;
const SHINGLE_INSERT_BATCH = 50_000;
/** A short pause between pages keeps the import under the datasets-server's rate limit. */
const PAGE_PAUSE_MS = 250;
/**
 * When Hugging Face turns the import away, the next job waits before trying
 * again: its Retry-After, or 30 s doubling up to 10 minutes. After this many
 * refusals in a row with no page getting through, the import gives up.
 */
const FIRST_DEFERRAL_MS = 30_000;
const MIN_DEFERRAL_MS = 5_000;
const MAX_DEFERRAL_MS = 10 * 60_000;
const MAX_DEFERRALS = 8;

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
  /** Dataset configs to read, each with every split below. HumanEvalPack has one per language. */
  configs: string[];
  splits: string[];
  /** Rows per page, for datasets whose rows are too large for the default. */
  pageSize?: number;
  /**
   * Parts fingerprinted separately. A long part (a SWE-bench patch) would
   * otherwise dilute a copied short one (its issue text); a part too short to
   * fingerprint on its own (an MBPP problem sentence) is combined with its code.
   */
  parts: BenchmarkPart[];
  label(row: Row, rowIndex: number, split: string, config: string): string;
}

export const BENCHMARK_SOURCES: BenchmarkSource[] = [
  {
    id: "humaneval",
    title: "HumanEval",
    dataset: "openai/openai_humaneval",
    configs: ["openai_humaneval"],
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
    configs: ["full"],
    splits: ["train", "test", "validation", "prompt"],
    parts: [{ suffix: "problem and solution", fields: ["text", "code"] }],
    label: (row) => `MBPP/${String(row.task_id ?? "")}`,
  },
  {
    id: "swe-bench",
    title: "SWE-bench",
    dataset: "princeton-nlp/SWE-bench",
    configs: ["default"],
    // The whole evaluation set: SWE-bench Lite and SWE-bench Verified are
    // both drawn from its test split.
    splits: ["test", "dev"],
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
    configs: ["main"],
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
    configs: ["default"],
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
  {
    id: "bigcodebench",
    title: "BigCodeBench",
    dataset: "bigcode/bigcodebench",
    configs: ["default"],
    // Each split is a release of the same 1,140 tasks; the latest is enough.
    splits: ["v0.1.4"],
    parts: [
      { suffix: "problem", fields: ["complete_prompt"] },
      { suffix: "instruction", fields: ["instruct_prompt"] },
      { suffix: "solution", fields: ["code_prompt", "canonical_solution"] },
    ],
    label: (row) => String(row.task_id ?? ""),
  },
  {
    id: "humanevalpack",
    title: "HumanEvalPack",
    dataset: "bigcode/humanevalpack",
    // HumanEval in six languages, with the buggy variants HumanEvalFix uses.
    configs: ["python", "js", "java", "go", "cpp", "rust"],
    splits: ["test"],
    parts: [
      { suffix: "problem", fields: ["prompt"] },
      { suffix: "solution", fields: ["declaration", "canonical_solution"] },
      { suffix: "buggy solution", fields: ["declaration", "buggy_solution"] },
    ],
    label: (row) => String(row.task_id ?? ""),
  },
  {
    id: "cruxeval",
    title: "CRUXEval",
    dataset: "cruxeval-org/cruxeval",
    configs: ["default"],
    splits: ["test"],
    // The functions are short, so each is fingerprinted with its example.
    parts: [{ suffix: "function and example", fields: ["code", "input", "output"] }],
    label: (row) => `CRUXEval/${String(row.id ?? "")}`,
  },
  {
    id: "code-contests",
    title: "CodeContests",
    dataset: "deepmind/code_contests",
    configs: ["default"],
    splits: ["test", "valid"],
    // A row also carries every contestant's solutions, hundreds of KB, so
    // pages stay small. Only the problem statement is fingerprinted.
    pageSize: 10,
    parts: [{ suffix: "problem", fields: ["description"] }],
    label: (row) => String(row.name ?? ""),
  },
  {
    id: "spider",
    title: "Spider",
    dataset: "xlangai/spider",
    configs: ["spider"],
    splits: ["validation"],
    // Questions and queries are short, so each pair is fingerprinted
    // together. Pairs under about 17 words are still too short to index.
    parts: [{ suffix: "question and query", fields: ["question", "query"] }],
    label: (row, rowIndex) => `Spider/${String(row.db_id ?? "")}/${rowIndex}`,
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
  const running = await prisma.benchmarkCorpus.findFirst({ where: { status: "importing" }, select: { id: true } });
  if (running) {
    // An import whose jobs are all gone (its process died between parts) would
    // otherwise block every import after it. Only one with a job still queued,
    // running or due a retry is really running.
    const live = await prisma.jobQueue.findFirst({
      where: {
        type: "benchmark.corpus_import",
        idempotencyKey: { startsWith: `benchmark.corpus_import:${running.id}` },
        status: { in: [JobStatus.pending, JobStatus.processing, JobStatus.failed] },
      },
      select: { id: true },
    });
    if (live) throw new BenchmarkImportError("An import is already running. Wait for it to finish.", 409);
    await failImport(running.id, new Error("The import stopped before it finished."));
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
      { idempotencyKey: `benchmark.corpus_import:${corpus.id}`, maxAttempts: 3, tx },
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

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Hugging Face asked the import to slow down (429) or didn't answer (5xx, a
 * timeout). Worth waiting for: the next part of the import waits and retries.
 */
class HuggingFaceBusy extends Error {
  constructor(
    message: string,
    readonly retryAfterMs: number | null,
  ) {
    super(message);
  }
}

function retryAfterMs(header: string | null): number | null {
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1_000);
  const at = Date.parse(header);
  return Number.isNaN(at) ? null : Math.max(0, at - Date.now());
}

function requestHeaders(): Record<string, string> {
  const headers: Record<string, string> = { Accept: "application/json", "User-Agent": "databounty-contamination-corpus/1.0" };
  // Optional: the datasets are public, but a token raises the datasets-server's rate limit.
  const token = process.env.HUGGINGFACE_API_TOKEN;
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function getJson(url: string, limits: ImportLimits): Promise<Record<string, unknown>> {
  let lastError = "no answer";
  for (let attempt = 1; attempt <= FETCH_ATTEMPTS; attempt += 1) {
    let res: Response | null = null;
    try {
      res = await fetch(url, { headers: requestHeaders(), signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
      if (res.ok) return (await res.json()) as Record<string, unknown>;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    if (res && !res.ok) {
      // Rate limited: this job doesn't sit waiting. The next part of the import does.
      if (res.status === 429) {
        throw new HuggingFaceBusy("Hugging Face rate-limited the import (HTTP 429)", retryAfterMs(res.headers.get("retry-after")));
      }
      // Anything else under 500 (a missing config or split) won't fix itself.
      if (res.status < 500) throw new Error(`Couldn't read ${url}: HTTP ${res.status}`);
      lastError = `HTTP ${res.status}`;
    }
    if (attempt < FETCH_ATTEMPTS) await sleep(limits.retryPauseMs * attempt);
  }
  throw new HuggingFaceBusy(`Hugging Face didn't answer the import (${lastError})`, null);
}

async function fetchPage(
  source: BenchmarkSource,
  config: string,
  split: string,
  offset: number,
  limits: ImportLimits,
): Promise<{ rows: { row: Row; rowIndex: number }[]; total: number }> {
  const query = new URLSearchParams({
    dataset: source.dataset,
    config,
    split,
    offset: String(offset),
    length: String(source.pageSize ?? PAGE_SIZE),
  });
  const page = await getJson(`${DATASETS_SERVER}/rows?${query}`, limits);
  const rows = Array.isArray(page.rows) ? (page.rows as { row: Row; row_idx: number }[]) : [];
  return { rows: rows.map((entry) => ({ row: entry.row, rowIndex: entry.row_idx })), total: Number(page.num_rows_total ?? 0) };
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

/** Shingles are lowercase hex, so these ranges cover them all, in sixteen slices. */
const HEX = "0123456789abcdef";

async function clearCorpusRows(corpusId: string) {
  // A slice at a time (by a shingle's first hex digit, along the primary key):
  // one statement for a whole corpus could outlast the statement timeout.
  for (let i = 0; i < HEX.length; i += 1) {
    const from = HEX[i]!;
    const to = HEX[i + 1];
    if (to) {
      await prisma.$executeRaw`DELETE FROM benchmark_shingles WHERE corpus_id = ${corpusId} AND shingle >= ${from} AND shingle < ${to}`;
    } else {
      await prisma.$executeRaw`DELETE FROM benchmark_shingles WHERE corpus_id = ${corpusId} AND shingle >= ${from}`;
    }
  }
  await prisma.benchmarkShingle.deleteMany({ where: { corpusId } });
  await prisma.benchmarkItem.deleteMany({ where: { corpusId } });
}

async function failImport(corpusId: string, error: unknown) {
  await clearCorpusRows(corpusId);
  await prisma.benchmarkCorpus.update({
    where: { id: corpusId },
    data: { status: "failed", itemCount: 0, error: (error instanceof Error ? error.message : String(error)).slice(0, 500) },
  });
}

/** Where an import has got to: the next page to read. */
export interface ImportCursor {
  source: number;
  config: number;
  split: number;
  offset: number;
}

/** One `benchmark.corpus_import` job: the first has no cursor. */
export interface CorpusImportJob {
  corpusId: string;
  cursor?: ImportCursor;
  /** How many times in a row Hugging Face has turned the import away without progress. */
  deferrals?: number;
}

export interface ImportLimits {
  /** A job stops reading after this long and queues the rest. */
  chunkBudgetMs: number;
  /** ...or after this many pages. */
  pagesPerChunk: number;
  /** ...or once it has this many shingles to save. */
  shingleBudget: number;
  pagePauseMs: number;
  retryPauseMs: number;
}

const DEFAULT_LIMITS: ImportLimits = {
  chunkBudgetMs: CHUNK_BUDGET_MS,
  pagesPerChunk: PAGES_PER_CHUNK,
  shingleBudget: PART_SHINGLE_BUDGET,
  pagePauseMs: PAGE_PAUSE_MS,
  retryPauseMs: RETRY_PAUSE_MS,
};

/** The cursor at the start of the next split, config or source, or null after the last. */
function nextPart(sources: BenchmarkSource[], cursor: ImportCursor): ImportCursor | null {
  const source = sources[cursor.source]!;
  if (cursor.split + 1 < source.splits.length) return { ...cursor, split: cursor.split + 1, offset: 0 };
  if (cursor.config + 1 < source.configs.length) return { ...cursor, config: cursor.config + 1, split: 0, offset: 0 };
  if (cursor.source + 1 < sources.length) return { source: cursor.source + 1, config: 0, split: 0, offset: 0 };
  return null;
}

/**
 * A problem's row id, the same every time the same row is read for the same
 * version. A part that runs twice (its worker died after saving) writes the
 * same ids again, which the database skips instead of doubling the corpus.
 */
function benchmarkItemId(corpusId: string, source: BenchmarkSource, config: string, split: string, rowIndex: number, part: number): string {
  const key = [corpusId, source.id, config, split, rowIndex, part].join("\u0000");
  return `bmi_${createHash("sha256").update(key).digest("hex").slice(0, 32)}`;
}

/**
 * Save one part of an import, and in the same transaction either queue the
 * next part or, after the last, make this version the one screening uses. The
 * rows and the next step commit together, so a part that dies before saving
 * leaves nothing behind and simply runs again.
 */
type ItemRow = { id: string; corpusId: string; canonicalHash: string; shingles: string[]; label: string };

async function savePart(
  corpusId: string,
  items: ItemRow[],
  next: ImportCursor | null,
  deferrals: number,
  runAfter: Date | undefined,
) {
  await prisma.$transaction(
    async (tx) => {
      let added = 0;
      for (let start = 0; start < items.length; start += INSERT_BATCH) {
        added += (await tx.benchmarkItem.createMany({ data: items.slice(start, start + INSERT_BATCH), skipDuplicates: true })).count;
      }
      if (items.length > 0) {
        // The index rows, at most SHINGLE_INSERT_BATCH per statement.
        let ids: string[] = [];
        let size = 0;
        const index = async () => {
          if (ids.length === 0) return;
          await tx.$executeRaw`
            INSERT INTO benchmark_shingles (corpus_id, shingle, item_id)
            SELECT corpus_id, unnest(shingles), id FROM benchmark_items WHERE id = ANY(${ids}::text[])
            ON CONFLICT DO NOTHING`;
          ids = [];
          size = 0;
        };
        for (const item of items) {
          if (size + item.shingles.length > SHINGLE_INSERT_BATCH) await index();
          ids.push(item.id);
          size += item.shingles.length;
        }
        await index();
        // Progress the admin console shows while the import runs.
        await tx.benchmarkCorpus.update({ where: { id: corpusId }, data: { itemCount: { increment: added } } });
      }
      if (next) {
        const key = `benchmark.corpus_import:${corpusId}:${next.source}.${next.config}.${next.split}.${next.offset}:${deferrals}`;
        // Queued once. Queueing an existing key re-arms it, so a part that runs
        // again must not queue a step that has already run: the rest of the
        // import would run twice.
        if (!(await tx.jobQueue.findUnique({ where: { idempotencyKey: key }, select: { id: true } }))) {
          await enqueueJob("benchmark.corpus_import", { corpusId, cursor: next, deferrals }, { idempotencyKey: key, maxAttempts: 3, runAfter, tx });
        }
        return;
      }
      const { itemCount } = await tx.benchmarkCorpus.findUniqueOrThrow({ where: { id: corpusId }, select: { itemCount: true } });
      if (itemCount === 0) throw new Error("No benchmark problems could be fingerprinted.");
      await tx.benchmarkCorpus.updateMany({ where: { active: true, id: { not: corpusId } }, data: { active: false } });
      await tx.benchmarkCorpus.update({ where: { id: corpusId }, data: { active: true, status: "ready", importedAt: new Date(), error: null } });
    },
    { maxWait: 15_000, timeout: 120_000 },
  );
}

/**
 * `benchmark.corpus_import` handler: read and fingerprint the next pages of the
 * import, for at most `chunkBudgetMs`, then queue the rest as a new job. The
 * worker runs one job at a time, so short parts let validations run in between
 * instead of waiting for the whole import. When Hugging Face turns the import
 * away, the next part waits (its Retry-After, or 30 s doubling to 10 minutes)
 * and resumes at the same page; after MAX_DEFERRALS refusals in a row the
 * import gives up. On failure the version is marked failed, its rows are
 * removed, and the version already in force stays.
 */
export async function runBenchmarkCorpusImport(job: CorpusImportJob, limits: ImportLimits = DEFAULT_LIMITS): Promise<void> {
  const corpus = await prisma.benchmarkCorpus.findUnique({ where: { id: job.corpusId } });
  if (!corpus || corpus.status !== "importing") return;
  const sources = corpus.source.split("+").map(sourceById).filter((source): source is BenchmarkSource => Boolean(source));

  try {
    let cursor: ImportCursor | null = job.cursor ?? (sources.length > 0 ? { source: 0, config: 0, split: 0, offset: 0 } : null);
    const deadline = Date.now() + limits.chunkBudgetMs;
    const items: ItemRow[] = [];
    let shingles = 0;
    let pages = 0;
    let busy: HuggingFaceBusy | null = null;

    while (cursor && pages < limits.pagesPerChunk && shingles < limits.shingleBudget && (pages === 0 || Date.now() < deadline)) {
      const source = sources[cursor.source]!;
      const config = source.configs[cursor.config]!;
      const split = source.splits[cursor.split]!;
      let page: Awaited<ReturnType<typeof fetchPage>>;
      try {
        page = await fetchPage(source, config, split, cursor.offset, limits);
      } catch (error) {
        if (error instanceof HuggingFaceBusy) {
          busy = error;
          break;
        }
        throw error;
      }
      pages += 1;
      for (const { row, rowIndex } of page.rows) {
        const label = source.label(row, rowIndex, split, config);
        source.parts.forEach((part, partIndex) => {
          const fingerprint = benchmarkFingerprint(part.fields.map((field) => asText(row[field])).join("\n"));
          if (!fingerprint) return;
          shingles += fingerprint.shingles.length;
          items.push({
            id: benchmarkItemId(corpus.id, source, config, split, rowIndex, partIndex),
            corpusId: corpus.id,
            ...fingerprint,
            label: `${source.dataset} ${label} (${part.suffix})`,
          });
        });
      }
      const offset = cursor.offset + page.rows.length;
      cursor = page.rows.length === 0 || offset >= page.total ? nextPart(sources, cursor) : { ...cursor, offset };
      if (cursor && pages < limits.pagesPerChunk && shingles < limits.shingleBudget) await sleep(limits.pagePauseMs);
    }

    let deferrals = 0;
    let runAfter: Date | undefined;
    if (busy) {
      // Refusals count toward giving up only while no page gets through.
      deferrals = pages === 0 ? (job.deferrals ?? 0) + 1 : 1;
      if (deferrals > MAX_DEFERRALS) {
        throw new Error(`${busy.message}, ${MAX_DEFERRALS} times in a row. Start the import again later.`);
      }
      const wait = busy.retryAfterMs ?? FIRST_DEFERRAL_MS * 2 ** (deferrals - 1);
      runAfter = new Date(Date.now() + Math.min(Math.max(wait, MIN_DEFERRAL_MS), MAX_DEFERRAL_MS));
    }
    await savePart(corpus.id, items, cursor, deferrals, runAfter);
  } catch (error) {
    await failImport(corpus.id, error);
  }
}
