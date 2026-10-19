"use client";

import { Check, X } from "lucide-react";
import Link from "next/link";
import { Suspense } from "react";
import { toast } from "sonner";

import { DecideDialog } from "@/components/approvals/decide-dialog";
import { ActionCode, DecidedBy } from "@/components/runs/approval-card";
import { PolicyTag } from "@/components/runs/badges";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { ApprovalsIllustration } from "@/components/states/illustrations";
import { EmptyState, ErrorState, RowsSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { Approval, Decision } from "@/lib/api/client";
import { agentName, formatRelative, formatTimestamp } from "@/lib/format";
import { useApprovals, useDecide } from "@/lib/queries";
import { useSearchParam } from "@/lib/use-search-param";
import { useWorkspace } from "@/lib/workspace";

function ApprovalItem({ approval, decided = false }: { approval: Approval; decided?: boolean }) {
  const decide = useDecide();
  const act = (decision: Decision) =>
    decide.mutate(
      { approval, decision },
      {
        onSuccess: () => toast.success(decision === "approve" ? "Approved. The run continues." : "Rejected"),
        onError: (error) => toast.error(error.message),
      },
    );
  return (
    <li className="grid gap-3 px-4 py-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)] md:gap-6 md:px-5">
      <div className="min-w-0 space-y-1">
        <div className="flex items-center gap-2">
          <span className="font-medium">{agentName(approval.run_agent)}</span>
          <PolicyTag policy={approval.policy} />
        </div>
        <Link
          href={`/runs/${approval.run_id}`}
          className="line-clamp-2 text-muted-foreground hover:text-foreground"
        >
          {approval.run_goal}
        </Link>
        <time
          dateTime={approval.created_at}
          title={formatTimestamp(approval.created_at)}
          className="block text-[12px] text-subtle"
        >
          Requested {formatRelative(approval.created_at)}
        </time>
      </div>
      <div className="min-w-0 space-y-3">
        <ActionCode approval={approval} />
        {decided ? (
          <DecidedBy approval={approval} />
        ) : (
          <div className="flex gap-2">
            <DecideDialog approval={approval} decision="approve" onConfirm={() => act("approve")}>
              <Button size="sm" disabled={decide.isPending}>
                <Check aria-hidden="true" />
                Approve
              </Button>
            </DecideDialog>
            <DecideDialog approval={approval} decision="reject" onConfirm={() => act("reject")}>
              <Button size="sm" variant="outline" disabled={decide.isPending}>
                <X aria-hidden="true" />
                Reject
              </Button>
            </DecideDialog>
          </div>
        )}
      </div>
    </li>
  );
}

function ApprovalList({ tab }: { tab: "pending" | "decided" }) {
  const { workspace } = useWorkspace();
  const approvals = useApprovals(workspace?.id, tab);
  if (approvals.error) {
    return (
      <div className="p-4">
        <ErrorState error={approvals.error} onRetry={() => void approvals.refetch()} />
      </div>
    );
  }
  if (approvals.isLoading || !workspace) return <RowsSkeleton rows={3} />;
  const items = approvals.data ?? [];
  if (items.length === 0) {
    return (
      <div className="px-5">
        {tab === "pending" ? (
          <EmptyState illustration={<ApprovalsIllustration />} title="Nothing to approve">
            When an agent wants to change something, the exact action waits here until you approve or
            reject it.
          </EmptyState>
        ) : (
          <EmptyState title="No decisions yet">Approved and rejected actions show up here with who decided.</EmptyState>
        )}
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border">
      {items.map((approval) => (
        <ApprovalItem key={approval.id} approval={approval} decided={tab === "decided"} />
      ))}
    </ul>
  );
}

export default function ApprovalsPage() {
  // The tab lives in the URL (useSearchParams).
  return (
    <Suspense>
      <Approvals />
    </Suspense>
  );
}

/** /approvals?tab=decided opens the decided list; switching tabs adds a history entry. */
function Approvals() {
  const { workspace } = useWorkspace();
  const pending = useApprovals(workspace?.id, "pending");
  const count = pending.data?.length ?? 0;
  const [param, setParam] = useSearchParam("tab");
  const tab = param === "decided" ? "decided" : "pending";
  return (
    <>
      <PageHeader title="Approvals" />
      <PageBody className="max-w-5xl">
        <Tabs value={tab} onValueChange={(v) => setParam(v === "decided" ? "decided" : null)}>
          <TabsList>
            <TabsTrigger value="pending">
              Pending
              {count > 0 && <span className="tabular ml-1.5 text-[var(--status-waiting)]">{count}</span>}
            </TabsTrigger>
            <TabsTrigger value="decided">Decided</TabsTrigger>
          </TabsList>
          <div className="mt-4 rounded-lg border">
            <TabsContent value="pending" className="mt-0">
              <ApprovalList tab="pending" />
            </TabsContent>
            <TabsContent value="decided" className="mt-0">
              <ApprovalList tab="decided" />
            </TabsContent>
          </div>
        </Tabs>
      </PageBody>
    </>
  );
}
