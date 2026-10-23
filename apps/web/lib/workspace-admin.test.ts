import { describe, expect, it } from "vitest";

import { approveBlockedReason, auditSummary, draftFromSettings, parseInvitee, settingsFromDraft } from "./workspace-admin";

describe("parseInvitee", () => {
  it("reads an email address or a GitHub login", () => {
    expect(parseInvitee(" Ada@Example.com ")).toEqual({ email: "ada@example.com" });
    expect(parseInvitee("@Octocat")).toEqual({ github_login: "Octocat" });
    expect(parseInvitee("not an email")).toBeNull();
    expect(parseInvitee("a@b")).toBeNull();
  });
});

describe("settings form", () => {
  it("round-trips the settings and checks the cap", () => {
    const settings = { emergency_approvers: "owners" as const, incident_watch: true, daily_usd_cap: 2.5 };
    expect(settingsFromDraft(draftFromSettings(settings))).toEqual({ settings });
    const draft = { ...draftFromSettings(settings), cap: "-1" };
    expect(settingsFromDraft(draft).error).toMatch(/0 or more/);
    expect(settingsFromDraft({ ...draft, capMode: "default" }).settings?.daily_usd_cap).toBeNull();
  });
});

describe("approveBlockedReason", () => {
  it("blocks emergency approvals for approvers unless the workspace allows them", () => {
    expect(approveBlockedReason("emergency", "approver", "owners")).toMatch(/owners/);
    expect(approveBlockedReason("emergency", "approver", "approvers")).toBeNull();
    expect(approveBlockedReason("write", "approver", "owners")).toBeNull();
    expect(approveBlockedReason("emergency", "owner", "owners")).toBeNull();
  });

  it("never lets a viewer decide", () => {
    expect(approveBlockedReason("write", "viewer", "approvers")).toMatch(/Viewers/);
    expect(approveBlockedReason("emergency", "viewer", "approvers")).toMatch(/Viewers/);
  });
});

describe("auditSummary", () => {
  it("says what happened in a few words", () => {
    expect(auditSummary({ action: "approval.approved", detail: { tool: "target_apply", policy: "write" } })).toBe(
      "approved target_apply (write)",
    );
    expect(auditSummary({ action: "member.role_changed", detail: { from: "viewer", to: "approver" } })).toBe(
      "changed a role: viewer to approver",
    );
    expect(auditSummary({ action: "invite.created", detail: { email: "a@b.co", role: "viewer" } })).toBe(
      "invited a@b.co as viewer",
    );
    expect(auditSummary({ action: "member.added", detail: { github_login: "val", role: "approver" } })).toBe(
      "added a member @val as approver",
    );
    expect(auditSummary({ action: "connection.deleted", detail: { kind: "aws", name: "AWS" } })).toBe(
      "removed a connection: AWS",
    );
    expect(auditSummary({ action: "something.new", detail: {} })).toBe("something new");
  });

  it("says what an assistant asked for, and why it was refused", () => {
    expect(
      auditSummary({ action: "assistant.start_deploy", detail: { repository: "acme/shop", result: "started" } }),
    ).toBe("asked to deploy: acme/shop");
    expect(
      auditSummary({ action: "assistant.report_incident", detail: { result: "This token can only read." } }),
    ).toBe("asked to report an incident, refused: This token can only read.");
    expect(auditSummary({ action: "token.created", detail: { name: "laptop" } })).toBe(
      "made an assistant token: laptop",
    );
  });
});
