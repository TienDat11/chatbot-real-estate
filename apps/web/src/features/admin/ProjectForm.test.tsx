// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";


vi.mock("./projectAdminApi", () => ({
  fetchAdminProjectCatalogue: vi.fn(),
  triggerProjectPublish: vi.fn(),
}));

import { ProjectForm } from "./ProjectForm";

const VALID_TEXT_BY_KIND: Record<string, string> = {
  project_info: JSON.stringify({
    project: "soleil_riverside",
    ten_phap_ly: "CTCP Soleil",
    ten_thuong_mai: "Soleil Riverside",
    vi_tri: "Q.7",
  }),
  price_matrix: JSON.stringify({ project: "soleil_riverside", types: [{}, {}] }),
  unit_catalog: JSON.stringify({ project: "soleil_riverside", units: [{}] }),
  payment_methods: JSON.stringify({ project: "soleil_riverside", methods: [] }),
  sales_contacts: JSON.stringify({ project: "soleil_riverside", contacts: [] }),
  business_rules: JSON.stringify({ project: "soleil_riverside", rules: [] }),
};

const KIND_ORDER = Object.keys(VALID_TEXT_BY_KIND);

function fakeJsonFile(content: string): File {
  return { name: "doc.json", size: content.length, text: async () => content } as unknown as File;
}

function fileInputs(): HTMLInputElement[] {
  return Array.from(
    document.querySelectorAll('input[type="file"]'),
  ) as HTMLInputElement[];
}

async function typeProjectKey(projectKey: string) {
  fireEvent.change(screen.getByTestId("admin-project-key-input"), {
    target: { value: projectKey },
  });
}

function uploadAllKinds(textByKind: Record<string, string>) {
  const inputs = fileInputs();
  KIND_ORDER.forEach((kind, kindIndex) => {
    fireEvent.change(inputs[kindIndex], {
      target: { files: [fakeJsonFile(textByKind[kind])] },
    });
  });
}

describe("ProjectForm", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders one upload slot per standardized document", () => {
    render(<ProjectForm />);
    expect(screen.getAllByRole("button", { name: /Tải file JSON/ })).toHaveLength(6);
  });

  it("rejects a malformed project key before any upload", async () => {
    render(<ProjectForm />);
    typeProjectKey("Soleil Riverside!");
    expect(await screen.findByText(/Mã dự án chỉ gồm/)).toBeTruthy();
  });

  it("builds the fact preview once all six valid files are uploaded", async () => {
    const { container } = render(<ProjectForm />);
    typeProjectKey("soleil_riverside");

    uploadAllKinds(VALID_TEXT_BY_KIND);

    await waitFor(() =>
      expect(screen.getByText(/Xem trước ánh xạ fact/)).toBeTruthy(),
    );
    expect(container.textContent).toContain("1 units catalogued");
    expect(container.textContent).toContain("2 price types");
  });

  it("flags a cross-project mismatch instead of showing the preview", async () => {
    const { container } = render(<ProjectForm />);
    typeProjectKey("soleil_riverside");

    // unit_catalog declares another project — the ingest would scatter facts.
    uploadAllKinds({
      ...VALID_TEXT_BY_KIND,
      unit_catalog: JSON.stringify({ project: "camellia", units: [] }),
    });

    await waitFor(() =>
      expect(screen.getByText(/không khớp mã dự án/)).toBeTruthy(),
    );
    expect(container.textContent).not.toContain("Xem trước ánh xạ fact");
  });
});
