// SPDX-License-Identifier: Apache-2.0

import { z } from "zod";
import { DatasetRequestStatus, DomainId, Prisma } from "@prisma/client";
import { prisma } from "../lib/prisma.js";
import { BUNDLED_LICENSE_IDS, datasetLicense } from "../lib/publication/license-texts.js";
import { PLANNER_DIFFICULTY_MIXES, canonicalLanguageFor, coherenceProblems, isLaunchableType } from "./planner.js";
import { getCommunityPool } from "./bounties.js";

/**
 * A member's own dataset requests: filing one, and listing theirs.
 *
 * The shared core of `POST /v1/community/requests` and
 * `GET /v1/community/requests/mine` (routes/v1/community.ts) and of the MCP
 * tools `request_dataset` and `list_my_dataset_requests` (mcp/tools.ts). Both
 * routes' handler bodies moved here unchanged, so the dashboard and an agent
 * can never disagree about what a valid request is or what a member sees of
 * theirs.
 *
 * The planner's finalize (services/planner.ts `createDatasetRequestFromAnswers`)
 * still mirrors rather than calls `createDatasetRequest`: it writes the row
 * inside its own transaction, together with the hand-off of the draft's
 * reference samples, and validates the draft's answers with `validateAnswers`,
 * which applies the same `coherenceProblems` used here.
 */

/** Characters that must never survive into a stored string this API later
 * serves to anonymous readers: C0/C1 controls (NUL, CR/LF — log and header
 * injection) and the Unicode bidi overrides/isolates, which reorder RENDERED
 * text so a stored value can display as something other than what it is.
 *
 * Checked explicitly rather than left to a length bound or to the licence
 * allowlist below, because `language`/`framework` are free text by design and
 * are projected by `PUBLIC_DATASET_TYPE_SELECT` — they reach an unauthenticated
 * catalog reader once the request is minted, so the rejection has to be
 * deterministic rather than incidental. Verified live before this change: a
 * `proposedLicense` of `‮CC-BY-4.0` was accepted, stored, and served
 * verbatim by `GET /v1/bounties/:id`. */
const UNSAFE_TEXT_CHARS = /[\u0000-\u001f\u007f-\u009f‎‏‪-‮⁦-⁩]/;

/** Bounded, control/bidi-free free text. Every string field on this endpoint
 * needs a `.max()`: without one `language` was stored at 2,500,000 characters
 * (verified live), and `language` is public post-mint. */
export function safeText(max: number, min = 1) {
  return z
    .string()
    .trim()
    .min(min)
    .max(max)
    .refine((v) => !UNSAFE_TEXT_CHARS.test(v), {
      message: "Value must not contain control or bidirectional-override characters.",
    });
}

/** Closed licence allowlist, canonicalised to its SPDX spelling.
 *
 * This was `z.string().trim().min(2)` — no allowlist, no case folding and no
 * upper bound at all — on BOTH create and edit. The value is what contributors
 * are told they are working under and what publication maps to a licence tag,
 * and it is served to anonymous callers by `GET /v1/bounties/:id` once the
 * request is minted, so an unrecognised value is a trust problem rather than a
 * cosmetic one. Verified live before this change: script tags, path traversal,
 * SQL fragments, a 200,000-character string and a bidi-overridden id were all
 * accepted and carried through PATCH -> approve -> mint -> public read.
 *
 * The set is DERIVED from `BUNDLED_LICENSE_IDS` (lib/publication/
 * license-texts.ts) rather than hand-listed a second time: those are exactly
 * the licences whose full text this deployment can actually ship with a
 * published dataset, so a value outside them could never be honoured anyway.
 * `datasetLicense()` is that module's own case-insensitive lookup, which is
 * what folds `cc-by-4.0` onto `CC-BY-4.0` instead of rejecting it — existing
 * rows hold both spellings. Mirrors v1's `COMMUNITY_LICENSES` +
 * case-canonicalising transform (databounty-api/src/routes/v1/community.ts),
 * with the list shared instead of duplicated so it cannot drift from what
 * publication can serve. */
export const proposedLicenseField = safeText(60, 3).transform((raw, ctx) => {
  const canonical = datasetLicense(raw)?.spdx;
  if (!canonical) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: `Unsupported licence. Choose one of: ${BUNDLED_LICENSE_IDS.join(", ")}.`,
    });
    return z.NEVER;
  }
  return canonical;
});

/** The named presets the planner offers, and the only values
 * `resolvePoolDifficulty` (routes/v1/admin-community.ts) can actually resolve.
 * This was a bare `z.string()`: `"qa_api_freeform_difficulty"` stored fine
 * (verified live) and then resolved to `intermediate` at mint time, pricing
 * karma as though intermediate had been chosen with nothing on the record
 * saying it never was. Shared with the planner's own `answersSchema`
 * (services/planner.ts `PLANNER_DIFFICULTY_MIXES`), which already used an
 * enum, so the two entry points into the same column agree.
 *
 * Unlike v1 there is deliberately NO `custom:<a>/<b>/<c>` spec here: this
 * deployment has no `deriveSlots`, and `resolvePoolDifficulty` collapses the
 * mix to one pool difficulty, so a custom weight spec has nothing to act on
 * and would be silently flattened. */
