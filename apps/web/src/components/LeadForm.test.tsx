// @vitest-environment jsdom
/**
 * LeadForm submission contract tests (ISSUE-7 acceptance additions).
 *
 * Pinned here:
 *  1. one valid submit fires POST /api/lead EXACTLY once — a racing second
 *     activation while pending cannot create a duplicate lead;
 *  2. the request carries full chat attribution: project_key, session_id,
 *     device_id (+ X-Device-Id header), anon_token, note, consent:true;
 *  3. a network failure keeps every typed value (no lost form state), keeps
 *     the form mounted with a retry affordance, and never calls onSuccess;
 *  4. a 201 persists the returned lead id (dup-guard key) and forwards
 *     (lead_id, quota_bonus_granted) to onSuccess.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { LEAD_ID_STORAGE_KEY, LeadForm } from "@/components/LeadForm";

const BODY_201 = {
  lead_id: 123,
  will_call_within_minutes: 5,
  quota_bonus_granted: 5,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function leadCalls(fetchMock: ReturnType<typeof vi.fn>): Array<{ url: string; init: RequestInit }> {
  return fetchMock.mock.calls
    .map((call) => ({ url: String(call[0]), init: (call[1] ?? {}) as RequestInit }))
    .filter((entry) => entry.url.includes("/api/lead"));
}

function mount(onSuccess = vi.fn(), onClose = vi.fn()) {
  render(
    <LeadForm
      open
      sessionId="sess_issue7"
      deviceId="dev_issue7"
      projectKey="camellia"
      projectName="The Camellia"
      notePrefill="Quan tam gia 2PN"
      anonToken="anon_tok_issue7"
      onClose={onClose}
      onSuccess={onSuccess}
    />
  );
}

async function fillAndSubmit(): Promise<void> {
  fireEvent.change(screen.getByPlaceholderText(/Nguyễn Văn An/), {
    target: { value: "Trần Thị Bích" },
  });
  fireEvent.change(screen.getByPlaceholderText("Ví dụ: 0905123456"), {
    target: { value: "0905 111 222" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: /Nhận tư vấn miễn phí/ }));
}

describe("LeadForm ISSUE-7 submission path", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    window.localStorage.clear();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("creates exactly one lead per valid submit even under a racing double-click", async () => {
    // A controlled pending promise lets the second activation land strictly
    // before the first resolves, proving the loading-state gate.
    let release!: (value: Response) => void;
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        })
    );

    mount();
    await fillAndSubmit();

    // First submit consumed the endpoint once; the mid-flight re-activation
    // must be swallowed by the pending state instead of re-firing.
    await waitFor(() => expect(leadCalls(fetchMock)).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: /Đang kết nối/ }));
    await waitFor(() => expect(leadCalls(fetchMock)).toHaveLength(1));

    release(jsonResponse(BODY_201, 201));

    // The resolved 201 replaced the form with the confirmation view, which is
    // itself the post-success double-submit guard.
    await waitFor(() => expect(screen.getByText(/Chuyên viên sẽ gọi lại/)).toBeTruthy());
    await waitFor(() => expect(leadCalls(fetchMock)).toHaveLength(1));
  });

  it("sends chat-attributed payload and device header, then stores the lead id", async () => {
    fetchMock.mockResolvedValue(jsonResponse(BODY_201, 201));
    const onSuccess = vi.fn();

    mount(onSuccess);
    await fillAndSubmit();

    await waitFor(() => expect(screen.getByText(/Chuyên viên sẽ gọi lại/)).toBeTruthy());

    const [call] = leadCalls(fetchMock);
    expect(call.url).toContain("/api/lead");
    expect((call.init.headers as Record<string, string>)["X-Device-Id"]).toBe("dev_issue7");
    const body = JSON.parse(String(call.init.body)) as Record<string, unknown>;
    expect(body.project_key).toBe("camellia");
    expect(body.session_id).toBe("sess_issue7");
    expect(body.device_id).toBe("dev_issue7");
    expect(body.anon_token).toBe("anon_tok_issue7");
    expect(body.note).toBe("Quan tam gia 2PN");
    expect(body.consent).toBe(true);
    // Phone normalization matches backend normalize_phone.
    expect(body.phone).toBe("0905111222");

    expect(window.localStorage.getItem(LEAD_ID_STORAGE_KEY)).toBe("123");
    expect(onSuccess).toHaveBeenCalledWith(123, 5);
  });

  it("keeps typed values and retry affordance on failure without calling onSuccess", async () => {
    // Controlled failure: the POST stays pending until released, so every
    // UI expectation below reads a settled commit instead of racing antd's
    // async validation/loading transitions.
    let failRequest!: (err: Error) => void;
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((_resolve, reject) => {
          failRequest = reject;
        })
    );
    const onSuccess = vi.fn();

    mount(onSuccess);
    await fillAndSubmit();

    // Pending state: the request fired exactly once and no success view exists.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/Chuyên viên sẽ gọi lại/)).toBeNull();

    failRequest(new TypeError("network down"));

    // Failure surfaces inline while the form stays usable with typed values.
    // The transport failure becomes api.ts' typed network LeadSubmitError,
    // whose message is what LeadForm renders inline.
    await waitFor(() =>
      expect(screen.getByText("Không kết nối được máy chủ. Vui lòng thử lại.")).toBeTruthy()
    );
    expect(screen.getByDisplayValue("0905 111 222")).toBeTruthy();
    expect(screen.getByDisplayValue("Trần Thị Bích")).toBeTruthy();

    // A failed submission must neither persist a dup-guard id nor pay out.
    expect(window.localStorage.getItem(LEAD_ID_STORAGE_KEY)).toBeNull();
    expect(onSuccess).not.toHaveBeenCalled();

    // Retry on the SAME mounted form succeeds without retyping.
    fetchMock.mockImplementationOnce(() => Promise.resolve(jsonResponse(BODY_201, 201)));
    fireEvent.click(await screen.findByRole("button", { name: /Thử lại/ }));
    await waitFor(() => expect(screen.getByText(/Chuyên viên sẽ gọi lại/)).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onSuccess).toHaveBeenCalledWith(123, 5);
  });

  it("renders the duplicate-notice view on 409 without paying out turns", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "A lead with this phone number was submitted recently" }, 409)
    );
    const onSuccess = vi.fn();

    mount(onSuccess);
    await fillAndSubmit();

    await waitFor(() => expect(screen.getByText(/Số này đã đăng ký/)).toBeTruthy());
    expect(window.localStorage.getItem(LEAD_ID_STORAGE_KEY)).toBeNull();
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("closes via the explicit Đóng button and the X icon without any submission", () => {
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    mount(onSuccess, onClose);

    // Explicit dismiss control beside the primary action.
    fireEvent.click(screen.getByRole("button", { name: "Đóng" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    // The antd Modal header X funnels into the same onClose contract.
    const closeIcon = document.querySelector(".ant-modal-close");
    expect(closeIcon).toBeTruthy();
    fireEvent.click(closeIcon as Element);
    expect(onClose).toHaveBeenCalledTimes(2);

    // Dismissal never creates a lead.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
  });
});
