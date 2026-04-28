import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ErrorState } from "../components/ErrorState";

describe("ErrorState", () => {
  it("shows status code and message", () => {
    render(<ErrorState status={503} message="Backend unavailable" onRetry={vi.fn()} />);
    expect(screen.getByText("503")).toBeTruthy();
    expect(screen.getByText("Backend unavailable")).toBeTruthy();
  });

  it("calls onRetry when Retry is clicked", () => {
    const onRetry = vi.fn();
    render(<ErrorState status={500} message="Error" onRetry={onRetry} />);
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("uses role=alert for accessibility", () => {
    render(<ErrorState status={500} message="oops" onRetry={vi.fn()} />);
    expect(screen.getByRole("alert")).toBeTruthy();
  });
});
