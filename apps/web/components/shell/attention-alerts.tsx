"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";
import { toast } from "sonner";

import { useSummary } from "@/lib/queries";
import { useWorkspace } from "@/lib/workspace";

/**
 * Tells people when something new needs them while the app is open in another page or tab: a
 * toast with a link when pending approvals or open incidents go up, and the total in the tab
 * title ("(2) Runs | Groundcrew"). It reads the workspace summary the sidebar already polls, so
 * it costs no extra request. Polling pauses in hidden tabs, so the title catches up on return.
 */

export type AttentionCounts = { approvals: number; incidents: number };

export type AttentionAlert = { kind: "approvals" | "incidents"; added: number; href: string; message: string };

/** What went up between two polls. Counts that went down or stayed the same say nothing. */
export function attentionAlerts(previous: AttentionCounts, next: AttentionCounts): AttentionAlert[] {
  const alerts: AttentionAlert[] = [];
  const approvals = next.approvals - previous.approvals;
  if (approvals > 0) {
    alerts.push({
      kind: "approvals",
      added: approvals,
      href: "/approvals",
      message: approvals === 1 ? "An action is waiting for your approval" : `${approvals} actions are waiting for your approval`,
    });
  }
  const incidents = next.incidents - previous.incidents;
  if (incidents > 0) {
    alerts.push({
      kind: "incidents",
      added: incidents,
      href: "/incidents",
      message: incidents === 1 ? "A new incident is open" : `${incidents} new incidents are open`,
    });
  }
  return alerts;
}

const COUNT_PREFIX = /^\(\d+\+?\) /;

/** "Runs | Groundcrew" -> "(3) Runs | Groundcrew"; a zero count removes the prefix. */
export function titleWithCount(title: string, count: number): string {
  const base = title.replace(COUNT_PREFIX, "");
  if (count <= 0) return base;
  return `(${count > 99 ? "99+" : count}) ${base}`;
}

/**
 * Keep the count in document.title. Next.js rewrites the title on every navigation, so a
 * MutationObserver on <head> puts the prefix back whenever that happens.
 */
export function useTitleCount(count: number) {
  useEffect(() => {
    const apply = () => {
      const next = titleWithCount(document.title, count);
      if (document.title !== next) document.title = next;
    };
    apply();
    const observer = new MutationObserver(apply);
    observer.observe(document.head, { subtree: true, childList: true, characterData: true });
    return () => {
      observer.disconnect();
      document.title = titleWithCount(document.title, 0);
    };
  }, [count]);
}

export function AttentionAlerts() {
  const { workspace } = useWorkspace();
  const summary = useSummary(workspace?.id);
  const pathname = usePathname();
  const path = useRef(pathname);
  const last = useRef<{ workspaceId: string; counts: AttentionCounts } | null>(null);
  const workspaceId = workspace?.id;
  const approvals = summary.data?.pending_approvals;
  const incidents = summary.data?.open_incidents;

  useEffect(() => {
    path.current = pathname;
  }, [pathname]);

  useEffect(() => {
    if (!workspaceId || approvals === undefined) return;
    const counts = { approvals, incidents: incidents ?? 0 };
    const previous = last.current;
    last.current = { workspaceId, counts };
    // The first numbers after loading or switching workspace are the baseline, not news.
    if (!previous || previous.workspaceId !== workspaceId) return;
    for (const alert of attentionAlerts(previous.counts, counts)) {
      // The page itself already shows the new item.
      if (path.current === alert.href || path.current.startsWith(`${alert.href}/`)) continue;
      const id = `attention-${alert.kind}`;
      toast(alert.message, {
        id,
        action: (
          <Link
            href={alert.href}
            onClick={() => toast.dismiss(id)}
            className="ml-auto shrink-0 rounded-md border px-2 py-1 text-[12px] font-medium hover:bg-accent"
          >
            {alert.kind === "approvals" ? "Review" : "Open incidents"}
          </Link>
        ),
      });
    }
  }, [workspaceId, approvals, incidents]);

  useTitleCount(workspaceId !== undefined && approvals !== undefined ? approvals + (incidents ?? 0) : 0);
  return null;
}
