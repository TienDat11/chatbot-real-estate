// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SalesLayout from "./layout";
import SalesLeadsPage from "./leads/page";
import SalesChatPage from "./chat/page";
import SalesTrainPage from "./train/page";

vi.mock("antd", () => ({
  App: Object.assign(({ children }: { children: ReactNode }) => <>{children}</>, { useApp: () => ({ message: {} }) }),
  Typography: { Title: ({ children }: { children: ReactNode }) => <>{children}</> },
  Select: ({ children, ...props }: { children?: ReactNode; [key: string]: unknown }) => <select {...props}>{children}</select>,
}));
vi.mock("@/lib/AuthProvider", () => ({ AuthProvider: ({ children }: { children: ReactNode }) => <>{children}</>, useOptionalAuth: () => null }));
vi.mock("@/components/RequireRole", () => ({ RequireRole: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/lib/realtime/RealtimeProvider", () => ({ RealtimeProvider: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/features/notifications/SalesNotificationProvider", () => ({
  SalesNotificationProvider: ({ children }: { children: ReactNode }) => <div data-testid="sales-notifications">{children}</div>,
}));
vi.mock("@/components/AppShell", () => ({ AppShell: ({ children }: { children: ReactNode }) => <div data-testid="sales-shell">{children}</div> }));
vi.mock("@/features/crm/CrmWorkspace", () => ({ CrmWorkspace: () => <main>Leads workspace</main> }));
vi.mock("@/features/sales/SalesChatShell", () => ({ SalesChatShell: () => <main>Sales chat workspace</main> }));
vi.mock("@/features/train/TrainWorkspace", () => ({ TrainWorkspace: () => <main>Training workspace</main> }));

const captured = vi.hoisted(() => ({ redirects: [] as string[] }));
vi.mock("next/navigation", () => ({
  redirect: (url: string) => {
    captured.redirects.push(url);
  },
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/sales/train",
}));

afterEach(() => {
  cleanup();
  captured.redirects = [];
});

describe("sales routes", () => {
  it.each([
    ["leads", <SalesLeadsPage key="leads" />, "Leads workspace"],
    ["chat", <SalesChatPage key="chat" />, "Sales chat workspace"],
  ])("keeps the notification provider and shell mounted on the %s route", (_route, page, workspace) => {
    render(<SalesLayout>{page}</SalesLayout>);

    const content = screen.getByText(workspace);
    expect(screen.getByTestId("sales-notifications").contains(content)).toBe(true);
    expect(screen.getByTestId("sales-shell").contains(content)).toBe(true);
  });

  it("renders the authenticated training workspace", async () => {
    render(
      <SalesLayout>
        {await SalesTrainPage({ searchParams: Promise.resolve({}) })}
      </SalesLayout>
    );

    expect(captured.redirects).toEqual([]);
  });
});
