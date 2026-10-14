/**
 * Messenger permission probe API port (Meta App Review evidence).
 *
 * Wraps GET (read-only probes) and POST (full probes that send a real
 * message to one PSID) on /api/integrations/messenger-bot/permission-probe.
 * Expected failures come back as typed outcomes instead of thrown errors so
 * the panel can render each state explicitly.
 */

export interface PermissionProbeEntry {
  permission: string;
  endpoint?: string;
  required: boolean;
  ok: boolean;
  /** Present only when the probe did not run; the value is "skipped". */
  status?: "skipped";
  http_status?: number;
  summary: string;
}

export interface PermissionProbeReport {
  page_id: string;
  api_version: string;
  /** False when MESSENGER_PROBE_PSID is unset, so the send probes were skipped. */
  sends_enabled: boolean;
  probes: PermissionProbeEntry[];
}

export type PermissionProbeOutcome =
  | { kind: "ok"; report: PermissionProbeReport }
  | { kind: "unauthenticated" }
  | { kind: "forbidden" }
  | { kind: "network" }
  | { kind: "unexpected"; httpStatus: number | null };

const PROBE_PATH = "/api/integrations/messenger-bot/permission-probe";

export interface RunPermissionProbeInput {
  bearerToken: string;
}

/**
 * Runs the probe. Always POST: the backend owns the recipient through
 * MESSENGER_PROBE_PSID, so the caller never supplies one. Maps HTTP and
 * transport failures onto the outcome union.
 */
export async function runPermissionProbe({
  bearerToken,
}: RunPermissionProbeInput): Promise<PermissionProbeOutcome> {
  const headers: Record<string, string> = { Authorization: `Bearer ${bearerToken}` };
  let response: Response;
  try {
    response = await fetch(PROBE_PATH, { method: "POST", headers });
  } catch {
    return { kind: "network" };
  }

  if (response.status === 401) return { kind: "unauthenticated" };
  if (response.status === 403) return { kind: "forbidden" };
  if (!response.ok) return { kind: "unexpected", httpStatus: response.status };

  const report = parsePermissionProbeReport(await response.json().catch(() => null));
  if (!report) return { kind: "unexpected", httpStatus: response.status };
  return { kind: "ok", report };
}

/** Narrows an untrusted 200 body; returns null when the contract is violated. */
function parsePermissionProbeReport(body: unknown): PermissionProbeReport | null {
  if (typeof body !== "object" || body === null) return null;
  const candidate = body as Record<string, unknown>;
  if (typeof candidate.page_id !== "string" || typeof candidate.api_version !== "string") {
    return null;
  }
  if (!Array.isArray(candidate.probes)) return null;
  if (typeof candidate.sends_enabled !== "boolean") return null;

  const probes: PermissionProbeEntry[] = [];
  for (const raw of candidate.probes) {
    if (typeof raw !== "object" || raw === null) return null;
    const probe = raw as Record<string, unknown>;
    if (
      typeof probe.permission !== "string" ||
      typeof probe.required !== "boolean" ||
      typeof probe.ok !== "boolean" ||
      typeof probe.summary !== "string"
    ) {
      return null;
    }
    probes.push({
      permission: probe.permission,
      endpoint: typeof probe.endpoint === "string" ? probe.endpoint : undefined,
      required: probe.required,
      ok: probe.ok,
      status: probe.status === "skipped" ? "skipped" : undefined,
      http_status: typeof probe.http_status === "number" ? probe.http_status : undefined,
      summary: probe.summary,
    });
  }
  return {
    page_id: candidate.page_id,
    api_version: candidate.api_version,
    sends_enabled: candidate.sends_enabled,
    probes,
  };
}
