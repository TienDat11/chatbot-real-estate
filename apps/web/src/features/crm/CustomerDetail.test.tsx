// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CustomerDetail } from "./CustomerDetail";

const searchCustomerByPhone = vi.fn();
const fetchRevealedPhoneNumber = vi.fn();
const notifyCallStarted = vi.fn();
const updateLeadStatus = vi.fn();
const withdrawMarketingConsent = vi.fn();
const getFreshIdToken = vi.fn(async () => "fresh-token");
const TEST_PHONE = "0000000000";
const MASKED_PHONE = "00******00";
vi.mock("./crmApiClient", () => ({
  CrmApiClientError: class extends Error {},
  searchCustomerByPhone: (...args: unknown[]) => searchCustomerByPhone(...args),
  fetchRevealedPhoneNumber: (...args: unknown[]) => fetchRevealedPhoneNumber(...args),
  notifyCallStarted: (...args: unknown[]) => notifyCallStarted(...args),
  updateLeadStatus: (...args: unknown[]) => updateLeadStatus(...args),
  withdrawMarketingConsent: (...args: unknown[]) => withdrawMarketingConsent(...args),
}));
vi.mock("./LeadConversationPanel", () => ({ LeadConversationPanel: () => null }));
vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  getFreshIdToken: () => getFreshIdToken(),
}));
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    App: { useApp: () => ({ message: { error: vi.fn(), success: vi.fn(), warning: vi.fn() } }) },
  };
});

