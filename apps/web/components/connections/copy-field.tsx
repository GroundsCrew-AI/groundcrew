"use client";

import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";

/** A value to paste somewhere else, with a copy button. */
export function CopyField({ label, value, multiline = false }: { label: string; value: string; multiline?: boolean }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard blocked (insecure origin or permissions): the value stays selectable.
    }
  }
  return (
    <div className="space-y-1">
      <p className="text-[12px] text-muted-foreground">{label}</p>
      <div className="flex items-start gap-1.5 rounded-md border bg-muted/40 py-1 pr-1 pl-2.5">
        <code
          className={
            multiline
              ? "min-w-0 flex-1 py-1 font-mono text-[12px] leading-relaxed break-all whitespace-pre-wrap"
              : "min-w-0 flex-1 truncate py-1 font-mono text-[12px]"
          }
        >
          {value}
        </code>
        <Button type="button" variant="ghost" size="icon-xs" aria-label={`Copy ${label}`} onClick={() => void copy()}>
          {copied ? <Check /> : <Copy />}
        </Button>
      </div>
    </div>
  );
}
