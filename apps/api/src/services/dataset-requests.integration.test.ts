// SPDX-License-Identifier: Apache-2.0

/**
 * Requesting a dataset over MCP, against the real database: the
 * `request_dataset` and `list_my_dataset_requests` tools, and reference
 * samples attached to a request through `prepare_file_upload`.
 *
 * Proves:
 *  1. an agent's request is stored exactly as the dashboard route stores the
 *     same body (both call services/dataset-requests.ts);
 *  2. the type checks refuse with the dashboard's own messages;
 *  3. a retry with the same idempotencyKey, even a racing one, returns the
 *     request already filed instead of a second row;
 *  4. the hourly limit matches the route's, counts dashboard requests too,
 *     and never counts a retry;
 *  5. the listing is the caller's own, with each request's sample gate;
 *  6. only the requester can attach a sample, and only while the request is
 *     still open to changes;
 *  7. the gate applies the verified-email and onboarding preconditions.
 */
import { createHash } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { FastifyInstance } from "fastify";
import { buildApp } from "../app.js";
import { prisma } from "../lib/prisma.js";
import { requireDisposableDatabase } from "../test-support/require-disposable-database.js";
import { tools } from "../mcp/tools.js";
import { assertToolAllowed } from "../mcp/tool-gate.js";
import { resetOnboardingCache } from "../mcp/onboarding-gate.js";
import { McpToolError } from "../mcp/core/errors.js";
import { REQUESTS_PER_HOUR } from "./dataset-requests.js";

requireDisposableDatabase();

let app: FastifyInstance;
const createdUserIds: string[] = [];

// `fixreq_mcp` prefix on every row this file creates: the verification
// database is shared, and afterAll deletes by these ids.
const TYPE_ID = "fixreq_mcp_choice";

const requestTool = tools.find((t) => t.name === "request_dataset")!;
const listTool = tools.find((t) => t.name === "list_my_dataset_requests")!;
const uploadTool = tools.find((t) => t.name === "prepare_file_upload")!;

let requester: { userId: string; cookie: string };
let stranger: { userId: string; cookie: string };

const BODY = {
  title: "fixreq_mcp flaky test fixes",
  description: "Real flaky tests, each with its fix and an explanation of the race.",
  datasetTypeId: TYPE_ID,
  language: "typescript",
  proposedLicense: "cc-by-sa-4.0",
  targetItems: 40,
  difficultyMix: "mostly_beginner" as const,
};

async function signup(prefix: string, opts: { verified: boolean; onboarded: boolean }) {
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  const res = await app.inject({
    method: "POST",
    url: "/v1/auth/signup",
    payload: {
      email: `fixreq-mcp-${prefix}-${stamp}@example.com`,
      password: "Test@12345",
      handle: `fixreqmcp${prefix}${Date.now()}${Math.floor(Math.random() * 1000)}`.slice(0, 30),
      displayName: `fixreq_mcp_${prefix}`,
    },
  });
  expect(res.statusCode).toBe(201);
  const userId = res.json().user.id as string;
  createdUserIds.push(userId);
  // Signup leaves the handle unclaimed; a finished account has both.
  await prisma.user.update({
    where: { id: userId },
    data: {
      emailVerifiedAt: opts.verified ? new Date() : null,
      onboarded: opts.onboarded,
      handle: opts.onboarded ? `fixreqmcp${prefix}${stamp.replace(/\W/g, "")}`.slice(0, 30) : null,
    },
  });
  const setCookie = res.headers["set-cookie"];
  const cookie = (Array.isArray(setCookie) ? setCookie[0]! : String(setCookie)).split(";")[0]!;
  return { userId, cookie };
}

async function toolError(fn: () => Promise<unknown>): Promise<McpToolError> {
  try {
    await fn();
  } catch (err) {
    expect(err).toBeInstanceOf(McpToolError);
    return err as McpToolError;
  }
  throw new Error("expected the tool to refuse");
}

const fileRequest = (userId: string, args: Record<string, unknown>) =>
  requestTool.call(requestTool.schema.parse(args), { userId, credentialKind: "api_key" });

beforeAll(async () => {
  app = await buildApp();
  await app.ready();
  await prisma.datasetRequest.deleteMany({ where: { datasetTypeId: TYPE_ID } });
  await prisma.datasetType.deleteMany({ where: { id: TYPE_ID } });
  // A closed language choice and three difficulty levels, so both coherence
  // checks have something to refuse.
  await prisma.datasetType.create({
    data: {
      id: TYPE_ID,
      domain: "coding",
      name: "fixreq_mcp fixture",
      description: "Fixture dataset type for the dataset-request MCP tools.",
      status: "active",
      origin: "platform",
      category: "implementation",
      trustTier: "llm_verified",
      fields: [
        { key: "instruction", label: "Instruction", role: "instruction", required: true },
        { key: "language", label: "Language", options: ["typescript", "python"], required: true },
      ],
      verification: { pipeline: ["schema", "human_audit"], dedupeFields: ["instruction"], auditOptions: [25, 100] },
      difficultyLevels: ["beginner", "intermediate", "advanced"],
      complexityScore: 2,
      verificationUnits: 1,
    },
  });
  requester = await signup("req", { verified: true, onboarded: true });
  stranger = await signup("str", { verified: true, onboarded: true });
});

