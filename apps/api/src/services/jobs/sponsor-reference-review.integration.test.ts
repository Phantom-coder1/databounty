// SPDX-License-Identifier: Apache-2.0

/**
 * The checks a sponsor's reference sample goes through before an admin
 * decides on it (services/jobs/sponsor-reference-review.ts), against the real
 * database and storage.
 *
 * Samples are what contributors copy, so they are checked the way an item is:
 * against the dataset type's field contract, and through its sandbox harness.
 * These tests pin that a check either reaches an honest verdict or records why
 * it couldn't, and that a sample uploaded during request creation, before any
 * request exists, is checked against the type picked in the planner.
 *
 * No sandbox provider is configured in tests, so a harness-backed type records
 * `pending` (not run) rather than a verdict: exactly the production state today.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { ArtifactKind, ArtifactStatus, DatasetTypeStatus } from "@prisma/client";
import { prisma } from "../../lib/prisma.js";
import { putArtifactData } from "../storage.js";
import {
  SAMPLE_EXECUTION_STAGE,
  SAMPLE_RECORD_COUNT_STAGE,
  SAMPLE_SCHEMA_STAGE,
  runSponsorReferenceReviewJob,
  samplePayload,
} from "./sponsor-reference-review.js";
import { requireDisposableDatabase } from "../../test-support/require-disposable-database.js";

requireDisposableDatabase();

const stamp = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
const TYPE_ID = `sample-checks-${stamp}`;
let sponsorId = "";
let requestId = "";
let debuggingRequestId = "";
let plannerSessionId = "";
const artifactIds: string[] = [];

beforeAll(async () => {
  await prisma.datasetType.create({
    data: {
      id: TYPE_ID,
      name: `Sample checks ${stamp}`,
      description: "Fixture dataset type for the sponsor sample checks.",
      domain: "coding",
      status: DatasetTypeStatus.active,
      origin: "platform",
      category: "implementation",
      trustTier: "llm_verified",
      fields: [
        { key: "instruction", label: "Instruction", role: "instruction", required: true },
        { key: "difficulty", label: "Difficulty", role: "enum", options: ["easy", "hard"] },
        { key: "notes", label: "Notes", role: "rationale" },
        // A sample can't carry another upload's id, so a required file field must not fail it.
        { key: "attachment", label: "Attachment", role: "file", required: true },
      ],
      verification: { pipeline: ["schema", "human_audit"] },
    },
  });
  const sponsor = await prisma.user.create({
    data: {
      email: `sample-checks-${stamp}@example.com`,
      displayName: "Sample sponsor",
      authMethod: "email",
      emailVerifiedAt: new Date(),
    },
  });
  sponsorId = sponsor.id;
  const request = (typeId: string, key: string) =>
    prisma.datasetRequest.create({
      data: {
        requesterUserId: sponsorId,
        title: `Sample checks request ${key} ${stamp}`,
        description: "Fixture request for the sponsor sample checks.",
        datasetTypeId: typeId,
        proposedLicense: "CC-BY-4.0",
        idempotencyKey: `sample-checks-${key}-${stamp}`,
      },
    });
  requestId = (await request(TYPE_ID, "custom")).id;
  debuggingRequestId = (await request("debugging", "debugging")).id;
  plannerSessionId = (
    await prisma.plannerSession.create({ data: { userId: sponsorId, answersJson: { category: TYPE_ID }, transcript: [] } })
  ).id;
});

afterAll(async () => {
  await prisma.artifactProcessingEvent.deleteMany({ where: { artifactId: { in: artifactIds } } });
  await prisma.artifact.deleteMany({ where: { id: { in: artifactIds } } });
  await prisma.plannerSession.deleteMany({ where: { id: plannerSessionId } });
  await prisma.datasetRequest.deleteMany({ where: { id: { in: [requestId, debuggingRequestId] } } });
  await prisma.user.deleteMany({ where: { id: sponsorId } });
  await prisma.datasetType.deleteMany({ where: { id: TYPE_ID } });
  await prisma.$disconnect();
});

async function sample(owner: { datasetRequestId: string } | { plannerSessionId: string }, filename: string, body: string) {
  const id = `art_sample_${Math.random().toString(16).slice(2)}${Date.now().toString(16)}`;
  const storageKey = `artifacts/test/${id}/${filename}`;
  const contentType = filename.endsWith(".csv") ? "text/csv" : "application/json";
  await putArtifactData(storageKey, Buffer.from(body, "utf8"), contentType);
  await prisma.artifact.create({
    data: {
      id,
      kind: ArtifactKind.sponsor_reference,
      filename,
      contentType,
      storageDriver: "local",
      storageKey,
      status: ArtifactStatus.ready,
      ownerUserId: sponsorId,
      ...owner,
    },
  });
  artifactIds.push(id);
  return id;
}

async function events(artifactId: string, stage: string) {
  return prisma.artifactProcessingEvent.findMany({ where: { artifactId, stage }, orderBy: { createdAt: "asc" } });
}

const run = (artifactId: string) => runSponsorReferenceReviewJob(artifactId, { attempts: 1, maxAttempts: 4 });

describe("the field-contract check", () => {
  it("passes a sample that fills the type's fields, leaving out file fields a sample can't carry", async () => {
    const id = await sample({ datasetRequestId: requestId }, "good.json", JSON.stringify({ instruction: "Sort the list", difficulty: "easy" }));
    await run(id);

    expect((await events(id, SAMPLE_RECORD_COUNT_STAGE))[0]?.status).toBe("passed");
    const [schema] = await events(id, SAMPLE_SCHEMA_STAGE);
    expect(schema?.status).toBe("passed");
    expect(schema?.handlerVersion).toBe(`schema:1:${TYPE_ID}@1`);
    expect((schema?.detail as { fieldsChecked: string[] }).fieldsChecked).toEqual(["instruction", "difficulty", "notes"]);
  });

  it("fails a sample that breaks the contract, naming each problem, and runs nothing in the sandbox", async () => {
    const id = await sample({ datasetRequestId: requestId }, "bad.json", JSON.stringify({ difficulty: "medium" }));
    await run(id);

    const [schema] = await events(id, SAMPLE_SCHEMA_STAGE);
    expect(schema?.status).toBe("failed");
    expect((schema?.detail as { reason: string }).reason).toBe(
      'This sample doesn\'t match the dataset type\'s fields: "instruction" is required; "difficulty" must be one of "easy", "hard".',
    );
    expect(await events(id, SAMPLE_EXECUTION_STAGE)).toHaveLength(0);
  });

  it("fails a sample that isn't one item, because contributors can't tell which value is which field", async () => {
    const id = await sample({ datasetRequestId: requestId }, "prose.json", JSON.stringify("just a sentence"));
    await run(id);

    const [schema] = await events(id, SAMPLE_SCHEMA_STAGE);
    expect(schema?.status).toBe("failed");
    expect((schema?.detail as { reason: string }).reason).toMatch(/isn't one item written as a JSON object/);
  });

  it("checks a CSV sample with a header and one row as one item", async () => {
    const id = await sample({ datasetRequestId: requestId }, "row.csv", "instruction,difficulty\nSort the list,hard\n");
    await run(id);
    expect((await events(id, SAMPLE_SCHEMA_STAGE))[0]?.status).toBe("passed");
  });

  it("checks a sample uploaded during request creation against the type picked in the planner", async () => {
    const id = await sample({ plannerSessionId }, "draft.json", JSON.stringify({ notes: "no instruction yet" }));
    await run(id);

    const [schema] = await events(id, SAMPLE_SCHEMA_STAGE);
    expect(schema?.status).toBe("failed");
    expect(schema?.handlerVersion).toBe(`schema:1:${TYPE_ID}@1`);
  });
});

describe("the sandbox check", () => {
  it("records a type with no sandbox harness as not supported, never as a pass", async () => {
    const id = await sample({ datasetRequestId: requestId }, "no-harness.json", JSON.stringify({ instruction: "Explain recursion" }));
    await run(id);

    const [execution] = await events(id, SAMPLE_EXECUTION_STAGE);
    expect(execution?.status).toBe("not_supported");
    expect((execution?.detail as { outcome: string }).outcome).toBe("no_executable_harness");
  });

  it("records `pending` when no sandbox is configured, and retries it on a re-run without redoing finished checks", async () => {
    const id = await sample(
      { datasetRequestId: debuggingRequestId },
      "debugging.json",
      JSON.stringify({
        prompt: "Fix the off-by-one in add()",
        broken_code: "function add(a,b){ return a+b+1; }",
        fixed_code: "function add(a,b){ return a+b; }\nmodule.exports = { add };",
        tests:
          "const { test } = require('node:test');\nconst assert = require('node:assert');\nconst { add } = require('./solution.js');\ntest('adds', () => { assert.strictEqual(add(2,3), 5); });",
        explanation: "Removed the stray +1.",
        bug_type: "logic",
      }),
    );
    await run(id);

    expect((await events(id, SAMPLE_SCHEMA_STAGE))[0]?.status).toBe("passed");
    const [execution] = await events(id, SAMPLE_EXECUTION_STAGE);
    expect(execution?.status).toBe("pending");
    expect(execution?.detail).toMatchObject({
      outcome: "no_provider_configured",
      isolationVerified: null,
      reason: "No sandbox is configured, so the sample wasn't run. Re-run the checks once the sandbox is switched on.",
    });

    await run(id);
    expect(await events(id, SAMPLE_SCHEMA_STAGE)).toHaveLength(1);
    expect(await events(id, SAMPLE_RECORD_COUNT_STAGE)).toHaveLength(1);
    expect(await events(id, SAMPLE_EXECUTION_STAGE)).toHaveLength(2);
  });
});

describe("samplePayload", () => {
  it("reads one JSON object, a one-line JSONL file, or a CSV header with exactly one row", () => {
    expect(samplePayload("a.json", '{"instruction":"x"}')).toEqual({ instruction: "x" });
    expect(samplePayload("a.jsonl", '{"instruction":"x"}\n')).toEqual({ instruction: "x" });
    expect(samplePayload("a.csv", "instruction,difficulty\nx,easy\n")).toEqual({ instruction: "x", difficulty: "easy" });
    expect(samplePayload("a.csv", "instruction\nx\ny\n")).toBeNull();
    expect(samplePayload("a.json", "[1,2]")).toBeNull();
    expect(samplePayload("a.json", "not json")).toBeNull();
  });
});
