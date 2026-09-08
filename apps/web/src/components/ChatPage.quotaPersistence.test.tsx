// @vitest-environment jsdom
/**
 * R5 (FR-20 + G3-r4 amendment) pins: the quota wall keeps an accessible
 * LeadForm entry point AND its persisted snapshot is keyed by BOTH a
 * non-empty anon token AND a non-empty project_key.
 *
 * Pinned here:
 *  1. Pure helper matrix: empty-token write skipped, empty-project write
 *     skipped, token mismatch discarded, project mismatch discarded,
 *     happy-path restore, payoff clear (no shared empty sentinel anywhere).
 *  2. The wall renders an always-present, keyboard-focusable, labeled button
 *     that opens the LeadForm — including after a simulated reload from a
 *     keyed snapshot, before any server round trip.
 *  3. A granted lead bonus clears the persisted row: reload is NOT walled.
 *  4. Staff identities (sales/admin = staff/training surfaces) never see the
 *     wall/button even when an exhaustion mark exists for their token/project.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

const routerMock = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }));
const authMock = vi.hoisted(() => ({ role: undefined as string | undefined }));

vi.mock("next/navigation", () => ({ useRouter: () => routerMock }));
vi.mock("@/lib/AuthProvider", () => ({
  useOptionalAuth: () =>
    authMock.role ? { user: { role: authMock.role } } : undefined,
}));

vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/MessageList", () => ({ MessageList: () => <div data-testid="messages-stub" /> }));
vi.mock("@/components/Composer", () => ({
  Composer: (props: { disabled?: boolean }) => (
    <input aria-label="Câu hỏi" disabled={props.disabled} readOnly />
  ),
}));

let leadFormOpen = false;
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: (props: { open: boolean }) => {
    leadFormOpen = props.open;
    return props.open ? <div data-testid="lead-form-stub">lead form</div> : null;
  },
}));

import {
  ChatPage,
  QUOTA_EXHAUSTED_STORAGE_KEY,
  clearQuotaExhaustedMark,
  persistQuotaExhaustedMark,
  wasQuotaExhaustedMarked,
} from "@/components/ChatPage";
import { ANON_TOKEN_KEY } from "@/features/chat/identity";

function makeStorage() {
  const store = new Map<string, string>();
  return {
    storage: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
    },
    peek: () => store.get(QUOTA_EXHAUSTED_STORAGE_KEY),
  };
}

describe("quota mark helpers (pure, FR-20 G3-r4 keying)", () => {
  it("empty-token and empty-project writes are skipped entirely (no sentinel row)", () => {
    const { storage, peek } = makeStorage();
    // Absent key degrades to pre-wave behavior.
    expect(wasQuotaExhaustedMarked(storage, null, null)).toBe(false);
    // Every empty-half combination must SKIP the write — no shared sentinel.
    for (const [token, project] of [
      ["", "camellia"],
      ["tok-a", ""],
      ["", ""],
    ] as const) {
      persistQuotaExhaustedMark(storage, token, project);
      expect(peek()).toBeUndefined();
      expect(wasQuotaExhaustedMarked(storage, token, project)).toBe(false);
    }
  });

  it("keys by token+project; any half-mismatch is discarded", () => {
    const { storage } = makeStorage();
    persistQuotaExhaustedMark(storage, "tok-a", "camellia");
    // Happy path: exact pair restores.
    expect(wasQuotaExhaustedMarked(storage, "tok-a", "camellia")).toBe(true);
    // Token mismatch: another identity does not inherit the wall.
    expect(wasQuotaExhaustedMarked(storage, "tok-b", "camellia")).toBe(false);
    // Project mismatch: the same identity on another project stays unwalled.
    expect(wasQuotaExhaustedMarked(storage, "tok-a", "soleil")).toBe(false);
    // Missing halves restore nothing.
    expect(wasQuotaExhaustedMarked(storage, "tok-a", null)).toBe(false);
    expect(wasQuotaExhaustedMarked(storage, null, "camellia")).toBe(false);
  });

  it("legacy/unreadable rows read as no mark (server re-walls instead)", () => {
    const { storage } = makeStorage();
    // Pre-amendment token-only format and arbitrary garbage.
    for (const legacy of ["tok_reload_1", "{not json"]) {
      storage.setItem(QUOTA_EXHAUSTED_STORAGE_KEY, legacy);
      expect(wasQuotaExhaustedMarked(storage, "tok_reload_1", "camellia")).toBe(false);
    }
  });

  it("clear removes the row so no identity restores the wall afterwards", () => {
    const { storage, peek } = makeStorage();
    persistQuotaExhaustedMark(storage, "tok-a", "camellia");
    expect(peek()).not.toBeUndefined();
    clearQuotaExhaustedMark(storage);
    expect(peek()).toBeUndefined();
    expect(wasQuotaExhaustedMarked(storage, "tok-a", "camellia")).toBe(false);
  });
});

describe("ChatPage persistent quota wall (FR-20)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    window.localStorage.setItem("ragre.project_key", "camellia");
    authMock.role = undefined;
    leadFormOpen = false;
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  function stubProjectFetch(): void {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/projects")) {
        return Promise.resolve(new Response(JSON.stringify({ projects: [
          { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
        ] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } }));
    }));
  }

  it("restores the wall + LeadForm button after a simulated reload keyed by token+project", async () => {
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_reload_1");
    persistQuotaExhaustedMark(window.localStorage, "tok_reload_1", "camellia");
    stubProjectFetch();

    render(<ChatPage />);
    // Wall present WITHOUT any query/429 round trip (snapshot hydration).
    const wallButton = await screen.findByRole("button", { name: /Để lại số điện thoại nhận thêm lượt tư vấn/i });
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toMatch(/hết lượt tư vấn miễn phí/i);

    // The button opens the existing LeadForm and survives double activation.
    fireEvent.click(wallButton);
    fireEvent.click(screen.getByRole("button", { name: /Để lại số điện thoại nhận thêm lượt tư vấn/i }));
    expect(leadFormOpen).toBe(true);
  });

  it("a mark for ANOTHER PROJECT does not wall this one (keying enforced at mount)", () => {
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_now");
    persistQuotaExhaustedMark(window.localStorage, "tok_now", "soleil");
    stubProjectFetch();
    render(<ChatPage />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false);
    expect(screen.queryByRole("button", { name: /Để lại số điện thoại nhận thêm lượt tư vấn/i })).toBeNull();
  });

  it("a token mismatch or cleared mark degrades to no restored wall", () => {
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_now");
    persistQuotaExhaustedMark(window.localStorage, "tok_other", "camellia");
    stubProjectFetch();
    render(<ChatPage />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false);
    expect(screen.queryByRole("button", { name: /Để lại số điện thoại nhận thêm lượt tư vấn/i })).toBeNull();
  });

  it.each(["sales", "admin"] as const)(
    "%s staff never sees the wall even when an exhaustion mark exists for their token",
    (role) => {
      authMock.role = role;
      window.localStorage.setItem(ANON_TOKEN_KEY, "tok_staff_1");
      persistQuotaExhaustedMark(window.localStorage, "tok_staff_1", "camellia");
      stubProjectFetch();

      render(<ChatPage audience={role === "sales" ? "sales" : "customer"} />);
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.queryByRole("button", { name: /Để lại số điện thoại nhận thêm lượt tư vấn/i })).toBeNull();
      expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false);
    }
  );
});