export const difficultyMixField = z.enum(PLANNER_DIFFICULTY_MIXES);

/** Bounded id, matching v1's `datasetTypeId: z.string().trim().min(1).max(100)`.
 * Was unbounded; a 200KB value was accepted and stored (verified live). */
export const datasetTypeIdField = safeText(100);

/** A fresh idempotency key, for a request whose caller sent none. */
export function newIdempotencyKey(): string {
  return `req_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export const requestDatasetBody = z.object({
  title: safeText(120, 5),
  description: z.string().trim().min(20).max(2000),
  datasetTypeId: datasetTypeIdField.optional(),
  domain: z.nativeEnum(DomainId).default(DomainId.coding),
  proposedLicense: proposedLicenseField.default("CC-BY-4.0"),
  language: safeText(60).optional(),
  framework: safeText(60).optional(),
  targetItems: z.number().int().min(10).max(100_000).default(100),
  difficultyMix: difficultyMixField.optional(),
  auditCoveragePct: z.number().int().min(0).max(100).default(10),
  // Bounded too: this is a client-supplied uniqueness key, and it was
  // `z.string().min(1)` with no ceiling.
  idempotencyKey: safeText(200).default(newIdempotencyKey),
});

export type RequestDatasetInput = z.infer<typeof requestDatasetBody>;

export const requestsMineQuery = z.object({
  cursor: z.string().optional(),
  limit: z.coerce.number().int().min(1).max(100).default(20),
  q: z.string().trim().max(200).optional(),
  status: z.nativeEnum(DatasetRequestStatus).optional(),
});

export type RequestsMineQuery = z.infer<typeof requestsMineQuery>;

/** How many dataset requests one member may file per hour. Each one lands a
 * row in the admin review queue, so this matches `resubmit`'s 20/hour, the
 * other route that puts NEW work in front of a reviewer. */
export const REQUESTS_PER_HOUR = 20;

/** A request the caller must fix: the message is written for them. */
export class DatasetRequestError extends Error {
  constructor(
    message: string,
    readonly status: number = 400,
    readonly code: string = "invalid_request",
  ) {
    super(message);
    this.name = "DatasetRequestError";
  }
}

/** File a dataset request for `requesterUserId`, after checking it against the
 * dataset type it names. Throws `DatasetRequestError` (400) when the request
 * names no type, an unknown or inactive one, or options that type cannot
 * honour. `data` is `requestDatasetBody`'s output. */
export async function createDatasetRequest(requesterUserId: string, data: RequestDatasetInput) {
  // `datasetTypeId` is `.optional()` on this schema (unlike the PATCH body's
  // required field), but nothing downstream can proceed without one — an
  // omitted id previously reached `findUnique({ where: { id: undefined } })`
  // unguarded, which Prisma throws on synchronously with its full schema
  // dumped into the error, and that raw exception reached the client as an
  // unhandled 500 (verified live). A missing id is a client input error, not
  // a server fault.
  if (!data.datasetTypeId) throw new DatasetRequestError("datasetTypeId is required.");

  // Coherence against the type this request names — the check this door
  // never had. It previously wrote `datasetTypeId`, `language`,
  // `difficultyMix` and `auditCoveragePct` straight through without so much
  // as loading the type, so a direct API call could create a request whose
  // difficulty mix the type does not offer and whose language its contract
  // cannot verify. The planner UI blocks all of that client-side; this is
  // the same rule server-side, shared with `validateAnswers` so the two
  // creation paths cannot drift.
  //
  // Also closes a related gap: `datasetTypeId` had no existence or status
  // check here at all, so a `platform_review` type was accepted and could
  // be approved, failing only much later at mint with "not activated yet".
  const datasetType = await prisma.datasetType.findUnique({ where: { id: data.datasetTypeId } });
  if (!datasetType) throw new DatasetRequestError("That dataset template does not exist.");
  if (!isLaunchableType(datasetType)) throw new DatasetRequestError("This dataset type is not open for new requests yet.");
  const incoherent = coherenceProblems(datasetType, data);
  if (incoherent.length > 0) throw new DatasetRequestError(incoherent[0]!);

  // Fold the language onto the template's own spelling before storing it
  // (see canonicalLanguageFor) so listings do not accumulate one row per
  // casing of the same language.
  const canonicalLanguage = canonicalLanguageFor(datasetType, data.language);

  return prisma.datasetRequest.create({
    data: {
      requesterUserId,
      title: data.title,
      description: data.description,
      datasetTypeId: data.datasetTypeId,
      domain: data.domain,
      proposedLicense: data.proposedLicense,
      language: canonicalLanguage,
      framework: data.framework,
      targetItems: data.targetItems,
      difficultyMix: data.difficultyMix,
      auditCoveragePct: data.auditCoveragePct,
      idempotencyKey: data.idempotencyKey,
      status: DatasetRequestStatus.submitted,
    },
  });
}

/** The request this member already filed under `idempotencyKey`, if any, so a
 * retried call can return it instead of failing on the
 * `@@unique([requesterUserId, idempotencyKey])` constraint. */
export async function findDatasetRequestByIdempotencyKey(requesterUserId: string, idempotencyKey: string) {
  return prisma.datasetRequest.findUnique({
    where: { requesterUserId_idempotencyKey: { requesterUserId, idempotencyKey } },
  });
}

/** Throws `DatasetRequestError` (429) once the member has filed
 * `REQUESTS_PER_HOUR` requests in the last hour. The REST route enforces the
 * same number with its per-route limiter; MCP calls never pass through that
 * limiter (only the shared per-credential one, which allows hundreds a
 * minute), so the MCP tool counts the member's own rows instead. Counting rows
 * means requests filed on the dashboard count too. */
export async function assertWithinRequestLimit(requesterUserId: string, now = new Date()): Promise<void> {
  const since = new Date(now.getTime() - 60 * 60 * 1000);
  const filed = await prisma.datasetRequest.count({ where: { requesterUserId, createdAt: { gte: since } } });
  if (filed >= REQUESTS_PER_HOUR) {
    throw new DatasetRequestError(
      `You have filed ${REQUESTS_PER_HOUR} dataset requests in the last hour, the limit. Each one goes to an admin for review; try again later.`,
      429,
      "rate_limited",
    );
  }
}

/** `createDatasetRequest`, safe to retry and within the hourly limit, for
 * callers the REST route's per-route limiter never sees (the MCP tool
 * `request_dataset`). A request this member already filed under the same
 * `idempotencyKey` is returned as it is (`alreadyFiled: true`), whatever else
 * the retry says, including when two retries race to the
 * `@@unique([requesterUserId, idempotencyKey])` constraint. A retry that
 * returns an existing request does not count towards the limit. */
export async function fileDatasetRequest(requesterUserId: string, data: RequestDatasetInput) {
  const filed = await findDatasetRequestByIdempotencyKey(requesterUserId, data.idempotencyKey);
  if (filed) return { request: filed, alreadyFiled: true };
  await assertWithinRequestLimit(requesterUserId);
  try {
    return { request: await createDatasetRequest(requesterUserId, data), alreadyFiled: false };
  } catch (err) {
    if (err instanceof Prisma.PrismaClientKnownRequestError && err.code === "P2002") {
      const winner = await findDatasetRequestByIdempotencyKey(requesterUserId, data.idempotencyKey);
      if (winner) return { request: winner, alreadyFiled: true };
    }
    throw err;
  }
}

/** The member's own dataset requests, keyset-paginated over
 * (createdAt desc, id desc) so the cursor stays stable when two requests share
 * a timestamp, with status counts over their WHOLE request set and the pool
 * each approved request became. */
export async function listMyDatasetRequests(requesterUserId: string, query: RequestsMineQuery) {
  const { cursor, limit, q, status } = query;
  const where: Prisma.DatasetRequestWhereInput = {
    requesterUserId,
    ...(status ? { status } : {}),
    ...(q ? { title: { contains: q, mode: Prisma.QueryMode.insensitive } } : {}),
  };
  const rows = await prisma.datasetRequest.findMany({
    where,
    orderBy: [{ createdAt: "desc" }, { id: "desc" }],
    ...(cursor ? { cursor: { id: cursor }, skip: 1 } : {}),
    take: limit + 1,
  });
  const hasMore = rows.length > limit;
  const requests = rows.slice(0, limit);
  const nextCursor = hasMore ? requests[requests.length - 1]?.id ?? null : null;
  // Aggregate status counts for the caller's WHOLE request set, ignoring
  // `q`/`status`/`cursor` so a filter can never change the totals.
  const grouped = await prisma.datasetRequest.groupBy({
    by: ["status"],
    where: { requesterUserId },
    _count: { _all: true },
  });
  const countOf = (...statuses: DatasetRequestStatus[]) =>
    grouped.filter((row) => statuses.includes(row.status)).reduce((sum, row) => sum + row._count._all, 0);
  const statusCounts = {
    total: grouped.reduce((sum, row) => sum + row._count._all, 0),
    inReview: countOf(
      DatasetRequestStatus.submitted,
      DatasetRequestStatus.under_review,
      DatasetRequestStatus.changes_requested,
      DatasetRequestStatus.disputed,
    ),
    approved: countOf(DatasetRequestStatus.approved, DatasetRequestStatus.implemented),
    declined: countOf(DatasetRequestStatus.declined),
  };
  const mintedIds = [...new Set(requests.map((r) => r.mintedBountyId).filter((x): x is string => Boolean(x)))];
  const mintedPools = await Promise.all(mintedIds.map((id) => getCommunityPool(id)));
  const mintedById = new Map(mintedIds.map((id, i) => [id, mintedPools[i] ?? null]));
  return {
    requests: requests.map((r) => ({
      ...r,
      mintedBounty: r.mintedBountyId ? mintedById.get(r.mintedBountyId) ?? null : null,
    })),
    nextCursor,
    statusCounts,
  };
}
