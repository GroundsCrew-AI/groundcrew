import { describe, expect, it } from "vitest";

import { approveBlockedReason, draftFromSettings, parseInvitee, settingsFromDraft } from "./workspace-admin";

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
  it("blocks emergency approvals for members unless the workspace allows them", () => {
    expect(approveBlockedReason("emergency", "member", "owners")).toMatch(/owners/);
    expect(approveBlockedReason("emergency", "member", "members")).toBeNull();
    expect(approveBlockedReason("write", "member", "owners")).toBeNull();
    expect(approveBlockedReason("emergency", "owner", "owners")).toBeNull();
  });
});
