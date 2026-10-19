"use client";

import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { type Connection, useAwsSetRegions } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

/** Commercial regions, grouped the way the AWS console lists them. */
export const AWS_REGIONS: { group: string; regions: [string, string][] }[] = [
  {
    group: "North America",
    regions: [
      ["us-east-1", "N. Virginia"],
      ["us-east-2", "Ohio"],
      ["us-west-1", "N. California"],
      ["us-west-2", "Oregon"],
      ["ca-central-1", "Canada (Central)"],
    ],
  },
  {
    group: "Europe",
    regions: [
      ["eu-west-1", "Ireland"],
      ["eu-west-2", "London"],
      ["eu-west-3", "Paris"],
      ["eu-central-1", "Frankfurt"],
      ["eu-north-1", "Stockholm"],
      ["eu-south-1", "Milan"],
    ],
  },
  {
    group: "Asia Pacific",
    regions: [
      ["ap-south-1", "Mumbai"],
      ["ap-southeast-1", "Singapore"],
      ["ap-southeast-2", "Sydney"],
      ["ap-northeast-1", "Tokyo"],
      ["ap-northeast-2", "Seoul"],
    ],
  },
  {
    group: "Other",
    regions: [
      ["sa-east-1", "São Paulo"],
      ["me-central-1", "UAE"],
      ["af-south-1", "Cape Town"],
    ],
  },
];

export const MAX_REGIONS = 10;

/**
 * Choose the regions Groundcrew discovers, watches, and acts in for one AWS account. The
 * read-only role is not tied to a region, so no stack update is needed; a discovery afterwards
 * finds the resources in a newly added region.
 */
export function AwsRegionsDialog({
  connection,
  open,
  onOpenChange,
  onSaved,
}: {
  connection: Connection;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called after saving, e.g. to start a discovery. */
  onSaved?: (connection: Connection) => void;
}) {
  const { workspace } = useWorkspace();
  const save = useAwsSetRegions(workspace?.id);
  const [chosen, setChosen] = useState<string[]>(connection.regions ?? []);
  const tooMany = chosen.length > MAX_REGIONS;

  function toggle(region: string, on: boolean) {
    setChosen((now) => (on ? [...now, region] : now.filter((r) => r !== region)));
  }

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) save.reset();
  }

  function submit() {
    save.mutate(
      { connectionId: connection.id, regions: chosen },
      {
        onSuccess: (updated) => {
          toast.success(`Regions saved for ${connection.name}`);
          onSaved?.(updated);
          close(false);
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90dvh] grid-cols-[minmax(0,1fr)] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Regions</DialogTitle>
          <DialogDescription>
            Where Groundcrew discovers resources, watches for incidents, and runs approved fixes in this account. No
            stack update is needed; Groundcrew refreshes the map after you save.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          {AWS_REGIONS.map(({ group, regions }) => (
            <fieldset key={group} className="space-y-1.5">
              <legend className="text-[12px] font-medium text-muted-foreground">{group}</legend>
              <div className="grid gap-1.5 sm:grid-cols-2">
                {regions.map(([id, name]) => (
                  <label key={id} className="flex min-h-8 cursor-pointer items-center gap-2 text-[13px] pointer-coarse:min-h-11">
                    <Checkbox checked={chosen.includes(id)} onCheckedChange={(on) => toggle(id, on === true)} />
                    <span className="font-mono">{id}</span>
                    <span className="text-muted-foreground">{name}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          ))}
        </div>
        {tooMany && (
          <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
            Choose at most {MAX_REGIONS} regions.
          </p>
        )}
        {save.error && (
          <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
            {save.error.message}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={save.isPending || tooMany || chosen.length === 0}>
            {save.isPending ? "Saving" : "Save regions"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
