"use client";

import { useEffect, useRef, useState } from "react";

import { api, unwrap } from "@/lib/api/client";
import type { ApprovalStepEvent, RunDetail, RunStatusEvent, Step } from "@/lib/api/client";
import { DEPLOY_EVENTS } from "@/lib/deploys";
import { MEDIC_EVENTS } from "@/lib/incidents";
import type { TimelineAction } from "@/lib/timeline";

const TERMINAL = new Set(["succeeded", "failed", "stopped"]);

export type StreamState = "idle" | "connecting" | "live" | "reconnecting" | "closed";

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
const MAX_BACKOFF_MS = 15_000;
/** While the stream is down, the timeline is read from GET /runs/{id} this often. */
export const POLL_FALLBACK_MS = 5_000;

/**
 * Live run events straight from the API (long streams time out behind serverless proxies).
 *
 * Each connection asks the web proxy for a fresh 60-second stream token, then resumes after
 * the last step it has seen (`after`), so a dropped connection, an expired token, or a page
 * refresh never repeats or skips a step. Reconnects back off up to 15 s.
 *
 * While the stream is not live (the API unreachable from the browser, a proxy dropping SSE),
 * the run is polled every 5 s, so the page still updates without a reload.
 */
export function useRunStream(
  runId: string,
  options: {
    enabled: boolean;
    lastSeq: number;
    dispatch: (action: TimelineAction) => void;
  },
): StreamState {
  const { enabled, dispatch } = options;
  const [state, setState] = useState<StreamState>("idle");
  const lastSeq = useRef(options.lastSeq);
  lastSeq.current = Math.max(lastSeq.current, options.lastSeq);

  useEffect(() => {
    if (!enabled) return;
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let attempt = 0;

    const track = (event: MessageEvent) => {
      const seq = Number(event.lastEventId);
      if (Number.isFinite(seq) && seq > lastSeq.current) lastSeq.current = seq;
    };

    let poll: ReturnType<typeof setInterval> | undefined;
    const stopPolling = () => {
      clearInterval(poll);
      poll = undefined;
    };
    const startPolling = () => {
      if (poll !== undefined || stopped) return;
      const read = async () => {
        try {
          const run: RunDetail = unwrap(
            await api.GET("/runs/{run_id}", { params: { path: { run_id: runId } } }),
          );
          if (stopped) return;
          dispatch({ type: "loaded", run });
          for (const s of run.steps) if (s.seq > lastSeq.current) lastSeq.current = s.seq;
          if (TERMINAL.has(run.status)) {
            stopped = true;
            stopPolling();
            clearTimeout(timer);
            source?.close();
            setState("closed");
          }
        } catch {
          // The next tick tries again.
        }
      };
      void read();
      poll = setInterval(() => void read(), POLL_FALLBACK_MS);
    };

    const scheduleReconnect = () => {
      if (stopped) return;
      startPolling();
      setState("reconnecting");
      const delay = Math.min(1000 * 2 ** attempt, MAX_BACKOFF_MS);
      attempt += 1;
      timer = setTimeout(connect, delay);
    };

    async function connect() {
      if (stopped) return;
      setState((s) => (s === "live" || s === "reconnecting" ? "reconnecting" : "connecting"));
      let token: string;
      try {
        const result = await api.POST("/runs/{run_id}/stream-token", {
          params: { path: { run_id: runId } },
        });
        token = unwrap(result).token;
      } catch {
        scheduleReconnect();
        return;
      }
      if (stopped) return;

      const url = `${API_URL}/runs/${encodeURIComponent(runId)}/events?token=${encodeURIComponent(
        token,
      )}&after=${lastSeq.current}`;
      source = new EventSource(url);
      source.onopen = () => {
        attempt = 0;
        stopPolling();
        setState("live");
      };
      source.addEventListener("step", (event) => {
        track(event);
        dispatch({ type: "step", step: JSON.parse(event.data) as Step });
      });
      // Deploy and incident events are steps with typed `data` (docs/events.md), sent under
      // their own names.
      for (const type of [...DEPLOY_EVENTS, ...MEDIC_EVENTS]) {
        source.addEventListener(type, (event) => {
          track(event);
          dispatch({ type: "step", step: JSON.parse(event.data) as Step });
        });
      }
      for (const type of ["approval_requested", "approval_decided"] as const) {
        source.addEventListener(type, (event) => {
          track(event);
          dispatch({ type, event: JSON.parse(event.data) as ApprovalStepEvent });
        });
      }
      source.addEventListener("status", (event) => {
        dispatch({ type: "status", status: JSON.parse(event.data) as RunStatusEvent });
      });
      source.addEventListener("done", (event) => {
        dispatch({ type: "done", status: JSON.parse(event.data) as RunStatusEvent });
        stopped = true;
        source?.close();
        setState("closed");
      });
      // The browser's own retry would reuse the expired token, so we reconnect ourselves.
      source.onerror = () => {
        source?.close();
        source = null;
        scheduleReconnect();
      };
    }

    void connect();
    return () => {
      stopped = true;
      stopPolling();
      clearTimeout(timer);
      source?.close();
    };
  }, [runId, enabled, dispatch]);

  return enabled ? state : "closed";
}
