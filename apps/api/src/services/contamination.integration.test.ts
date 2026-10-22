// SPDX-License-Identifier: Apache-2.0

/**
 * Contamination screening against public benchmarks (services/contamination.ts,
 * services/benchmark-corpus.ts), against the real database.
 *
 * A small corpus is built from synthetic "benchmark problems" through the same
 * fingerprinting the importer uses, so these tests exercise the real shingling,
 * the GIN-indexed candidate lookup and the containment arithmetic. The importer
 * itself runs against a stubbed Hugging Face datasets-server.
 *
 * Which corpus is active is global state, so every test leaves it as it found it.
 */
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import type { FastifyInstance } from "fastify";
import { ArtifactKind, ArtifactStatus, BountyKind, BountyStatus, DatasetTypeStatus, Prisma, SubmissionStatus } from "@prisma/client";
import { buildApp } from "../app.js";
import { prisma } from "../lib/prisma.js";
import { putArtifactData } from "./storage.js";
import { setAdminSetting } from "./admin-settings.js";
import {
  benchmarkFingerprint,
  contaminationShingles,
  contaminationTokens,
  screenForContamination,
} from "./contamination.js";
import { indexBenchmarkShingles, runBenchmarkCorpusImport, startBenchmarkImport } from "./benchmark-corpus.js";
import { runSubmissionValidation } from "./validation.js";
import { runSponsorReferenceReviewJob } from "./jobs/sponsor-reference-review.js";
import { SAMPLE_CONTAMINATION_STAGE } from "./sample-checks.js";
import { requireDisposableDatabase } from "../test-support/require-disposable-database.js";

requireDisposableDatabase();

const stamp = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

// Long enough to fingerprint, and specific enough that unrelated text never shares a shingle with them.
const PROBLEM_A =
  "def separate_paren_groups(paren_string: str) -> List[str]: Input to this function is a string containing multiple " +
  "groups of nested parentheses. Your goal is to separate those group into separate strings and return the list of " +
  "those. Separate groups are balanced, each open brace is properly closed, and not nested within each other. Ignore " +
  "any spaces in the input string.";
const PROBLEM_B =
  "Janet's ducks lay sixteen eggs per day. She eats three for breakfast every morning and bakes muffins for her friends " +
  "every day with four. She sells the remainder at the farmers market daily for two dollars per fresh duck egg. How " +
  "much in dollars does she make every day at the farmers market?";
// Exactly MIN_ITEM_SHINGLES shingles, and a longer one: for "any match over a line decides".
const SHORT_PROBLEM = Array.from({ length: 17 }, (_, i) => `shortword${i}`).join(" ");
const LONG_PROBLEM = Array.from({ length: 47 }, (_, i) => `longword${i}`).join(" ");

let app: FastifyInstance;
let corpusId = "";
const previouslyActive: string[] = [];
const corpusIds: string[] = [];
let userId = "";
let adminCookie = "";
let memberCookie = "";
let datasetTypeId = "";
let bountyId = "";
let requestId = "";
const artifactIds: string[] = [];
const realFetch = globalThis.fetch;

async function signup(prefix: string) {
  const res = await app.inject({
    method: "POST",
    url: "/v1/auth/signup",
    payload: { email: `${prefix}-${stamp}@example.com`, password: "Test@12345", displayName: prefix },
  });
  const id = res.json().user.id as string;
  await prisma.user.update({ where: { id }, data: { emailVerifiedAt: new Date() } });
  const setCookie = res.headers["set-cookie"];
  return { id, cookie: (Array.isArray(setCookie) ? setCookie[0]! : String(setCookie)).split(";")[0]! };
}

