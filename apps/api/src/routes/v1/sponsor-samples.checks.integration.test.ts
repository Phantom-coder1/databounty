// SPDX-License-Identifier: Apache-2.0

/**
 * A sponsor's reference samples, from upload to an admin's decision, over HTTP
 * against the real database.
 *
 * What this pins:
 *  - a new sample starts out `pending`, so the admin queue sees it, and the
 *    queue also lists samples nobody ever reviewed (NULL status);
 *  - the queue carries every automated check, and which hard checks failed;
 *  - approving a sample that failed a hard check needs `override` and a note,
 *    and the audit row records exactly which checks the reviewer saw;
 *  - an admin can re-run a sample's checks;
 *  - the sponsor can read their sample's checks, and nobody else can;
 *  - finalizing a planner draft, or changing a request's dataset type, queues
 *    the checks again.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { FastifyInstance } from "fastify";
import { ArtifactKind, ArtifactStatus, DatasetTypeStatus } from "@prisma/client";
import { buildApp } from "../../app.js";
import { prisma } from "../../lib/prisma.js";
import { putArtifactData } from "../../services/storage.js";
import {
  SAMPLE_SCHEMA_STAGE,
  SAMPLE_STAGES,
  runSponsorReferenceReviewJob,
} from "../../services/jobs/sponsor-reference-review.js";
import { requireDisposableDatabase } from "../../test-support/require-disposable-database.js";

requireDisposableDatabase();

const ORIGIN = "http://localhost:3010";
const stamp = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
const TYPE_A = `sample-flow-a-${stamp}`;
const TYPE_B = `sample-flow-b-${stamp}`;

let app: FastifyInstance;
let sponsor: { userId: string; cookie: string };
let stranger: { userId: string; cookie: string };
let admin: { userId: string; cookie: string };
let requestId = "";
const userIds: string[] = [];
const artifactIds: string[] = [];
const requestIds: string[] = [];
const sessionIds: string[] = [];

async function signupVerified(prefix: string) {
  const email = `${prefix}-${stamp}-${Math.random().toString(36).slice(2, 6)}@example.com`;
  const res = await app.inject({ method: "POST", url: "/v1/auth/signup", payload: { email, password: "Test@12345", displayName: prefix } });
  expect(res.statusCode).toBe(201);
  const userId = res.json().user.id as string;
  await prisma.user.update({ where: { id: userId }, data: { emailVerifiedAt: new Date() } });
  userIds.push(userId);
  const setCookie = res.headers["set-cookie"];
  return { userId, cookie: (Array.isArray(setCookie) ? setCookie[0]! : String(setCookie)).split(";")[0]! };
}

async function datasetType(id: string, requiredKey: string) {
  await prisma.datasetType.create({
    data: {
      id,
      name: `Sample flow ${id}`,
      description: "Fixture dataset type for the sponsor sample flow.",
      domain: "coding",
      status: DatasetTypeStatus.active,
      origin: "platform",
      category: "implementation",
      trustTier: "llm_verified",
      fields: [{ key: requiredKey, label: requiredKey, role: "instruction", required: true }],
      verification: { pipeline: ["schema", "human_audit"] },
    },
  });
}

async function sample(owner: { datasetRequestId: string } | { plannerSessionId: string }, body: unknown, reviewStatus?: null) {
  const id = `art_flow_${Math.random().toString(16).slice(2)}${Date.now().toString(16)}`;
  const storageKey = `artifacts/test/${id}/sample.json`;
  await putArtifactData(storageKey, Buffer.from(JSON.stringify(body), "utf8"), "application/json");
  await prisma.artifact.create({
    data: {
      id,
      kind: ArtifactKind.sponsor_reference,
      filename: "sample.json",
      contentType: "application/json",
      storageDriver: "local",
      storageKey,
      status: ArtifactStatus.ready,
      ownerUserId: sponsor.userId,
      sponsorReviewStatus: reviewStatus === null ? null : "pending",
      ...owner,
    },
  });
  artifactIds.push(id);
  return id;
}

const checked = (id: string) => runSponsorReferenceReviewJob(id, { attempts: 1, maxAttempts: 4 });

function review(id: string, body: Record<string, unknown>) {
  return app.inject({
    method: "POST",
    url: `/v1/artifacts/${id}/sponsor-review`,
    headers: { cookie: admin.cookie, origin: ORIGIN },
    payload: body,
  });
}

async function reviewJob(id: string) {
  return prisma.jobQueue.findUnique({ where: { idempotencyKey: `sponsor_reference.review:${id}` } });
}

beforeAll(async () => {
  app = await buildApp();
  await app.ready();
  await datasetType(TYPE_A, "instruction");
  await datasetType(TYPE_B, "question");
  sponsor = await signupVerified("flowsponsor");
  stranger = await signupVerified("flowstranger");
  admin = await signupVerified("flowadmin");
  await prisma.userRole.create({ data: { userId: admin.userId, role: "admin" } });
  const request = await prisma.datasetRequest.create({
    data: {
      requesterUserId: sponsor.userId,
      title: `Sample flow request ${stamp}`,
      description: "Fixture request for the sponsor sample flow tests.",
      datasetTypeId: TYPE_A,
      proposedLicense: "CC-BY-4.0",
      idempotencyKey: `sample-flow-${stamp}`,
    },
  });
  requestId = request.id;
  requestIds.push(request.id);
});

afterAll(async () => {
  await app.close();
  await prisma.jobQueue.deleteMany({ where: { idempotencyKey: { in: artifactIds.map((id) => `sponsor_reference.review:${id}`) } } });
  await prisma.artifactProcessingEvent.deleteMany({ where: { artifactId: { in: artifactIds } } });
  await prisma.artifact.deleteMany({ where: { id: { in: artifactIds } } });
  await prisma.plannerSession.deleteMany({ where: { id: { in: sessionIds } } });
  await prisma.datasetRequest.deleteMany({ where: { id: { in: requestIds } } });
  await prisma.user.deleteMany({ where: { id: { in: userIds } } });
  await prisma.datasetType.deleteMany({ where: { id: { in: [TYPE_A, TYPE_B] } } });
  await prisma.$disconnect();
});

describe("reaching the admin queue", () => {
  it("starts a newly uploaded sample out as pending review", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/v1/artifacts/upload-slot",
      headers: { cookie: sponsor.cookie, origin: ORIGIN },
      payload: { kind: "sponsor_reference", filename: "new.json", contentType: "application/json", sizeBytes: 24, datasetRequestId: requestId },
    });
    expect(res.statusCode).toBeLessThan(300);
    const id = (res.json().artifact?.id ?? res.json().artifactId) as string;
    artifactIds.push(id);
    expect((await prisma.artifact.findUniqueOrThrow({ where: { id } })).sponsorReviewStatus).toBe("pending");
  });

  it("lists a never-reviewed sample with every check and the hard checks it failed", async () => {
    const id = await sample({ datasetRequestId: requestId }, { notes: "no instruction" }, null);
    await checked(id);

    const res = await app.inject({ method: "GET", url: "/v1/admin/sponsor-examples", headers: { cookie: admin.cookie } });
    expect(res.statusCode).toBe(200);
    const row = (res.json().examples as Array<Record<string, any>>).find((example) => example.id === id);
    expect(row).toBeDefined();
    expect(row!.evidence.schema.status).toBe("failed");
    expect(row!.evidence.recordCount.status).toBe("passed");
    expect(row!.failedHardChecks).toEqual([SAMPLE_SCHEMA_STAGE]);
    expect(row!.stageOrder).toEqual([...SAMPLE_STAGES]);
  });
});

describe("deciding on a sample", () => {
  it("needs an override and a reason to approve a sample that failed a hard check", async () => {
    const id = await sample({ datasetRequestId: requestId }, { notes: "still no instruction" });
    await checked(id);

    const blocked = await review(id, { decision: "approved" });
    expect(blocked.statusCode).toBe(409);
    expect(blocked.json()).toMatchObject({ code: "SAMPLE_CHECKS_FAILED", failedChecks: [{ stage: SAMPLE_SCHEMA_STAGE }] });
    expect(blocked.json().failedChecks[0].reason).toMatch(/"instruction" is required/);

    const noReason = await review(id, { decision: "approved", override: true });
    expect(noReason.statusCode).toBe(409);
    expect((await prisma.artifact.findUniqueOrThrow({ where: { id } })).sponsorReviewStatus).toBe("pending");

    const overridden = await review(id, { decision: "approved", override: true, note: "The sponsor confirmed this pool does not need an instruction." });
    expect(overridden.statusCode).toBe(200);
    const audit = await prisma.adminAuditLog.findFirst({ where: { targetId: id, action: "sponsor_example.reviewed" } });
    const after = audit?.after as { overrodeFailedChecks: string[]; checks: Array<{ stage: string; status: string; eventId: string }> };
    expect(after.overrodeFailedChecks).toEqual([SAMPLE_SCHEMA_STAGE]);
    expect(after.checks.find((check) => check.stage === SAMPLE_SCHEMA_STAGE)).toMatchObject({ status: "failed" });
  });

  it("approves a sample that passed without an override, recording what was checked", async () => {
    const id = await sample({ datasetRequestId: requestId }, { instruction: "Sort the list" });
    await checked(id);

    const res = await review(id, { decision: "approved" });
    expect(res.statusCode).toBe(200);
    const audit = await prisma.adminAuditLog.findFirst({ where: { targetId: id, action: "sponsor_example.reviewed" } });
    const after = audit?.after as { overrodeFailedChecks?: string[]; checks: Array<{ stage: string; status: string }> };
    expect(after.overrodeFailedChecks).toBeUndefined();
    expect(after.checks.find((check) => check.stage === SAMPLE_SCHEMA_STAGE)?.status).toBe("passed");
  });

  it("asks for changes on a failing sample without any override", async () => {
    const id = await sample({ datasetRequestId: requestId }, { notes: "nope" });
    await checked(id);
    const res = await review(id, { decision: "needs_changes", note: "Add the instruction field." });
    expect(res.statusCode).toBe(200);
  });
});

describe("re-running checks", () => {
  it("queues the sample's checks again and audits who asked", async () => {
    const id = await sample({ datasetRequestId: requestId }, { instruction: "Re-run me" });
    const res = await app.inject({
      method: "POST",
      url: `/v1/admin/sponsor-examples/${id}/recheck`,
      headers: { cookie: admin.cookie, origin: ORIGIN },
    });
    expect(res.statusCode).toBe(202);
    expect((await reviewJob(id))?.status).toBe("pending");
    expect(await prisma.adminAuditLog.count({ where: { targetId: id, action: "sponsor_example.recheck_requested" } })).toBe(1);
  });

  it("is not available to a sponsor", async () => {
    const id = await sample({ datasetRequestId: requestId }, { instruction: "Not yours to re-run" });
    const res = await app.inject({
      method: "POST",
      url: `/v1/admin/sponsor-examples/${id}/recheck`,
      headers: { cookie: sponsor.cookie, origin: ORIGIN },
    });
    expect(res.statusCode).toBe(403);
  });
});

describe("the sponsor's view of a sample's checks", () => {
  it("shows the sponsor every check on their sample, and nobody else", async () => {
    const id = await sample({ datasetRequestId: requestId }, { notes: "missing instruction" });
    await checked(id);

    const mine = await app.inject({ method: "GET", url: `/v1/artifacts/${id}/processing-events`, headers: { cookie: sponsor.cookie } });
    expect(mine.statusCode).toBe(200);
    const schema = (mine.json().events as Array<{ stage: string; status: string; detail: { reason?: string } }>).find(
      (event) => event.stage === SAMPLE_SCHEMA_STAGE,
    );
    expect(schema).toMatchObject({ status: "failed" });
    expect(schema!.detail.reason).toMatch(/"instruction" is required/);

    const theirs = await app.inject({ method: "GET", url: `/v1/artifacts/${id}/processing-events`, headers: { cookie: stranger.cookie } });
    expect(theirs.statusCode).toBe(403);
    const anonymous = await app.inject({ method: "GET", url: `/v1/artifacts/${id}/processing-events` });
    expect(anonymous.statusCode).toBe(401);
  });
});

describe("queuing the checks again when the contract changes", () => {
  it("re-runs a request's sample checks when its dataset type changes", async () => {
    const id = await sample({ datasetRequestId: requestId }, { instruction: "Typed for A" });
    expect(await reviewJob(id)).toBeNull();

    const res = await app.inject({
      method: "PATCH",
      url: `/v1/community/requests/${requestId}`,
      headers: { cookie: sponsor.cookie, origin: ORIGIN },
      payload: { datasetTypeId: TYPE_B },
    });
    expect(res.statusCode).toBe(200);
    expect((await reviewJob(id))?.status).toBe("pending");
  });

  it("queues a planner draft's sample checks when the draft is finalized into a request", async () => {
    const created = await app.inject({ method: "POST", url: "/v1/planner/sessions", headers: { cookie: sponsor.cookie, origin: ORIGIN } });
    const sessionId = created.json().session.id as string;
    sessionIds.push(sessionId);
    await app.inject({
      method: "POST",
      url: `/v1/planner/sessions/${sessionId}/answers`,
      headers: { cookie: sponsor.cookie, origin: ORIGIN },
      payload: {
        answers: {
          category: "debugging",
          title: `Planner sample flow ${stamp}`,
          description: "A description long enough to satisfy the 20-character minimum.",
          targetItems: 250,
          difficultyMix: "balanced",
          auditCoveragePct: 15,
          proposedLicense: "CC-BY-4.0",
          language: "TypeScript",
          framework: "Node.js",
        },
      },
    });
    const id = await sample({ plannerSessionId: sessionId }, { prompt: "Fix it" });

    const finalized = await app.inject({
      method: "POST",
      url: `/v1/planner/sessions/${sessionId}/finalize`,
      headers: { cookie: sponsor.cookie, origin: ORIGIN },
    });
    expect(finalized.statusCode).toBe(201);
    const newRequestId = finalized.json().request.id as string;
    requestIds.push(newRequestId);

    expect((await prisma.artifact.findUniqueOrThrow({ where: { id } })).datasetRequestId).toBe(newRequestId);
    expect((await reviewJob(id))?.status).toBe("pending");
  });
});
