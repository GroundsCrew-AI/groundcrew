"use client";

import { Radar } from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useDiscoverWorkspace } from "@/lib/discovery";
import { canAct, VIEWER_READ_ONLY } from "@/lib/workspace-admin";
import { useWorkspace } from "@/lib/workspace";

/** Start discovery for the whole workspace and open the live run. */
export function RunDiscoveryButton({
  size = "sm",
  variant = "default",
  label = "Run discovery",
}: {
  size?: "sm" | "default";
  variant?: "default" | "outline";
  label?: string;
}) {
  const router = useRouter();
  const { workspace } = useWorkspace();
  const discover = useDiscoverWorkspace();
  return (
    <Button
      size={size}
      variant={variant}
      disabled={!workspace || discover.isPending || !canAct(workspace.role)}
      title={workspace && !canAct(workspace.role) ? VIEWER_READ_ONLY : undefined}
      onClick={() =>
        workspace &&
        discover.mutate(workspace.id, {
          onSuccess: (run) => router.push(`/runs/${run.id}`),
          onError: (error) => toast.error(error.message),
        })
      }
    >
      <Radar aria-hidden="true" />
      {discover.isPending ? "Starting" : label}
    </Button>
  );
}
