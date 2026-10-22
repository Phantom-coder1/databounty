// SPDX-License-Identifier: Apache-2.0

import { createHash } from "node:crypto";
import { Prisma } from "@prisma/client";
import { prisma } from "../lib/prisma.js";
import { canonicalizePayload } from "./dedup-lsh.js";
import { getAdminSetting } from "./admin-settings.js";

/**
 * Contamination screening: is this item copied from a public benchmark?
 *
 * Datasets built here are used to train and evaluate models. An item lifted
 * from a benchmark (HumanEval, MBPP, SWE-bench and the like) leaks that
 * benchmark's test set into training data and makes every later score on it
 * meaningless, so such an item is rejected rather than paid for.
 *
 * How a match is found. Text is reduced to lowercase word tokens (letters,
 * digits and underscores, so formatting and punctuation don't matter) and cut
 * into overlapping 8-token shingles. Each shingle is stored only as a short
 * hash: the corpus never holds a benchmark's text. A GIN index on those hashes
 * finds every benchmark problem sharing at least one shingle with the item,
 * and for each the CONTAINMENT is computed: the share of that problem's
 * shingles found in the item. Containment, not plain similarity, because an
 * item copies a problem into a larger payload (its own explanation, tests,
 * a broken variant), and plain similarity would be diluted by everything the
 * benchmark doesn't have.
 *
 * Two bands, like dedupe: at or above the reject line the item is rejected; at
 * or above the review line it continues, with the match shown to the human
 * reviewer. Both lines also need a minimum number of shared shingles, so a
 * short benchmark problem can't be "contained" by coincidence.
 */

/** Shingle width, in tokens. Long enough that unrelated texts rarely share one. */
const SHINGLE_TOKENS = 8;
/** A benchmark problem shorter than this many shingles isn't indexed: too short to tell a copy from a coincidence. */
export const MIN_ITEM_SHINGLES = 10;
/** Shared shingles needed before a match counts at all. */
const MIN_OVERLAP_REVIEW = 6;
const MIN_OVERLAP_REJECT = 10;
/** Candidate problems examined per item. Ordered by nothing in particular, so it is set well above any real match count. */
const MAX_CANDIDATES = 400;

const TOKEN = /[\p{L}\p{N}_]+/gu;

/** Lowercase word tokens: formatting, punctuation and casing never matter. */
export function contaminationTokens(text: string): string[] {
  return text.toLowerCase().match(TOKEN) ?? [];
}

function shingleHash(shingle: string): string {
  return createHash("sha256").update(shingle).digest("hex").slice(0, 16);
}

/** The hashed shingles of a text. Empty when it is too short to have one. */
export function contaminationShingles(text: string): string[] {
  const tokens = contaminationTokens(text);
  if (tokens.length < SHINGLE_TOKENS) return [];
  const out = new Set<string>();
  for (let i = 0; i + SHINGLE_TOKENS <= tokens.length; i += 1) {
    out.add(shingleHash(tokens.slice(i, i + SHINGLE_TOKENS).join(" ")));
  }
  return [...out];
}

/** The fingerprint stored for one benchmark problem: no text, only hashes. */
export function benchmarkFingerprint(text: string): { canonicalHash: string; shingles: string[] } | null {
  const shingles = contaminationShingles(text);
  if (shingles.length < MIN_ITEM_SHINGLES) return null;
  const canonicalHash = createHash("sha256").update(contaminationTokens(text).join(" ")).digest("hex");
  return { canonicalHash, shingles };
}

export interface ContaminationMatch {
  /** The benchmark problem, e.g. "openai/openai_humaneval HumanEval/12". */
  label: string | null;
  containment: number;
  sharedShingles: number;
  problemShingles: number;
}

export type ContaminationDecision = "rejected" | "review_required" | "accepted";

export interface ContaminationResult {
  /** `rejected` only for a confident match. Everything else lets the item continue. */
  decision: ContaminationDecision;
  /** False when nothing was compared (switched off, or no corpus imported): a hold, never a pass. */
  screened: boolean;
  /**
   * What was established, never a pass that wasn't earned:
   * `clean` (screened, no match), `match` (a review or reject match),
   * `disabled` (switched off), `no_corpus` (nothing imported to screen against),
   * `too_short` (the item has too little text to fingerprint).
   */
  outcome: "clean" | "match" | "disabled" | "no_corpus" | "too_short";
  containment: number;
  detail: Record<string, unknown>;
}

