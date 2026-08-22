"use client";

/**
 * Active-project catalogue for the admin screen (story 8.4 / ISSUE-07).
 *
 * Reads the same GET /api/projects the customer picker uses — the registry is
 * the single source of truth for what is live. Publish status/publish_at
 * polling arrives with ISSUE-13's background workflow; until then the table
 * shows what the catalogue actually knows.
 */
import { useEffect, useState } from "react";
import { Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  fetchAdminProjectCatalogue,
  type AdminProjectCatalogueEntry,
} from "./projectAdminApi";

const CATALOGUE_COLUMNS: ColumnsType<AdminProjectCatalogueEntry> = [
  { dataIndex: "project_key", title: "Mã dự án" },
  { dataIndex: "name", title: "Tên thương mại" },
  { dataIndex: "location", title: "Vị trí" },
  { dataIndex: "status", title: "Trạng thái", width: 110 },
];

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
      <Table<AdminProjectCatalogueEntry>
        rowKey="project_key"
        size="small"
        loading={isLoadingCatalogue}
        columns={CATALOGUE_COLUMNS}
        dataSource={catalogueEntries}
        pagination={false}
        locale={{ emptyText: "Chưa có dự án nào trong catalogue." }}
      />
    </div>
  );
}
