// SPDX-License-Identifier: Apache-2.0

/**
 * Registration, argument and gate coverage for the dataset-request MCP tools,
 * `request_dataset` and `list_my_dataset_requests`, and for the
 * `datasetRequestId` sample target the upload tools now accept.
 *
 * Pins:
 *
 *  1. both tools are registered under the `sponsor` scope;
 *  2. `request_dataset` accepts and refuses exactly what
 *     `POST /v1/community/requests` does (`requestDatasetBody`), except that
 *     `idempotencyKey` is optional with NO default in the published input
 *     schema (see `requestDatasetSchema` in tools.ts for why);
 *  3. the gate refuses a credential without `sponsor` (403) and an anonymous
 *     caller (401), and `request_dataset` carries the dashboard's
 *     verified-email and onboarding preconditions, while listing does not;
 *  4. `prepare_file_upload` and `prepare_large_file_upload` pass a
 *     `datasetRequestId` through to the upload service, which authorizes it.
 *
 * No database: the tools' behaviour against real rows is covered by
 * services/dataset-requests.integration.test.ts.
 */
import { describe, expect, it } from "vitest";
import { zodToJsonSchema } from "zod-to-json-schema";
import { tools } from "./tools.js";
import { assertToolAllowed, toolRequiresVerifiedEmail } from "./tool-gate.js";
import { toolRequiresOnboarding } from "./onboarding-gate.js";
import { McpToolError } from "./core/errors.js";
import { requestDatasetBody } from "../services/dataset-requests.js";

const requestTool = tools.find((t) => t.name === "request_dataset")!;
const listTool = tools.find((t) => t.name === "list_my_dataset_requests")!;
const uploadTool = tools.find((t) => t.name === "prepare_file_upload")!;
const largeUploadTool = tools.find((t) => t.name === "prepare_large_file_upload")!;

const VALID = {
  title: "Flaky test fixes",
  description: "Real flaky tests, each with its fix and an explanation of the race.",
  datasetTypeId: "debugging",
};

async function gateError(fn: () => Promise<void>): Promise<McpToolError> {
  try {
    await fn();
  } catch (err) {
    expect(err).toBeInstanceOf(McpToolError);
    return err as McpToolError;
  }
  throw new Error("expected the gate to refuse");
}

describe("dataset-request MCP tools", () => {
  it("registers both under the sponsor scope", () => {
    expect(requestTool).toBeDefined();
    expect(listTool).toBeDefined();
    expect(requestTool.scope).toBe("sponsor");
    expect(listTool.scope).toBe("sponsor");
  });

  it("request_dataset reads a body exactly as the REST create route does, defaults included", () => {
    const viaTool = requestTool.schema.parse(VALID);
    const { idempotencyKey, ...viaRest } = requestDatasetBody.parse(VALID);
    expect(viaTool).toEqual(viaRest);
    expect(viaTool.idempotencyKey).toBeUndefined(); // the tool mints one per call
    expect(idempotencyKey).toMatch(/^req_/);
    // The licence allowlist canonicalises on both.
    expect(requestTool.schema.parse({ ...VALID, proposedLicense: "cc-by-sa-4.0" }).proposedLicense).toBe("CC-BY-SA-4.0");
  });

  it("request_dataset refuses everything the REST create route refuses", () => {
    const refused: Record<string, unknown> = {
      "short title": { ...VALID, title: "abcd" },
      "short description": { ...VALID, description: "too short" },
      "too few items": { ...VALID, targetItems: 9 },
      "too many items": { ...VALID, targetItems: 100_001 },
      "unknown difficulty mix": { ...VALID, difficultyMix: "custom:1/1/1" },
      "unbundled licence": { ...VALID, proposedLicense: "MIT" },
      "bidi override in language": { ...VALID, language: "‮TypeScript" },
      "coverage over 100": { ...VALID, auditCoveragePct: 101 },
      "no title": { description: VALID.description },
    };
    for (const [name, body] of Object.entries(refused)) {
      expect(requestTool.schema.safeParse(body).success, name).toBe(false);
      expect(requestDatasetBody.safeParse(body).success, name).toBe(false);
    }
  });

  it("publishes no idempotencyKey default, so a client can never replay one", () => {
    const json = zodToJsonSchema(requestTool.schema) as { properties: Record<string, { default?: unknown }>; required?: string[] };
    expect(json.properties.idempotencyKey).toBeDefined();
    expect(json.properties.idempotencyKey!.default).toBeUndefined();
    expect(json.required).toEqual(expect.arrayContaining(["title", "description"]));
    expect(json.required).not.toContain("idempotencyKey");
  });

  it("list_my_dataset_requests takes the REST listing's query", () => {
    expect(listTool.schema.safeParse({}).success).toBe(true);
    expect(listTool.schema.safeParse({ cursor: "c1", limit: 5, q: "flaky", status: "approved" }).success).toBe(true);
    expect(listTool.schema.safeParse({ limit: 0 }).success).toBe(false);
    expect(listTool.schema.safeParse({ limit: 101 }).success).toBe(false);
    expect(listTool.schema.safeParse({ status: "pending" }).success).toBe(false);
  });

  it("the gate refuses a credential without the sponsor scope (403) and an anonymous caller (401)", async () => {
    for (const tool of [requestTool, listTool]) {
      const missing = await gateError(() => assertToolAllowed(tool, { userId: "u1", scopes: ["read", "contribute", "validate", "artifact", "account"] }));
      expect(missing.status).toBe(403);
      expect(missing.message).toContain("sponsor");

      const anonymous = await gateError(() => assertToolAllowed(tool, {}));
      expect(anonymous.status).toBe(401);
    }
  });

  it("filing a request needs a verified email and a finished account, like the dashboard; listing does not", () => {
    expect(toolRequiresVerifiedEmail("request_dataset")).toBe(true);
    expect(toolRequiresOnboarding("request_dataset")).toBe(true);
    expect(toolRequiresVerifiedEmail("list_my_dataset_requests")).toBe(false);
    expect(toolRequiresOnboarding("list_my_dataset_requests")).toBe(false);
  });

  it("both upload tools take a datasetRequestId for a reference sample", () => {
    const small = uploadTool.schema.parse({
      filename: "example-1.json",
      contentType: "application/json",
      kind: "sponsor_reference",
      datasetRequestId: "r1",
    });
    expect(small.datasetRequestId).toBe("r1");
    const large = largeUploadTool.schema.parse({
      filename: "example-1.jsonl",
      contentType: "application/x-ndjson",
      kind: "sponsor_reference",
      totalSizeBytes: 10,
      parts: [{ partNumber: 1, sizeBytes: 10, checksumSha256Hex: "a".repeat(64) }],
      datasetRequestId: "r1",
    });
    expect(large.datasetRequestId).toBe("r1");
  });
});
