"use client";

import { Image as AntImage, Tag, Typography } from "antd";
import { CheckCircleFilled, PictureOutlined } from "@ant-design/icons";
import type { ReactNode } from "react";
import type { Image as ImageContract, ImageMatch } from "@rag-ragre/contracts";
import { C, RADIUS, SHADOW } from "@/lib/tokens";

interface ImageGalleryProps {
  images: ImageContract[];
}

/**
 * Kind metadata for the four asset types the backend can return. The accent is
 * always drawn from the app's single navy primary; each kind only shifts its
 * soft tint and label so the row stays harmonious instead of rainbow-colored.
 * Each intro is a short sales-toned line shown under the backend caption.
 */
const KIND_META: Record<string, { label: string; soft: string; text: string; intro: string }> = {
  matbang: {
    label: "Mặt bằng",
    soft: C.primarySoft,
    text: C.primary,
    intro: "Bố cục không gian tối ưu, hình dung trọn cuộc sống tại dự án.",
  },
  banggia: {
    label: "Bảng giá",
    soft: C.successSoft,
    text: C.success,
    intro: "Cơ hội đầu tư rõ ràng — tham khảo mức giá theo từng căn.",
  },
  toroi: {
    label: "Tờ rơi",
    soft: "#FDF3E3",
    text: C.warning,
    intro: "Tổng quan dự án trong một cái nhìn, từ tiện ích đến vị trí.",
  },
  "thanh-toan": {
    label: "Thanh toán",
    soft: "#F4EFFF",
    text: "#6F42C1",
    intro: "Chính sách thanh toán linh hoạt, chủ động kế hoạch tài chính.",
  },
};

const DEFAULT_KIND = {
  label: "Hình ảnh",
  soft: C.surfaceAlt,
  text: C.textMuted,
  intro: "",
};

/**
 * Visual treatment for the "why this image" badge. "exact" is the unit the user
 * asked for, so it leads with a filled check on the navy soft tint; "similar" is
 * a comparable unit for side-by-side reference, so it stays muted on the neutral
 * surface. "semantic" (or an absent match) renders no badge at all.
 */
const MATCH_META: Record<
  Exclude<ImageMatch, "semantic">,
  { soft: string; text: string; icon: ReactNode; fallback: string }
> = {
  exact: {
    soft: C.primarySoft,
    text: C.primary,
    icon: <CheckCircleFilled />,
    fallback: "Đúng căn bạn hỏi",
  },
  similar: {
    soft: C.surfaceAlt,
    text: C.textMuted,
    icon: null,
    fallback: "Căn tương tự để so sánh",
  },
};

/** Resolve the badge copy, falling back to a sensible default when reason is empty. */
function reasonText(image: ImageContract): string | null {
  if (image.match !== "exact" && image.match !== "similar") return null;
  const reason = image.reason?.trim();
  return reason && reason.length > 0 ? reason : MATCH_META[image.match].fallback;
}

/** Human-friendly grouping header for the whole gallery. */
const GALLERY_TITLE = "Hình ảnh & tài liệu dự án";

/** SVG placeholder shown when a CDN asset fails to load, keeping the layout
 *  intact instead of rendering a broken-image icon. */
const FALLBACK_IMG =
  "data:image/svg+xml;charset=utf-8," +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">' +
      '<rect width="100%" height="100%" fill="#F4F6FA"/>' +
      '<text x="50%" y="50%" fill="#ABB3C3" text-anchor="middle" ' +
      'dominant-baseline="middle" font-family="sans-serif" font-size="26">🏠</text>' +
      "</svg>"
  );

/**
 * Renders a responsive, lightbox-capable gallery of project illustration assets
 * inside an assistant answer. Cards lift on hover and open antd's PreviewGroup
 * so the reader can zoom / swipe between every sheet from the same chat turn.
 */
