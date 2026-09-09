import { redirect } from "next/navigation";
import { resolveSessionParam } from "@/lib/canonicalProjectUrl";

/**
 * Legacy /train entry: normalizes onto the authenticated training surface at
 * /sales/train, preserving the session id (canonical spelling) so deep links
 * keep hydrating; train-only query params (filters, mode) have no meaning on
 * the training workspace. redirect() throws inside Next, so execution never
 * reaches past the redirect call in production; mocked redirects fall through
 * for tests.
 */
export default async function LegacyTrainPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}): Promise<void> {
  const sessionId = resolveSessionParam(await searchParams);
  redirect(`/sales/train${sessionId ? `?sessionId=${encodeURIComponent(sessionId)}` : ""}`);
}