beforeAll(async () => {
  app = await buildApp();
  await app.ready();

  previouslyActive.push(...(await prisma.benchmarkCorpus.findMany({ where: { active: true }, select: { id: true } })).map((c) => c.id));
  await prisma.benchmarkCorpus.updateMany({ where: { active: true }, data: { active: false } });
  const corpus = await prisma.benchmarkCorpus.create({
    data: { name: `contamination-test-${stamp}`, version: 1, source: "test", status: "ready", active: true },
  });
  corpusId = corpus.id;
  corpusIds.push(corpus.id);
  for (const [label, text] of [
    ["TEST/A (problem)", PROBLEM_A],
    ["TEST/B (question)", PROBLEM_B],
    ["TEST/C (short)", SHORT_PROBLEM],
    ["TEST/D (long)", LONG_PROBLEM],
  ] as const) {
    await prisma.benchmarkItem.create({ data: { corpusId, label, ...benchmarkFingerprint(text)! } });
  }
  await indexBenchmarkShingles(corpusId);

  const contributor = await signup("contam");
  userId = contributor.id;
  const admin = await signup("contamadmin");
  await prisma.userRole.create({ data: { userId: admin.id, role: "admin" } });
  adminCookie = admin.cookie;
  memberCookie = contributor.cookie;

  datasetTypeId = `contam-type-${stamp}`;
  await prisma.datasetType.create({
    data: {
      id: datasetTypeId,
      name: `Contamination ${stamp}`,
      description: "Fixture dataset type for contamination screening.",
      domain: "coding",
      status: DatasetTypeStatus.active,
      origin: "platform",
      category: "implementation",
      trustTier: "llm_verified",
      fields: [{ key: "instruction", label: "Instruction", role: "instruction", required: true }],
      verification: { pipeline: ["schema", "human_audit"] },
    },
  });
  bountyId = (
    await prisma.bounty.create({
      data: {
        requesterUserId: userId,
        title: `Contamination pool ${stamp}`,
        description: "Fixture pool.",
        datasetCategory: "implementation",
        language: "Python",
        framework: "None",
        auditCoveragePct: 10,
        auditMode: "partial",
        holdDays: 0,
        kind: BountyKind.community,
        status: BountyStatus.active,
        datasetTypeId,
        targetItems: 100,
      },
    })
  ).id;
  requestId = (
    await prisma.datasetRequest.create({
      data: {
        requesterUserId: userId,
        title: `Contamination request ${stamp}`,
        description: "Fixture request.",
        datasetTypeId,
        proposedLicense: "CC-BY-4.0",
        idempotencyKey: `contam-${stamp}`,
      },
    })
  ).id;
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

afterAll(async () => {
  await app.close();
  await prisma.artifactProcessingEvent.deleteMany({ where: { artifactId: { in: artifactIds } } });
  await prisma.artifact.deleteMany({ where: { id: { in: artifactIds } } });
  await prisma.jobQueue.deleteMany({ where: { idempotencyKey: { in: corpusIds.map((id) => `benchmark.corpus_import:${id}`) } } });
  await prisma.benchmarkCorpus.deleteMany({ where: { id: { in: corpusIds } } });
  await prisma.benchmarkCorpus.updateMany({ where: { id: { in: previouslyActive } }, data: { active: true } });
  await prisma.submission.deleteMany({ where: { bountyId } });
  await prisma.bounty.deleteMany({ where: { id: bountyId } });
  await prisma.datasetRequest.deleteMany({ where: { id: requestId } });
  await prisma.datasetType.deleteMany({ where: { id: datasetTypeId } });
  await prisma.$disconnect();
});

describe("fingerprints", () => {
  it("ignore formatting, punctuation and case, and store no text", () => {
    expect(contaminationTokens("def F(x):  return x+1")).toEqual(["def", "f", "x", "return", "x", "1"]);
    expect(contaminationShingles(PROBLEM_A)).toEqual(contaminationShingles(PROBLEM_A.toUpperCase().replaceAll(" ", "\n  ")));
    const fingerprint = benchmarkFingerprint(PROBLEM_A)!;
    expect(fingerprint.shingles.every((shingle) => /^[0-9a-f]{16}$/.test(shingle))).toBe(true);
    expect(JSON.stringify(fingerprint)).not.toContain("parentheses");
  });

  it("refuse text too short to tell a copy from a coincidence", () => {
    expect(contaminationShingles("return the sum of the list")).toEqual([]);
    expect(benchmarkFingerprint("Write a function to add two numbers together.")).toBeNull();
  });
});

describe("screening", () => {
  it("rejects an item that reproduces a benchmark problem inside its own fields", async () => {
    const result = await screenForContamination({
      instruction: `Solve this: ${PROBLEM_A}`,
      solution: "def separate_paren_groups(s): ...",
      explanation: "Track the depth and cut at zero.",
    });
    expect(result.decision).toBe("rejected");
    expect(result.outcome).toBe("match");
    expect(result.containment).toBeGreaterThan(0.9);
    expect((result.detail.matches as Array<{ label: string }>)[0]!.label).toBe("TEST/A (problem)");
  });

  it("sends a partial copy to review without rejecting it", async () => {
    const words = PROBLEM_B.split(" ");
    const partial = words.slice(0, Math.floor(words.length * 0.45)).join(" ");
    const result = await screenForContamination({ instruction: `${partial} Now explain how a farmer would price eggs.` });
    expect(result.decision).toBe("review_required");
    expect(result.containment).toBeGreaterThanOrEqual(0.3);
    expect(result.containment).toBeLessThan(0.6);
  });

  it("rejects on any confident match, even when a shorter problem is closer by share", async () => {
    // 8 of the short problem's 10 shingles (80%, but too few to reject on) and
    // 30 of the long one's 40 (75%, a confident copy).
    const shortPart = SHORT_PROBLEM.split(" ").slice(0, 15).join(" ");
    const longPart = LONG_PROBLEM.split(" ").slice(0, 37).join(" ");
    const result = await screenForContamination({ instruction: `${shortPart} and then ${longPart}` });
    expect(result.decision).toBe("rejected");
    expect(result.containment).toBe(0.75);
    expect((result.detail.matches as unknown[])[0]).toMatchObject({ label: "TEST/D (long)", sharedShingles: 30 });
    expect((result.detail.matches as unknown[])[1]).toMatchObject({ label: "TEST/C (short)", sharedShingles: 8 });
  });

  it("passes original work, and says when an item is too short to screen", async () => {
    const original = await screenForContamination({
      instruction: "Implement a rate limiter that allows a fixed number of requests per rolling window per client id.",
    });
    expect(original).toMatchObject({ decision: "accepted", outcome: "clean", containment: 0 });
    expect((await screenForContamination({ instruction: "Add two numbers." })).outcome).toBe("too_short");
  });

  it("records that nothing was screened when no corpus is active, never a pass", async () => {
    await prisma.benchmarkCorpus.update({ where: { id: corpusId }, data: { active: false } });
    try {
      const result = await screenForContamination({ instruction: PROBLEM_A });
      expect(result).toMatchObject({ decision: "accepted", outcome: "no_corpus", screened: false });
    } finally {
      await prisma.benchmarkCorpus.update({ where: { id: corpusId }, data: { active: true } });
    }
  });

  it("can be switched off", async () => {
    const key = "validation.contamination.enabled";
    const before = await prisma.adminSetting.findUnique({ where: { key } });
    await setAdminSetting({ key, value: false, updatedByUserId: userId });
    try {
      expect((await screenForContamination({ instruction: PROBLEM_A })).outcome).toBe("disabled");
    } finally {
      if (before) await prisma.adminSetting.update({ where: { key }, data: { value: before.value as Prisma.InputJsonValue } });
      else await prisma.adminSetting.delete({ where: { key } });
    }
  });
});

describe("the validation pipeline", () => {
  async function validated(instruction: string) {
    const submission = await prisma.submission.create({
      data: {
        bountyId,
        contributorUserId: userId,
        title: `Contamination probe ${Math.random().toString(36).slice(2, 8)}`,
        payloadJson: { instruction },
        generationMethod: "human",
        dedupeKey: `contam-${stamp}-${Math.random()}`,
        status: SubmissionStatus.submitted,
      },
    });
    await runSubmissionValidation(submission.id, 0);
    const row = await prisma.submission.findUniqueOrThrow({ where: { id: submission.id } });
    const stage = await prisma.validationResult.findFirst({ where: { submissionId: submission.id, stage: "contamination" } });
    return { row, stage };
  }

  it("rejects a contaminated item and records which benchmark it reproduces", async () => {
    const { row, stage } = await validated(`Please solve: ${PROBLEM_A}`);
    expect(row.status).toBe(SubmissionStatus.rejected);
    expect(row.contaminationScore).toBeGreaterThan(0.9);
    expect(stage).toMatchObject({ passed: false, outcome: "match" });
    expect((stage!.detailJson as { contaminationDecision: string }).contaminationDecision).toBe("rejected");
    expect(await prisma.validationResult.count({ where: { submissionId: row.id, stage: "execution" } })).toBe(0);
  });

  it("lets a clean item continue through the pipeline", async () => {
    const { row, stage } = await validated(
      "Write a function that merges overlapping calendar intervals and returns them sorted by start time.",
    );
    expect(row.status).not.toBe(SubmissionStatus.rejected);
    expect(row.contaminationScore).toBe(0);
    expect(stage).toMatchObject({ passed: true, outcome: "clean" });
  });

  it("lets a partial copy continue, with the match recorded for the validator", async () => {
    const words = PROBLEM_B.split(" ");
    const { row, stage } = await validated(
      `${words.slice(0, Math.floor(words.length * 0.45)).join(" ")} Now explain how a farmer would price eggs.`,
    );
    expect(row.status).not.toBe(SubmissionStatus.rejected);
    expect(stage).toMatchObject({ passed: true, outcome: "match" });
    expect((stage!.detailJson as { contaminationDecision: string }).contaminationDecision).toBe("review_required");
  });

  it("records a hold, not a pass, when there is no corpus to screen against", async () => {
    await prisma.benchmarkCorpus.update({ where: { id: corpusId }, data: { active: false } });
    try {
      const { row, stage } = await validated(`Please solve: ${PROBLEM_A}`);
      expect(row.status).not.toBe(SubmissionStatus.rejected);
      expect(row.contaminationScore).toBeNull();
      expect(stage).toMatchObject({ passed: false, score: null, outcome: "no_corpus" });
      expect((stage!.detailJson as { status: string }).status).toBe("no_benchmark_corpus");
    } finally {
      await prisma.benchmarkCorpus.update({ where: { id: corpusId }, data: { active: true } });
    }
  });
});

describe("importing a corpus", () => {
  function stubDatasetsServer(handler: (url: URL) => Response) {
    globalThis.fetch = (async (input: unknown) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      if (url.hostname !== "datasets-server.huggingface.co") throw new Error(`blocked: ${url}`);
      return handler(url);
    }) as typeof globalThis.fetch;
  }

  it("fingerprints every row and switches screening to the new version only when it finishes", async () => {
    stubDatasetsServer((url) => {
      const offset = Number(url.searchParams.get("offset"));
      const rows =
        offset === 0
          ? [
              { row_idx: 0, row: { task_id: "HumanEval/0", prompt: PROBLEM_A, canonical_solution: "    return []" } },
              { row_idx: 1, row: { task_id: "HumanEval/1", prompt: PROBLEM_B, canonical_solution: "    return 18" } },
            ]
          : [];
      return new Response(JSON.stringify({ rows, num_rows_total: 2 }), { headers: { "content-type": "application/json" } });
    });

    const corpus = await startBenchmarkImport(["humaneval"], { userId });
    corpusIds.push(corpus.id);
    expect(corpus.active).toBe(false);
    await runBenchmarkCorpusImport(corpus.id);

    const imported = await prisma.benchmarkCorpus.findUniqueOrThrow({ where: { id: corpus.id } });
    expect(imported).toMatchObject({ status: "ready", active: true, itemCount: 2 });
    const fingerprints = await prisma.benchmarkItem.findMany({ where: { corpusId: corpus.id }, select: { shingles: true } });
    expect(await prisma.benchmarkShingle.count({ where: { corpusId: corpus.id } })).toBe(
      fingerprints.reduce((total, item) => total + item.shingles.length, 0),
    );
    expect((await prisma.benchmarkCorpus.findUniqueOrThrow({ where: { id: corpusId } })).active).toBe(false);
    expect(await prisma.benchmarkItem.findMany({ where: { corpusId: corpus.id }, select: { label: true }, orderBy: { label: "asc" } })).toEqual([
      { label: "openai/openai_humaneval HumanEval/0 (problem)" },
      { label: "openai/openai_humaneval HumanEval/1 (problem)" },
    ]);

    // Hand the test corpus back to the rest of this file.
    await prisma.benchmarkCorpus.update({ where: { id: corpus.id }, data: { active: false } });
    await prisma.benchmarkCorpus.update({ where: { id: corpusId }, data: { active: true } });
  });

  it("marks a failed import and leaves the version in force untouched", async () => {
    stubDatasetsServer(() => new Response("upstream down", { status: 503 }));
    const corpus = await startBenchmarkImport(["gsm8k"], { userId });
    corpusIds.push(corpus.id);
    await runBenchmarkCorpusImport(corpus.id);

    const failed = await prisma.benchmarkCorpus.findUniqueOrThrow({ where: { id: corpus.id } });
    expect(failed).toMatchObject({ status: "failed", active: false, itemCount: 0 });
    expect(failed.error).toMatch(/HTTP 503/);
    expect(await prisma.benchmarkItem.count({ where: { corpusId: corpus.id } })).toBe(0);
    expect(await prisma.benchmarkShingle.count({ where: { corpusId: corpus.id } })).toBe(0);
    expect((await prisma.benchmarkCorpus.findUniqueOrThrow({ where: { id: corpusId } })).active).toBe(true);
  }, 30_000);
});

describe("admin routes", () => {
  it("lists corpus versions and sources, and lets only an admin start an import", async () => {
    const list = await app.inject({ method: "GET", url: "/v1/admin/contamination/corpus", headers: { cookie: adminCookie } });
    expect(list.statusCode).toBe(200);
    expect(list.json().sources.map((source: { id: string }) => source.id)).toContain("humaneval");
    expect(list.json().screening).toMatchObject({ enabled: true, thresholds: { reject: 0.6, review: 0.3 } });

    const refused = await app.inject({
      method: "POST",
      url: "/v1/admin/contamination/corpus/import",
      headers: { cookie: memberCookie, origin: "http://localhost:3010" },
      payload: {},
    });
    expect(refused.statusCode).toBe(403);

    const started = await app.inject({
      method: "POST",
      url: "/v1/admin/contamination/corpus/import",
      headers: { cookie: adminCookie, origin: "http://localhost:3010" },
      payload: { sources: ["mbpp"] },
    });
    expect(started.statusCode).toBe(202);
    corpusIds.push(started.json().corpus.id);

    const second = await app.inject({
      method: "POST",
      url: "/v1/admin/contamination/corpus/import",
      headers: { cookie: adminCookie, origin: "http://localhost:3010" },
      payload: {},
    });
    expect(second.statusCode).toBe(409);
    await prisma.benchmarkCorpus.update({ where: { id: started.json().corpus.id }, data: { status: "failed" } });

    const unknown = await app.inject({
      method: "POST",
      url: "/v1/admin/contamination/corpus/import",
      headers: { cookie: adminCookie, origin: "http://localhost:3010" },
      payload: { sources: ["not-a-benchmark"] },
    });
    expect(unknown.statusCode).toBe(400);
  });
});

describe("sponsor samples", () => {
  it("fails a sample copied from a benchmark as a hard check", async () => {
    const id = `art_contam_${Math.random().toString(16).slice(2)}`;
    const storageKey = `artifacts/test/${id}/sample.json`;
    await putArtifactData(storageKey, Buffer.from(JSON.stringify({ instruction: PROBLEM_B }), "utf8"), "application/json");
    await prisma.artifact.create({
      data: {
        id,
        kind: ArtifactKind.sponsor_reference,
        filename: "sample.json",
        contentType: "application/json",
        storageDriver: "local",
        storageKey,
        status: ArtifactStatus.ready,
        ownerUserId: userId,
        datasetRequestId: requestId,
      },
    });
    artifactIds.push(id);

    await runSponsorReferenceReviewJob(id, { attempts: 1, maxAttempts: 4 });

    const event = await prisma.artifactProcessingEvent.findFirst({ where: { artifactId: id, stage: SAMPLE_CONTAMINATION_STAGE } });
    expect(event?.status).toBe("failed");
    expect((event?.detail as { reason: string }).reason).toMatch(/TEST\/B/);
  });
});
