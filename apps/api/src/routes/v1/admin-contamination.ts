// SPDX-License-Identifier: Apache-2.0

import type { FastifyInstance, FastifyRequest } from "fastify";
import { z } from "zod";
import { prisma } from "../../lib/prisma.js";
import { requireRole, ADMIN_ONLY, ADMIN_AND_ABOVE_READONLY, type AuthedUser } from "../../lib/rbac.js";
import { BENCHMARK_SOURCES, BenchmarkImportError, startBenchmarkImport } from "../../services/benchmark-corpus.js";
import { contaminationThresholds } from "../../services/contamination.js";
import { getAdminSetting } from "../../services/admin-settings.js";

const importBody = z.object({ sources: z.array(z.string().min(1).max(60)).max(20).optional() }).strict();

/**
 * The public-benchmark corpus contamination screening compares items against
 * (services/contamination.ts, services/benchmark-corpus.ts): which version is
 * in force, how its imports went, and starting a new one.
 *
 * Not to be confused with /admin/benchmarks (admin-benchmarks.ts), which
 * authors DataBounty's own benchmark releases.
 */
export async function adminContaminationRoutes(app: FastifyInstance) {
  app.get("/contamination/corpus", { preHandler: requireRole(...ADMIN_AND_ABOVE_READONLY) }, async (_req, reply) => {
    const [corpora, thresholds, enabled] = await Promise.all([
      prisma.benchmarkCorpus.findMany({
        orderBy: { createdAt: "desc" },
        take: 20,
        select: {
          id: true,
          name: true,
          version: true,
          source: true,
          active: true,
          status: true,
          error: true,
          itemCount: true,
          importedAt: true,
          createdAt: true,
        },
      }),
      contaminationThresholds(),
      getAdminSetting<boolean>("validation.contamination.enabled", true),
    ]);
    return reply.send({
      screening: { enabled, thresholds },
      sources: BENCHMARK_SOURCES.map((source) => ({
        id: source.id,
        title: source.title,
        dataset: source.dataset,
        splits: source.splits,
        url: `https://huggingface.co/datasets/${source.dataset}`,
      })),
      corpora: corpora.map((corpus) => ({ ...corpus, sources: corpus.source.split("+") })),
    });
  });

  // Import a new corpus version from the Hub. Screening switches to it only
  // once the import finishes; until then the version in force stays.
  app.post("/contamination/corpus/import", { preHandler: requireRole(...ADMIN_ONLY) }, async (req, reply) => {
    const parsed = importBody.safeParse(req.body ?? {});
    if (!parsed.success) return reply.badRequest("Send { sources?: string[] } naming benchmark sources to import.");
    const user = (req as FastifyRequest & { authedUser: AuthedUser }).authedUser;
    try {
      const corpus = await startBenchmarkImport(parsed.data.sources, {
        userId: user.id,
        ip: req.ip,
        userAgent: req.headers["user-agent"] ?? null,
        requestId: req.id,
      });
      return reply.code(202).send({ corpus });
    } catch (error) {
      if (error instanceof BenchmarkImportError) return reply.code(error.status).send({ message: error.message });
      throw error;
    }
  });
}
