import {
  AppWindow,
  Archive,
  Box,
  Boxes,
  Container,
  Database,
  GitBranch,
  Globe,
  KeyRound,
  Layers,
  type LucideIcon,
  Network,
  Package,
  Rocket,
  ScrollText,
  Server,
  Shield,
  ShieldCheck,
  SquareFunction,
  Workflow,
} from "lucide-react";
import type { CSSProperties } from "react";

const ICONS: Record<string, LucideIcon> = {
  "ecs:cluster": Boxes,
  "ecs:service": Container,
  "ecr:repository": Package,
  "rds:db": Database,
  "rds:cluster": Database,
  "dynamodb:table": Database,
  "microsoft.dbforpostgresql/flexibleservers": Database,
  "secretsmanager:secret": KeyRound,
  "microsoft.keyvault/vaults": KeyRound,
  "logs:log-group": ScrollText,
  "iam:role": ShieldCheck,
  "ec2:security-group": Shield,
  "ec2:vpc": Network,
  "ec2:instance": Server,
  "agent:host": Server,
  "agent:service": Container,
  "lambda:function": SquareFunction,
  "s3:bucket": Archive,
  "github:repository": GitBranch,
  "github:workflow": Workflow,
  "vercel:project": Globe,
  "vercel:deployment": Rocket,
  "k8s:deployment": Layers,
  "k8s:statefulset": Database,
  "k8s:service": Network,
  "k8s:ingress": Globe,
  "microsoft.web/sites": AppWindow,
  "microsoft.containerregistry/registries": Package,
};

export function ResourceIcon({ type, className }: { type: string; className?: string }) {
  const Icon = ICONS[type] ?? Box;
  return <Icon aria-hidden="true" className={className} />;
}

export const PROVIDER_MARKS: Record<string, string> = {
  aws: "AWS",
  azure: "Az",
  github: "GH",
  k8s: "K8s",
  vercel: "Vc",
  vm: "VM",
  agent: "Agt",
  gcp: "GCP",
  groundcrew: "GC",
  tooling: "Tools",
};

/** A hue per provider, so groups and their resources are easy to tell apart on the map. */
export const PROVIDER_COLORS: Record<string, string> = {
  aws: "var(--area-docs)",
  azure: "var(--area-runs)",
  gcp: "var(--area-overview)",
  github: "var(--area-deploy)",
  k8s: "var(--area-overview)",
  vercel: "var(--foreground)",
  vm: "var(--area-map)",
  agent: "var(--area-connections)",
  groundcrew: "var(--primary)",
  tooling: "var(--area-settings)",
};

export function providerColor(provider: string): string {
  return PROVIDER_COLORS[provider] ?? "var(--area-settings)";
}

export function ProviderMark({ provider, className }: { provider: string; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`flex shrink-0 items-center justify-center rounded-md border bg-card font-mono text-muted-foreground ${className ?? "size-9 text-[11px]"}`}
    >
      {PROVIDER_MARKS[provider] ?? provider.slice(0, 3)}
    </span>
  );
}

export const COVERAGE: Record<string, { label: string; color: string; hint: string }> = {
  observed: {
    label: "Observed",
    color: "var(--status-neutral)",
    hint: "Groundcrew knows it exists and how it is configured.",
  },
  diagnosable: {
    label: "Diagnosable",
    color: "var(--status-ok)",
    hint: "Logs or metrics are linked, so Medic can investigate it.",
  },
  actionable: {
    label: "Actionable",
    color: "var(--primary)",
    hint: "A deploy target can act on it, with your approval.",
  },
};

export function CoverageBadge({ coverage }: { coverage: string }) {
  const meta = COVERAGE[coverage] ?? COVERAGE.observed!;
  return (
    <span
      className="tint inline-flex h-5 items-center gap-1.5 rounded-full px-2 text-[12px] font-medium whitespace-nowrap"
      style={{ "--tint": meta.color } as CSSProperties}
      title={meta.hint}
    >
      <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
      {meta.label}
    </span>
  );
}

export const CONFIDENCE: Record<string, { label: string; color: string }> = {
  high: { label: "High", color: "var(--status-ok)" },
  medium: { label: "Medium", color: "var(--status-waiting)" },
  low: { label: "Low", color: "var(--status-neutral)" },
};

export function ConfidenceTag({ confidence }: { confidence: string }) {
  const meta = CONFIDENCE[confidence] ?? CONFIDENCE.low!;
  return (
    <span
      className="tint inline-flex h-[18px] items-center rounded px-1.5 text-[11px] font-medium"
      style={{ "--tint": meta.color } as CSSProperties}
      title={`${meta.label} confidence`}
    >
      {meta.label}
    </span>
  );
}
