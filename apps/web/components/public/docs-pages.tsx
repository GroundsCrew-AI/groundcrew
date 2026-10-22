/** The docs' table of contents: the sidebar groups, and the order of Previous and Next. */
export type DocsPage = { href: string; title: string };
export type DocsGroup = { title: string; pages: DocsPage[] };

export const DOCS_GROUPS: DocsGroup[] = [
  {
    title: "Get started",
    pages: [
      { href: "/docs", title: "Overview" },
      { href: "/docs/getting-started", title: "Quick start" },
    ],
  },
  {
    title: "Connect",
    pages: [
      { href: "/docs/connect/aws", title: "AWS" },
      { href: "/docs/connect/github", title: "GitHub" },
      { href: "/docs/connect/vercel", title: "Vercel" },
      { href: "/docs/connect/kubernetes", title: "Kubernetes" },
      { href: "/docs/connect/agent", title: "Servers (agent)" },
    ],
  },
  {
    title: "Use Groundcrew",
    pages: [
      { href: "/docs/discovery", title: "Map and discovery" },
      { href: "/docs/deploys", title: "Deploys" },
      { href: "/docs/incidents", title: "Incidents and fixes" },
      { href: "/docs/notifications", title: "Notifications" },
    ],
  },
  {
    title: "Workspaces",
    pages: [
      { href: "/docs/roles", title: "Members and roles" },
      { href: "/docs/approvals", title: "Approvals and audit log" },
      { href: "/docs/limits", title: "Limits and usage" },
      { href: "/docs/data", title: "Delete data" },
    ],
  },
  {
    title: "Reference",
    pages: [
      { href: "/security", title: "Security model" },
      { href: "/docs/faq", title: "FAQ" },
    ],
  },
];

/** Pages inside /docs, in reading order (the Security page lives outside the docs frame). */
export const DOCS_ORDER: DocsPage[] = DOCS_GROUPS.flatMap((g) => g.pages).filter((p) => p.href.startsWith("/docs"));

export function neighbours(href: string): { previous?: DocsPage; next?: DocsPage } {
  const i = DOCS_ORDER.findIndex((p) => p.href === href);
  if (i < 0) return {};
  return { previous: DOCS_ORDER[i - 1], next: DOCS_ORDER[i + 1] };
}
