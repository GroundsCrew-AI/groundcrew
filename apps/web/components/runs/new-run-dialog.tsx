"use client";

import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, type ReactNode, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useCreateRun } from "@/lib/queries";
import { useWorkspace } from "@/lib/workspace";

const AGENTS = [
  {
    id: "echo_ops",
    name: "Echo Ops",
    hint: "Test agent on Nemotron Nano with fake tools. Restarting a service needs your approval.",
  },
];
const DEFAULT_GOAL = "List the services and restart the one named api.";

/** Dev helper until the real agents land: start a run and open its timeline. */
export function NewRunDialog({ trigger }: { trigger?: ReactNode }) {
  const router = useRouter();
  const { workspace } = useWorkspace();
  const create = useCreateRun();
  const [open, setOpen] = useState(false);
  const [agent, setAgent] = useState(AGENTS[0]!.id);
  const [goal, setGoal] = useState(DEFAULT_GOAL);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!workspace) return;
    create.mutate(
      { workspace_id: workspace.id, agent, goal: goal.trim() },
      {
        onSuccess: (run) => {
          setOpen(false);
          toast.success("Run started");
          router.push(`/runs/${run.id}`);
        },
        onError: (error) => toast.error(error.message),
      },
    );
  }

  const selected = AGENTS.find((a) => a.id === agent);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" disabled={!workspace}>
            <Plus aria-hidden="true" />
            New run
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={submit} className="space-y-5">
          <DialogHeader>
            <DialogTitle>Start a test run</DialogTitle>
            <DialogDescription>
              Runs in {workspace?.name ?? "your workspace"}. You can watch it live and stop it at any time.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="run-agent">Agent</Label>
            <Select value={agent} onValueChange={setAgent}>
              <SelectTrigger id="run-agent" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {AGENTS.map((a) => (
                  <SelectItem key={a.id} value={a.id}>
                    {a.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {selected && <p className="text-[12px] text-muted-foreground">{selected.hint}</p>}
          </div>
          <div className="space-y-2">
            <Label htmlFor="run-goal">Goal</Label>
            <Textarea
              id="run-goal"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              rows={3}
              maxLength={4000}
              required
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!goal.trim() || create.isPending}>
              {create.isPending ? "Starting" : "Start run"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
