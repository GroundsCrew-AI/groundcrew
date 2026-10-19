"use client";

import { cn } from "cn";
import { Check } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useState } from "react";

import { RunDiscoveryButton } from "@/components/connections/run-discovery-button";
import { Button } from "@/components/ui/button";
import { useDeploys } from "@/lib/deploys";
import { useConnections } from "@/lib/discovery";
import { useWorkspaceConfig } from "@/lib/queries";
import {
  readSetupHidden,
  type SetupChecklist,
  setupChecklist,
  type SetupStep,
  type SetupStepId,
  writeSetupHidden,
} from "@/lib/setup-checklist";
import { useWorkspace } from "@/lib/workspace";

type StepCopy = { title: string; body: ReactNode; action?: ReactNode; doneNote?: ReactNode };

function copy(step: SetupStep, checklist: SetupChecklist, canManage: boolean): StepCopy {
  const id: SetupStepId = step.id;
  switch (id) {
    case "connect":
      return {
        title: "Connect your cloud or code",
        body: (
          <>
            AWS, Azure, Kubernetes, or Vercel for what runs, GitHub for the code. Access starts read-only.
            {!canManage && " Only workspace owners can add connections, so ask one of them."}
          </>
        ),
        action: canManage ? (
          <Button size="sm" asChild>
            <Link href="/connections">Add a connection</Link>
          </Button>
        ) : undefined,
        doneNote: (
          <Link href="/connections" className="hover:text-foreground">
            Connections
          </Link>
        ),
      };
    case "discover":
      return {
        title: "Discover your infrastructure",
        body: checklist.foundNothing
          ? "The last discovery found nothing. Check each connection's region and access on Connections, then run it again."
          : "Groundcrew reads each connection and maps how your services, databases, and repositories connect.",
        action: <RunDiscoveryButton variant={checklist.foundNothing ? "outline" : "default"} />,
        doneNote: (
          <Link href="/map" className="hover:text-foreground">
            {checklist.resources} {checklist.resources === 1 ? "resource" : "resources"} on the map
          </Link>
        ),
      };
    case "deploy":
      return {
        title: "Deploy an app",
        body: "Pick a repository and where it runs. Groundcrew builds it, shows you the plan, and waits for your approval before anything changes.",
        action: (
          <Button size="sm" variant="outline" asChild>
            <Link href="/deploy">New deploy</Link>
          </Button>
        ),
      };
    case "watch":
      return {
        title: "Watch for incidents",
        body: checklist.watchOn
          ? "Medic starts watching once discovery has found your services. It opens an incident when health checks fail, tasks crash, or errors spike, and proposes a fix for you to approve."
          : "Incident watch is off for this workspace, so only people open incidents. Owners can turn it on in Settings.",
        action: checklist.watchOn ? undefined : (
          <Button size="sm" variant="outline" asChild>
            <Link href="/settings">Open Settings</Link>
          </Button>
        ),
        doneNote: "Medic is watching",
      };
  }
}

/** The checklist card. Pure, so it can be shown for any state. */
export function SetupCard({
  checklist,
  canManage,
  onHide,
}: {
  checklist: SetupChecklist;
  canManage: boolean;
  onHide?: () => void;
}) {
  const current = checklist.steps.findIndex((s) => !s.done && s.ready);
  return (
    <section aria-labelledby="setup-title" className="rounded-lg border bg-card">
      <div className="flex items-start justify-between gap-4 px-5 pt-4 pb-3">
        <div className="min-w-0">
          <h2 id="setup-title" className="text-[14px] font-medium">
            Get started
          </h2>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            <span className="tabular">
              {checklist.doneCount} of {checklist.steps.length}
            </span>{" "}
            done. Connect what you run, and Groundcrew maps it, deploys to it, and watches it.
          </p>
        </div>
        {onHide && (
          <Button variant="ghost" size="sm" className="-mr-2 text-muted-foreground" onClick={onHide}>
            Hide
          </Button>
        )}
      </div>
      <ol className="divide-y border-t">
        {checklist.steps.map((step, i) => {
          const text = copy(step, checklist, canManage);
          const active = i === current;
          return (
            <li
              key={step.id}
              aria-current={active ? "step" : undefined}
              className={cn("flex gap-3 px-5 py-3", !step.done && !active && "text-muted-foreground")}
            >
              <span
                aria-hidden="true"
                className={cn(
                  "tabular mt-px flex size-5 shrink-0 items-center justify-center rounded-full border text-[11px]",
                  step.done && "border-transparent bg-[var(--status-ok)] text-background",
                  active && "border-primary text-primary",
                )}
              >
                {step.done ? <Check className="size-3" strokeWidth={2.5} /> : i + 1}
              </span>
              <div className="min-w-0 flex-1 space-y-1">
                <p className="flex flex-wrap items-baseline justify-between gap-x-3 text-[13px]">
                  <span className={cn(active && "font-medium", step.done && "text-muted-foreground")}>
                    {text.title}
                    {step.done && <span className="sr-only"> (done)</span>}
                  </span>
                  {step.done && text.doneNote && (
                    <span className="text-[12px] text-muted-foreground">{text.doneNote}</span>
                  )}
                </p>
                {!step.done && <p className="text-[12.5px] leading-relaxed text-muted-foreground">{text.body}</p>}
                {!step.done && step.ready && text.action && <div className="pt-1.5">{text.action}</div>}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/**
 * The overview's first-run checklist, from the workspace's connections, deploys, and settings.
 * It shows nothing until those have loaded (so it never flashes for a set-up workspace). Once
 * every step is done, or someone hides it, it is remembered for that workspace on this browser,
 * so a set-up workspace's overview does not keep loading (and polling) what the list needs.
 */
export function GettingStarted() {
  const { workspace } = useWorkspace();
  const [hidden, setHidden] = useState<string | null>(null);
  if (!workspace || hidden === workspace.id || readSetupHidden(workspace.id)) return null;
  const hide = () => {
    writeSetupHidden(workspace.id);
    setHidden(workspace.id);
  };
  return (
    <SetupLoader key={workspace.id} workspaceId={workspace.id} canManage={workspace.role === "owner"} onHide={hide} />
  );
}

function SetupLoader({
  workspaceId,
  canManage,
  onHide,
}: {
  workspaceId: string;
  canManage: boolean;
  onHide: () => void;
}) {
  const connections = useConnections(workspaceId);
  const deploys = useDeploys(workspaceId);
  const config = useWorkspaceConfig(workspaceId);
  // Deploys and settings only refine the list: if they fail to load, it still shows.
  const deployItems = deploys.data?.items ?? (deploys.error ? [] : undefined);
  const checklist =
    connections.data && deployItems && !config.isLoading
      ? setupChecklist({
          connections: connections.data,
          deploys: deployItems,
          incidentWatch: config.data?.incident_watch ?? true,
        })
      : null;
  const complete = checklist?.complete ?? false;

  useEffect(() => {
    if (complete) onHide();
  }, [complete, onHide]);

  if (!checklist || complete) return null;
  return <SetupCard checklist={checklist} canManage={canManage} onHide={onHide} />;
}