const lead = {
  id: "lead-1",
  leadId: 1,
  deviceId: null,
  name: "Khách hàng",
  maskedPhone: MASKED_PHONE,
  note: null,
  projectKey: "camellia",
  budgetVnd: null,
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
  workflowStatus: "new" as const,
  rejectionReason: null,
  reengageAt: null,
  marketingWithdrawnAt: null,
  consentFlags: { consentService: true, consentMarketing: true },
  assignedSalesId: null,
  assignedSalesFirebaseUid: null,
  escalCount: 0,
  closedAt: null,
};
const props = (bearerToken: string | null) => ({
  open: true,
  anchorLead: lead,
  customerLeads: [lead],
  bearerToken,
  currentStaffUid: "staff-1",
  noteStore: { readLeadNote: vi.fn(async () => null), saveLeadNote: vi.fn(async () => undefined) },
  onOptimisticLeadPatch: vi.fn(),
  onClearOptimisticLeadPatch: vi.fn(),
  onClose: vi.fn(),
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

async function searchAndReveal(): Promise<void> {
  searchCustomerByPhone.mockResolvedValue({ customerId: "customer-1", leads: [] });
  fetchRevealedPhoneNumber.mockResolvedValue(TEST_PHONE);
  const search = screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng");
  await waitFor(() => expect((search as HTMLInputElement).value).toBe(""));
  fireEvent.change(search, { target: { value: TEST_PHONE } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
  });
  const reveal = await screen.findByRole("button", { name: "Hiện số đầy đủ" });
  fireEvent.click(reveal);
  await screen.findByText(TEST_PHONE);
}

describe("CustomerDetail phone reveal toggle", () => {
  it("reveals through an icon button with a Vietnamese accessible name, then conceals without any API call", async () => {
    render(<CustomerDetail {...props("token-a")} />);

    // Before authorization: masked number stays visible, reveal button exists
    // as an icon-only button whose accessible name is the Vietnamese action.
    expect(screen.getByText(MASKED_PHONE)).toBeTruthy();
    const revealButton = screen.getByRole("button", { name: "Hiện số đầy đủ" });
    expect(revealButton.getAttribute("aria-label")).toBe("Hiện số đầy đủ");
    expect(revealButton.getAttribute("title")).toBe("Hiện số điện thoại đầy đủ");
    expect(revealButton.querySelector(".anticon-eye")).not.toBeNull();

    await searchAndReveal();

    // After the authorized reveal: plaintext + conceal affordance.
    expect(screen.getByText(TEST_PHONE)).toBeTruthy();
    const concealButton = screen.getByRole("button", { name: "Ẩn số điện thoại" });
    expect(concealButton.getAttribute("title")).toBe("Ẩn số điện thoại khách hàng");
    expect(concealButton.querySelector(".anticon-eye-invisible")).not.toBeNull();

    const revealCallsBefore = fetchRevealedPhoneNumber.mock.calls.length;
    fireEvent.click(concealButton);

    // Conceal removes the plaintext without any API round-trip.
    expect(screen.queryByText(TEST_PHONE)).toBeNull();
    expect(screen.getByText(MASKED_PHONE)).toBeTruthy();
    expect(fetchRevealedPhoneNumber.mock.calls.length).toBe(revealCallsBefore);
    // The reveal control is back for a later authorized re-reveal.
    expect(screen.getByRole("button", { name: "Hiện số đầy đủ" })).toBeTruthy();
  });

  it("conveys reveal state by the eye icon alone: no purple pin, compact copy icon, stable single-line row", async () => {
    render(<CustomerDetail {...props("token-a")} />);
    await searchAndReveal();

    // Req 1: the "Đã hiển thị đầy đủ" pin is gone — the eye icon/aria is the
    // sole reveal indicator, so it can no longer push the copy action outward.
    expect(screen.queryByText("Đã hiển thị đầy đủ")).toBeNull();
    expect(screen.queryByText(/hiển thị đầy đủ/i)).toBeNull();

    // Req 1/2: copy is a compact icon button (no wide text label reflowing the row).
    const copyButton = screen.getByRole("button", { name: "Sao chép số điện thoại" });
    expect(copyButton.querySelector(".anticon-copy")).not.toBeNull();

    // Req 2: the revealed row is the same single non-wrapping flex line as the
    // masked row, so toggling reveal cannot change its height.
    const revealedText = screen.getByText(TEST_PHONE);
    const revealedRow = revealedText.parentElement as HTMLElement;
    expect(revealedRow.style.display).toBe("flex");
    expect(revealedRow.style.flexWrap).toBe("nowrap");
    expect(revealedText.style.whiteSpace).toBe("nowrap");
    expect(revealedText.style.textOverflow).toBe("ellipsis");
  });

  it("keeps the reveal icon button in a loading state while the authorized request is pending and shows no plaintext before success", async () => {
    let resolveReveal!: (phone: string) => void;
    searchCustomerByPhone.mockResolvedValue({ customerId: "customer-1", leads: [] });
    fetchRevealedPhoneNumber.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveReveal = resolve; })
    );
    render(<CustomerDetail {...props("token-a")} />);

    const search = screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng");
    fireEvent.change(search, { target: { value: TEST_PHONE } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
    });
    const reveal = await screen.findByRole("button", { name: "Hiện số đầy đủ" });
    fireEvent.click(reveal);

    // Pending: antd swaps the icon for a spinner, no plaintext anywhere.
    await waitFor(() =>
      expect(reveal.querySelector(".anticon-loading")).not.toBeNull()
    );
    expect(screen.queryByText(TEST_PHONE)).toBeNull();

    resolveReveal(TEST_PHONE);
    await screen.findByText(TEST_PHONE);
    expect(screen.getByRole("button", { name: "Ẩn số điện thoại" })).toBeTruthy();
  });

  it("keeps the masked row on a single non-wrapping line with the actions inline", () => {
    render(<CustomerDetail {...props("token-a")} />);

    const maskedText = screen.getByText(MASKED_PHONE);
    // The masked number itself carries the nowrap/ellipsis truncation; its
    // flex row parent forbids wrapping so actions stay inline at desktop.
    expect(maskedText.style.whiteSpace).toBe("nowrap");
    expect(maskedText.style.overflow).toBe("hidden");
    expect(maskedText.style.textOverflow).toBe("ellipsis");
    const row = maskedText.parentElement as HTMLElement;
    expect(row.style.flexWrap).toBe("nowrap");
    expect(row.style.display).toBe("flex");
  });

  it("clears the revealed phone when concealed and auth is then lost (no persistence path)", async () => {
    const view = render(<CustomerDetail {...props("token-a")} />);
    await searchAndReveal();

    fireEvent.click(screen.getByRole("button", { name: "Ẩn số điện thoại" }));
    expect(screen.queryByText(TEST_PHONE)).toBeNull();

    // Losing auth while concealed must not resurrect the plaintext.
    view.rerender(<CustomerDetail {...props(null)} />);
    expect(screen.queryByText(TEST_PHONE)).toBeNull();
    expect(screen.getByText(MASKED_PHONE)).toBeTruthy();
  });
});

