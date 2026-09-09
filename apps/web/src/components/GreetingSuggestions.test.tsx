/** @vitest-environment jsdom */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageList } from "@/components/MessageList";

describe("greeting bubble and scoped suggestions", () => {
  it("renders the assistant greeting and never renders another project's name", () => {
    render(<MessageList messages={[{ id: "g1", role: "assistant", content: "Kính chào Anh/Chị!" }]} suggestions={["Dự án Soleil có tiện ích gì?", "Dự án Camellia có pháp lý thế nào?"]} excludedProjectNames={["Camellia"]} streaming={false} />);
    expect(screen.getByText("Kính chào Anh/Chị!")).toBeTruthy();
    expect(screen.getByText("Dự án Soleil có tiện ích gì?")).toBeTruthy();
    expect(screen.queryByText(/Camellia/)).toBeNull();
  });

  it("uses generic fallback suggestions without project-specific names", () => {
    render(<MessageList messages={[{ id: "g2", role: "assistant", content: "Kính chào Anh/Chị!" }]} suggestions={[]} streaming={false} />);
    expect(screen.getByText(/Dự án có những tiện ích gì nổi bật/i)).toBeTruthy();
  });
});
