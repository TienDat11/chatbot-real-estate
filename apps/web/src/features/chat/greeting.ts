import type { Image, Video } from "@rag-ragre/contracts";
import {
  GREETING_STATIC_TEXT,
  GREETING_IMAGES,
  GREETING_VIDEOS,
} from "@/lib/greetingContent";

/**
 * Per-project first-open greeting (wave-1 UX).
 *
 * The FE greeting is intentionally static content (instant render, no network
 * dependency — see lib/greetingContent.ts). Now that the project choice gates
 * the greeting (force-picker rule), the copy must match the chosen project so
 * a Soleil visitor is never greeted as Camellia. Camellia keeps the rich
 * media greeting (films + floor-plan gallery); other projects fall back to a
 * text-only greeting grounded in their project_info, since no static media
 * bundle exists for them yet.
 */

/** Soleil intro copy, grounded in data/_processed/soleil/project_info.json. */
export const SOLEIL_GREETING_TEXT =
  "Kính chào Anh/Chị! Em là chuyên viên tư vấn The Soleil Đà Nẵng. " +
  "Dự án nằm ngay giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà - " +
  "tổ hợp căn hộ khách sạn hạng thương gia với view sông - biển tuyệt đẹp. " +
  "Anh/Chị đang tìm căn để ở, đầu tư hay cho thuê ạ? " +
  "Cứ nhắn nhu cầu, em sẽ tư vấn tận tình và gợi ý căn phù hợp nhất để Anh/Chị an tâm sở hữu nhé.";

export interface GreetingBundle {
  text: string;
  images: Image[];
  videos: Video[];
}

/**
 * Returns the greeting bundle for a chosen project. The rich media belongs to
 * Camellia (the first project with a processed static media bundle); every
 * other project greets with grounded text only.
 */
export function greetingForProject(projectKey: string | null | undefined): GreetingBundle {
  if (projectKey === "soleil") {
    return { text: SOLEIL_GREETING_TEXT, images: [], videos: [] };
  }
  // Default/camellia and any unknown key: the Camellia bundle. camellia is
  // also the backend registry DEFAULT_PROJECT_KEY, so this keeps the legacy
  // single-project behaviour intact.
  return { text: GREETING_STATIC_TEXT, images: GREETING_IMAGES, videos: GREETING_VIDEOS };
}