describe("CustomerDetail auth privacy", () => {
  it("starts with the table-derived phone masked and does not expose a call or copy action", () => {
    render(<CustomerDetail {...props("token-a")} />);

    expect(screen.getByText(MASKED_PHONE)).toBeTruthy();
    expect(screen.queryByText(TEST_PHONE)).toBeNull();
    expect(screen.queryByRole("link", { name: "Gọi số điện thoại khách hàng" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Sao chép số điện thoại" })).toBeNull();
    expect(screen.getByRole("button", { name: "Hiện số đầy đủ" }).hasAttribute("disabled")).toBe(true);
  });

  it("blocks CRM search without firing the endpoint when a fresh bearer cannot be minted", async () => {
    getFreshIdToken.mockRejectedValueOnce(new Error("signed out"));
    render(<CustomerDetail {...props("token-a")} />);

    const search = screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng");
    fireEvent.change(search, { target: { value: TEST_PHONE } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
    });

    expect(searchCustomerByPhone).not.toHaveBeenCalled();
  });

  it("sends the call-started side effect only with a freshly minted bearer", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    // The prop carries a DIFFERENT token than the mint: a "token-a" header on
    // the side effect would prove the stale replay we just removed.
    render(<CustomerDetail {...props("token-a")} />);

    await searchAndReveal();

    fireEvent.click(screen.getByRole("link", { name: "Gọi số điện thoại khách hàng" }));

    await waitFor(() =>
      expect(notifyCallStarted).toHaveBeenCalledWith({
        leadId: 1,
        bearerToken: "fresh-token",
      })
    );
    expect(screen.getByRole("link", { name: "Gọi số điện thoại khách hàng" }).getAttribute("href")).toBe(`tel:${TEST_PHONE}`);
  });

  it("reveals only after the authorized endpoint and call/copy use the returned number", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<CustomerDetail {...props("token-a")} />);

    await searchAndReveal();

    expect(fetchRevealedPhoneNumber).toHaveBeenCalledWith({
      customerId: "customer-1",
      bearerToken: "fresh-token",
    });
    expect(screen.getByRole("link", { name: "Gọi số điện thoại khách hàng" }).getAttribute("href")).toBe(`tel:${TEST_PHONE}`);
    fireEvent.click(screen.getByRole("button", { name: "Sao chép số điện thoại" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(TEST_PHONE));
  });

  it("enables assigned lead reveal from its opaque identity without a phone lookup", async () => {
    const writeText = vi.fn(async () => undefined);
    const opaqueLeadIdentity = "opaque-customer-hmac-1";
    const assignedLead = {
      ...lead,
      id: opaqueLeadIdentity,
      workflowStatus: "assigned" as const,
      assignedSalesId: 42,
      assignedSalesFirebaseUid: "staff-1",
    };
    const revealedPhone = TEST_PHONE;
    fetchRevealedPhoneNumber.mockResolvedValue(revealedPhone);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    render(
      <CustomerDetail
        {...props("token-a")}
        anchorLead={assignedLead}
        customerLeads={[assignedLead]}
      />
    );

    const reveal = screen.getByRole("button", { name: "Hiện số đầy đủ" });
    expect(reveal.hasAttribute("disabled")).toBe(false);
    expect(searchCustomerByPhone).not.toHaveBeenCalled();

    fireEvent.click(reveal);

    await waitFor(() =>
      expect(fetchRevealedPhoneNumber).toHaveBeenCalledWith({
        customerId: opaqueLeadIdentity,
        bearerToken: "fresh-token",
      })
    );
    expect(screen.getByRole("link", { name: "Gọi số điện thoại khách hàng" }).getAttribute("href")).toBe(`tel:${revealedPhone}`);
    fireEvent.click(screen.getByRole("button", { name: "Sao chép số điện thoại" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(revealedPhone));
  });

  it("keeps the table-derived phone masked when authorized reveal is denied", async () => {
    searchCustomerByPhone.mockResolvedValue({ customerId: "customer-1", leads: [] });
    fetchRevealedPhoneNumber.mockRejectedValue(new Error("forbidden"));
    render(<CustomerDetail {...props("token-a")} />);

    const search = screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng");
    await waitFor(() => expect((search as HTMLInputElement).value).toBe(""));
    fireEvent.change(search, { target: { value: TEST_PHONE } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
    });
    const reveal = await screen.findByRole("button", { name: "Hiện số đầy đủ" });
    fireEvent.click(reveal);
    await waitFor(() => expect(fetchRevealedPhoneNumber).toHaveBeenCalled());

    expect(screen.queryByText(TEST_PHONE)).toBeNull();
    expect(screen.getByText(MASKED_PHONE)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Gọi số điện thoại khách hàng" })).toBeNull();
  });

  it("clears a revealed phone immediately when bearer auth is lost", async () => {
    searchCustomerByPhone.mockResolvedValue({ customerId: "customer-1", leads: [] });
    fetchRevealedPhoneNumber.mockResolvedValue(TEST_PHONE);
    const view = render(<CustomerDetail {...props("token-a")} />);

    await waitFor(() => expect((screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng") as HTMLInputElement).value).toBe(""));
    fireEvent.change(screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng"), { target: { value: TEST_PHONE } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Hiện số đầy đủ" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Hiện số đầy đủ" }));
    await waitFor(() => expect(screen.getByText(TEST_PHONE)).toBeTruthy());

    view.rerender(<CustomerDetail {...props(null)} />);
    expect(screen.queryByText(TEST_PHONE)).toBeNull();
    expect(screen.getByText(MASKED_PHONE)).toBeTruthy();
  });

  it("does not render a pending reveal after bearer auth is lost", async () => {
    searchCustomerByPhone.mockResolvedValue({ customerId: "customer-1", leads: [] });
    let resolveReveal!: (phone: string) => void;
    fetchRevealedPhoneNumber.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveReveal = resolve; })
    );
    const view = render(<CustomerDetail {...props("token-a")} />);

    await waitFor(() => expect((screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng") as HTMLInputElement).value).toBe(""));
    fireEvent.change(screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng"), { target: { value: TEST_PHONE } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Hiện số đầy đủ" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Hiện số đầy đủ" }));
    await waitFor(() => expect(fetchRevealedPhoneNumber).toHaveBeenCalled());

    view.rerender(<CustomerDetail {...props(null)} />);
    resolveReveal(TEST_PHONE);
    await act(async () => {});

    expect(screen.queryByText(TEST_PHONE)).toBeNull();
    expect(screen.getByText(MASKED_PHONE)).toBeTruthy();
  });

  it("does not restore a raw phone when the staff UID changes while reveal is in flight", async () => {
    searchCustomerByPhone.mockResolvedValue({ customerId: "customer-1", leads: [] });
    let resolveReveal!: (phone: string) => void;
    fetchRevealedPhoneNumber.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveReveal = resolve; })
    );
    const view = render(<CustomerDetail {...props("token-a")} />);
    const search = screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng");
    await waitFor(() => expect((search as HTMLInputElement).value).toBe(""));
    fireEvent.change(search, { target: { value: TEST_PHONE } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
    });
    fireEvent.click(await screen.findByRole("button", { name: "Hiện số đầy đủ" }));
    await waitFor(() => expect(fetchRevealedPhoneNumber).toHaveBeenCalled());

    view.rerender(<CustomerDetail {...{ ...props("token-a"), currentStaffUid: "staff-2" }} />);
    resolveReveal(TEST_PHONE);
    await act(async () => {});

    expect(screen.queryByText(TEST_PHONE)).toBeNull();
    expect(screen.getByText(MASKED_PHONE)).toBeTruthy();
  });

  it("does not restore a raw phone after the selected lead changes while reveal is in flight", async () => {
    searchCustomerByPhone.mockResolvedValue({ customerId: "customer-1", leads: [] });
    let resolveReveal!: (phone: string) => void;
    fetchRevealedPhoneNumber.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveReveal = resolve; })
    );
    const view = render(<CustomerDetail {...props("token-a")} />);
    const search = screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng");
    await waitFor(() => expect((search as HTMLInputElement).value).toBe(""));
    fireEvent.change(search, { target: { value: TEST_PHONE } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
    });
    fireEvent.click(await screen.findByRole("button", { name: "Hiện số đầy đủ" }));
    await waitFor(() => expect(fetchRevealedPhoneNumber).toHaveBeenCalled());
    const nextLead = { ...lead, id: "lead-2", maskedPhone: "11******11" };

    view.rerender(<CustomerDetail {...{ ...props("token-a"), anchorLead: nextLead, customerLeads: [nextLead] }} />);
    resolveReveal(TEST_PHONE);
    await act(async () => {});

    expect(screen.queryByText(TEST_PHONE)).toBeNull();
    expect(screen.getByText("11******11")).toBeTruthy();
  });

  it("rejects a rapid second reveal while the first is still minting its token", async () => {
    // Regression (reviewer): the old guard read phoneAction state, which only
    // flips AFTER the async mint — a double-click passed the guard twice and
    // started two reveals. The lock is now a ref set synchronously.
    searchCustomerByPhone.mockResolvedValue({ customerId: "customer-1", leads: [] });
    render(<CustomerDetail {...props("token-a")} />);

    const search = screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng");
    await waitFor(() => expect((search as HTMLInputElement).value).toBe(""));
    fireEvent.change(search, { target: { value: TEST_PHONE } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
    });
    // The lookup minted its own token (default mock); gate the reveal mint.
    await waitFor(() => expect(getFreshIdToken).toHaveBeenCalledTimes(1));
    let resolveMint!: (token: string) => void;
    getFreshIdToken.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveMint = resolve; })
    );

    const reveal = await screen.findByRole("button", { name: "Hiện số đầy đủ" });
    fireEvent.click(reveal);
    await waitFor(() => expect(getFreshIdToken).toHaveBeenCalledTimes(2));
    // Second click lands while the first mint is still pending.
    fireEvent.click(reveal);

    resolveMint("fresh-token");
    await screen.findByText(TEST_PHONE);

    expect(fetchRevealedPhoneNumber).toHaveBeenCalledTimes(1);
  });

  it("rejects a rapid second status activation while the first is still minting its token", async () => {
    // Regression (reviewer): two rapid activations issued two PATCHes and the
    // first failure's optimistic rollback could wipe the second's state. The
    // in-flight lock is now acquired synchronously before the mint await.
    updateLeadStatus.mockResolvedValue(undefined);
    let resolveMint!: (token: string) => void;
    getFreshIdToken.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveMint = resolve; })
    );
    const p = props("token-a");
    render(<CustomerDetail {...p} />);

    // Real (clickable) options live in the virtual list; [role="option"] also
    // matches hidden a11y measurement rows (children = raw values), so target
    // the option-content element and let the click bubble to its wrapper.
    const pickOption = async (label: string) => {
      fireEvent.mouseDown(document.querySelector(".ant-select-selector")!);
      const content = await screen.findByText(label, {
        selector: ".ant-select-item-option-content",
      });
      fireEvent.click(content.closest(".ant-select-item-option") ?? content);
    };
    await pickOption("Đã gọi");
    await waitFor(() => expect(getFreshIdToken).toHaveBeenCalledTimes(1));
    // Second activation while the first mint is still pending.
    await pickOption("Gọi lại sau");
    resolveMint("fresh-token");
    await waitFor(() => expect(updateLeadStatus).toHaveBeenCalledTimes(1));

    // Exactly one PATCH, one optimistic patch application.
    expect(updateLeadStatus).toHaveBeenCalledWith(
      expect.objectContaining({ leadId: "lead-1", status: "called" })
    );
    expect(p.onOptimisticLeadPatch).toHaveBeenCalledTimes(1);
  });
});

