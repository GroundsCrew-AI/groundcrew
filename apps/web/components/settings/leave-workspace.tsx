"use client";

import { LogOut } from "lucide-react";
import { useRouter } from "next/navigation";

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
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/lib/workspace";
import { useLeaveWorkspace } from "@/lib/workspace-admin";

import { toastResult } from "./toast-result";

/**
 * Leave the workspace. The last owner cannot (the API answers 409), so the button is disabled
 * with the reason. Afterwards the app switches to another workspace and opens its overview.
 */
export function LeaveWorkspace({
  workspaceId,
  workspaceName,
  userId,
  onlyOwner,
}: {
  workspaceId: string;
  workspaceName: string;
  userId: string;
  onlyOwner: boolean;
}) {
  const router = useRouter();
  const { workspaces, setWorkspaceId } = useWorkspace();
  const leave = useLeaveWorkspace();

  async function confirm() {
    const left = await toastResult(leave.mutateAsync({ workspaceId, userId }), () => `You left ${workspaceName}`);
    if (!left) return;
    const next = workspaces.find((w) => w.id !== workspaceId);
    if (next) setWorkspaceId(next.id);
    router.push("/");
  }

  return (
    <div className="space-y-2">
      <AlertDialog>
        <AlertDialogTrigger asChild>
          <Button
            variant="outline"
            disabled={onlyOwner || leave.isPending}
            aria-describedby={onlyOwner ? "leave-only-owner" : undefined}
          >
            <LogOut aria-hidden="true" />
            {leave.isPending ? "Leaving" : "Leave workspace"}
          </Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Leave {workspaceName}?</AlertDialogTitle>
            <AlertDialogDescription>
              You lose access to its connections, runs, and incidents at once. An owner can add you again.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction variant="destructive-solid" onClick={() => void confirm()}>
              Leave workspace
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      {onlyOwner && (
        <p id="leave-only-owner" className="text-[12px] text-muted-foreground">
          You are the only owner. Make someone else an owner before you leave.
        </p>
      )}
    </div>
  );
}
