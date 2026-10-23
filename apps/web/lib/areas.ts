import {
  BookOpen,
  Inbox,
  LayoutGrid,
  type LucideIcon,
  Network,
  Plug,
  Rocket,
  Settings,
  Siren,
  Workflow,
} from "lucide-react";

/** The areas of the app: where they live, how they look, and where their docs are. The sidebar,
 * the page headers, and the docs links all read this, so a page always has the same name, icon,
 * hue, and help link everywhere. */
export type AreaKey =
  | "overview"
  | "map"
  | "incidents"
  | "approvals"
  | "deploy"
  | "runs"
  | "connections"
  | "settings";

export type Area = {
  key: AreaKey;
  href: string;
  label: string;
  icon: LucideIcon;
  /** What the area is for, in a few words (nav tooltips, page headers). */
  hint: string;
  /** The docs page that explains it. */
  docs: string;
  group: "operate" | "ship" | "setup";
};

export const AREAS: Area[] = [
  {
    key: "overview",
    href: "/",
    label: "Overview",
    icon: LayoutGrid,
    hint: "Health, open incidents, and what needs you",
    docs: "/docs/getting-started",
    group: "operate",
  },
  {
    key: "map",
    href: "/map",
    label: "Map",
    icon: Network,
    hint: "Everything Groundcrew found, and how it connects",
    docs: "/docs/discovery",
    group: "operate",
  },
  {
    key: "incidents",
    href: "/incidents",
    label: "Incidents",
    icon: Siren,
    hint: "Outages Medic is diagnosing and fixing",
    docs: "/docs/incidents",
    group: "operate",
  },
  {
    key: "approvals",
    href: "/approvals",
    label: "Approvals",
    icon: Inbox,
    hint: "Changes waiting for a person",
    docs: "/docs/approvals",
    group: "operate",
  },
  {
    key: "deploy",
    href: "/deploy",
    label: "Deploy",
    icon: Rocket,
    hint: "Build a repository and ship it",
    docs: "/docs/deploys",
    group: "ship",
  },
  {
    key: "runs",
    href: "/runs",
    label: "Runs",
    icon: Workflow,
    hint: "Every agent run, step by step",
    docs: "/docs/getting-started",
    group: "ship",
  },
  {
    key: "connections",
    href: "/connections",
    label: "Connections",
    icon: Plug,
    hint: "Clouds, clusters, repositories, and servers",
    docs: "/docs/connect/aws",
    group: "setup",
  },
  {
    key: "settings",
    href: "/settings",
    label: "Settings",
    icon: Settings,
    hint: "Members, notifications, limits, and your account",
    docs: "/docs/roles",
    group: "setup",
  },
];

export const GROUPS: { key: Area["group"]; label: string }[] = [
  { key: "operate", label: "Operate" },
  { key: "ship", label: "Ship" },
  { key: "setup", label: "Set up" },
];

export const DOCS = { href: "/docs", label: "Docs", icon: BookOpen } as const;

/** The CSS custom property of an area's hue. */
export function areaColor(key: AreaKey): string {
  return `var(--area-${key})`;
}

/** The area a path belongs to ("/" only for the overview itself). */
export function areaFor(pathname: string): Area | undefined {
  if (pathname === "/") return AREAS[0];
  return AREAS.find((a) => a.href !== "/" && (pathname === a.href || pathname.startsWith(`${a.href}/`)));
}
