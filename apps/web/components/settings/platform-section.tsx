"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ErrorState, RowsSkeleton } from "@/components/states/states";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api, type Schemas, unwrap } from "@/lib/api/client";
import { formatTimestamp } from "@/lib/format";
import { toastResult } from "@/components/settings/toast-result";

type Platform = Schemas["PlatformOut"];
const platformKey = ["admin", "platform"] as const;

/** Platform switches for operators (PLATFORM_ADMINS): pausing new sign-ups. */
export function PlatformSection() {
  const client = useQueryClient();
  const platform = useQuery({
    queryKey: platformKey,
    queryFn: async () => unwrap(await api.GET("/admin/platform")),
  });
  const setSignups = useMutation({
    mutationFn: async (paused: boolean) =>
      unwrap(await api.PUT("/admin/platform/signups", { body: { paused } })),
    onSuccess: (data: Platform) => client.setQueryData(platformKey, data),
  });

  if (platform.error) return <ErrorState error={platform.error} onRetry={() => void platform.refetch()} />;
  if (!platform.data) {
    return (
      <div className="overflow-hidden rounded-lg border">
        <RowsSkeleton rows={1} />
      </div>
    );
  }
  const p = platform.data;
  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Label htmlFor="platform-signups" className="text-[13px]">
            Pause new sign-ups
          </Label>
          <p className="mt-1 text-[12px] text-muted-foreground">
            Existing users sign in as usual, and people with an invite can still join. {p.signups_last_hour} new{" "}
            {p.signups_last_hour === 1 ? "account" : "accounts"} in the last hour (limit {p.signups_per_hour}),{" "}
            {p.users} in total.
            {p.signups_changed_at && (
              <>
                {" "}
                Last changed <time dateTime={p.signups_changed_at}>{formatTimestamp(p.signups_changed_at)}</time>.
              </>
            )}
          </p>
        </div>
        <Switch
          id="platform-signups"
          checked={p.signups_paused}
          disabled={setSignups.isPending}
          onCheckedChange={(paused) =>
            void toastResult(setSignups.mutateAsync(paused), () =>
              paused ? "New sign-ups are paused" : "New sign-ups are open",
            )
          }
        />
      </div>
    </div>
  );
}
