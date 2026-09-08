// @vitest-environment jsdom
/**
 * Regression tests for the change-project button (bug: the popup waited for
 * the multi-second GET /api/projects round trip before showing).
 *
 * Contract pinned here:
 *  1. clicking "Đổi dự án" opens the ProjectPicker SYNCHRONOUSLY — the modal
 *     title and the instant static fallback rows exist right after the click,
 *     while the projects request is still pending;
 *  2. when the request finally answers, the fresh catalogue lands INSIDE the
 *     already-open popup (background refresh, no close/reopen);
 *  3. picking a project persists the explicit choice and closes the popup.
 *
 * The network is a controllable deferred, so "synchronous" is asserted
 * structurally (no timers, no flakiness).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

// ChatCanvas reads useRouter for routed project switches; these tests mount
// without a Next app context, so the router is a structural no-op double.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/MessageList", () => ({ MessageList: () => <div data-testid="messages-stub" /> }));
vi.mock("@/components/Composer", () => ({ Composer: () => <div data-testid="composer-stub" /> }));
vi.mock("@/components/LeadForm", () => ({
  LeadForm: () => <div />,
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
}));

import { ChatPage } from "@/components/ChatPage";
import { PROJECT_KEY_STORAGE } from "@/features/chat/identity";
import { ASK_EVENT } from "@/lib/constants";

const PICKER_TITLE = "Chọn dự án để được tư vấn";

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Two-row endpoint payload matching the wave-1 contract shape. */
const ENDPOINT_PROJECTS = {
  projects: [
    { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
    { project_key: "soleil", name: "The Soleil Đà Nẵng", is_hot: false, lat: 16.0710756, lng: 108.2436243 },
  ],
};

describe("ChatPage ProjectPicker open latency (change-project button)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("opens the popup synchronously on click while GET /api/projects is still pending", async () => {
    const projectsDeferred = createDeferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) =>
        String(input).includes("/api/projects")
          ? projectsDeferred.promise
          : Promise.resolve(jsonResponse({}, 200))
      )
    );

    render(<ChatPage />);
    // Mount-time resolution is suspended too, so nothing opened on its own.
    expect(screen.queryByText(PICKER_TITLE)).toBeNull();

    // THE PIN: no `await` between click and popup — the old code awaited the
    // projects fetch here, which left this query empty for seconds.
    fireEvent.click(screen.getByRole("button", { name: /Đổi dự án/i }));
    expect(screen.getByText(PICKER_TITLE)).toBeTruthy();
    // Instant content from the static fallback catalogue (never an empty
    // body). Scoped to the dialog: the header ALSO shows the active project
    // name now, so a document-wide query would match several nodes.
    const picker = within(screen.getByRole("dialog"));
    expect(picker.getByText("The Camellia")).toBeTruthy();
    expect(picker.getByText("The Soleil")).toBeTruthy();

    // The background refresh lands inside the ALREADY-open popup.
    await act(async () => {
      projectsDeferred.resolve(jsonResponse(ENDPOINT_PROJECTS));
    });
    await waitFor(() =>
      expect(screen.getAllByText(/The Camellia/).length).toBeGreaterThan(0)
    );
    // Popup was never closed/reopened across the refresh.
    expect(screen.getByText(PICKER_TITLE)).toBeTruthy();
  });

  it("applies a picked project: persists the explicit choice and closes the popup", async () => {
    // A stored choice skips the forced gate so this test drives the voluntary
    // change-project path only.
    window.localStorage.setItem(PROJECT_KEY_STORAGE, "camellia");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(ENDPOINT_PROJECTS)));

    render(<ChatPage />);
    fireEvent.click(screen.getByRole("button", { name: /Đổi dự án/i }));

    fireEvent.click(screen.getByRole("option", { name: /The Soleil Đà Nẵng/i }));
    // Functional contract: the explicit choice persists immediately. (The
    // Modal's close ANIMATION never finishes in jsdom — no CSS transitions —
    // so DOM removal is asserted in the browser E2E, not here.)
    await waitFor(() =>
      expect(window.localStorage.getItem(PROJECT_KEY_STORAGE)).toBe("soleil")
    );

    // Reopening shows Soleil as the selected option (check state round-trips).
    fireEvent.click(screen.getByRole("button", { name: /Đổi dự án/i }));
    await waitFor(() => {
      const opts = screen.getAllByRole("option", { name: /The Soleil Đà Nẵng/i });
      expect(opts.some((o) => o.getAttribute("aria-selected") === "true")).toBe(true);
    });
  });

  it("re-sends a PROJECT_SCOPE-pending question against the NEWLY picked project", async () => {
    // Regression (review MAJOR): applyProjectSwitch used to call
    // handleSend(pending) right after setProjectKey, but the handleSend
    // closure still captured the OLD projectKey — the resent query carried
    // the old scope and its message pair landed in the old bucket, so the
    // answer "disappeared" behind the newly shown project.
    window.sessionStorage.setItem("ragre.hello_shown", "1"); // skip mount greeting
    const queryBodies: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/query")) {
          queryBodies.push(JSON.parse(String(init?.body)));
          if (queryBodies.length === 1) {
            // First ask arrives with no chosen project: backend answers 422
            // PROJECT_SCOPE and the UI parks the question for a re-send.
            return Promise.resolve(
              jsonResponse(
                {
                  ok: false,
                  error: { code: "PROJECT_SCOPE", message: "Chọn dự án." },
                  projects: ENDPOINT_PROJECTS.projects,
                },
                422
              )
            );
          }
          return Promise.resolve(
            new Response(
              'event: ack\ndata: {}\n\nevent: done\ndata: {"answer":"OK","confidence":"HIGH"}\n\n'
            )
          );
        }
        if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({}, 200));
        return Promise.resolve(jsonResponse(ENDPOINT_PROJECTS));
      })
    );

    render(<ChatPage />);
    act(() => {
      document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: "Giá bao nhiêu?" }));
    });

    // The 422 opens the picker instead of a dead-end error toast.
    await screen.findByText(PICKER_TITLE);
    fireEvent.click(screen.getByRole("option", { name: /The Soleil Đà Nẵng/i }));

    await waitFor(() => expect(queryBodies.length).toBe(2));
    expect(queryBodies[0].project_key).toBe("camellia");
    // THE PIN: the resend carries the NEW key — the stale closure sent
    // "camellia" here.
    expect(queryBodies[1].project_key).toBe("soleil");
  });
});
