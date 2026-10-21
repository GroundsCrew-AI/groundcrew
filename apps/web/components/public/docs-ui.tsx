import { ArrowLeft, ArrowRight, Info, ShieldCheck, TriangleAlert } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { neighbours } from "./docs-pages";

const FOCUS = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50";

export type TocEntry = { id: string; title: string };

/** One docs page: title and lead, its sections, "On this page" beside it on wide screens, and
 * Previous / Next links in the docs' reading order. */
export function DocsArticle({
  path,
  title,
  lead,
  toc,
  children,
}: {
  path: string;
  title: string;
  lead?: ReactNode;
  toc?: TocEntry[];
  children: ReactNode;
}) {
  const { previous, next } = neighbours(path);
  return (
    <div className="xl:grid xl:grid-cols-[minmax(0,1fr)_200px] xl:gap-10">
      <article className="min-w-0 max-w-[72ch]">
        <h1 className="text-[30px] leading-tight font-semibold tracking-tight sm:text-[34px]">{title}</h1>
        {lead && <p className="mt-4 text-[17px] leading-[1.6] text-muted-foreground">{lead}</p>}
        <div className="mt-10 space-y-12">{children}</div>
        {(previous || next) && (
          <nav aria-label="Previous and next" className="mt-16 grid gap-3 border-t pt-6 sm:grid-cols-2">
            {previous ? (
              <Link
                href={previous.href}
                className={`group flex min-h-11 flex-col rounded-lg border px-4 py-3 transition-colors duration-150 hover:bg-muted ${FOCUS}`}
              >
                <span className="flex items-center gap-1.5 text-[12.5px] text-muted-foreground">
                  <ArrowLeft aria-hidden="true" className="size-3.5" />
                  Previous
                </span>
                <span className="mt-0.5 text-[15px] font-medium">{previous.title}</span>
              </Link>
            ) : (
              <span aria-hidden="true" />
            )}
            {next && (
              <Link
                href={next.href}
                className={`group flex min-h-11 flex-col rounded-lg border px-4 py-3 text-right transition-colors duration-150 hover:bg-muted sm:col-start-2 ${FOCUS}`}
              >
                <span className="flex items-center justify-end gap-1.5 text-[12.5px] text-muted-foreground">
                  Next
                  <ArrowRight aria-hidden="true" className="size-3.5" />
                </span>
                <span className="mt-0.5 text-[15px] font-medium">{next.title}</span>
              </Link>
            )}
          </nav>
        )}
      </article>
      {toc && toc.length > 0 && (
        <aside className="hidden xl:block">
          <nav aria-label="On this page" className="sticky top-20">
            <p className="text-[12px] font-medium tracking-wide text-subtle uppercase">On this page</p>
            <ul className="mt-3 space-y-1 border-l">
              {toc.map((entry) => (
                <li key={entry.id}>
                  <a
                    href={`#${entry.id}`}
                    className={`-ml-px block border-l border-transparent py-1 pl-3 text-[13px] leading-snug text-muted-foreground transition-colors duration-150 hover:border-foreground/40 hover:text-foreground ${FOCUS}`}
                  >
                    {entry.title}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </aside>
      )}
    </div>
  );
}

const BODY =
  "mt-4 space-y-4 text-[16px] leading-[1.65] text-foreground/90 " +
  "[&_a]:font-medium [&_a]:text-foreground [&_a]:underline [&_a]:decoration-foreground/30 [&_a]:underline-offset-4 [&_a:hover]:decoration-foreground " +
  "[&_li]:ml-5 [&_li]:pl-1 [&_ol]:list-decimal [&_ol]:space-y-2 [&_ul]:list-disc [&_ul]:space-y-2 [&_strong]:font-semibold [&_strong]:text-foreground";

export function DocSection({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-24">
      <h2 id={`${id}-title`} className="text-[21px] leading-snug font-semibold tracking-tight">
        {title}
      </h2>
      <div className={BODY}>{children}</div>
    </section>
  );
}

export function SubHeading({ children }: { children: ReactNode }) {
  return <h3 className="pt-3 text-[16.5px] font-semibold text-foreground">{children}</h3>;
}

/** A command or file excerpt. Long lines wrap so the page never scrolls sideways. */
export function Code({ children }: { children: string }) {
  return (
    <pre className="rounded-lg border bg-card px-4 py-3 font-mono text-[13px] leading-relaxed break-words whitespace-pre-wrap">
      <code>{children}</code>
    </pre>
  );
}

export function Inline({ children }: { children: ReactNode }) {
  return <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em] break-words">{children}</code>;
}

/** Numbered steps, each with its own body. */
export function Steps({ children }: { children: ReactNode }) {
  return <ol className="!ml-0 !list-none space-y-5 [counter-reset:step]">{children}</ol>;
}

export function Step({ title, children }: { title: string; children: ReactNode }) {
  return (
    <li className="!ml-0 grid grid-cols-[28px_minmax(0,1fr)] gap-3 !pl-0 [counter-increment:step]">
      <span
        aria-hidden="true"
        className="flex size-7 items-center justify-center rounded-full border bg-card font-mono text-[12px] text-muted-foreground before:content-[counter(step)]"
      />
      <div className="min-w-0 space-y-2 pt-0.5">
        <p className="font-semibold text-foreground">{title}</p>
        <div className="space-y-2">{children}</div>
      </div>
    </li>
  );
}

const CALLOUT = {
  note: { icon: Info, label: "Note" },
  safety: { icon: ShieldCheck, label: "Safety" },
  warning: { icon: TriangleAlert, label: "Watch out" },
} as const;

export function Callout({ kind = "note", children }: { kind?: keyof typeof CALLOUT; children: ReactNode }) {
  const { icon: Icon, label } = CALLOUT[kind];
  return (
    <div
      className={`flex gap-3 rounded-lg border px-4 py-3 text-[15px] leading-[1.6] ${
        kind === "warning" ? "border-[var(--status-waiting)]/40" : ""
      } ${kind === "safety" ? "border-[var(--status-ok)]/40" : ""}`}
    >
      <Icon
        aria-hidden="true"
        className={`mt-1 size-4 shrink-0 ${
          kind === "warning"
            ? "text-[var(--status-waiting)]"
            : kind === "safety"
              ? "text-[var(--status-ok)]"
              : "text-muted-foreground"
        }`}
      />
      <div className="min-w-0 space-y-2">
        <span className="sr-only">{label}: </span>
        {children}
      </div>
    </div>
  );
}

/** A small reference table that scrolls inside itself on narrow screens. */
export function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-left text-[14.5px] leading-snug">
        <thead className="bg-muted/50 text-[13px] text-muted-foreground">
          <tr>
            {head.map((h) => (
              <th key={h} scope="col" className="px-3 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j} className="px-3 py-2 align-top">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A question and its answer, closed until opened. */
export function Faq({ question, children }: { question: string; children: ReactNode }) {
  return (
    <details className="group rounded-lg border px-4 [&_summary::-webkit-details-marker]:hidden">
      <summary
        className={`flex min-h-12 cursor-pointer list-none items-center justify-between gap-3 py-3 text-[16px] font-medium text-foreground ${FOCUS}`}
      >
        {question}
        <ArrowRight
          aria-hidden="true"
          className="size-4 shrink-0 text-muted-foreground transition-transform duration-200 group-open:rotate-90 motion-reduce:transition-none"
        />
      </summary>
      <div className="space-y-3 pb-4">{children}</div>
    </details>
  );
}
