// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AuthContextValue } from "@/lib/AuthProvider";

vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  getFreshIdToken: vi.fn(),
}));

vi.mock("@/lib/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

vi.mock("./messengerPermissionProbe", () => ({
  runPermissionProbe: vi.fn(),
}));

import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import { useAuth } from "@/lib/AuthProvider";
import { runPermissionProbe, type PermissionProbeReport } from "./messengerPermissionProbe";
import { MessengerPermissionPanel } from "./MessengerPermissionPanel";

const SIGNED_IN: AuthContextValue = {
  user: { uid: "u1", email: "a@b.vn", displayName: null, photoURL: null, role: "admin" },
  loading: false,
  signUp: async () => undefined,
  signIn: async () => undefined,
  signOut: async () => undefined,
};

const REPORT: PermissionProbeReport = {
  page_id: "1363133426887894",
  api_version: "v26.0",
  sends_enabled: true,
  probes: [
    {
      permission: "pages_manage_metadata",
      endpoint: "GET /v26.0/1363133426887894/messenger_profile",
      required: true,
      ok: true,
      http_status: 200,
      summary: "messenger_profile readable",
    },
    {
      permission: "pages_messaging",
      required: true,
      ok: false,
      status: "skipped",
      summary: "MESSENGER_PROBE_PSID not set",
    },
    {
      permission: "pages_show_list",
      required: false,
      ok: true,
      http_status: 200,
      summary: "page listed",
    },
  ],
};

function runProbe() {
  render(<MessengerPermissionPanel />);
  fireEvent.click(screen.getByRole("button", { name: /Chạy kiểm tra quyền/ }));
}

describe("MessengerPermissionPanel", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders one row per permission with the page and API version above the table", async () => {
    vi.mocked(useAuth).mockReturnValue(SIGNED_IN);
    vi.mocked(getFreshIdToken).mockResolvedValue("id-token");
    vi.mocked(runPermissionProbe).mockResolvedValue({ kind: "ok", report: REPORT });

    runProbe();

    await waitFor(() => expect(screen.getByText("pages_manage_metadata")).toBeTruthy());
    expect(screen.getByText("pages_messaging")).toBeTruthy();
    expect(screen.getByText("pages_show_list")).toBeTruthy();
    expect(screen.getByText("1363133426887894")).toBeTruthy();
    expect(screen.getByText("v26.0")).toBeTruthy();
    expect(runPermissionProbe).toHaveBeenCalledWith({ bearerToken: "id-token" });
  });

  it("renders a skipped probe as skipped, not as a failure", async () => {
    vi.mocked(useAuth).mockReturnValue(SIGNED_IN);
    vi.mocked(getFreshIdToken).mockResolvedValue("id-token");
    vi.mocked(runPermissionProbe).mockResolvedValue({ kind: "ok", report: REPORT });

    runProbe();

    await waitFor(() => expect(screen.getByText("Bỏ qua")).toBeTruthy());
    expect(screen.queryByText("Lỗi")).toBeNull();
  });

  it("warns when the backend has no probe recipient configured", async () => {
    vi.mocked(useAuth).mockReturnValue(SIGNED_IN);
    vi.mocked(getFreshIdToken).mockResolvedValue("id-token");
    vi.mocked(runPermissionProbe).mockResolvedValue({
      kind: "ok",
      report: { ...REPORT, sends_enabled: false },
    });

    runProbe();

    await waitFor(() =>
      expect(screen.getByText(/Chưa cấu hình MESSENGER_PROBE_PSID/)).toBeTruthy()
    );
  });

  it("never offers a field for entering a message recipient", async () => {
    vi.mocked(useAuth).mockReturnValue(SIGNED_IN);

    render(<MessengerPermissionPanel />);

    expect(screen.queryByPlaceholderText(/PSID/)).toBeNull();
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
  });

  it("surfaces a 403 outcome as a typed error instead of crashing", async () => {
    vi.mocked(useAuth).mockReturnValue(SIGNED_IN);
    vi.mocked(getFreshIdToken).mockResolvedValue("id-token");
    vi.mocked(runPermissionProbe).mockResolvedValue({ kind: "forbidden" });

    runProbe();

    await waitFor(() => expect(screen.getByText(/không có quyền admin/i)).toBeTruthy());
    expect(screen.queryByText("pages_messaging")).toBeNull();
  });

  it("surfaces a network failure as a typed error instead of crashing", async () => {
    vi.mocked(useAuth).mockReturnValue(SIGNED_IN);
    vi.mocked(getFreshIdToken).mockResolvedValue("id-token");
    vi.mocked(runPermissionProbe).mockResolvedValue({ kind: "network" });

    runProbe();

    await waitFor(() => expect(screen.getByText(/Không kết nối được máy chủ/)).toBeTruthy());
  });
});
