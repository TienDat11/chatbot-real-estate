// @vitest-environment jsdom
/**
 * Cross-project redirect card (guardrail feature) render tests.
 *
 * Pinned here:
 *  1. an assistant message carrying project_redirect renders the prominent
 *     "Chuyển sang dự án <display_name> →" card linking to /project/<key>;
 *  2. the same message without the field renders nothing — graceful absence;
 *  3. normalizeProjectRedirect maps the backend payload and degrades every
 *     malformed shape to null (no crash, no dead card).
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MessageBubble, type ChatMessage } from "@/components/MessageBubble";
import { ProjectRedirectCard } from "@/components/ProjectRedirectCard";
import { normalizeProjectRedirect } from "@/lib/projectRedirect";

afterEach(() => {
  cleanup();
});

const REDIRECT = { project_key: "soleil", displayName: "The Soleil Đà Nẵng" };

function assistantMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return { id: "m1", role: "assistant", content: "Câu trả lời mẫu.", ...overrides };
}

describe("ProjectRedirectCard", () => {
  it("renders the switch CTA with the target project link", () => {
    render(<ProjectRedirectCard redirect={REDIRECT} />);
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toMatch(/^\/project\/soleil(?:\?session=.+)?$/);
    expect(screen.getByText(/Chuyển sang dự án The Soleil Đà Nẵng →/)).toBeTruthy();
  });

  it("URL-encodes unsafe keys in the href", () => {
    render(<ProjectRedirectCard redirect={{ project_key: "a b", displayName: "X" }} />);
    expect(screen.getByRole("link").getAttribute("href")).toMatch(/^\/project\/a%20b(?:\?session=.+)?$/);
  });
});

describe("MessageBubble with project_redirect", () => {
  it("shows the card below a finished answer when the field is present", () => {
    render(<MessageBubble message={assistantMessage({ projectRedirect: REDIRECT })} />);
    expect(screen.getByRole("link").getAttribute("href")).toBe("/project/soleil");
  });

  it("renders nothing when the field is absent or the answer still streams", () => {
    const { container: absent } = render(<MessageBubble message={assistantMessage()} />);
    expect(absent.querySelector("a")).toBeNull();

    const { container: streaming } = render(
      <MessageBubble message={assistantMessage({ streaming: true, projectRedirect: REDIRECT })} />
    );
    expect(streaming.querySelector("a")).toBeNull();
  });
});

describe("normalizeProjectRedirect", () => {
  it("maps the backend payload (snake_case contract)", () => {
    expect(normalizeProjectRedirect({ project_key: "soleil", display_name: "The Soleil Đà Nẵng" })).toEqual({
      project_key: "soleil",
      displayName: "The Soleil Đà Nẵng",
    });
  });

  it("accepts camelCase aliases and falls back to the key as display name", () => {
    expect(normalizeProjectRedirect({ projectKey: "camellia" })).toEqual({
      project_key: "camellia",
      displayName: "camellia",
    });
  });

  it("returns null for every malformed/absent shape", () => {
    expect(normalizeProjectRedirect(undefined)).toBeNull();
    expect(normalizeProjectRedirect(null)).toBeNull();
    expect(normalizeProjectRedirect("soleil")).toBeNull();
    expect(normalizeProjectRedirect({})).toBeNull();
    expect(normalizeProjectRedirect({ project_key: "" })).toBeNull();
  });
});
