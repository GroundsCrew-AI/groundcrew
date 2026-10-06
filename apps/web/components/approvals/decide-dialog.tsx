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
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{approve ? "Approve this action?" : "Reject this action?"}</AlertDialogTitle>
          <AlertDialogDescription>
            {approve
              ? `${agentName(approval.run_agent)} will run exactly this, once, and then continue.`
              : `${agentName(approval.run_agent)} will be told it was rejected and will not run it.`}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
            <PolicyTag policy={approval.policy} />
            <span className="truncate">{approval.run_goal}</span>
          </div>
          <ActionCode approval={approval} />
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            className={approve ? undefined : "bg-destructive text-white hover:bg-destructive/90"}
          >
            {approve ? "Approve and run" : "Reject"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
