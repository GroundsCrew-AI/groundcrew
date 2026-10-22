"use client";

import { Panel, useReactFlow } from "@xyflow/react";
import { cn } from "cn";
import { ChevronDown, Focus, Map as MapIcon } from "lucide-react";
import { type CSSProperties, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { edgeColor, edgeLabel, type Group } from "@/lib/graph";

import { COVERAGE, providerColor } from "./resource-meta";

function reducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Jump to the whole map or to one group (an account and region, a cluster, a server). */
export function MapViews({ groups }: { groups: (Group & { count: number })[] }) {
  const flow = useReactFlow();
  const duration = reducedMotion() ? 0 : 400;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="bg-card shadow-sm">
          <Focus aria-hidden="true" />
          View
          <ChevronDown aria-hidden="true" className="text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-80">
        <DropdownMenuItem onSelect={() => void flow.fitView({ padding: 0.06, duration })}>
          <MapIcon aria-hidden="true" />
          Whole map
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Zoom to a group</DropdownMenuLabel>
        {groups.map((g) => (
          <DropdownMenuItem
            key={g.id}
            onSelect={() => void flow.fitView({ nodes: [{ id: g.id }], padding: 0.08, maxZoom: 1.2, duration })}
          >
            <span
              aria-hidden="true"
              className="size-2.5 shrink-0 rounded-sm"
              style={{ background: providerColor(g.provider) }}
            />
            <span className="min-w-0 flex-1 truncate">{g.label}</span>
            <span className="tabular text-[12px] text-muted-foreground">{g.count}</span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

const LEGEND_KEY = "groundcrew.map.legend";

// Whether the legend is open is a per-viewer convenience; storage may be unavailable.
function readOpen(): boolean {
  try {
    return window.localStorage.getItem(LEGEND_KEY) === "open";
  } catch {
    return false;
  }
}

function saveOpen(open: boolean) {
  try {
    window.localStorage.setItem(LEGEND_KEY, open ? "open" : "closed");
  } catch {
    // Not remembered; it still works.
  }
}

/** What the colors and lines mean. Each kind of line can be hidden (the layout stays put). */
export function MapLegend({
  kinds,
  hidden,
  onToggle,
}: {
  kinds: string[];
  hidden: Set<string>;
  onToggle: (kind: string) => void;
}) {
  const [open, setOpen] = useState(readOpen);
  return (
    <Panel position="top-right" className="!m-3">
      <div className={cn("rounded-lg border bg-card/95 text-[12px] shadow-sm backdrop-blur", open ? "w-56" : "w-auto")}>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => {
            setOpen(!open);
            saveOpen(!open);
          }}
          className="flex h-8 w-full items-center justify-between gap-2 px-3 font-medium"
        >
          <span className="flex items-center gap-2">
            Legend
            {!open && (
              <span aria-hidden="true" className="flex gap-0.5">
                {kinds.slice(0, 4).map((k) => (
                  <span key={k} className="h-1 w-2.5 rounded-full" style={{ background: edgeColor(k) }} />
                ))}
              </span>
            )}
          </span>
          <ChevronDown aria-hidden="true" className={cn("size-3.5 transition-transform", !open && "-rotate-90")} />
        </button>
        {open && (
          <div className="space-y-3 border-t px-3 pt-2 pb-3">
            {kinds.length > 0 && (
              <fieldset>
                <legend className="mb-1 text-muted-foreground">Lines (click to hide)</legend>
                <ul className="space-y-0.5">
                  {kinds.map((kind) => {
                    const off = hidden.has(kind);
                    return (
                      <li key={kind}>
                        <button
                          type="button"
                          aria-pressed={!off}
                          onClick={() => onToggle(kind)}
                          className={cn(
                            "flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-accent",
                            off && "text-muted-foreground line-through opacity-60",
                          )}
                        >
                          <svg aria-hidden="true" width="22" height="6" className="shrink-0">
                            <line x1="0" y1="3" x2="22" y2="3" stroke={edgeColor(kind)} strokeWidth="2.5" />
                          </svg>
                          {edgeLabel(kind)}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </fieldset>
            )}
            <div>
              <p className="mb-1 text-muted-foreground">Confidence</p>
              <p className="flex items-center gap-2 px-1">
                <svg aria-hidden="true" width="22" height="6" className="shrink-0">
                  <line x1="0" y1="3" x2="22" y2="3" stroke="var(--subtle)" strokeWidth="1.5" />
                </svg>
                High or medium
              </p>
              <p className="flex items-center gap-2 px-1">
                <svg aria-hidden="true" width="22" height="6" className="shrink-0">
                  <line x1="0" y1="3" x2="22" y2="3" stroke="var(--subtle)" strokeWidth="1.5" strokeDasharray="4 4" />
                </svg>
                Low
              </p>
            </div>
            <div>
              <p className="mb-1 text-muted-foreground">Coverage (dot by the name)</p>
              {Object.values(COVERAGE).map((c) => (
                <p key={c.label} className="flex items-center gap-2 px-1" title={c.hint}>
                  <span
                    aria-hidden="true"
                    className="ml-[7px] size-2 shrink-0 rounded-full"
                    style={{ background: c.color } as CSSProperties}
                  />
                  <span className="ml-[7px]">{c.label}</span>
                </p>
              ))}
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}