/** The corpus version screening uses now, or null when none has been imported. */
export async function activeBenchmarkCorpus() {
  return prisma.benchmarkCorpus.findFirst({
    where: { active: true },
    select: { id: true, name: true, version: true, source: true, itemCount: true },
    orderBy: { createdAt: "desc" },
  });
}

export async function contaminationThresholds(): Promise<{ reject: number; review: number }> {
  const reject = await getAdminSetting<number>("validation.contamination.reject_containment", 0.6);
  const review = await getAdminSetting<number>("validation.contamination.review_containment", 0.3);
  return { reject, review: Math.min(review, reject) };
}

/** Screen one item's payload against the active benchmark corpus. */
export async function screenForContamination(payload: unknown): Promise<ContaminationResult> {
  if (!(await getAdminSetting<boolean>("validation.contamination.enabled", true))) {
    return {
      decision: "accepted",
      outcome: "disabled",
      screened: false,
      containment: 0,
      detail: { status: "screening_switched_off", reason: "Contamination screening is switched off, so this item was not screened." },
    };
  }
  const corpus = await activeBenchmarkCorpus();
  if (!corpus) {
    return {
      decision: "accepted",
      outcome: "no_corpus",
      screened: false,
      containment: 0,
      detail: {
        status: "no_benchmark_corpus",
        reason: "No benchmark corpus has been imported, so this item was not screened. It is not a pass.",
      },
    };
  }
  const about = { corpus: { name: corpus.name, version: corpus.version, source: corpus.source } };

  const shingles = contaminationShingles(canonicalizePayload(payload));
  if (shingles.length === 0) {
    return {
      decision: "accepted",
      outcome: "too_short",
      // Every indexed problem has at least MIN_ITEM_SHINGLES shingles and this
      // item has none, so it cannot contain one: a real result, not a skip.
      screened: true,
      containment: 0,
      detail: { ...about, reason: "This item has too little text to compare against benchmarks." },
    };
  }

  const candidates = await prisma.$queryRaw<{ label: string | null; shingles: string[] }[]>(Prisma.sql`
    SELECT label, shingles
      FROM benchmark_items
     WHERE corpus_id = ${corpus.id}
       AND shingles && ${shingles}::text[]
     LIMIT ${MAX_CANDIDATES}
  `);

  const mine = new Set(shingles);
  const matches: ContaminationMatch[] = candidates
    .map((candidate) => {
      const shared = candidate.shingles.reduce((count, shingle) => count + (mine.has(shingle) ? 1 : 0), 0);
      return {
        label: candidate.label,
        containment: candidate.shingles.length > 0 ? shared / candidate.shingles.length : 0,
        sharedShingles: shared,
        problemShingles: candidate.shingles.length,
      };
    })
    .sort((a, b) => b.containment - a.containment || b.sharedShingles - a.sharedShingles);

  const { reject, review } = await contaminationThresholds();
  const best = matches[0];
  const decision: ContaminationDecision =
    best && best.containment >= reject && best.sharedShingles >= MIN_OVERLAP_REJECT
      ? "rejected"
      : best && best.containment >= review && best.sharedShingles >= MIN_OVERLAP_REVIEW
        ? "review_required"
        : "accepted";

  return {
    decision,
    outcome: decision === "accepted" ? "clean" : "match",
    screened: true,
    containment: best?.containment ?? 0,
    detail: {
      ...about,
      thresholds: { reject, review },
      // The closest few, so a reviewer can see what the item resembles.
      matches: matches.slice(0, 3).map((match) => ({ ...match, containment: Number(match.containment.toFixed(3)) })),
      ...(decision === "rejected"
        ? { reason: `This item reproduces a public benchmark problem (${best!.label ?? "unlabelled"}).` }
        : decision === "review_required"
          ? { reason: `Parts of this item resemble a public benchmark problem (${best!.label ?? "unlabelled"}); a reviewer decides.` }
          : {}),
    },
  };
}
