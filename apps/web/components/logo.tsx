import { cn } from "cn";

/** The mark: a taxiway centerline turning onto a stand. Drawn, not an image. */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 20 20"
      aria-hidden="true"
      className={cn("size-5 shrink-0 text-foreground", className)}
      fill="none"
    >
      <rect x="0.75" y="0.75" width="18.5" height="18.5" rx="5" className="stroke-border" strokeWidth="1.5" />
      <path
        d="M5.5 15V9.5a3 3 0 0 1 3-3h6"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <circle cx="14.5" cy="6.5" r="1.6" className="fill-primary" />
    </svg>
  );
}

export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("flex items-center gap-2 font-semibold tracking-tight", className)}>
      <LogoMark />
      <span className="text-[14px]">Groundcrew</span>
    </span>
  );
}
