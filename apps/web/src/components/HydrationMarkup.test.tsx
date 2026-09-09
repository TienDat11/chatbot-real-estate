// @vitest-environment jsdom
/**
 * Hydration checkpoint for application-owned markup. Browser extensions can
 * append fdprocessedid after SSR, so that known external mutation is excluded
 * while React hydration and validateDOMNesting errors remain test failures.
 */
import { act } from "react";
import { hydrateRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { App } from "antd";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("next/image", () => ({
  default: ({ fill: _fill, priority: _priority, ...props }: Record<string, unknown>) => <img {...props} />,
}));

vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  signInWithEmail: vi.fn(),
  signUpWithEmail: vi.fn(),
  signOutUser: vi.fn(),
  onAuthChange: vi.fn(() => () => {}),
  getFreshIdToken: vi.fn(),
}));

vi.mock("@/features/crm/crmApiClient", () => ({
  CrmApiClientError: class CrmApiClientError extends Error {},
  fetchRevealedPhoneNumber: vi.fn(),
  searchCustomerByPhone: vi.fn(),
  updateLeadStatus: vi.fn(),
  withdrawMarketingConsent: vi.fn(),
}));

vi.mock("@/features/crm/LeadConversationPanel", () => ({
  LeadConversationPanel: () => <section aria-label="Lịch sử hội thoại" />,
}));

import { LoginScreen } from "@/features/auth/LoginScreen";
import { AuthProvider } from "@/lib/AuthProvider";
import { MessageBubble, type ChatMessage } from "@/components/MessageBubble";
import { CustomerDetail } from "@/features/crm/CustomerDetail";
import { makeCrmLeadFixture } from "@/features/crm/crmLeadFixture";

const roots: Root[] = [];

function applicationMarkupErrors(calls: unknown[][]): string[] {
  return calls
    .map((call) => call.map(String).join(" "))
    .filter((message) => /hydration|validateDOMNesting/i.test(message))
    // fdprocessedid is injected by browser extensions, not emitted by this app.
    .filter((message) => !/fdprocessedid/i.test(message));
}

async function expectHydratesWithoutApplicationMarkupErrors(element: ReactElement) {
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  const container = document.createElement("div");
  document.body.append(container);
  const recoverableErrors: unknown[] = [];

  try {
    container.innerHTML = renderToString(element);
    await act(async () => {
      roots.push(hydrateRoot(container, element, {
        onRecoverableError: (error) => recoverableErrors.push(error),
      }));
      await Promise.resolve();
    });

    expect(applicationMarkupErrors(errors.mock.calls)).toEqual([]);
    expect(recoverableErrors.map(String).filter((message) => /hydration|validateDOMNesting/i.test(message))).toEqual([]);
  } finally {
    errors.mockRestore();
  }
}

afterEach(() => {
  while (roots.length > 0) roots.pop()?.unmount();
  document.body.replaceChildren();
  vi.clearAllMocks();
});

describe("application hydration markup", () => {
  it("hydrates Login without application-owned nesting or hydration errors", async () => {
    await expectHydratesWithoutApplicationMarkupErrors(
      <AuthProvider><LoginScreen /></AuthProvider>,
    );
  });

  it("hydrates ChatPage assistant markdown tables and lists without invalid nesting", async () => {
    const message: ChatMessage = {
      id: "hydration-chat-answer",
      role: "assistant",
      content: [
        "Bảng giá tham khảo:",
        "| Loại căn | Giá |",
        "| --- | --- |",
        "| 2PN | 3,2 tỷ |",
        "- Lưu ý: giá do sales xác nhận",
        "- Chính sách thanh toán: đợt 1 là 10%",
      ].join("\n"),
    };

    await expectHydratesWithoutApplicationMarkupErrors(<MessageBubble message={message} />);
  });

  it("hydrates an answer with a --- thematic break without hr-in-p nesting (B1)", async () => {
    // Regression: the model emits a `---` divider line; before the divider
    // block kind existed, ParagraphBlock rendered <p><hr></p>, which React
    // flags as invalid DOM nesting and a hydration mismatch.
    const message: ChatMessage = {
      id: "hydration-chat-divider",
      role: "assistant",
      content: [
        "Phần chính sách bán hàng như sau:",
        "",
        "---",
        "",
        "Chính sách thanh toán theo tiến độ xây dựng.",
      ].join("\n"),
    };

    await expectHydratesWithoutApplicationMarkupErrors(<MessageBubble message={message} />);
  });

  it("hydrates CRM customer detail without application-owned nesting or hydration errors", async () => {
    const lead = makeCrmLeadFixture();
    await expectHydratesWithoutApplicationMarkupErrors(
      <App>
        <CustomerDetail
          open
          anchorLead={lead}
          customerLeads={[lead]}
          bearerToken="test-token"
          currentStaffUid="staff-1"
          noteStore={{ readLeadNote: vi.fn().mockResolvedValue(null), saveLeadNote: vi.fn().mockResolvedValue(undefined) }}
          onOptimisticLeadPatch={vi.fn()}
          onClearOptimisticLeadPatch={vi.fn()}
          onClose={vi.fn()}
        />
      </App>,
    );
  });
});
