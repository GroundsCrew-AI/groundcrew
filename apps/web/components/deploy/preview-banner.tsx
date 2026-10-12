import { FlaskConical } from "lucide-react";

/** Shown while the deploy API answers 501 and the page runs on lib/deploys-fake.ts (M6 removes it). */
export function PreviewBanner() {
  return (
    <p
      role="note"
      className="flex items-start gap-2 rounded-md border border-dashed px-3 py-2 text-[12.5px] text-muted-foreground"
    >
      <FlaskConical aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      <span>
        <span className="font-medium text-foreground">Preview.</span> The deploy pipeline is not connected yet,
        so this run is simulated in your browser: it always deploys GroundsCrew-AI/legacy-flask-app, nothing is
        built or deployed, and it is gone after a reload. Times are simulated too.
      </span>
    </p>
  );
}
