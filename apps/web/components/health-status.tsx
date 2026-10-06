"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { API_URL, getHealth, type Health } from "@/lib/api";

type State =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

export function HealthStatus() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    getHealth(controller.signal)
      .then((health) => setState({ kind: "ok", health }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const message = err instanceof Error ? err.message : "Unknown error";
        setState({ kind: "error", message });
      });
    return () => controller.abort();
  }, []);

  return (
    <div className="flex flex-col gap-2 text-sm">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground">API status:</span>
        {state.kind === "loading" && <Badge variant="secondary">checking</Badge>}
        {state.kind === "ok" && <Badge>{state.health.status}</Badge>}
        {state.kind === "error" && <Badge variant="destructive">unreachable</Badge>}
      </div>
      {state.kind === "ok" && (
        <p className="text-muted-foreground">Version {state.health.version}</p>
      )}
      {state.kind === "error" && (
        <p className="text-muted-foreground">
          Could not reach {API_URL}/health ({state.message})
        </p>
      )}
    </div>
  );
}