describe("CustomerDetail direct reveal for owner-scoped rows", () => {
  it("enables the eye for a REST-backed row carrying the backend customer identity and reveals via the authorized endpoint", async () => {
    fetchRevealedPhoneNumber.mockResolvedValue(TEST_PHONE);
    // A REST /leads row: the listing is server-scoped to the caller's own
    // leads and its id already IS the customer HMAC the reveal route keys by,
    // but the wire carries no isolation uid — the eye must still be enabled
    // for the assigned sales (E2E regression: button stuck disabled).
    const restLead = { ...lead, customerId: "customer-rest" };
    render(
      <CustomerDetail
        {...{ ...props("token-a"), anchorLead: restLead, customerLeads: [restLead] }}
      />
    );

    const reveal = screen.getByRole("button", { name: "Hiện số đầy đủ" });
    expect(reveal.hasAttribute("disabled")).toBe(false);
    // An own lead needs no manual phone lookup round-trip.
    expect(
      screen.queryByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng")
    ).toBeNull();

    fireEvent.click(reveal);
    await screen.findByText(TEST_PHONE);
    expect(fetchRevealedPhoneNumber).toHaveBeenCalledWith(
      expect.objectContaining({ customerId: "customer-rest" })
    );
  });

  it("keeps the eye disabled and shows the lookup fallback for a row with no provable customer identity", () => {
    render(<CustomerDetail {...props("token-a")} />);
    expect(
      screen.getByRole("button", { name: "Hiện số đầy đủ" }).hasAttribute("disabled")
    ).toBe(true);
    expect(
      screen.getByPlaceholderText("Nhập số điện thoại để tra cứu khách hàng")
    ).toBeTruthy();
  });
});