afterAll(async () => {
  const requests = await prisma.datasetRequest.findMany({ where: { datasetTypeId: TYPE_ID }, select: { id: true } });
  await prisma.artifact.deleteMany({ where: { datasetRequestId: { in: requests.map((r) => r.id) } } });
  await prisma.datasetRequest.deleteMany({ where: { OR: [{ datasetTypeId: TYPE_ID }, { requesterUserId: { in: createdUserIds } }] } });
  await prisma.datasetType.deleteMany({ where: { id: TYPE_ID } });
  await prisma.user.deleteMany({ where: { id: { in: createdUserIds } } });
  await app.close();
  await prisma.$disconnect();
});

describe("request_dataset", () => {
  it("stores an agent's request exactly as the dashboard route stores the same body", async () => {
    const viaTool = await fileRequest(requester.userId, BODY);
    expect(viaTool.alreadyFiled).toBe(false);
    expect(viaTool.nextStep).toContain(viaTool.request.id);

    const viaRoute = await app.inject({
      method: "POST",
      url: "/v1/community/requests",
      headers: { cookie: requester.cookie, origin: "http://localhost:3010" },
      payload: BODY,
    });
    expect(viaRoute.statusCode).toBe(201);

    const [a, b] = await Promise.all([
      prisma.datasetRequest.findUniqueOrThrow({ where: { id: viaTool.request.id } }),
      prisma.datasetRequest.findUniqueOrThrow({ where: { id: viaRoute.json().request.id } }),
    ]);
    const comparable = ({ id: _id, idempotencyKey: _key, createdAt: _c, updatedAt: _u, ...rest }: typeof a) => rest;
    expect(comparable(a)).toEqual(comparable(b));
    expect(a).toMatchObject({
      requesterUserId: requester.userId,
      status: "submitted",
      language: "TypeScript", // folded onto the contract's spelling
      proposedLicense: "CC-BY-SA-4.0",
      targetItems: 40,
      difficultyMix: "mostly_beginner",
      auditCoveragePct: 10,
    });
  });

  it("refuses an unknown type or a language the type can't verify, with the dashboard's own words", async () => {
    const unknown = await toolError(() => fileRequest(requester.userId, { ...BODY, datasetTypeId: "fixreq_mcp_nope" }));
    expect(unknown.status).toBe(400);
    expect(unknown.message).toBe("That dataset template does not exist.");

    const language = await toolError(() => fileRequest(requester.userId, { ...BODY, language: "java" }));
    expect(language.status).toBe(400);
    expect(language.message).toMatch(/is not a language this template can verify/);

    const missing = await toolError(() => fileRequest(requester.userId, { ...BODY, datasetTypeId: undefined }));
    expect(missing.message).toBe("datasetTypeId is required.");
  });

  it("returns the request already filed when a retry reuses its idempotencyKey, even racing", async () => {
    const key = `fixreq-mcp-retry-${Date.now()}`;
    const first = await fileRequest(requester.userId, { ...BODY, idempotencyKey: key });
    const again = await fileRequest(requester.userId, { ...BODY, title: "fixreq_mcp changed on retry", idempotencyKey: key });
    expect(again).toMatchObject({ alreadyFiled: true, request: { id: first.request.id, title: BODY.title } });

    const raceKey = `fixreq-mcp-race-${Date.now()}`;
    const racing = await Promise.all([1, 2, 3].map(() => fileRequest(requester.userId, { ...BODY, idempotencyKey: raceKey })));
    expect(new Set(racing.map((r) => r.request.id)).size).toBe(1);
    expect(await prisma.datasetRequest.count({ where: { requesterUserId: requester.userId, idempotencyKey: raceKey } })).toBe(1);
  });

  it(`stops at ${REQUESTS_PER_HOUR} an hour, counting dashboard requests and never a retry`, async () => {
    const busy = await signup("busy", { verified: true, onboarded: true });
    await prisma.datasetRequest.createMany({
      data: Array.from({ length: REQUESTS_PER_HOUR }, (_, i) => ({
        requesterUserId: busy.userId,
        title: `fixreq_mcp busy ${i}`,
        description: "Seeded as if filed on the dashboard within the hour.",
        datasetTypeId: TYPE_ID,
        proposedLicense: "CC-BY-4.0",
        idempotencyKey: `fixreq-busy-${i}`,
      })),
    });

    const limited = await toolError(() => fileRequest(busy.userId, BODY));
    expect(limited.status).toBe(429);
    expect(limited.code).toBe("rate_limited");

    const retry = await fileRequest(busy.userId, { ...BODY, idempotencyKey: "fixreq-busy-3" });
    expect(retry.alreadyFiled).toBe(true);

    await prisma.datasetRequest.updateMany({
      where: { requesterUserId: busy.userId },
      data: { createdAt: new Date(Date.now() - 2 * 60 * 60 * 1000) },
    });
    expect((await fileRequest(busy.userId, BODY)).alreadyFiled).toBe(false);
  });
});

