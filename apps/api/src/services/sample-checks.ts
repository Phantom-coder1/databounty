// SPDX-License-Identifier: Apache-2.0

import type { Prisma } from "@prisma/client";
import { prisma } from "../lib/prisma.js";

/**
 * The automated checks on a sponsor's reference sample, as their results are
 * read: by the admin queue, by the approve/reject route, and by the sponsor's
 * own view. The checks themselves run in services/jobs/sponsor-reference-review.ts.
 *
 * Kept apart from that job on purpose. The job can run a sample in the sandbox,
 * and code that only reads results should not have to load the sandbox to do so.
 */

export const SAMPLE_RECORD_COUNT_STAGE = "sponsor_sample_record_count";
export const SAMPLE_SCHEMA_STAGE = "sponsor_sample_schema";
export const SAMPLE_CONTAMINATION_STAGE = "sponsor_sample_contamination";
export const SAMPLE_SIMILARITY_STAGE = "sponsor_sample_similarity";
export const SAMPLE_EXECUTION_STAGE = "sponsor_sample_execution";
export const SAMPLE_LLM_REVIEW_STAGE = "sponsor_sample_llm_review";

/** Every stage, in the order the job runs them. */
export const SAMPLE_STAGES = [
  SAMPLE_RECORD_COUNT_STAGE,
  SAMPLE_SCHEMA_STAGE,
  SAMPLE_CONTAMINATION_STAGE,
  SAMPLE_SIMILARITY_STAGE,
  SAMPLE_EXECUTION_STAGE,
  SAMPLE_LLM_REVIEW_STAGE,
] as const;

/** Stages a sample must not have failed to be approved without an override:
 * several examples packed in one file, fields that don't match the dataset
 * type, a sample copied from a public benchmark, or the sample's own tests
 * failing in the sandbox. */
export const HARD_SAMPLE_STAGES: readonly string[] = [
  SAMPLE_RECORD_COUNT_STAGE,
  SAMPLE_SCHEMA_STAGE,
  SAMPLE_CONTAMINATION_STAGE,
  SAMPLE_EXECUTION_STAGE,
];

export interface SampleCheck {
  stage: string;
  status: string;
  handlerVersion: string;
  detail: unknown;
  createdAt: Date;
  eventId: string;
}

/** The latest recorded result of every stage, per sample. A stage that never
 * ran is simply absent: it is never reported as a pass. */
export async function latestSampleChecks(
  artifactIds: string[],
  client: Prisma.TransactionClient | typeof prisma = prisma
): Promise<Map<string, Map<string, SampleCheck>>> {
  const byArtifact = new Map<string, Map<string, SampleCheck>>();
  if (artifactIds.length === 0) return byArtifact;
  const rows = await client.artifactProcessingEvent.findMany({
    where: { artifactId: { in: artifactIds }, stage: { in: [...SAMPLE_STAGES] } },
    orderBy: { createdAt: "desc" },
  });
  for (const row of rows) {
    const stages = byArtifact.get(row.artifactId) ?? new Map<string, SampleCheck>();
    if (!stages.has(row.stage)) {
      stages.set(row.stage, {
        stage: row.stage,
        status: row.status,
        handlerVersion: row.handlerVersion,
        detail: row.detail,
        createdAt: row.createdAt,
        eventId: row.id,
      });
    }
    byArtifact.set(row.artifactId, stages);
  }
  return byArtifact;
}

/** The hard checks a sample's latest results say it failed. */
export function failedHardChecks(checks: Map<string, SampleCheck> | undefined): SampleCheck[] {
  return [...(checks?.values() ?? [])].filter((check) => HARD_SAMPLE_STAGES.includes(check.stage) && check.status === "failed");
}
