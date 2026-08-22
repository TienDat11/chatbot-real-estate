// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { NearbyPlace } from "@rag-ragre/contracts";
import { MapPanel } from "@/components/MapPanel";

// QA M3 hardening: ESC / backdrop dismissals close the geolocation consent
// modal WITHOUT opening any tab — only the explicit "Không" decline button may
// call the Google Maps deep-link fallback (popup-trap guard). The component is
// rendered in list mode so maplibre/map canvas never loads; only the consent
// modal wiring is under test here (the OSRM flow itself lives in
// MapPanel.directions.test.ts).
//
// antd keeps the closed dialog mounted (removeOnLeave=false), so "closed" is
// asserted through the hidden state rc-dialog applies once the leave motion
// ends. jsdom never fires transitionend/animationend, so each assertion loop
// also flushes those events to let the leave motion finish.

const PLACE: NearbyPlace = {
  name: "Co.opmart Sơn Trà",
  kinds: ["supermarket"],
  lat: 16.09,
  lng: 108.25,
  distance_m: 900,
};

const MODAL_TITLE = "Cho phép lấy vị trí của bạn?";

let openSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function modalWrap(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".ant-modal-wrap");
}

function modalPanel(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".ant-modal");
}

/** True once rc-dialog's leave motion finished and hid the dialog. */
function modalHidden(): boolean {
  const wrap = modalWrap();
  const panel = modalPanel();
  if (!wrap || !panel) return false;
  return wrap.style.display === "none" || panel.style.display === "none";
}

/** jsdom lacks real transition events: flush both motion-end events. */
function flushLeaveMotion(): void {
  for (const el of document.querySelectorAll<HTMLElement>(".ant-modal, .ant-modal-mask")) {
    fireEvent.transitionEnd(el);
    fireEvent.animationEnd(el);
  }
}

async function expectModalClosed(): Promise<void> {
  await waitFor(() => {
    flushLeaveMotion();
    expect(modalHidden()).toBe(true);
  });
}

async function openConsentModal(): Promise<void> {
  render(<MapPanel places={[PLACE]} mode="list" />);
  fireEvent.click(screen.getAllByText("Chỉ đường")[0]);
  expect(await screen.findByText(MODAL_TITLE)).toBeTruthy();
  expect(modalHidden()).toBe(false);
}

describe("MapPanel consent modal — dismiss must not open a tab (QA M3)", () => {
  it("closes on ESC without calling window.open", async () => {
    await openConsentModal();
    // rc-dialog listens for ESC on the wrap element, not on document.
    fireEvent.keyDown(modalWrap()!, { key: "Escape", code: "Escape", keyCode: 27 });
    await expectModalClosed();
    expect(openSpy).not.toHaveBeenCalled();
  });

  it("closes on a backdrop click without calling window.open", async () => {
    await openConsentModal();
    fireEvent.click(modalWrap()!);
    await expectModalClosed();
    expect(openSpy).not.toHaveBeenCalled();
  });

  it("opens the deep-link fallback only from the explicit decline button", async () => {
    await openConsentModal();
    fireEvent.click(screen.getByRole("button", { name: "Không" }));
    await expectModalClosed();
    expect(openSpy).toHaveBeenCalledTimes(1);
    const [url, target] = openSpy.mock.calls[0] as [string, string];
    expect(url).toContain("https://www.google.com/maps/dir/?api=1&destination=");
    expect(target).toBe("_blank");
  });
});
