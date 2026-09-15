// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ReviewBanner } from "./ReviewBanner";

afterEach(() => cleanup());

describe("ReviewBanner", () => {
  it("renders the advisor-confirmation warning", () => {
    render(<ReviewBanner />);
    expect(screen.getByText("Câu trả lời cần tư vấn viên xác nhận")).toBeTruthy();
  });
});
