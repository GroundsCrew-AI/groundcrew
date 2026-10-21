"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { signOutAfterAccountDeleted } from "@/app/actions";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, type Schemas, unwrap } from "@/lib/api/client";
import { meKey, useWorkspace } from "@/lib/workspace";

type Cleanup = Schemas["ConnectionCleanup"];

/** Delete the workspace (owners), after typing its name. Shows what to remove in the connected
 * accounts afterwards, since Groundcrew changes nothing there. */
export function DeleteWorkspace({ workspaceId, workspaceName }: { workspaceId: string; workspaceName: string }) {
  const router = useRouter();
  const client = useQueryClient();
  const { workspaces, setWorkspaceId } = useWorkspace();
  const [typed, setTyped] = useState("");
  const [cleanup, setCleanup] = useState<Cleanup[] | null>(null);
  const remove = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/workspaces/{workspace_id}", {
          params: { path: { workspace_id: workspaceId } },
          body: { confirm_name: typed },
        }),
      ),
    onSuccess: async (result) => {
      const next = workspaces.find((w) => w.id !== workspaceId);
      if (next) setWorkspaceId(next.id);
      await client.invalidateQueries({ queryKey: meKey });
      setCleanup(result.cleanup);
    },
  });

  if (cleanup) {
    return (
      <div role="status" className="space-y-2 rounded-lg border px-4 py-3 text-[12.5px]">
        <p className="font-medium">{workspaceName} is deleted.</p>
        {cleanup.length > 0 && (
          <>
            <p className="text-muted-foreground">To end Groundcrew&apos;s access in the connected accounts too:</p>
            <ul className="list-disc space-y-1 pl-5">
              {cleanup.map((c) => (
                <li key={c.connection}>
                  <span className="font-medium">{c.connection}</span>: {c.steps.join(" ")}
                </li>
              ))}
            </ul>
          </>
        )}
        <Button size="sm" variant="outline" onClick={() => router.push("/")}>
          Done
        </Button>
      </div>
    );
  }

  return (
    <AlertDialog onOpenChange={() => setTyped("")}>
      <AlertDialogTrigger asChild>
        <Button variant="outline" className="text-[var(--status-failed)]">
          <Trash2 aria-hidden="true" />
          Delete workspace
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete {workspaceName}?</AlertDialogTitle>
          <AlertDialogDescription>
            Everything Groundcrew stored for it goes at once: members and invites, connections and their credentials,
            the map, runs, approvals, incidents, and the activity log. Apps it deployed keep running in your accounts.
            This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor="delete-workspace-name" className="text-[12px] font-normal text-muted-foreground">
            Type <span className="font-medium text-foreground">{workspaceName}</span> to confirm
          </Label>
          <Input id="delete-workspace-name" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          {remove.error && (
            <p role="alert" className="text-[12px] text-[var(--status-failed)]">
              {remove.error.message}
            </p>
          )}
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button
            variant="destructive-solid"
            disabled={typed.trim() !== workspaceName || remove.isPending}
            onClick={() => remove.mutate()}
          >
            {remove.isPending ? "Deleting" : "Delete workspace"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

const CONFIRM = "delete my account";

/** Delete your account, after a preview of which workspaces go with it. */
export function DeleteAccount() {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const preview = useQuery({
    queryKey: ["account-deletion"],
    enabled: open,
    queryFn: async () => unwrap(await api.GET("/me/account-deletion")),
  });
  const remove = useMutation({
    mutationFn: async () => unwrap(await api.DELETE("/me", { body: { confirm: typed } })),
    onSuccess: () => void signOutAfterAccountDeleted(),
  });
  const plan = preview.data;
  const blocked = (plan?.blocked_by.length ?? 0) > 0;

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        setTyped("");
      }}
    >
      <AlertDialogTrigger asChild>
        <Button variant="outline" className="text-[var(--status-failed)]">
          <Trash2 aria-hidden="true" />
          Delete account
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete your account?</AlertDialogTitle>
          <AlertDialogDescription>
            Your sign-in and memberships go at once, with every workspace only you are in. Your name stays on approvals
            you made in shared workspaces. This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {plan && (
          <div className="space-y-2 text-[12.5px]">
            {plan.deletes.length > 0 && (
              <p>
                Deleted with it: <span className="font-medium">{plan.deletes.map((w) => w.name).join(", ")}</span>.
              </p>
            )}
            {plan.leaves.length > 0 && <p>You leave: {plan.leaves.map((w) => w.name).join(", ")}.</p>}
            {blocked && (
              <p role="alert" className="text-[var(--status-failed)]">
                You are the only owner of {plan.blocked_by.map((w) => w.name).join(", ")}. Make someone else an owner
                there first, or delete the workspace.
              </p>
            )}
          </div>
        )}
        {!blocked && (
          <div className="space-y-1.5">
            <Label htmlFor="delete-account-confirm" className="text-[12px] font-normal text-muted-foreground">
              Type <span className="font-medium text-foreground">{CONFIRM}</span> to confirm
            </Label>
            <Input
              id="delete-account-confirm"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
            />
          </div>
        )}
        {remove.error && (
          <p role="alert" className="text-[12px] text-[var(--status-failed)]">
            {remove.error.message}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button
            variant="destructive-solid"
            disabled={!plan || blocked || typed.trim().toLowerCase() !== CONFIRM || remove.isPending}
            onClick={() => remove.mutate()}
          >
            {remove.isPending ? "Deleting" : "Delete account"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
