// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

vi.mock("./projectAdminApi", () => ({
  fetchAdminProjectCatalogue: vi.fn(),
}));

import { fetchAdminProjectCatalogue } from "./projectAdminApi";
import { ProjectList } from "./ProjectList";

describe("ProjectList", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders the catalogue rows once the fetch resolves", async () => {
    vi.mocked(fetchAdminProjectCatalogue).mockResolvedValue([
      {
        project_key: "soleil_riverside",
        name: "Soleil Riverside",
        location: "Q.7, TP.HCM",
        status: "active",
      },
    ]);

    const { container } = render(<ProjectList />);

    await waitFor(() => {
      expect(screen.getByText("soleil_riverside")).toBeTruthy();
      // antd's Spin wrapper node can stay mounted during its leave motion in
      // jsdom (no transitionend), so assert the spinning state, not the node.
      expect(container.querySelector(".ant-spin-spinning")).toBeFalsy();
    });
    expect(screen.getByText("Soleil Riverside")).toBeTruthy();
    expect(screen.getByText("Q.7, TP.HCM")).toBeTruthy();
  });

  it("shows an empty state when the catalogue is unreachable", async () => {
    vi.mocked(fetchAdminProjectCatalogue).mockResolvedValue([]);

    render(<ProjectList />);

    await waitFor(() =>
      expect(screen.getByText(/Chưa có dự án nào/)).toBeTruthy(),
    );
  });
});
