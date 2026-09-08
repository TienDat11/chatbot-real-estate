// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { NotificationConsentControl } from "@/features/notifications/NotificationConsentControl";

afterEach(() => { cleanup(); });

// The status dot is the aria-hidden span carrying the semantic color; reading
// its serialized style lets the test assert the exact state color without
// depending on text alone.
function dotStyle(): string {
  const dot = document.querySelector('span[aria-hidden="true"]') as HTMLElement | null;
  return dot?.getAttribute("style") ?? "";
}

describe("NotificationConsentControl", () => {
  it("shows the green 'Đã bật thông báo' state when granted and enabled", () => {
    render(<NotificationConsentControl enabled nativePermission="granted" loading={false} onChange={vi.fn()} />);
    const button = screen.getByRole("button");
    expect(button.textContent).toContain("Đã bật thông báo");
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).not.toBeDisabled();
    expect(dotStyle()).toMatch(/16a34a|rgb\(22, 163, 74\)/);
  });

  it("shows the red 'Đã chặn thông báo' disabled state when the browser denied", () => {
    render(<NotificationConsentControl enabled={false} nativePermission="denied" loading={false} onChange={vi.fn()} />);
    const button = screen.getByRole("button");
    expect(button.textContent).toContain("Đã chặn thông báo");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-label", "Thông báo đã bị chặn");
    expect(dotStyle()).toMatch(/dc2626|rgb\(220, 38, 38\)/);
  });

  it("shows the neutral grey 'Thông báo' state when permission is default and off", () => {
    render(<NotificationConsentControl enabled={false} nativePermission="default" loading={false} onChange={vi.fn()} />);
    const button = screen.getByRole("button");
    expect(button.textContent).toContain("Thông báo");
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).not.toBeDisabled();
    expect(dotStyle()).toMatch(/94a3b8|rgb\(148, 163, 184\)/);
  });

  it("invokes onChange with the toggled value on click", () => {
    const onChange = vi.fn();
    render(<NotificationConsentControl enabled={false} nativePermission="default" loading={false} onChange={onChange} />);
    screen.getByRole("button").click();
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("renders the stable neutral placeholder while not hydrated, even with granted+enabled", () => {
    // Before the hook has read the real browser permission the control must show
    // the same neutral state the server emitted, so hydration never mismatches.
    render(<NotificationConsentControl enabled nativePermission="granted" loading={false} hydrated={false} onChange={vi.fn()} />);
    const button = screen.getByRole("button");
    expect(button.textContent).toContain("Thông báo");
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).not.toBeDisabled();
    expect(dotStyle()).toMatch(/94a3b8|rgb\(148, 163, 184\)/);
  });

  it("does not show the blocked state while not hydrated even if permission is denied", () => {
    render(<NotificationConsentControl enabled={false} nativePermission="denied" loading={false} hydrated={false} onChange={vi.fn()} />);
    const button = screen.getByRole("button");
    expect(button.textContent).toContain("Thông báo");
    expect(button).not.toBeDisabled();
    expect(dotStyle()).toMatch(/94a3b8|rgb\(148, 163, 184\)/);
  });
});
