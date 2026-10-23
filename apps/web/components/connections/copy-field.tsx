"use client";

import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";

/** A value to paste somewhere else, with a copy button. The whole value is always shown (it
 * wraps), so nothing to replace can hide past the edge; `placeholders` are words in it the
 * person must replace, called out under it. */
export function CopyField({
  label,
  value,
  hint,
  placeholders = [],
  multiline = false,
}: {
  label: string;
  value: string;
  hint?: string;
  placeholders?: string[];
  multiline?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const toReplace = placeholders.filter((p) => value.includes(p));
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
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <p className="font-mono text-[12.5px] font-medium text-foreground">{label}</p>
        {hint && <p className="text-[12px] text-muted-foreground">{hint}</p>}
      </div>
      <div className="flex items-start gap-1.5 rounded-md border bg-muted/40 py-1 pr-1 pl-2.5">
        <code
          className={`min-w-0 flex-1 py-1 font-mono text-[12.5px] leading-relaxed break-all ${multiline ? "whitespace-pre-wrap" : ""}`}
        >
          {value}
        </code>
        <Button type="button" variant="ghost" size="icon-sm" aria-label={`Copy ${label}`} onClick={() => void copy()}>
          {copied ? <Check /> : <Copy />}
        </Button>
      </div>
      {toReplace.length > 0 && (
        <p className="text-[12px] font-medium text-[var(--status-waiting)]">
          Replace {toReplace.join(" and ")} with your own value before you paste it.
        </p>
      )}
    </div>
  );
}
