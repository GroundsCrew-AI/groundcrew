import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { step } from "@/tests/fixtures";

import { Timeline } from "./timeline";

describe("Timeline", () => {
  it("shows a rejected call as not run, without the model-facing text", () => {
    render(
      <Timeline
        steps={[
          step(1, {
            kind: "tool_rejected",
            tool_name: "restart_fake_service",
            tool_policy: "write",
            latency_ms: null,
            output_summary: "Rejected by the user: restart_fake_service(...) was not run.",
          }),
        ]}
        approvals={{}}
        status="succeeded"
        result={null}
        onDecide={vi.fn()}
        deciding={null}
      />,
    );
    expect(screen.getByText("Not run: rejected")).toBeInTheDocument();
    expect(screen.getByText(/did not run/)).toBeInTheDocument();
    expect(screen.queryByText(/Rejected by the user/)).not.toBeInTheDocument();
  });
});
