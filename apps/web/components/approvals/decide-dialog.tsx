"use client";

import { type ReactNode, useState } from "react";

import { ActionCode } from "@/components/runs/approval-card";
import { PolicyTag } from "@/components/runs/badges";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import type { Approval, Decision } from "@/lib/api/client";
import { agentName } from "@/lib/format";

/** Confirm a decision. Repeats the exact action so nobody approves the wrong thing. */
export function DecideDialog({
  approval,
  decision,
  onConfirm,
  children,
}: {
  approval: Approval;
  decision: Decision;
  onConfirm: () => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const approve = decision === "approve";
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>{children}</AlertDialogTrigger>
      <AlertDialogContent className="sm:max-w-lg">
        <AlertDialogHeader>
          <AlertDialogTitle>{approve ? "Approve this action?" : "Reject this action?"}</AlertDialogTitle>
          <AlertDialogDescription>
            {approve
              ? `${agentName(approval.run_agent)} will run exactly this, once, and then continue.`
              : `${agentName(approval.run_agent)} will be told it was rejected and will not run it.`}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {/* min-w-0: a long line in the action must not widen the dialog past the screen. */}
        <div className="min-w-0 space-y-2">
          <div className="flex min-w-0 items-center gap-2 text-[12px] text-muted-foreground">
            <PolicyTag policy={approval.policy} />
            <span className="truncate">{approval.run_goal}</span>
          </div>
          <ActionCode approval={approval} className="max-h-64 overflow-y-auto break-all whitespace-pre-wrap" />
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            variant={approve ? "default" : "destructive-solid"}
          >
            {approve ? "Approve and run" : "Reject"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
