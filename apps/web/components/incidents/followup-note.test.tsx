import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { FollowUp } from "@/lib/incidents";
import { incidentSummary } from "@/tests/incident-fixtures";

import { FollowUpNote, FollowUpNotes } from "./followup-note";
import { IncidentList } from "./incident-list";

const PR = "https://github.com/GroundsCrew-AI/checkout-demo/pull/3";

/** The note as read aloud without the link's "(opens in a new tab)" hint. */
function noteText(): string {
  return (screen.getByRole("status").textContent ?? "").replaceAll("(opens in a new tab)", "");
}

function followup(overrides: Partial<FollowUp> = {}): FollowUp {
  return {
    id: "fu-1",
    kind: "fix_pr",
    state: "open",
    repo: "GroundsCrew-AI/checkout-demo",
    number: 3,
    url: PR,
    triggering_commit: "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432",
    merge_commit: null,
    merged_at: null,
    deployed_at: null,
    deploy_url: null,
    closed_at: null,
    created_at: "2026-10-14T10:00:00Z",
    updated_at: "2026-10-14T10:00:00Z",
    tracked_until: "2026-10-21T10:00:00Z",
    ...overrides,
  };
}

describe("FollowUpNote", () => {
  it("says the permanent fix is pending while the PR is open", () => {
    render(<FollowUpNote followup={followup()} />);
    expect(noteText()).toContain("Permanent fix pending: PR #3 not merged yet.");
    expect(screen.getByRole("link", { name: /PR #3/ })).toHaveAttribute("href", PR);
    expect(screen.getByRole("status").className).toContain("status-waiting");
  });

  it("says it is deploying once merged", () => {
    render(<FollowUpNote followup={followup({ state: "merged", merge_commit: "a".repeat(40) })} />);
    expect(noteText()).toBe("PR #3 merged, deploying...");
    expect(screen.getByRole("status").className).toContain("status-waiting");
  });

  it("turns green when the fix is deployed", () => {
    render(
      <FollowUpNote
        followup={followup({ state: "deployed", deploy_url: "https://github.com/GroundsCrew-AI/checkout-demo/actions/runs/7" })}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Permanent fix deployed");
    expect(screen.getByRole("status").className).toContain("status-ok");
    expect(screen.getByRole("link", { name: "deploy run" })).toHaveAttribute("href", expect.stringContaining("/runs/7"));
  });

  it("warns in red when the PR was closed without merging", () => {
    render(<FollowUpNote followup={followup({ state: "closed_unmerged" })} />);
    const note = screen.getByRole("status");
    expect(noteText()).toBe(
      "PR #3 was closed without merging. The running service is on a rollback, but the repo still contains the change from commit 9f8e7d6; the next deploy will bring the failure back.",
    );
    expect(note.className).toContain("status-failed");
  });

  it("shows nothing without follow-ups", () => {
    const { container } = render(<FollowUpNotes followups={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("IncidentList follow-ups", () => {
  it("tags resolved incidents whose permanent fix is pending", () => {
    render(
      <IncidentList
        incidents={[
          incidentSummary({ id: "a", status: "resolved", has_open_followups: true }),
          incidentSummary({ id: "b", status: "resolved", has_open_followups: false }),
        ]}
      />,
    );
    expect(screen.getAllByText("fix pending")).toHaveLength(1);
  });
});
