"use client";

// @ant-design/v5-patch-for-react-19 must load before any antd render; it is
// already imported at the top of ChatPage.tsx, which mounts this tree.

import { useEffect, useRef, useState } from "react";
import { Tag, Typography, Segmented } from "antd";
import { PlayCircleFilled, PauseCircleFilled, SoundOutlined, VideoCameraOutlined } from "@ant-design/icons";
import type { Image as ImageContract, Video } from "@rag-ragre/contracts";
import { ImageGallery } from "./ImageGallery";
import { GREETING_MID_TEXT } from "@/lib/greetingContent";
import { C, RADIUS, SHADOW } from "@/lib/tokens";

interface GreetingMediaProps {
  videos?: Video[];
  images?: ImageContract[];
  /** Whether the greeting text has finished streaming (media fades in after it). */
  ready?: boolean;
}

/**
 * Welcome-screen media block: a cinematic brand/drone film hero followed by the
 * project image gallery. Rendered inside the first assistant message. Each half
 * is optional (videos?.length / images?.length guards) so the component stays
 * defensive while the backend still lands the `videos` payload.
 *
 * Video behavior — autoplay muted playsInline (no loop: it freezes on the last
 * frame when ended) so the hero reads as a live brand film on open (mobile-safe),
 * the poster render bridges the loading gap, and a play/pause + sound toggle let
 * the reader take control. On any media error (brand MP4 not yet uploaded) we
 * swap to a muted static poster card so the layout never breaks.
 */
export function GreetingMedia({ videos, images, ready = true }: GreetingMediaProps) {
  // The backend may return several tapes sharing one kind (two `brand` films, a
  // web build plus the original). Dedupe to exactly one film per kind so the
  // segmented picker never shows duplicated labels.
  const heroVideos = pickHeroVideos(videos);
  const showVideo = heroVideos.length > 0;
  const showImages = Array.isArray(images) && images.length > 0;
  if (!showVideo && !showImages) return null;

  return (
    <div
      style={{
        marginTop: 14,
        borderTop: `1px solid ${C.border}`,
        paddingTop: 14,
        opacity: ready ? 1 : 0,
        transform: ready ? "translateY(0)" : "translateY(8px)",
        transition: `opacity 0.5s cubic-bezier(0.16,1,0.3,1), transform 0.5s cubic-bezier(0.16,1,0.3,1)`,
      }}
    >
      {showVideo && <VideoHero videos={heroVideos} />}
      {/* Bridge copy between the film and the sheets: a short sales line that
          keeps momentum while separating the two media blocks visually. */}
      {(showVideo || showImages) && (
        <Typography.Paragraph
          style={{
            margin: "12px 0 0",
            color: C.textMuted,
            fontSize: 13,
            lineHeight: "20px",
            fontStyle: "italic",
          }}
        >
          {GREETING_MID_TEXT}
        </Typography.Paragraph>
      )}
      {showVideo && showImages && <div style={{ height: 16 }} />}
      {showImages && <ImageGallery images={images} />}
    </div>
  );
}

/**
 * Collapse the raw list to at most one brand film and one drone film. When
 * multiple brand tapes exist, prefer the web-optimized build (brand-film-web)
 * so the reader does not see two tabs named "Phim giới thiệu".
 */
function pickHeroVideos(videos: Video[] | undefined): Video[] {
  if (!Array.isArray(videos) || videos.length === 0) return [];
  const isBrand = (v: Video) => (v.kind ?? "brand") === "brand";
  const brand =
    videos.find((v) => isBrand(v) && v.url_cdn.includes("brand-film-web")) ??
    videos.find(isBrand);
  const drone = videos.find((v) => v.kind === "drone");
  const out: Video[] = [];
  if (brand) out.push(brand);
  if (drone) out.push(drone);
  return out;
}

/** Sales-toned eyebrow above a media block. */
function MediaLabel({ icon, title, sub }: { icon: React.ReactNode; title: string; sub: string }) {
  return (
    <header style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
      <span
        style={{
          width: 26,
          height: 26,
          borderRadius: 8,
          background: C.primarySoft,
          color: C.primary,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 14,
          flexShrink: 0,
        }}
      >
        {icon}
      </span>
      <div style={{ minWidth: 0 }}>
        <Typography.Text
          strong
          style={{ color: C.text, fontSize: 14, lineHeight: "18px", display: "block" }}
        >
          {title}
        </Typography.Text>
        <Typography.Text style={{ color: C.textMuted, fontSize: 12, lineHeight: "16px" }}>
          {sub}
        </Typography.Text>
      </div>
    </header>
  );
}

