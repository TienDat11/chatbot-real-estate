// @vitest-environment jsdom
/**
 * Contract tests for the canonical sales CRM chat route.
 *
 * These intentionally specify the route boundary without calling an LLM. The
 * implementation must keep ChatPage as the only canvas and let the persistent
 * sales layout own the shell/header.
 */
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const captured = vi.hoisted(() => ({
  chatProps: [] as Array<Record<string, unknown>>,
  redirects: [] as string[],
}));

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
  redirect: (url: string) => captured.redirects.push(url),
}));

import SalesChatPage from "@/app/sales/chat/page";
import SalesTrainPage from "@/app/sales/train/page";
import LegacyTrainPage from "@/app/train/page";

type RouteInput = {
  params: Promise<{ projectKey: string }>;
  searchParams: Promise<{ sessionId?: string; session?: string }>;
};

const routeInput = (projectKey: string, sessionId?: string): RouteInput => ({
  params: Promise.resolve({ projectKey }),
  searchParams: Promise.resolve(sessionId ? { sessionId } : {}),
});

describe("canonical sales chat route contract", () => {
  afterEach(() => {
    cleanup();
    captured.chatProps = [];
    captured.redirects = [];
  });

  it("mounts /sales/chat/project/{projectKey} with route project and canonical sessionId", async () => {
    // Route adapters must accept the same Next route context as the dynamic
    // canonical page, while keeping the shared ChatPage canvas.
    render(await (SalesChatPage as unknown as (input: RouteInput) => Promise<ReactElement>)(routeInput("soleil", "sess-42")));

    expect(screen.getByTestId("shared-chat-canvas")).toBeTruthy();
    expect(captured.chatProps).toEqual([{
      routeProjectKey: "soleil",
      sessionId: "sess-42",
      mode: "sales",
      audience: "sales",
      shellOwned: true,
    }]);
    expect(screen.queryByRole("navigation", { name: "Điều hướng chính" })).toBeNull();
  });

  it("keeps project switcher and history on the shared sales canvas", async () => {
    render(await (SalesChatPage as unknown as (input: RouteInput) => Promise<ReactElement>)(routeInput("camellia")));

    const props = captured.chatProps[0];
    expect(props).toMatchObject({ mode: "sales", audience: "sales", shellOwned: true });
    expect(props).not.toHaveProperty("trainingUi");
    expect(props).not.toHaveProperty("standaloneShell");
  });

  it.each([
    ["/sales/chat", SalesChatPage],
  ])("defines a canonical redirect preserving project and session for %s", async (_source, Page) => {
    await (Page as unknown as (input: RouteInput) => Promise<ReactElement | void>)(routeInput("soleil", "sess-redirect"));

    expect(captured.redirects).toContain("/sales/chat/project/soleil?sessionId=sess-redirect");
  });

  it("redirects legacy /train onto the authenticated training surface preserving the session", async () => {
    await (LegacyTrainPage as unknown as (input: RouteInput) => Promise<ReactElement | void>)(routeInput("soleil", "sess-redirect"));

    expect(captured.redirects).toEqual(["/sales/train?sessionId=sess-redirect"]);
  });

  it("keeps /sales/train on the authenticated training surface, not the shared sales canvas", async () => {
    render(await (SalesTrainPage as unknown as (input: RouteInput) => Promise<ReactElement>)(routeInput("soleil", "sess-training")));

    expect(screen.getByTestId("training-workspace")).toBeTruthy();
    expect(captured.chatProps).toEqual([]);
    expect(captured.redirects).toEqual([]);
  });
});
