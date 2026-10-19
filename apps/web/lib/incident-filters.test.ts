import { describe, expect, it } from "vitest";

import { sinceFor } from "@/lib/incidents";
import { incidentSummary } from "@/tests/incident-fixtures";

import {
  apiIncidentFilters,
  emptyTitle,
  hasFilters,
  incidentFiltersSearch,
  matchesSearch,
  NO_FILTERS,
  parseIncidentFilters,
} from "./incident-filters";

const parse = (search: string) => parseIncidentFilters(new URLSearchParams(search));

describe("parseIncidentFilters", () => {
  it("reads nothing as no filters", () => {
    expect(parse("")).toEqual(NO_FILTERS);
  });

  it("reads a status tab, severity, window, and search", () => {
    expect(parse("status=resolved&severity=high&window=7d&q=api")).toEqual({
      tab: "resolved",
      severity: "high",
      window: "7d",
      q: "api",
    });
    expect(parse("pending_fix=true").tab).toBe("pending_fix");
  });

  it("ignores values that are not one of the filters", () => {
    expect(parse("status=open&severity=urgent&window=1y")).toEqual(NO_FILTERS);
    expect(parse("severity=toString&window=constructor")).toEqual(NO_FILTERS);
  });
});

describe("incidentFiltersSearch", () => {
  it("leaves defaults out", () => {
    expect(incidentFiltersSearch(NO_FILTERS)).toBe("");
    expect(incidentFiltersSearch({ ...NO_FILTERS, q: "   " })).toBe("");
  });

  it("round-trips through the URL", () => {
    const filters = { tab: "pending_fix" as const, severity: "critical" as const, window: "24h" as const, q: "checkout api" };
    const search = incidentFiltersSearch(filters);
    expect(search).toBe("?pending_fix=true&severity=critical&window=24h&q=checkout+api");
    expect(parse(search.slice(1))).toEqual(filters);
  });
});

describe("apiIncidentFilters", () => {
  it("sends status, pending_fix, severity, and window, never the search", () => {
    expect(apiIncidentFilters(NO_FILTERS)).toEqual({});
    expect(apiIncidentFilters({ tab: "failed", severity: "low", window: "30d", q: "x" })).toEqual({
      status: "failed",
      severity: "low",
      window: "30d",
    });
    expect(apiIncidentFilters({ ...NO_FILTERS, tab: "pending_fix" })).toEqual({ pending_fix: true });
  });
});

describe("sinceFor", () => {
  it("counts back from now", () => {
    const now = Date.parse("2026-10-19T12:00:00Z");
    expect(sinceFor("24h", now)).toBe("2026-10-18T12:00:00.000Z");
    expect(sinceFor("7d", now)).toBe("2026-10-12T12:00:00.000Z");
    expect(sinceFor(null, now)).toBeUndefined();
  });
});

describe("matchesSearch", () => {
  const incident = incidentSummary({
    title: "Health checks failing",
    affected_resources: [{ id: "r", name: "orders-api", type: "ecs:service", provider: "aws" }],
  });

  it("matches title and resource names, case-insensitively, every word", () => {
    expect(matchesSearch(incident, "")).toBe(true);
    expect(matchesSearch(incident, "HEALTH")).toBe(true);
    expect(matchesSearch(incident, "orders")).toBe(true);
    expect(matchesSearch(incident, "orders failing")).toBe(true);
    expect(matchesSearch(incident, "orders database")).toBe(false);
  });
});

describe("hasFilters and emptyTitle", () => {
  it("describes the filters that matched nothing", () => {
    expect(hasFilters(NO_FILTERS)).toBe(false);
    expect(hasFilters({ ...NO_FILTERS, window: "7d" })).toBe(true);
    expect(emptyTitle({ ...NO_FILTERS, tab: "waiting_approval" })).toBe("No incidents waiting for approval");
    expect(emptyTitle({ tab: "resolved", severity: "high", window: "7d", q: "" })).toBe(
      "No high severity incidents resolved in the last 7 days",
    );
  });
});
