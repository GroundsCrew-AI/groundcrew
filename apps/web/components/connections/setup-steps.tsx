import { TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

/*
 * The layout of a setup guide inside a connect dialog: numbered steps, each with a short bold
 * title saying what to do and a body with the details, roomy enough to follow while switching
 * to the AWS console and back.
 */

export function SetupSteps({ children }: { children: ReactNode }) {
  return <ol className="space-y-6">{children}</ol>;
}

export function SetupStep({ n, title, children }: { n: number; title: ReactNode; children?: ReactNode }) {
  return (
    <li className="grid grid-cols-[28px_minmax(0,1fr)] gap-x-3">
      <span
        aria-hidden="true"
        className="flex size-7 items-center justify-center rounded-full bg-primary/10 text-[13px] font-semibold text-primary tabular"
      >
        {n}
      </span>
      <div className="min-w-0 space-y-2.5 pt-0.5">
        <h3 className="text-[14px] leading-snug font-semibold">{title}</h3>
        {children && <div className="space-y-2.5 text-[13.5px] leading-relaxed text-muted-foreground">{children}</div>}
      </div>
    </li>
  );
}

/** A name on screen in the AWS console (a button, a tab, an option). */
export function Ui({ children }: { children: ReactNode }) {
  return <span className="font-medium text-foreground">{children}</span>;
}

/** Something to notice before going on: a value to replace, a choice that matters. */
export function SetupNote({ children, tone = "info" }: { children: ReactNode; tone?: "info" | "warn" }) {
  return (
    <p
      className={
        tone === "warn"
          ? "flex gap-2 rounded-md border border-[color-mix(in_oklab,var(--status-waiting)_45%,var(--border))] bg-[color-mix(in_oklab,var(--status-waiting)_8%,transparent)] px-3 py-2 text-[13px] leading-relaxed text-foreground"
          : "rounded-md border bg-muted/40 px-3 py-2 text-[13px] leading-relaxed"
      }
    >
      {tone === "warn" && <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-[var(--status-waiting)]" />}
      <span>{children}</span>
    </p>
  );
}
