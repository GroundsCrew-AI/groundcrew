import type { ReactNode } from "react";

/** One block of the Settings page: title and a short explanation on the left, controls on the right. */
export function SettingsSection({
  id,
  title,
  description,
  children,
}: {
  id: string;
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      aria-labelledby={id}
      className="grid gap-4 border-t py-8 first:border-t-0 first:pt-0 md:grid-cols-[minmax(0,220px)_minmax(0,1fr)] md:gap-8"
    >
      <div>
        <h2 id={id} className="text-[14px] font-medium">
          {title}
        </h2>
        {description && <div className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">{description}</div>}
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}

/** A choice with a title and one line of explanation, as a native radio. */
export function RadioOption({
  name,
  value,
  checked,
  disabled,
  onChange,
  title,
  children,
}: {
  name: string;
  value: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: string) => void;
  title: ReactNode;
  children?: ReactNode;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2.5 has-[:checked]:border-[color-mix(in_oklab,var(--primary)_55%,var(--border))] has-[:checked]:bg-muted/40 has-[:disabled]:cursor-not-allowed">
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={() => onChange(value)}
        className="mt-0.5 accent-[var(--primary)]"
      />
      <span className="min-w-0">
        <span className="block text-[13px] font-medium">{title}</span>
        {children && <span className="block text-[12px] text-muted-foreground">{children}</span>}
      </span>
    </label>
  );
}
