"use client";

import { type FormEvent, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useWorkspace } from "@/lib/workspace";
import { useCreateWorkspace } from "@/lib/workspace-admin";

import { toastResult } from "./toast-result";

/** A new workspace you own (one per team, client, or environment); it becomes the current one. */
export function CreateWorkspaceDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const create = useCreateWorkspace();
  const { setWorkspaceId } = useWorkspace();
  const [name, setName] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    const created = await toastResult(create.mutateAsync(trimmed), (w) => `Created ${w.name}`);
    if (created) {
      setWorkspaceId(created.id);
      setName("");
      onOpenChange(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>New workspace</DialogTitle>
            <DialogDescription>
              Workspaces keep connections, runs, incidents, and members apart. You will be its owner.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="new-workspace-name" className="text-[13px]">
              Name
            </Label>
            <Input
              id="new-workspace-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Acme production"
              maxLength={200}
              autoFocus
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!name.trim() || create.isPending}>
              {create.isPending ? "Creating..." : "Create workspace"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
