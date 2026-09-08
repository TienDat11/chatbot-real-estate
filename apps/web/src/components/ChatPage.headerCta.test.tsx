// @vitest-environment jsdom
/**
 * FR-23 / FR-24 / R8-R9 (header lane) — presentation contract for the
 * ChatPage header block.
 *
 * Pinned here:
 *  1. The hot contact is an ACTIONABLE CTA for customers: a keyboard-focusable
 *     button ("Gọi tư vấn") that opens the existing LeadForm — not the old
 *     inert "09x xxx xxxx" badge.
 *  2. Staff surfaces never see customer lead-capture semantics (no CTA).
 *  3. Rendering drives no console logging of identity values (no PII logs):
 *     seeded session/device ids must never appear in any console output.
 * Project names here are synthetic ("Sunrise Test Bay") — no real project or
 * customer data is needed to verify the header contract.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/MessageList", () => ({ MessageList: () => <div data-testid="messages-stub" /> }));
vi.mock("@/components/Composer", () => ({
  Composer: () => <input aria-label="Câu hỏi" readOnly />,
}));
vi.mock("@/components/AccountControls", () => ({ AccountControls: () => <div data-testid="account-stub" /> }));
vi.mock("@/components/ChatHistoryDrawer", () => ({ ChatHistoryDrawer: () => null }));
vi.mock("@/features/chat/ProjectPicker", () => ({ ProjectPicker: () => null }));

// LeadForm double: flips `open` into the DOM so clicks can be asserted.
let leadFormOpen = false;
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: (props: { open: boolean }) => {
    leadFormOpen = props.open;
    return props.open ? <div data-testid="lead-form-open" /> : null;
  },
}));

// Role is injected per test via this mutable handle (staff vs customer).
let currentUserRole: string | null = null;
vi.mock("@/lib/AuthProvider", () => ({
  useOptionalAuth: () => ({
    user: currentUserRole ? { uid: "u_test", role: currentUserRole } : null,
  }),
}));

import { ChatPage } from "@/components/ChatPage";
import { PROJECT_KEY_STORAGE } from "@/features/chat/identity";

const SYNTHETIC_PROJECTS = {
  projects: [
    {
      project_key: "sunrise-test",
      name: "The Sunrise Test Bay - Đà Nẵng",
      is_hot: true,
      lat: 16.1052,
      lng: 108.2558,
    },
  ],
};

const PII_SESSION_ID = "pii-session-2b1f9e0a";
const PII_DEVICE_ID = "pii-device-77c04d13";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  // A stored choice skips the forced picker; hello latch suppresses greeting.
  window.localStorage.setItem(PROJECT_KEY_STORAGE, "sunrise-test");
  window.sessionStorage.setItem("ragre.hello_shown", "1");
  // Distinctive fake identity values used by the no-PII-logging assertions.
  window.sessionStorage.setItem("ragre.session_id", PII_SESSION_ID);
  window.localStorage.setItem("ragre.device_id", PII_DEVICE_ID);
  leadFormOpen = false;
  currentUserRole = null;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(SYNTHETIC_PROJECTS));
      return Promise.resolve(jsonResponse({}, 200));
    })
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("ChatPage header hot-contact CTA (FR-23/FR-24)", () => {
  it("renders an actionable keyboard-focusable CTA for customers that opens LeadForm", async () => {
    render(<ChatPage />);
    const cta = await screen.findByRole("button", { name: /Gọi tư vấn/i });
    expect(cta.getAttribute("aria-haspopup")).toBe("dialog");
    cta.focus();
    expect(document.activeElement).toBe(cta);

    fireEvent.click(cta);
    expect(leadFormOpen).toBe(true);
    expect(screen.getByTestId("lead-form-open")).toBeTruthy();

    // The placeholder phone-number badge is gone entirely.
    expect(screen.queryByText(/09x xxx xxxx/)).toBeNull();
  });

  it("never shows the customer lead-capture CTA on staff surfaces", async () => {
    currentUserRole = "sales";
    render(<ChatPage />);
    await screen.findByRole("status", { name: /Dự án đang tư vấn/i });
    expect(screen.queryByRole("button", { name: /Gọi tư vấn/i })).toBeNull();

    currentUserRole = "admin";
    cleanup();
    render(<ChatPage />);
    await screen.findByRole("status", { name: /Dự án đang tư vấn/i });
    expect(screen.queryByRole("button", { name: /Gọi tư vấn/i })).toBeNull();
  });

  it("logs no identity values (no PII) while mounting and opening the CTA", async () => {
    const spy = vi.spyOn(console, "log").mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const infoSpy = vi.spyOn(console, "info").mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    render(<ChatPage />);
    fireEvent.click(await screen.findByRole("button", { name: /Gọi tư vấn/i }));
    expect(leadFormOpen).toBe(true);

    for (const s of [spy, warnSpy, infoSpy, errorSpy]) {
      for (const call of s.mock.calls) {
        const joined = call.map(String).join(" ");
        expect(joined).not.toContain(PII_SESSION_ID);
        expect(joined).not.toContain(PII_DEVICE_ID);
        expect(joined).not.toMatch(/\b09\d{8}\b/); // phone-like strings
      }
    }
    spy.mockRestore();
    warnSpy.mockRestore();
    infoSpy.mockRestore();
    errorSpy.mockRestore();
  });
});
