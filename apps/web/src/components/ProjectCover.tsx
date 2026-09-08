"use client";

/**
 * Project cover image with a graceful gradient fallback.
 *
 * While the real CDN image loads — or if it never arrives (offline, 404) — the
 * premium navy→charcoal gradient shows through. The <img> only paints over the
 * gradient once it actually loads, so a failed or slow asset never reads as a
 * broken-image glyph.
 */
import { useState } from "react";

interface ProjectCoverProps {
  /** Real image URL from the backend hello media, or null to skip the fetch. */
  src: string | null;
  /** Accessible name for the image (project display name etc.). */
  alt: string;
  /** Aspect-ratio string for the media area (default 16/9). */
  ratio?: string;
  /** Optional warm variant (terracotta-tinted gradient). */
  warm?: boolean;
  className?: string;
}

export function ProjectCover({
  src,
  alt,
  ratio = "16 / 9",
  warm = false,
  className = "",
}: ProjectCoverProps) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const showImage = src !== null && !failed;

  return (
    <div
      className={
        "media-gradient" +
        (warm ? " media-gradient--warm" : "") +
        (className ? ` ${className}` : "")
      }
      style={{
        position: "relative",
        aspectRatio: ratio,
        overflow: "hidden",
        backgroundSize: "cover",
      }}
    >
      {showImage && (
        // Keep the gradient beneath while loading; the image fades in over it.
        // Plain <img> on purpose: the CDN URLs are dynamic (backend llms-hello
        // media), so next/image remote config cannot cover them, and antd's
        // Image fallback cannot render the required gradient.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src ?? ""}
          alt={alt}
          loading="lazy"
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
          style={{
            position: "absolute",
            inset: 0,
            width: "100%",
            height: "100%",
            objectFit: "cover",
            display: "block",
            opacity: loaded ? 1 : 0,
            transition: "opacity 0.3s ease",
          }}
        />
      )}
    </div>
  );
}
