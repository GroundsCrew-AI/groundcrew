import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ExampleBanner } from "./example-banner";

type W = { id: string; name: string; role: string; example: boolean };
const own: W = { id: "ws-1", name: "Mine", role: "owner", example: false };
const example: W = { id: "ex-1", name: "Example workspace", role: "viewer", example: true };
const state = vi.hoisted(() => ({ workspace: undefined as unknown, workspaces: [] as unknown[], set: vi.fn() }));

vi.mock("@/lib/workspace", () => ({
  useWorkspace: () => ({ workspace: state.workspace, workspaces: state.workspaces, setWorkspaceId: state.set }),
}));

describe("ExampleBanner", () => {
  afterEach(() => state.set.mockClear());

  it("shows nothing in your own workspace", () => {
    state.workspace = own;
    state.workspaces = [own, example];
    const { container } = render(<ExampleBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("says the example is read-only and takes you to your own workspace to connect", async () => {
    state.workspace = example;
    state.workspaces = [own, example];
    render(<ExampleBanner videoUrl="https://youtu.be/demo" />);
    expect(screen.getByRole("note")).toHaveTextContent(
      "Example workspace (read-only). Connect your cloud to use Groundcrew on your own infrastructure.",
    );
    expect(screen.getByRole("link", { name: "Docs" })).toHaveAttribute("href", "/docs");
    expect(screen.getByRole("link", { name: /Product video/ })).toHaveAttribute("href", "https://youtu.be/demo");
    await userEvent.click(screen.getByRole("link", { name: "Connect your cloud" }));
    expect(state.set).toHaveBeenCalledWith("ws-1");
  });

  it("hides the video link when none is set, and offers a workspace to someone without one", () => {
    state.workspace = example;
    state.workspaces = [example];
    render(<ExampleBanner videoUrl="" />);
    expect(screen.queryByRole("link", { name: /Product video/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create your workspace" })).toHaveAttribute("href", "/settings");
  });
});
