import { describe, expect, it } from "vitest";

import { stopReasonLabel } from "./badges";

describe("stopReasonLabel", () => {
  it("labels every stop reason, including no_progress", () => {
    expect(stopReasonLabel("budget")).toBe("Hit a token budget");
    expect(stopReasonLabel("no_progress")).toBe("Stopped: no progress");
    expect(stopReasonLabel(null)).toBeNull();
  });
});
