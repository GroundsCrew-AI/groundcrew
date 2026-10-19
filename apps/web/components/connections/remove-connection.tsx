"use client";

import { Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { type Connection, type ConnectionDeleted, useDeleteConnection } from "@/lib/discovery";

/** Remove a connection and what Groundcrew stored for it, then show what the customer still
 * has to remove on their side (the AWS stack, the GitHub App, the Vercel token). */
export function RemoveConnection({
  connection,
  disabled = false,
  describedBy,
}: {
  connection: Connection;
  /** Members cannot remove connections; the reason is the element `describedBy` points at. */
  disabled?: boolean;
  describedBy?: string;
}) {
  const remove = useDeleteConnection();
  const [done, setDone] = useState<ConnectionDeleted | null>(null);
  return (
    <>
      <AlertDialog>
        <AlertDialogTrigger asChild>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={`Remove ${connection.name}`}
            disabled={disabled}
            aria-describedby={describedBy}
          >
            <Trash2 />
          </Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove {connection.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              Groundcrew deletes this connection, its stored credentials, and the {connection.resource_count}{" "}
              resources it found, with their relationships. Nothing changes in your account. You will see
              how to remove Groundcrew&apos;s access on your side next.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() =>
                remove.mutate(connection, {
                  onSuccess: (result) => setDone(result),
                  onError: (error) => toast.error(error.message),
                })
              }
            >
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <Dialog open={done !== null} onOpenChange={(open) => !open && setDone(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Removed {connection.name}</DialogTitle>
            <DialogDescription>
              Deleted {done?.removed_resources ?? 0} resources from the map. To remove access completely:
            </DialogDescription>
          </DialogHeader>
          <ol className="list-decimal space-y-2 pl-5 text-[12.5px] leading-relaxed">
            {done?.cleanup.map((step) => (
              <li key={step} className={step.startsWith("Or run:") ? "font-mono text-[12px] break-all" : undefined}>
                {step}
              </li>
            ))}
          </ol>
          <DialogFooter>
            <Button onClick={() => setDone(null)}>Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
