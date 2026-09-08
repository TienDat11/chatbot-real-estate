// @vitest-environment jsdom
/** Page-level integration contract for the canonical /sales/chat route. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const capturedProps = vi.hoisted(() => ({ value: null as Record<string, unknown> | null }));

vi.mock("@/components/ChatPage", () => ({
  ChatPage: (props: Record<string, unknown>) => {
    capturedProps.value = props;
    return <main data-testid="chatpage-mounted" />;
  },
}));

import SalesChatPage from "@/app/sales/chat/page";

describe("/sales/chat page", () => {
  afterEach(() => {
    cleanup();
    capturedProps.value = null;
  });

  it("mounts the sales canvas with layout-owned chrome", () => {
    render(<SalesChatPage />);

    expect(screen.getByTestId("chatpage-mounted")).toBeTruthy();
    expect(capturedProps.value).toMatchObject({
      mode: "sales",
      audience: "sales",
      shellOwned: true,
    });
    expect(screen.queryByRole("navigation", { name: "Điều hướng chính" })).toBeNull();
  });
});
