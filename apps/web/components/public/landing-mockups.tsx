import { Check, CircleAlert, FileCode2, GitPullRequest, Loader2, RotateCcw, X } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";

/*
 * Static pictures of Groundcrew screens for the public landing page. They are drawn in HTML so
 * they follow the theme, carry no live data, and are hidden from assistive technology behind a
 * single text description (the figure's aria-label).
 */

const tint = (color: string) => ({ "--tint": color }) as CSSProperties;

function Frame({ label, title, children }: { label: string; title: string; children: ReactNode }) {
  return (
    <figure aria-label={label} className="overflow-hidden rounded-xl border bg-card shadow-sm">
      <div aria-hidden="true" className="flex items-center gap-2 border-b px-4 py-2.5">
        <span className="size-2 rounded-full bg-border" />
        <span className="size-2 rounded-full bg-border" />
        <span className="size-2 rounded-full bg-border" />
        <span className="ml-2 truncate text-[12px] text-muted-foreground">{title}</span>
      </div>
      <div aria-hidden="true" className="p-4 text-[12.5px] sm:p-5">
        {children}
      </div>
    </figure>
  );
}

function Pill({ color, children }: { color: string; children: ReactNode }) {
  return (
    <span
      className="tint inline-flex h-5 items-center gap-1 rounded-full px-2 text-[11.5px] font-medium whitespace-nowrap"
      style={tint(color)}
    >
      {children}
    </span>
  );
}

/** An incident with its root cause and the rollback waiting for approval. */
export function ApprovalMockup() {
  return (
    <Frame
      label="Illustration of an incident in Groundcrew: the root cause with linked evidence, and a rollback waiting for approval."
      title="Incidents / checkout-api"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Pill color="var(--status-failed)">
          <CircleAlert className="size-3" />
          Open
        </Pill>
        <span className="font-medium">checkout-api tasks keep stopping</span>
      </div>
      <p className="mt-3 text-[11.5px] font-medium text-muted-foreground">Root cause</p>
      <p className="mt-1 leading-relaxed">
        The 14:02 deploy changed <span className="font-mono text-[11.5px]">DATABASE_URL</span> to a host that does
        not resolve. Every new task exits on start.
      </p>
      <ul className="mt-3 space-y-1.5 border-l-2 pl-3 font-mono text-[11px] text-muted-foreground">
        <li>logs 14:03:11 getaddrinfo ENOTFOUND db-new.internal</li>
        <li>ecs 14:02:40 task definition revision 42 deployed</li>
        <li>github a91f3c2 &quot;Move database config&quot;</li>
      </ul>
      <div className="mt-4 rounded-lg border bg-background p-3">
        <div className="flex items-center gap-2">
          <RotateCcw className="size-3.5 text-primary" />
          <span className="font-medium">Roll back checkout-api</span>
          <span className="ml-auto">
            <Pill color="var(--status-waiting)">Needs approval</Pill>
          </span>
        </div>
        <p className="mt-1.5 font-mono text-[11px] text-muted-foreground">task definition revision 42 → 41</p>
        <div className="mt-3 flex gap-2">
          <span className="inline-flex h-7 items-center rounded-md bg-primary px-3 text-[12px] font-medium text-primary-foreground">
            Approve
          </span>
          <span className="inline-flex h-7 items-center rounded-md border px-3 text-[12px]">Reject</span>
        </div>
      </div>
      <p className="mt-3 flex items-center gap-1.5 text-[11.5px] text-muted-foreground">
        <GitPullRequest className="size-3.5" />
        Fix pull request opened: restore the database host
      </p>
    </Frame>
  );
}

const NODES = [
  { x: 16, y: 20, label: "checkout-web", kind: "Vercel" },
  { x: 196, y: 20, label: "checkout-alb", kind: "AWS ALB" },
  { x: 376, y: 20, label: "checkout-api", kind: "ECS service" },
  { x: 16, y: 120, label: "shop repo", kind: "GitHub" },
  { x: 376, y: 120, label: "orders-db", kind: "Aurora" },
] as const;

const EDGES = [
  [0, 1],
  [1, 2],
  [3, 2],
  [2, 4],
] as const;

/** A small resource graph across providers. */
export function MapMockup() {
  const w = 148;
  const h = 44;
  return (
    <Frame label="Illustration of the Groundcrew map: a Vercel app, a GitHub repository, a load balancer, an ECS service, and a database, linked." title="Map">
      <svg viewBox="0 0 540 180" className="h-auto w-full" fill="none">
        {EDGES.map(([a, b]) => {
          const from = NODES[a];
          const to = NODES[b];
          // Same column: straight down; otherwise from the right side to the left side.
          const d =
            from.x === to.x
              ? `M${from.x + w / 2} ${from.y + h} V${to.y}`
              : (() => {
                  const x1 = from.x + w;
                  const y1 = from.y + h / 2;
                  const x2 = to.x;
                  // Lower rows come in low on the left side, clear of the box above.
                  const y2 = to.y + (from.y > to.y ? h - 8 : h / 2);
                  const mid = (x1 + x2) / 2;
                  return `M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`;
                })();
          return (
            <path
              key={`${a}-${b}`}
              d={d}
              className="stroke-muted-foreground/50"
              strokeWidth="1.5"
            />
          );
        })}
        {NODES.map((n) => (
          <g key={n.label}>
            <rect x={n.x} y={n.y} width={w} height={h} rx="8" className="fill-background stroke-border" strokeWidth="1.5" />
            <text x={n.x + 12} y={n.y + 19} className="fill-foreground" fontSize="12.5" fontWeight="600">
              {n.label}
            </text>
            <text x={n.x + 12} y={n.y + 34} className="fill-muted-foreground" fontSize="11">
              {n.kind}
            </text>
          </g>
        ))}
      </svg>
      <p className="mt-2 text-[11.5px] text-muted-foreground">
        checkout-api → orders-db: from the task definition&apos;s DATABASE_HOST name and the security group rule.
      </p>
    </Frame>
  );
}

