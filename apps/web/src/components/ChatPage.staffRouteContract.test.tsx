// @vitest-environment jsdom
/**
 * CHAT-CONTRACT checkpoint for staff route adapters.
 *
 * The customer canvas owns project/session/history/stream semantics (covered by
 * ChatPage.routeAuthority.test.tsx). These tests pin that the staff routes
 * select that same canvas rather than reintroducing a separate chat shell, and
 * that /sales/train stays the authenticated training surface while every
 * legacy /train entry normalizes onto /sales/train preserving the session.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const captured = vi.hoisted(() => ({
  chatProps: [] as Array<Record<string, unknown>>,
  redirects: [] as string[],
  throwRedirect: false,
}));

/** Mirrors the error Next's redirect() actually throws during render. */
const nextRedirectError = (url: string) =>
  Object.assign(new Error("NEXT_REDIRECT"), { digest: `NEXT_REDIRECT;replace;${url};307;` });

vi.mock("@/components/ChatPage", () => ({
  ChatPage: (props: Record<string, unknown>) => {
    captured.chatProps.push(props);
    return <main data-testid="shared-chat-canvas" />;
  },
}));
vi.mock("@/features/train/TrainWorkspace", () => ({
  TrainWorkspace: () => <main data-testid="training-workspace" />,
}));
vi.mock("next/navigation", () => ({
  redirect: (url: string) => {
    captured.redirects.push(url);
    if (captured.throwRedirect) throw nextRedirectError(url);
  },
}));

import SalesChatPage from "@/app/sales/chat/page";
import SalesTrainPage from "@/app/sales/train/page";
import LegacyTrainPage from "@/app/train/page";

describe("CHAT-CONTRACT staff route adapters", () => {
  afterEach(() => {
    cleanup();
    captured.chatProps = [];
    captured.redirects = [];
    captured.throwRedirect = false;
  });

  it("/sales/chat mounts the shared sales canvas once and leaves chrome to its layout", () => {
    render(<SalesChatPage />);

    expect(screen.getByTestId("shared-chat-canvas")).toBeTruthy();
    expect(captured.chatProps).toEqual([{
      mode: "sales",
      audience: "sales",
      shellOwned: true,
    }]);
    expect(screen.queryByRole("navigation", { name: "Điều hướng chính" })).toBeNull();
  });

  it("/sales/train mounts the authenticated training surface without redirecting", async () => {
    render(await SalesTrainPage({ searchParams: Promise.resolve({}) }));

    expect(screen.getByTestId("training-workspace")).toBeTruthy();
    expect(captured.redirects).toEqual([]);
    expect(captured.chatProps).toEqual([]);
    expect(screen.queryByRole("navigation", { name: "Điều hướng chính" })).toBeNull();
  });

  it("legacy /train redirects onto the authenticated training surface preserving the session", async () => {
    await LegacyTrainPage({
      searchParams: Promise.resolve({
        sessionId: "canonical-session",
        filter: ["open", "recent"],
        mode: "list",
      }),
    });
    await LegacyTrainPage({ searchParams: Promise.resolve({ session: "legacy-spelling" }) });
    await LegacyTrainPage({ searchParams: Promise.resolve({}) });

    expect(captured.redirects).toEqual([
      "/sales/train?sessionId=canonical-session",
      "/sales/train?sessionId=legacy-spelling",
      "/sales/train",
    ]);
  });
});

/**
 * SALES-SHELL: under Next's real semantics redirect() throws during render, so
 * a legacy training entry may normalize the URL but must never get far enough
 * to mount a chat canvas or any staff chrome (nav/UI/component canvas).
 */
describe("CHAT-CONTRACT legacy redirect-only mounts", () => {
  afterEach(() => {
    captured.chatProps = [];
    captured.redirects = [];
    captured.throwRedirect = false;
  });

  it("legacy /train throws before any canvas can mount", async () => {
    captured.throwRedirect = true;

    await expect(
      LegacyTrainPage({ searchParams: Promise.resolve({ sessionId: "sess-legacy-throw" }) })
    ).rejects.toThrow(/NEXT_REDIRECT/);

    expect(captured.redirects).toEqual(["/sales/train?sessionId=sess-legacy-throw"]);
    expect(captured.chatProps).toEqual([]);
  });
});
