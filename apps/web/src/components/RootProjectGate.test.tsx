// @vitest-environment jsdom
/**
 * RootProjectGate unit tests (wave-1 forced-picker contract). Bare "/" with
 * more than one active project and no stored choice MUST render the ProjectPicker
 * instead of silently redirecting to a default project; picking a project stores
 * the choice and navigates to /project/<key>. A stored choice or a single active
 * project still navigates straight.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RootProjectGate } from "@/components/RootProjectGate";
import { PROJECT_KEY_STORAGE } from "@/features/chat/identity";

// The gate only calls router.replace (navigation is outside the unit under
// test); the double records the exact target URL so tests can pin the contract.
const routerMock = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Contract shape of GET /api/projects (display_name/short_name), mirroring the
// wave-1 endpoint payload with the hot Camellia listed first.
const CATALOG = {
  projects: [
    {
      project_key: "camellia",
      display_name: "The Camellia Sơn Trà - Đà Nẵng",
      short_name: "The Camellia",
      location: "Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Sơn Trà, Đà Nẵng",
      is_hot: true,
    },
    {
      project_key: "soleil",
      display_name: "The Soleil Đà Nẵng",
      short_name: "The Soleil",
      location: "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
      is_hot: false,
    },
  ],
};

beforeEach(() => {
  window.localStorage.clear();
  routerMock.replace.mockClear();
  routerMock.push.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

describe("RootProjectGate (wave-1 forced picker)", () => {
  it("renders the picker list from the catalogue and never auto-redirects on first visit", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(CATALOG)));
    render(<RootProjectGate />);

    await waitFor(() => {
      expect(screen.getByText("Chọn dự án để được tư vấn")).toBeTruthy();
    });
    // The forced picker must appear, not a silent redirect to catalog[0].
    expect(routerMock.replace).not.toHaveBeenCalled();

    // Both active projects are offered, hot project listed first.
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.getByText("The Camellia")).toBeTruthy();
    expect(screen.getByText("The Soleil")).toBeTruthy();
    expect(screen.getByText("Nổi bật")).toBeTruthy();
  });

  it("navigates to /project/<key> and persists the choice when a project is selected", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(CATALOG)));
    render(<RootProjectGate />);

    await waitFor(() => {
      expect(screen.getByText("Chọn dự án để được tư vấn")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("The Soleil"));
    expect(routerMock.replace).toHaveBeenCalledWith("/project/soleil");
    expect(window.localStorage.getItem(PROJECT_KEY_STORAGE)).toBe("soleil");
  });

  it("honours a stored choice by navigating straight without opening the picker", async () => {
    window.localStorage.setItem(PROJECT_KEY_STORAGE, "soleil");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(CATALOG)));
    render(<RootProjectGate />);

    await waitFor(() => {
      expect(routerMock.replace).toHaveBeenCalledWith("/project/soleil");
    });
    expect(screen.queryByText("Chọn dự án để được tư vấn")).toBeNull();
  });

  it("navigates straight when the catalogue holds a single active project", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ projects: [CATALOG.projects[0]] }))
    );
    render(<RootProjectGate />);

    await waitFor(() => {
      expect(routerMock.replace).toHaveBeenCalledWith("/project/camellia");
    });
    expect(screen.queryByText("Chọn dự án để được tư vấn")).toBeNull();
  });
});