const STAGES = [
  { state: "ok", title: "Read the repository", detail: "Node 22, Next.js, listens on 3000, health at /api/health" },
  { state: "ok", title: "Wrote a Dockerfile", detail: "multi-stage, runs as a non-root user" },
  { state: "failed", title: "Build attempt 1", detail: "npm ci: package-lock.json is out of date" },
  { state: "ok", title: "Build attempt 2", detail: "fixed: npm install in the deps stage; built in 74 s" },
  { state: "ok", title: "Smoke test", detail: "GET /api/health 200" },
  { state: "waiting", title: "Deploy plan", detail: "1 ECS service, 1 target group, 1 listener rule" },
] as const;

const STAGE_ICON = {
  ok: { icon: Check, color: "var(--status-ok)" },
  failed: { icon: X, color: "var(--status-failed)" },
  waiting: { icon: Loader2, color: "var(--status-waiting)" },
} as const;

/** A deploy run: the build fails once, Builder fixes it, the plan waits for approval. */
export function DeployMockup() {
  return (
    <Frame
      label="Illustration of a Groundcrew deploy run: the first build fails, the second passes, the smoke test passes, and the deploy plan waits for approval."
      title="Deploys / shop-web to ECS"
    >
      <ol className="space-y-3">
        {STAGES.map((s) => {
          const { icon: Icon, color } = STAGE_ICON[s.state];
          return (
            <li key={s.title} className="flex gap-3">
              <span className="tint mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full" style={tint(color)}>
                <Icon className="size-3" />
              </span>
              <div className="min-w-0">
                <p className="font-medium">{s.title}</p>
                <p className="mt-0.5 text-[11.5px] break-words text-muted-foreground">{s.detail}</p>
              </div>
            </li>
          );
        })}
      </ol>
    </Frame>
  );
}

/** A fix pull request and the recovery check after it. */
export function FixMockup() {
  return (
    <Frame
      label="Illustration of a Groundcrew fix: a pull request with a one-line change, and the recovery check that closes the incident."
      title="Fix / checkout-api"
    >
      <div className="flex items-center gap-2">
        <FileCode2 className="size-3.5 text-muted-foreground" />
        <span className="font-mono text-[11.5px]">infra/ecs/checkout-api.tf</span>
      </div>
      <pre className="mt-2 overflow-x-auto rounded-md border bg-background p-3 font-mono text-[11px] leading-relaxed">
        <span className="block text-[var(--status-failed)]">-  DATABASE_HOST = &quot;db-new.internal&quot;</span>
        <span className="block text-[var(--status-ok)]">+  DATABASE_HOST = &quot;orders-db.cluster.internal&quot;</span>
      </pre>
      <div className="mt-4 space-y-2">
        {[
          ["Pull request opened on shop", "ok"],
          ["Running tasks 0 → 3", "ok"],
          ["Health checks passing again", "ok"],
        ].map(([text, state]) => (
          <p key={text} className="flex items-center gap-2">
            <span
              className="tint flex size-4 items-center justify-center rounded-full"
              style={tint(state === "ok" ? "var(--status-ok)" : "var(--status-waiting)")}
            >
              <Check className="size-2.5" />
            </span>
            {text}
          </p>
        ))}
      </div>
      <p className="mt-3 text-[11.5px] text-muted-foreground">Incident resolved. The fix is verified on the same signals that opened it.</p>
    </Frame>
  );
}

const ERRORS = [2, 1, 3, 2, 2, 1, 2, 3, 2, 14, 31, 38, 36, 40] as const;

/** The watcher's signal and Medic's investigation steps. */
export function IncidentMockup() {
  const max = Math.max(...ERRORS);
  return (
    <Frame
      label="Illustration of a Groundcrew incident: a spike in log errors opens the incident, and Medic checks logs, recent deploys, and commits before naming the cause."
      title="Incidents / checkout-api / investigation"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium">Errors in checkout-api logs</span>
        <Pill color="var(--status-failed)">Error spike</Pill>
      </div>
      <div className="mt-3 flex h-16 items-end gap-1">
        {ERRORS.map((v, i) => (
          <span
            key={i}
            className="flex-1 rounded-sm"
            style={{
              height: `${Math.max(6, (v / max) * 100)}%`,
              background: i >= 9 ? "var(--status-failed)" : "var(--border)",
            }}
          />
        ))}
      </div>
      <p className="mt-1.5 flex justify-between font-mono text-[10.5px] text-muted-foreground">
        <span>13:50</span>
        <span>14:02 deploy</span>
        <span>14:10</span>
      </p>
      <p className="mt-4 text-[11.5px] font-medium text-muted-foreground">Medic</p>
      <ol className="mt-2 space-y-2">
        {[
          ["Read the last 200 error lines", "1 pattern: getaddrinfo ENOTFOUND"],
          ["Listed changes before 14:03", "task definition 41 → 42, commit a91f3c2"],
          ["Compared revisions 41 and 42", "DATABASE_URL host changed"],
          ["Searched past incidents", "none with this cause"],
        ].map(([step, found]) => (
          <li key={step} className="flex gap-2.5">
            <span className="tint mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full" style={tint("var(--status-ok)")}>
              <Check className="size-2.5" />
            </span>
            <span className="min-w-0">
              <span className="block">{step}</span>
              <span className="block text-[11.5px] break-words text-muted-foreground">{found}</span>
            </span>
          </li>
        ))}
      </ol>
    </Frame>
  );
}
