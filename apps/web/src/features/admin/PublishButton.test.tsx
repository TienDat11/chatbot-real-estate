// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  getFreshIdToken: vi.fn(),
}));

vi.mock("./projectAdminApi", () => ({
  triggerProjectPublish: vi.fn(),
}));

import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import { triggerProjectPublish } from "./projectAdminApi";
import { PublishButton } from "./PublishButton";

function renderButton() {
  return render(<PublishButton projectKey="soleil_riverside" />);
}

describe("PublishButton", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows success after the backend accepts the publish request", async () => {
    vi.mocked(getFreshIdToken).mockResolvedValue("id-token");
    vi.mocked(triggerProjectPublish).mockResolvedValue({ kind: "accepted" });

    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /publish/i }));

    await waitFor(() =>
      expect(screen.getByText(/Đã gửi yêu cầu publish/i)).toBeTruthy(),
    );
  });

  it("explains the missing endpoint instead of a generic failure on 404", async () => {
    vi.mocked(getFreshIdToken).mockResolvedValue("id-token");
    vi.mocked(triggerProjectPublish).mockResolvedValue({
      kind: "publishEndpointNotDeployed",
    });

    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /publish/i }));

    await waitFor(() =>
      expect(screen.getByText(/chưa được triển khai/i)).toBeTruthy(),
    );
    expect(screen.getByText(/ISSUE-13/)).toBeTruthy();
  });

  it("maps a forbidden outcome onto an admin-permission notice", async () => {
    vi.mocked(getFreshIdToken).mockResolvedValue("id-token");
    vi.mocked(triggerProjectPublish).mockResolvedValue({ kind: "forbidden" });

    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /publish/i }));

    await waitFor(() =>
      expect(screen.getByText(/không có quyền admin/i)).toBeTruthy(),
    );
  });

  it("asks for re-login when no fresh ID token is available", async () => {
    vi.mocked(getFreshIdToken).mockRejectedValue(new Error("signed out"));

    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /publish/i }));

    await waitFor(() =>
      expect(screen.getByText(/đăng nhập lại/i)).toBeTruthy(),
    );
    expect(triggerProjectPublish).not.toHaveBeenCalled();
  });
});
