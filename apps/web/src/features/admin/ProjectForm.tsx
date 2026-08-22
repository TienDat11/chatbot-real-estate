"use client";

/**
 * New-project draft form (story 8.4 / ISSUE-07).
 *
 * An admin uploads the six standardized JSON documents; each file is parsed
 * and structurally validated client-side (see adminProjectDocuments), then a
 * fact-mapping preview shows what the ingest will extract. Persistence and
 * the actual publish run arrive with ISSUE-13's PublishProjectWorkflow — the
 * form states that explicitly instead of pretending to save.
 */
import { useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  Row,
  Table,
  Typography,
  Upload,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { UploadOutlined } from "@ant-design/icons";
import {
  PROJECT_DOCUMENT_KINDS,
  PROJECT_DOCUMENT_LABELS,
  validateProjectDocumentText,
  validateProjectDraft,
  type ParsedProjectDocument,
  type ProjectDocumentIssue,
  type ProjectDocumentKind,
} from "./adminProjectDocuments";
import { PublishButton } from "./PublishButton";

interface ProjectFormProps {
  /** Injectable for tests; defaults to browser File.text(). */
  readFileAsText?: (file: File) => Promise<string>;
}

const PREVIEW_COLUMNS: ColumnsType<{
  key: string;
  documentLabel: string;
  recordCount: number;
  summary: string;
}> = [
  { dataIndex: "documentLabel", title: "Tài liệu" },
  { dataIndex: "recordCount", title: "Số bản ghi", width: 110 },
  { dataIndex: "summary", title: "Ánh xạ fact" },
];

export function ProjectForm({ readFileAsText }: ProjectFormProps) {
  const readText =
    readFileAsText ?? ((file: File) => file.text() as Promise<string>);
  const [draftProjectKey, setDraftProjectKey] = useState("");
  const [
    parsedDocuments,
    setParsedDocuments,
  ] = useState<Partial<Record<ProjectDocumentKind, ParsedProjectDocument>>>({});
  const [fileIssues, setFileIssues] = useState<ProjectDocumentIssue[]>([]);

  const draftValidation = useMemo(
    () => validateProjectDraft(draftProjectKey, parsedDocuments),
    [draftProjectKey, parsedDocuments],
  );
  const allIssues = [...fileIssues, ...draftValidation.errors];

  async function handleDocumentUpload(
    kind: ProjectDocumentKind,
    file: File,
  ): Promise<void> {
    try {
      const documentText = await readText(file);
      const issues = validateProjectDocumentText(kind, documentText);
      if (issues.length > 0) {
        setFileIssues(issues);
        return;
      }
      // Re-validate against the freshly parsed body on every upload so an
      // earlier failed file cannot leave stale issues behind.
      setFileIssues([]);
      const body = JSON.parse(documentText) as Record<string, unknown>;
      setParsedDocuments((currentDocuments) => ({
        ...currentDocuments,
        [kind]: { kind, body },
      }));
    } catch {
      setFileIssues([{ kind, message: "Không đọc được nội dung file." }]);
    }
  }

  return (
    <Row gutter={[16, 16]}>
      <Col span={24}>
        <Card size="small" title="Thông tin dự án mới">
          <Input
            data-testid="admin-project-key-input"
            placeholder="Mã dự án (ví dụ: soleil_riverside)"
            value={draftProjectKey}
            onChange={(inputEvent) => setDraftProjectKey(inputEvent.target.value)}
            style={{ maxWidth: 360 }}
          />
        </Card>
      </Col>

      <Col span={24}>
        <Card size="small" title="6 file chuẩn (JSON)">
          {PROJECT_DOCUMENT_KINDS.map((kind) => (
            <div key={kind} style={{ marginBottom: 12 }}>
              <Typography.Text strong>{PROJECT_DOCUMENT_LABELS[kind]}</Typography.Text>
              <Upload
                accept=".json,application/json"
                maxCount={1}
                beforeUpload={(file) => {
                  void handleDocumentUpload(kind, file as File);
                  return false;
                }}
              >
                <Button icon={<UploadOutlined />}>Tải file JSON</Button>
              </Upload>
            </div>
          ))}
          {allIssues.map((issue, issueIndex) => (
            <Alert
              key={`${issue.kind}-${issueIndex}`}
              type="error"
              showIcon
              style={{ marginTop: 8 }}
              message={`${PROJECT_DOCUMENT_LABELS[issue.kind]}: ${issue.message}`}
            />
          ))}
        </Card>
      </Col>

      {draftValidation.preview ? (
        <Col span={24}>
          <Card size="small" title="Xem trước ánh xạ fact">
            <Table
              rowKey="key"
              size="small"
              columns={PREVIEW_COLUMNS}
              dataSource={draftValidation.preview.rows.map((row) => ({
                key: row.kind,
                documentLabel: PROJECT_DOCUMENT_LABELS[row.kind],
                recordCount: row.recordCount,
                summary: row.summary,
              }))}
              pagination={false}
            />
          </Card>
        </Col>
      ) : null}

      <Col span={24}>
        <Alert
          type="info"
          showIcon
          message="Pipeline ingest + publish chạy trên backend thuộc wave 3 (ISSUE-13). Form hiện parse, validate và giữ dữ liệu phía client."
        />
      </Col>

      <Col span={24}>
        <PublishButton projectKey={draftProjectKey || "du_an_moi"} />
      </Col>
    </Row>
  );
}
