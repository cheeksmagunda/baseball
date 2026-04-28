import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EmptyState } from "../components/EmptyState";

describe("EmptyState", () => {
  it("renders no-games message", () => {
    render(<EmptyState />);
    expect(screen.getByText("No Games Today")).toBeTruthy();
  });
});