export function ImageGallery({ images }: ImageGalleryProps) {
  // Posterize on small screens, a tidy 5-up on desktops. The inner antd Image
  // keeps default preview so PreviewGroup collects every thumbnail; clicking
  // any card opens the lightbox anchored at that image.
  return (
    <section
      style={{
        marginTop: 14,
        borderTop: `1px solid ${C.border}`,
        paddingTop: 14,
      }}
      aria-label={GALLERY_TITLE}
    >
      <header style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
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
          <PictureOutlined />
        </span>
        <div style={{ minWidth: 0 }}>
          <Typography.Text
            strong
            style={{ color: C.text, fontSize: 14, lineHeight: "18px", display: "block" }}
          >
            {GALLERY_TITLE}
          </Typography.Text>
          <Typography.Text style={{ color: C.textMuted, fontSize: 12, lineHeight: "16px" }}>
            Bấm vào ảnh để xem cận cảnh
          </Typography.Text>
        </div>
      </header>

      <AntImage.PreviewGroup
        preview={{
          // Keep the lightbox keyboard/scroll zoom defaults without overriding
          // the current index — antd tracks it internally per open.
        }}
      >
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))",
            gap: 10,
          }}
        >
          {images.map((im) => {
            const kind = KIND_META[im.kind] ?? DEFAULT_KIND;
            return (
              <figure
                key={im.image_id}
                style={{
                  margin: 0,
                  background: C.surface,
                  border: `1px solid ${C.border}`,
                  borderRadius: RADIUS.card,
                  overflow: "hidden",
                  transition: "transform 0.18s ease, box-shadow 0.18s ease",
                  cursor: "zoom-in",
                  display: "flex",
                  flexDirection: "column",
                  boxShadow: SHADOW.card,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.transform = "translateY(-3px)";
                  e.currentTarget.style.boxShadow = SHADOW.pop;
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.transform = "translateY(0)";
                  e.currentTarget.style.boxShadow = SHADOW.card;
                }}
              >
                <div style={{ position: "relative", aspectRatio: "4 / 3", background: C.surfaceAlt }}>
                  <AntImage
                    src={im.url_cdn}
                    alt={im.alt_text ?? im.title ?? ""}
                    style={{
                      objectFit: "cover",
                      width: "100%",
                      height: "100%",
                      display: "block",
                    }}
                    fallback={FALLBACK_IMG}
                  />
                </div>
                <figcaption
                  style={{
                    padding: "8px 10px 10px",
                    display: "flex",
                    flexDirection: "column",
                    gap: 6,
                    flex: 1,
                  }}
                >
                  <Tag
                    style={{
                      alignSelf: "flex-start",
                      marginInlineEnd: 0,
                      background: kind.soft,
                      color: kind.text,
                      border: "none",
                      borderRadius: RADIUS.small,
                      fontSize: 11,
                      fontWeight: 600,
                      padding: "0 6px",
                      lineHeight: "20px",
                    }}
                  >
                    {kind.label}
                  </Tag>
                  <Typography.Text
                    strong
                    style={{ color: C.text, fontSize: 13, lineHeight: "18px", display: "block" }}
                  >
                    {im.title}
                  </Typography.Text>
                  {(() => {
                    const text = reasonText(im);
                    if (text == null) return null;
                    const meta = im.match === "exact" ? MATCH_META.exact : MATCH_META.similar;
                    return (
                      <Tag
                        icon={meta.icon}
                        style={{
                          alignSelf: "flex-start",
                          marginInlineEnd: 0,
                          background: meta.soft,
                          color: meta.text,
                          border: `1px solid ${im.match === "exact" ? C.primaryBorder : C.border}`,
                          borderRadius: RADIUS.small,
                          fontSize: 11,
                          fontWeight: 600,
                          padding: "0 7px",
                          lineHeight: "20px",
                        }}
                      >
                        {text}
                      </Tag>
                    );
                  })()}
                  {im.caption != null && im.caption.length > 0 && (
                    <Typography.Text style={{ color: C.textMuted, fontSize: 12, lineHeight: "18px" }}>
                      {im.caption}
                    </Typography.Text>
                  )}
                  {kind.intro.length > 0 && (
                    <Typography.Text
                      style={{
                        color: C.textFaint,
                        fontSize: 11,
                        lineHeight: "16px",
                        fontStyle: "italic",
                      }}
                    >
                      {kind.intro}
                    </Typography.Text>
                  )}
                </figcaption>
              </figure>
            );
          })}
        </div>
      </AntImage.PreviewGroup>
    </section>
  );
}
