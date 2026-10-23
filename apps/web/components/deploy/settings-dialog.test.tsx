import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { ChangeSettingsButton, settingsChange } from "./settings-dialog";

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
});

const state = vi.hoisted(() => ({
  blocked: null as string | null,
  sent: vi.fn(),
  push: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: state.push }) }));
vi.mock("@/lib/deploys", () => ({
  useDeploySettings: () => ({
    isLoading: false,
    data: {
      env: [
        { name: "DATABASE_URL", secret: true },
        { name: "FEATURE", secret: false },
        { name: "LOG_LEVEL", secret: false },
      ],
      blocked: state.blocked,
    },
  }),
  useChangeSettings: () => ({
    isPending: false,
    error: null,
    mutate: (body: unknown, opts: { onSuccess: (c: { deploy_id: string }) => void }) => {
      state.sent(body);
      opts.onSuccess({ deploy_id: "d-2" });
    },
  }),
}));

describe("settingsChange", () => {
  const existing = [
    { name: "A", secret: false, choice: "keep" as const, value: "" },
    { name: "B", secret: true, choice: "set" as const, value: "new" },
    { name: "C", secret: false, choice: "remove" as const, value: "" },
  ];

  it("sends only what changes: new values, removals, and added vars", () => {
    expect(settingsChange(existing, [{ key: 1, name: "D", value: "4", secret: false }])).toEqual({
      body: {
        env: [
          { name: "B", value: "new", secret: true },
          { name: "D", value: "4", secret: false },
        ],
        remove: ["C"],
      },
    });
  });

  it("refuses an empty value, a duplicate, a bad name, or no change", () => {
    expect(settingsChange([{ ...existing[1], value: "" }], [])).toEqual({ problem: "Give B its new value, or keep it." });
    expect(settingsChange(existing, [{ key: 1, name: "A", value: "x", secret: false }])).toEqual({
      problem: "A is listed twice.",
    });
    expect(settingsChange(existing, [{ key: 1, name: "1X", value: "x", secret: false }])).toMatchObject({
      problem: expect.stringContaining("1X needs a name"),
    });
    expect(settingsChange([existing[0]], [])).toEqual({ problem: "Nothing is changed yet." });
  });
});

describe("ChangeSettingsButton", () => {
  it("changes one value, removes one, adds one, and opens the new run", async () => {
    state.blocked = null;
    render(<ChangeSettingsButton deployId="d-1" />);
    await userEvent.click(screen.getByRole("button", { name: "Change settings" }));
    expect(screen.getByText("Env vars on this deploy")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("combobox", { name: "What to do with LOG_LEVEL" }));
    await userEvent.click(screen.getByRole("option", { name: "New value" }));
    await userEvent.type(screen.getByLabelText("New value of LOG_LEVEL"), "debug");
    await userEvent.click(screen.getByRole("combobox", { name: "What to do with FEATURE" }));
    await userEvent.click(screen.getByRole("option", { name: "Remove" }));
    await userEvent.click(screen.getByRole("button", { name: /Add env var/ }));
    await userEvent.type(screen.getByLabelText("Name"), "API_TOKEN");
    await userEvent.type(screen.getByLabelText("Value of API_TOKEN"), "t0k");
    // A secret-looking name is marked secret.
    expect(screen.getByRole("switch", { name: "API_TOKEN is a secret" })).toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: "Review the plan" }));
    expect(state.sent).toHaveBeenCalledWith({
      env: [
        { name: "LOG_LEVEL", value: "debug", secret: false },
        { name: "API_TOKEN", value: "t0k", secret: true },
      ],
      remove: ["FEATURE"],
    });
    expect(state.push).toHaveBeenCalledWith("/deploy/d-2");
  });

  it("says why a replaced deploy's settings cannot change", async () => {
    state.blocked = "A newer deploy of this app replaced this one; change its settings instead.";
    render(<ChangeSettingsButton deployId="d-1" />);
    await userEvent.click(screen.getByRole("button", { name: "Change settings" }));
    expect(screen.getByRole("alert")).toHaveTextContent("A newer deploy");
    expect(screen.getByRole("button", { name: "Review the plan" })).toBeDisabled();
  });
});
