import { TrainWorkspace } from "@/features/train/TrainWorkspace";
import { resolveSessionParam } from "@/lib/canonicalProjectUrl";

/**
 * Authenticated sales training surface; the parent sales layout owns access
 * control and shell. The canonical ?sessionId= deep link is read server-side
 * (mirroring /project/[projectKey]) and handed to the workspace so a shared
 * training session hydrates on load instead of starting a fresh one.
 */
export default async function SalesTrainPage({
  searchParams,
}: {
  searchParams: Promise<{ sessionId?: string | string[]; session?: string | string[] }>;
}) {
  const sessionId = resolveSessionParam(await searchParams);
  return <TrainWorkspace sessionId={sessionId} />;
}
