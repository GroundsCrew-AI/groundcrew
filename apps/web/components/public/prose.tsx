import type { ReactNode } from "react";

/** A long-form page (policies, docs): a title, an optional date and lead, then sections. */
export function Article({
  title,
  lead,
  updated,
  children,
}: {
  title: string;
  lead?: ReactNode;
  updated?: string;
  children: ReactNode;
}) {
  return (
    <article className="mx-auto w-full max-w-3xl px-4 py-12 sm:px-6 sm:py-16">
      <h1 className="text-[28px] leading-tight font-semibold tracking-tight">{title}</h1>
      {updated && <p className="mt-2 text-[12.5px] text-muted-foreground">Last updated: {updated}</p>}
      {lead && <p className="mt-4 text-[15px] leading-relaxed text-muted-foreground">{lead}</p>}
      <div className="mt-10 space-y-10">{children}</div>
    </article>
  );
}

const BODY =
  "mt-3 space-y-3 text-[14px] leading-relaxed text-foreground/90 [&_a]:underline [&_a]:underline-offset-4 " +
  "[&_li]:ml-5 [&_ol]:list-decimal [&_ol]:space-y-1.5 [&_ul]:list-disc [&_ul]:space-y-1.5";

export function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-20">
      <h2 id={`${id}-title`} className="text-[17px] font-semibold tracking-tight">
        {title}
      </h2>
      <div className={BODY}>{children}</div>
    </section>
  );
}

export function SubHeading({ children }: { children: ReactNode }) {
  return <h3 className="pt-2 text-[14px] font-semibold">{children}</h3>;
}

/** A command or file excerpt. Long lines wrap so the page never scrolls sideways. */
export function Code({ children }: { children: string }) {
  return (
    <pre className="rounded-lg border bg-card px-4 py-3 font-mono text-[12.5px] leading-relaxed break-words whitespace-pre-wrap">
      <code>{children}</code>
    </pre>
  );
}

export function Inline({ children }: { children: ReactNode }) {
  return <code className="rounded bg-muted px-1 py-0.5 font-mono text-[12.5px] break-words">{children}</code>;
}