describe("list_my_dataset_requests", () => {
  it("lists only the caller's requests, newest first, with counts and each one's sample gate", async () => {
    const mine = await listTool.call(listTool.schema.parse({ limit: 100 }), { userId: requester.userId });
    const total = await prisma.datasetRequest.count({ where: { requesterUserId: requester.userId } });
    expect(mine.requests.length).toBe(total);
    expect(mine.statusCounts.total).toBe(total);
    expect(mine.requests.every((r: { requesterUserId: string }) => r.requesterUserId === requester.userId)).toBe(true);
    expect(mine.requests[0].sampleGate).toMatchObject({ ok: false, approved: 0, min: 3 });

    const theirs = await listTool.call(listTool.schema.parse({}), { userId: stranger.userId });
    expect(theirs.requests).toEqual([]);

    const first = await listTool.call(listTool.schema.parse({ limit: 1 }), { userId: requester.userId });
    expect(first.nextCursor).toBeTruthy();
    const second = await listTool.call(listTool.schema.parse({ limit: 1, cursor: first.nextCursor }), { userId: requester.userId });
    expect(second.requests[0].id).not.toBe(first.requests[0].id);
  });
});

describe("reference samples through prepare_file_upload", () => {
  const sample = Buffer.from(JSON.stringify({ instruction: "Fix the race in the retry loop", language: "typescript" }));
  const declaration = (datasetRequestId: string) => ({
    filename: "example-1.json",
    contentType: "application/json",
    kind: "sponsor_reference",
    sizeBytes: sample.length,
    checksumSha256: createHash("sha256").update(sample).digest("hex"),
    datasetRequestId,
  });

  it("attaches a sample to the requester's own open request, and shows in its gate", async () => {
    const { request } = await fileRequest(requester.userId, { ...BODY, idempotencyKey: `fixreq-sample-${Date.now()}` });
    const slot = await uploadTool.call(uploadTool.schema.parse(declaration(request.id)), { userId: requester.userId });
    const artifact = await prisma.artifact.findUniqueOrThrow({ where: { id: slot.fileId } });
    expect(artifact).toMatchObject({ datasetRequestId: request.id, kind: "sponsor_reference", ownerUserId: requester.userId });

    const listed = await listTool.call(listTool.schema.parse({ limit: 100 }), { userId: requester.userId });
    const row = listed.requests.find((r: { id: string }) => r.id === request.id);
    expect(row.sampleGate).toMatchObject({ pending: 1, approved: 0, ok: false });
  });

  it("refuses anyone but the requester, and a request no longer open to changes", async () => {
    const { request } = await fileRequest(requester.userId, { ...BODY, idempotencyKey: `fixreq-frozen-${Date.now()}` });
    const stolen = await toolError(() => uploadTool.call(uploadTool.schema.parse(declaration(request.id)), { userId: stranger.userId }));
    expect(stolen.message).toBe("only the requester may attach reference samples");

    await prisma.datasetRequest.update({ where: { id: request.id }, data: { status: "approved" } });
    const frozen = await toolError(() => uploadTool.call(uploadTool.schema.parse(declaration(request.id)), { userId: requester.userId }));
    expect(frozen.message).toMatch(/frozen/);
  });
});

describe("the gate on request_dataset", () => {
  it("asks for a verified email first, then a finished account", async () => {
    const unverified = await signup("unv", { verified: false, onboarded: true });
    const email = await toolError(() => assertToolAllowed(requestTool, { userId: unverified.userId, scopes: ["sponsor"] }));
    expect(email.status).toBe(403);
    expect(email.code).toBe("email_unverified");

    resetOnboardingCache();
    const fresh = await signup("new", { verified: true, onboarded: false });
    const onboarding = await toolError(() => assertToolAllowed(requestTool, { userId: fresh.userId, scopes: ["sponsor"] }));
    expect(onboarding.status).toBe(428);

    await expect(assertToolAllowed(requestTool, { userId: requester.userId, scopes: ["sponsor"] })).resolves.toBeUndefined();
  });
});
