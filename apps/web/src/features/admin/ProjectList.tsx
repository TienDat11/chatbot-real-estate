"use client";

/**
 * Active-project catalogue for the admin screen (story 8.4 / ISSUE-07).
 *
 * Reads the same GET /api/projects the customer picker uses — the registry is
 * the single source of truth for what is live. Rendered as a premium card grid
 * with a real cover image fetched from the backend hello media (POST
 * /api/llms-hello with {project_key}) and a graceful gradient fallback while
 * it loads. Publish status/publish_at polling arrives with ISSUE-13's
 * background workflow; until then the grid shows what the catalogue actually
 * knows.
 */
import { useEffect, useState } from "react";
import { Spin, Tag, Typography } from "antd";
import { EnvironmentOutlined } from "@ant-design/icons";
import { ProjectCover } from "@/components/ProjectCover";
import { useProjectCover } from "@/features/chat/projectMedia";
import { C, RADIUS } from "@/lib/tokens";
import {
  fetchAdminProjectCatalogue,
  type AdminProjectCatalogueEntry,
} from "./projectAdminApi";

export function ProjectList() {
  const [catalogueEntries, setCatalogueEntries] = useState<AdminProjectCatalogueEntry[]>([]);
  const [isLoadingCatalogue, setIsLoadingCatalogue] = useState(true);

  useEffect(function loadCatalogueOnce() {
    let isMounted = true;
    fetchAdminProjectCatalogue().then((entries) => {
      if (isMounted) {
        setCatalogueEntries(entries);
        setIsLoadingCatalogue(false);
      }
    });
    return () => {
      isMounted = false;
    };
  }, []);

  return (
    <div>
      <Typography.Paragraph type="secondary">
        Danh sách dự án đang hoạt động trong catalogue (nguồn: bảng project_config).
      </Typography.Paragraph>
      {isLoadingCatalogue ? (
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            minHeight: 160,
          }}
        >
          <Spin size="large" />
        </div>
      ) : catalogueEntries.length === 0 ? (
        <div
          style={{
            textAlign: "center",
            padding: "48px 16px",
            border: "1px dashed " + C.borderStrong,
            borderRadius: RADIUS.card,
            background: C.surfaceAlt,
          }}
        >
          <Typography.Text style={{ color: C.textMuted, fontSize: 15 }}>
            Chưa có dự án nào trong catalogue.
          </Typography.Text>
        </div>
      ) : (
        <div
          className="card-in"
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
            gap: 16,
          }}
        >
          {catalogueEntries.map((entry) => (
            <ProjectCard key={entry.project_key} entry={entry} />
          ))}
        </div>
      )}
    </div>
  );
}

/** One premium catalogue card: cover image + name + code + location + status. */
function ProjectCard({ entry }: { entry: AdminProjectCatalogueEntry }) {
  const cover = useProjectCover(entry.project_key);
  const isActive = entry.status === "active";

  return (
    <article
      className="project-card card-in"
      style={{
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        background: C.surface,
      }}
    >
      <ProjectCover src={cover} alt={entry.name} ratio="16 / 10" />
      <div style={{ padding: "14px 16px 16px", display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <Typography.Text
            strong
            style={{
              color: C.text,
              fontSize: 15,
              lineHeight: "22px",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {entry.name}
          </Typography.Text>
          <Tag
            style={{
              marginInlineEnd: 0,
              flexShrink: 0,
              borderRadius: RADIUS.pill,
              border: "none",
              fontWeight: 600,
              fontSize: 11,
              background: isActive ? C.goldSoft : C.surfaceAlt,
              color: isActive ? C.primary : C.textMuted,
              padding: "0 8px",
              lineHeight: "20px",
            }}
          >
            {isActive ? "Đang hoạt động" : entry.status}
          </Tag>
        </div>
        <Typography.Text
          style={{
            color: C.textFaint,
            fontSize: 12,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            background: C.surfaceAlt,
            borderRadius: RADIUS.small,
            padding: "2px 8px",
            alignSelf: "flex-start",
          }}
        >
          {entry.project_key}
        </Typography.Text>
        <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
          <EnvironmentOutlined style={{ color: C.terracotta, fontSize: 12, flexShrink: 0 }} />
          <Typography.Text
            style={{
              color: C.textMuted,
              fontSize: 13,
              lineHeight: "20px",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {entry.location}
          </Typography.Text>
        </div>
      </div>
    </article>
  );
}
