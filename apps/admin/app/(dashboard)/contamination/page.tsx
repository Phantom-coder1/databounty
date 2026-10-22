"use client";

// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import {
  AdminButton,
  AdminDateTime,
  AdminErrorBanner,
  AdminPageHeader,
  AdminPill,
  AdminSectionHeading,
  AdminStat,
  AdminTable,
  ATd,
  type AdminPillTone,
} from "@/components/admin-shell";
import { adminAuthedFetch, useAdminRoleGates } from "@/lib/admin-auth";
import { useAdminResource } from "@/lib/use-admin-resource";

/**
 * Contamination screening: the public-benchmark corpus every submission is
 * compared against (GET /v1/admin/contamination/corpus and
 * POST /v1/admin/contamination/corpus/import; services/contamination.ts and
 * services/benchmark-corpus.ts on the API).
 *
 * Shows which corpus version screening uses and how each import went, and
 * lets an admin import a new version from Hugging Face. Until one is imported
 * every item is recorded as not screened, never as a pass. The on/off switch
 * and both thresholds are ordinary settings (`validation.contamination.*`).
 */

interface Corpus {
  id: string;
  name: string;
  version: number;
  sources: string[];
  active: boolean;
  status: string;
  error: string | null;
  itemCount: number;
  importedAt: string | null;
  createdAt: string;
}

interface CorpusState {
  screening: { enabled: boolean; thresholds: { reject: number; review: number } };
  sources: { id: string; title: string; dataset: string; configs?: string[]; splits: string[]; url: string }[];
  corpora: Corpus[];
}

const STATUS_TONE: Record<string, AdminPillTone> = { ready: "success", importing: "info", failed: "danger" };

function pct(n: number): string {
  return `${Math.round(n * 100)}%`;
}

function ImportButton({ disabled, onStarted }: { disabled: boolean; onStarted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const start = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await adminAuthedFetch("/v1/admin/contamination/corpus/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as { message?: string } | null;
        throw new Error(body?.message ?? `The import couldn't start (${res.status}).`);
      }
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The import couldn't start.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col items-end gap-1">
      <AdminButton
        variant="primary"
        disabled={disabled || busy}
        onClick={() => void start()}
        tooltip="Builds a new corpus version from every source below. Screening switches to it only once it finishes."
      >
        {busy ? "Starting…" : "Import from Hugging Face"}
      </AdminButton>
      {error && <div role="alert" className="max-w-xs text-right text-[11px] text-rose-400">{error}</div>}
    </div>
  );
}

export default function ContaminationPage() {
  const state = useAdminResource<CorpusState>("/v1/admin/contamination/corpus", {
    errorMessage: "Contamination screening state is unavailable.",
    pollMs: 15_000,
  });
  const { isAdmin } = useAdminRoleGates();
  const d = state.data;
  const inForce = d?.corpora.find((corpus) => corpus.active) ?? null;
  const importing = d?.corpora.some((corpus) => corpus.status === "importing") ?? false;

  return (
    <div className="space-y-5">
      <AdminPageHeader
        title="Contamination screening"
        eyebrow="System"
        sub="Every submission is compared against public benchmark problems. An item that reproduces one is rejected, and a partial match is shown to the validator. Only hashes of each problem's 8-word fragments are stored, never benchmark text."
        actions={isAdmin && d ? <ImportButton disabled={importing} onStarted={() => void state.refresh()} /> : undefined}
      />

      {state.error && <AdminErrorBanner message={state.error} onRetry={() => void state.refresh()} />}
      {state.loading && <div role="status" className="font-mono text-sm text-dark-soft">Loading contamination screening…</div>}

      {d && <>
        {!inForce && d.screening.enabled && (
          <div className="rounded-xl border border-dark-warn-border bg-amber-400/10 px-4 py-3 font-mono text-[12.5px] text-amber-400">
            No corpus imported yet, so submissions are recorded as not screened.
            {isAdmin ? " Import one to start screening." : " An admin can import one."}
          </div>
        )}

        <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <AdminStat
            label="screening"
            value={d.screening.enabled ? "on" : "off"}
            sub={d.screening.enabled ? "every submission" : "validation.contamination.enabled is off"}
            tone={d.screening.enabled ? "lime" : "amber"}
          />
          <AdminStat
            label="corpus in force"
            value={inForce ? `v${inForce.version}` : "none"}
            sub={inForce ? `${inForce.itemCount.toLocaleString()} problem fingerprints` : "nothing to screen against"}
            tone={inForce ? "default" : "amber"}
          />
          <AdminStat label="rejected at" value={pct(d.screening.thresholds.reject)} sub="of a problem's text found in the item" />
          <AdminStat label="sent to the validator at" value={pct(d.screening.thresholds.review)} sub="below the reject line" />
        </section>

        <section className="space-y-2">
          <AdminSectionHeading
            title="// sources"
            sub="Imported through the Hugging Face datasets-server. Problem statements and reference solutions are fingerprinted separately, so copying either one is caught."
          />
          <AdminTable headers={["Benchmark", "Dataset", "Read"]} caption="Benchmark sources">
            {d.sources.map((source) => (
              <tr key={source.id}>
                <ATd><span className="font-medium text-dark-text">{source.title}</span></ATd>
                <ATd>
                  <a href={source.url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">
                    {source.dataset}
                  </a>
                </ATd>
                <ATd>
                  {source.splits.join(", ")}
                  {(source.configs?.length ?? 0) > 1 && (
                    <span className="text-dark-dim"> · {source.configs!.join(", ")}</span>
                  )}
                </ATd>
              </tr>
            ))}
          </AdminTable>
        </section>

        <section className="space-y-2">
          <AdminSectionHeading
            title="// versions"
            sub="Each import builds a new version. Screening switches to it only once every row is in; a failed import leaves the version in force untouched."
          />
          <AdminTable headers={["Version", "Sources", "Problems", "Status", "Imported"]} caption="Corpus versions">
            {d.corpora.length === 0 ? (
              <tr>
                <ATd colSpan={5} className="text-dark-soft">No corpus has been imported.</ATd>
              </tr>
            ) : d.corpora.map((corpus) => (
              <tr key={corpus.id}>
                <ATd>
                  <span className="font-medium text-dark-text">{corpus.name} v{corpus.version}</span>
                  {corpus.active && <AdminPill tone="lime" className="ml-2">in force</AdminPill>}
                </ATd>
                <ATd>{corpus.sources.join(", ")}</ATd>
                <ATd>
                  {corpus.status === "ready"
                    ? corpus.itemCount.toLocaleString()
                    : corpus.status === "importing" && corpus.itemCount > 0
                      ? `${corpus.itemCount.toLocaleString()} so far`
                      : "—"}
                </ATd>
                <ATd>
                  <AdminPill tone={STATUS_TONE[corpus.status] ?? "neutral"}>{corpus.status}</AdminPill>
                  {corpus.error && <div className="mt-1 max-w-md text-[11px] text-rose-400">{corpus.error}</div>}
                </ATd>
                <ATd>{corpus.importedAt ? <AdminDateTime iso={corpus.importedAt} /> : <span className="text-dark-dim">started <AdminDateTime iso={corpus.createdAt} /></span>}</ATd>
              </tr>
            ))}
          </AdminTable>
        </section>
      </>}
    </div>
  );
}
