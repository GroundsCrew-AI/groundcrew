/**
 * Empty-state drawings built from simple shapes. They use theme colors only, so they work in
 * both themes. Each one sketches what the page will show, not decoration.
 */

const frame = "h-[100px] w-[190px] text-subtle";

function Svg({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 168 88" role="img" aria-label={label} className={frame} fill="none">
      {children}
    </svg>
  );
}

/** Resources in two clouds, joined by inferred edges. */
export function MapIllustration() {
  return (
    <Svg label="Resources from two clouds joined in one graph">
      <rect x="6" y="10" width="66" height="68" rx="8" stroke="currentColor" strokeDasharray="3 3" />
      <rect x="96" y="10" width="66" height="68" rx="8" stroke="currentColor" strokeDasharray="3 3" />
      <path d="M38 30 L130 26 M38 30 L39 58 M130 26 L128 58 M39 58 L128 58" stroke="var(--rail)" />
      <circle cx="38" cy="30" r="6" fill="var(--card)" stroke="var(--subtle)" />
      <circle cx="39" cy="58" r="6" fill="var(--card)" stroke="var(--subtle)" />
      <circle cx="128" cy="58" r="6" fill="var(--card)" stroke="var(--subtle)" />
      <circle cx="130" cy="26" r="6" fill="var(--card)" stroke="var(--primary)" />
    </Svg>
  );
}

/** Repo, build, target: the three stages of a deploy. */
export function DeployIllustration() {
  return (
    <Svg label="A deploy moving from repository to build to target">
      <rect x="6" y="30" width="40" height="28" rx="6" stroke="var(--subtle)" />
      <rect x="64" y="30" width="40" height="28" rx="6" stroke="var(--subtle)" />
      <rect x="122" y="30" width="40" height="28" rx="6" stroke="var(--primary)" />
      <path d="M46 44 H64 M104 44 H122" stroke="var(--rail)" strokeWidth="1.5" />
      <path d="M14 40 H34 M14 46 H28" stroke="var(--rail)" />
      <path d="M72 40 H96 M72 46 H90" stroke="var(--rail)" />
      <circle cx="142" cy="44" r="4" fill="var(--primary)" />
    </Svg>
  );
}

/** A steady signal with one spike, marked where the incident opened. */
export function IncidentsIllustration() {
  return (
    <Svg label="A metric line with one spike where an incident opened">
      <path d="M4 66 H164" stroke="currentColor" />
      <path
        d="M4 56 H58 L66 52 L74 58 L84 18 L94 60 L102 54 H164"
        stroke="var(--subtle)"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M84 18 V66" stroke="var(--status-waiting)" strokeDasharray="2 3" />
      <circle cx="84" cy="18" r="3.5" fill="var(--status-waiting)" />
    </Svg>
  );
}

/** Rows of switches and fields. */
export function SettingsIllustration() {
  return (
    <Svg label="Workspace settings">
      {[18, 42, 66].map((y, i) => (
        <g key={y}>
          <path d={`M8 ${y} H${88 - i * 14}`} stroke="var(--rail)" strokeWidth="5" strokeLinecap="round" />
          <rect
            x="128"
            y={y - 7}
            width="28"
            height="14"
            rx="7"
            stroke={i === 0 ? "var(--primary)" : "var(--subtle)"}
          />
          <circle cx={i === 0 ? 149 : 135} cy={y} r="4" fill={i === 0 ? "var(--primary)" : "var(--subtle)"} />
        </g>
      ))}
    </Svg>
  );
}

/** The run timeline rail with its first step waiting. */
export function RunsIllustration() {
  return (
    <Svg label="An empty run timeline">
      <path d="M20 10 V78" stroke="var(--rail)" strokeWidth="1.5" />
      {[18, 44, 70].map((y, i) => (
        <g key={y}>
          <circle cx="20" cy={y} r="4.5" fill="var(--card)" stroke={i === 0 ? "var(--primary)" : "var(--subtle)"} />
          <path d={`M36 ${y} H${132 - i * 22}`} stroke="var(--rail)" strokeWidth="5" strokeLinecap="round" />
        </g>
      ))}
    </Svg>
  );
}

/** An inbox tray with nothing waiting. */
export function ApprovalsIllustration() {
  return (
    <Svg label="No actions waiting for approval">
      <path d="M24 46 L40 18 H128 L144 46 V72 H24 Z" stroke="var(--subtle)" strokeLinejoin="round" />
      <path d="M24 46 H62 L68 56 H100 L106 46 H144" stroke="var(--subtle)" strokeLinejoin="round" />
      <path d="M74 34 L82 41 L96 27" stroke="var(--status-ok)" strokeWidth="1.6" strokeLinecap="round" />
    </Svg>
  );
}
