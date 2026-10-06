import type { ReactNode } from "react";

import { PageBody, PageHeader } from "@/components/shell/page-header";

/** A page that arrives in a later phase: what it will show, specifically, and when. */
export function PlaceholderPage({
  title,
  illustration,
  heading,
  description,
  points,
  phase,
  children,
}: {
  title: string;
  illustration: ReactNode;
  heading: string;
  description: string;
  points: string[];
  phase: string;
  children?: ReactNode;
}) {
  return (
    <>
      <PageHeader title={title} />
      <PageBody className="max-w-3xl">
        <div className="pt-6 md:pt-10">
          {illustration}
          <h2 className="mt-6 text-[17px] font-semibold tracking-tight">{heading}</h2>
          <p className="mt-2 max-w-[60ch] text-[13.5px] leading-relaxed text-muted-foreground">{description}</p>
          <ul className="mt-5 max-w-[60ch] space-y-2">
            {points.map((point) => (
              <li key={point} className="flex gap-2.5 text-[13px]">
                <span aria-hidden="true" className="mt-[7px] size-1 shrink-0 rounded-full bg-subtle" />
                {point}
              </li>
            ))}
          </ul>
          <p className="mt-6 text-[12px] text-subtle">{phase}</p>
        </div>
        {children}
      </PageBody>
    </>
  );
}