const KIND_META: Record<string, { label: string }> = {
  brand: { label: "Phim giới thiệu" },
  drone: { label: "Bay quay tổng quan" },
};

const headerRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
  marginBottom: 8,
};

/**
 * Cinematic brand-film hero. Autoplays muted on first open (no loop) with the
 * poster render bridging the loading frame; when a tape plays to the end it
 * freezes on the last frame and the toggle flips back to Play. One segmented
 * tab per kind (brand / drone). Switching tabs stops playback, rewinds to 00:00
 * and shows the poster; the play/pause toggle mirrors the live <video> state so
 * the icon can never drift from reality. Degrades to a styled poster card if
 * the MP4 is not ready.
 */
function VideoHero({ videos }: { videos: Video[] }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [activeIdx, setActiveIdx] = useState(0);
  const [canPlay, setCanPlay] = useState(false);
  const [muted, setMuted] = useState(true);
  const [toRenderPoster, setToRenderPoster] = useState(false);
  // Mirrors the video's real play state (via onPlaying/onPause) so the toggle
  // shows Pause while playing and Play while paused, never a stale glyph. Started
  // false because a browser may block even muted autoplay; an effect reconciles
  // it from the live element on load so the initial icon cannot lie.
  const [playing, setPlaying] = useState(false);
  // Only the first tape autoplays (muted); a tab switch resets to paused+poster.
  const [wantPlay, setWantPlay] = useState(true);
  // Guards against the strict-mode double-effect re-triggering the reset on the
  // initial render while still firing on any real activeIdx change.
  const prevIdxRef = useRef(activeIdx);

  const active = videos[Math.min(activeIdx, videos.length - 1)];
  const kind = (active.kind && KIND_META[active.kind]) || KIND_META.brand;
  const poster = active.poster_url || undefined;
  const multi = videos.length > 1;

  // Switching tapes must stop playback, rewind to the first frame and show the
  // poster. The `key={active.url_cdn}` remount already zeroes currentTime; this
  // effect only drives the paused+poster state and skips the very first render
  // so the opening tape still autoplays.
  useEffect(() => {
    if (prevIdxRef.current === activeIdx) return;
    prevIdxRef.current = activeIdx;
    setCanPlay(false);
    setToRenderPoster(false);
    setPlaying(false);
    setWantPlay(false);
  }, [activeIdx]);

  // Reconcile the toggle affordances with reality: once the first frame is
  // ready, read the element's actual pause + muted state (a browser can silently
  // block muted autoplay, or unmute in flight) instead of trusting what we asked
  // for. Keeps both icons honest on initial load and after a tab switch remount.
  useEffect(() => {
    if (!canPlay) return;
    const el = videoRef.current;
    if (el) {
      setPlaying(!el.paused);
      setMuted(el.muted);
    }
  }, [canPlay, activeIdx]);

  const togglePlay = () => {
    const el = videoRef.current;
    if (!el) return;
    if (el.paused) {
      void el.play().catch(() => setToRenderPoster(true));
    } else {
      el.pause();
    }
  };

  const toggleMute = () => {
    const el = videoRef.current;
    if (!el) return;
    // Read the live element instead of trusting a possibly-stale state value so
    // rapid clicks always toggle off the current reality (no lost updates from
    // functional-setState side effects inside the updater).
    const next = !el.muted;
    el.muted = next;
    setMuted(next);
    // First unmute counts as a user gesture, so the browser lets us start audio.
    if (!next) void el.play().catch(() => setToRenderPoster(true));
  };

  // A <video> is a replaced element that swallows the wheel target on some
  // engines, so moving the pointer over the hero can feel like the page is
  // stuck. Forward the wheel to the chat scroll container so scrolling over the
  // film still moves the conversation up/down naturally.
  const forwardWheel = (e: React.WheelEvent) => {
    const scroller = (e.currentTarget as HTMLElement).closest<HTMLElement>(".chat-scroll");
    const el = scroller ?? (e.currentTarget as HTMLElement).parentElement;
    if (!el) return;
    el.scrollTop += e.deltaY;
  };

  return (
    <section aria-label="Video giới thiệu dự án">
      <div style={headerRowStyle}>
        <MediaLabel
          icon={<VideoCameraOutlined />}
          title="Tham quan dự án"
          sub="Xem phim giới thiệu và góc bay tổng quan"
        />
        {multi && (
          <Segmented
            size="small"
            value={activeIdx}
            onChange={(v) => setActiveIdx(Number(v))}
            options={videos.map((v, i) => ({
              label: (v.kind && KIND_META[v.kind]?.label) || v.title || `Video ${i + 1}`,
              value: i,
            }))}
            style={{ fontSize: 12 }}
          />
        )}
      </div>

      <div
        onWheel={forwardWheel}
        style={{
          position: "relative",
          aspectRatio: "16 / 9",
          borderRadius: RADIUS.card,
          overflow: "hidden",
          background: C.surfaceAlt,
          boxShadow: SHADOW.pop,
        }}
      >
        {toRenderPoster ? (
          // Fallback frame: the poster render as a static card. Uses a faint
          // play glyph to keep the sales intent without calling a broken source.
          <div
            style={{
              width: "100%",
              height: "100%",
              backgroundImage: `url(${poster})`,
              backgroundSize: "cover",
              backgroundPosition: "center",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <PlayCircleFilled style={{ fontSize: 60, color: "rgba(255,255,255,0.9)" }} />
          </div>
        ) : (
          <video
            ref={videoRef}
            key={active.url_cdn}
            src={active.url_cdn}
            poster={poster}
            muted
            playsInline
            autoPlay={wantPlay}
            preload="metadata"
            style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
            onCanPlay={() => setCanPlay(true)}
            onPlaying={() => {
              setCanPlay(true);
              setPlaying(true);
            }}
            onPause={() => setPlaying(false)}
            onEnded={() => {
              // No loop: the tape freezes on its last frame and the toggle flips
              // back to Play so the reader can watch again from the start.
              setCanPlay(true);
              setPlaying(false);
              setWantPlay(false);
            }}
            onError={() => {
              setToRenderPoster(true);
              setPlaying(false);
            }}
          />
        )}

        {/* Navy gradient scrim bottom: anchors the title + kind tag so the film
            reads as a framed shot, not a raw rectangle. */}
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: 0,
            padding: "34px 14px 14px",
            background: "linear-gradient(to top, rgba(15,23,42,0.78), rgba(15,23,42,0))",
            display: "flex",
            alignItems: "flex-end",
            justifyContent: "space-between",
            gap: 10,
          }}
        >
          <div style={{ minWidth: 0 }}>
            <Tag
              style={{
                marginInlineEnd: 0,
                background: "rgba(255,255,255,0.18)",
                color: "#fff",
                border: "1px solid rgba(255,255,255,0.32)",
                borderRadius: RADIUS.small,
                fontSize: 11,
                fontWeight: 600,
                padding: "0 7px",
                lineHeight: "20px",
                marginBottom: 6,
              }}
            >
              {kind.label}
            </Tag>
            <Typography.Text
              style={{
                color: "#fff",
                fontSize: 14,
                fontWeight: 600,
                lineHeight: "20px",
                display: "block",
                textShadow: "0 1px 2px rgba(0,0,0,0.4)",
              }}
            >
              {active.title}
            </Typography.Text>
          </div>

          {/* Play/pause + sound. Hidden while the poster fallback is showing
              (there is nothing to control); the play button stays disabled until
              the first frame is ready so the affordance never acts on a dead
              source. */}
          {!toRenderPoster && (
            <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
              <button
                type="button"
                aria-label={playing ? "Tạm dừng video" : "Phát video"}
                onClick={togglePlay}
                disabled={!canPlay}
                style={controlBtnStyle}
              >
                {playing ? (
                  <PauseCircleFilled style={{ fontSize: 22 }} />
                ) : (
                  <PlayCircleFilled style={{ fontSize: 22 }} />
                )}
              </button>
              <button
                type="button"
                aria-label={muted ? "Bật tiếng" : "Tắt tiếng"}
                onClick={toggleMute}
                disabled={!canPlay}
                style={controlBtnStyle}
              >
                <SoundOutlined style={{ fontSize: 18, opacity: muted ? 0.55 : 1 }} />
              </button>
            </div>
          )}
        </div>

        {/* Buffering shimmer over the loading frame (before first frame). */}
        {!canPlay && !toRenderPoster && (
          <div
            style={{
              position: "absolute",
              inset: 0,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              background: "rgba(15,23,42,0.18)",
            }}
          >
            <div
              style={{
                color: "#fff",
                fontSize: 13,
                background: "rgba(15,23,42,0.55)",
                borderRadius: RADIUS.pill,
                padding: "8px 14px",
                fontWeight: 600,
              }}
            >
              Đang tải thước phim…
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

const controlBtnStyle: React.CSSProperties = {
  width: 38,
  height: 38,
  borderRadius: RADIUS.pill,
  border: "1px solid rgba(255,255,255,0.35)",
  background: "rgba(15,23,42,0.45)",
  color: "#fff",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  cursor: "pointer",
  backdropFilter: "blur(5px)",
  transition: "background 0.18s ease, transform 0.18s ease",
};
