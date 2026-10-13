"use client";

import { Siren } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
import type { Schemas } from "@/lib/api/client";
import { useGraph } from "@/lib/discovery";
import { typeLabel } from "@/lib/graph";
import { type Severity, SEVERITY, useReportIncident } from "@/lib/incidents";
import { useWorkspace } from "@/lib/workspace";

type Resource = Schemas["ResourceOut"];

// Services, databases, and front ends are what people see break; the rest is reached through
// the graph from them.
const REPORTABLE = /^(ecs:service|rds:db|vercel:project|lambda:function|k8s:|azure:)/;

/** "Report incident": pick what looks broken, say what you see; Medic starts at once. */
export function ReportIncidentDialog() {
  const router = useRouter();
  const { workspace } = useWorkspace();
  const graph = useGraph(workspace?.id, false);
  const report = useReportIncident();
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState<Severity | "auto">("auto");
  const [test, setTest] = useState(false);

  const resources = useMemo<Resource[]>(
    () =>
      (graph.data?.nodes ?? [])
        .filter((r) => REPORTABLE.test(r.type))
        .sort((a, b) => a.name.localeCompare(b.name)),
    [graph.data],
  );

  function toggle(id: string, on: boolean) {
    setSelected((ids) => (on ? [...ids, id] : ids.filter((x) => x !== id)));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!workspace || selected.length === 0) return;
    report.mutate(
      {
        workspace_id: workspace.id,
        resource_ids: selected,
        description: description.trim() || null,
        severity: severity === "auto" ? null : severity,
        test,
      },
      {
        onSuccess: (created) => {
          setOpen(false);
          toast.success(created.deduped ? "Added to the open incident" : "Incident reported. Medic is on it.");
          router.push(`/incidents/${created.incident_id}`);
        },
        onError: (error) => toast.error(error.message),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" disabled={!workspace}>
          <Siren aria-hidden="true" />
          Report incident
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <form onSubmit={submit} className="space-y-5">
          <DialogHeader>
            <DialogTitle>Report an incident</DialogTitle>
            <DialogDescription>
              Pick what looks broken. Medic reads its logs, metrics, recent changes, and the resources it depends
              on, then proposes fixes for you to approve.
            </DialogDescription>
          </DialogHeader>
          <fieldset className="space-y-2">
            <legend className="text-[13px] font-medium">Affected resources</legend>
            {graph.isLoading ? (
              <p className="text-[12.5px] text-muted-foreground">Loading the map</p>
            ) : resources.length === 0 ? (
              <p className="text-[12.5px] text-muted-foreground">
                No services found yet. Connect a cloud account and run discovery first.
              </p>
            ) : (
              <ul className="max-h-56 space-y-1.5 overflow-auto rounded-md border px-3 py-2">
                {resources.map((r) => (
                  <li key={r.id} className="flex items-center gap-2">
                    <Checkbox
                      id={`report-${r.id}`}
                      checked={selected.includes(r.id)}
                      onCheckedChange={(checked) => toggle(r.id, checked === true)}
                    />
                    <Label htmlFor={`report-${r.id}`} className="font-normal">
                      {r.name} <span className="text-muted-foreground">{typeLabel(r.type)}</span>
                    </Label>
                  </li>
                ))}
              </ul>
            )}
          </fieldset>
          <div className="space-y-2">
            <Label htmlFor="report-description">What do you see? (optional)</Label>
            <Textarea
              id="report-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="New orders stay pending since about 10:00"
              maxLength={4000}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="report-severity">Severity</Label>
            <Select value={severity} onValueChange={(v) => setSeverity(v as Severity | "auto")}>
              <SelectTrigger id="report-severity" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">Let Medic decide</SelectItem>
                {(Object.keys(SEVERITY) as Severity[]).map((s) => (
                  <SelectItem key={s} value={s}>
                    {SEVERITY[s].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-start gap-2">
            <Checkbox id="report-test" checked={test} onCheckedChange={(checked) => setTest(checked === true)} />
            <Label htmlFor="report-test" className="flex-col items-start gap-0.5 font-normal">
              This is a test
              <span className="text-[12px] text-muted-foreground">
                Medic investigates as usual, but the incident is never reused as incident memory.
              </span>
            </Label>
          </div>
          <DialogFooter>
            <Button type="submit" disabled={selected.length === 0 || report.isPending}>
              {report.isPending ? "Reporting" : "Report and investigate"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
