import { act, renderHook } from "@testing-library/react";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

import { useSearchParam } from "./use-search-param";

vi.mock("next/navigation", () => ({
  usePathname: () => window.location.pathname,
  // Read the URL the hook wrote with the History API, as the App Router does.
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
afterAll(() => window.history.replaceState(null, "", "/"));

describe("useSearchParam", () => {
  beforeEach(() => window.history.replaceState(null, "", "/map?stale=1"));

  it("reads the parameter, and writes each change as a history entry keeping the others", () => {
    const push = vi.spyOn(window.history, "pushState");
    const { result, rerender } = renderHook(() => useSearchParam("resource"));
    expect(result.current[0]).toBeNull();

    act(() => result.current[1]("ecs-1"));
    expect(push).toHaveBeenLastCalledWith(null, "", "/map?stale=1&resource=ecs-1");
    rerender();
    expect(result.current[0]).toBe("ecs-1");

    // Closing adds an entry too, so Back reopens what was open.
    act(() => result.current[1](null));
    expect(push).toHaveBeenLastCalledWith(null, "", "/map?stale=1");
    push.mockRestore();
  });

  it("adds no entry when the value is already there", () => {
    window.history.replaceState(null, "", "/approvals?tab=decided");
    const push = vi.spyOn(window.history, "pushState");
    const { result } = renderHook(() => useSearchParam("tab"));
    expect(result.current[0]).toBe("decided");
    act(() => result.current[1]("decided"));
    expect(push).not.toHaveBeenCalled();
    push.mockRestore();
  });
});
