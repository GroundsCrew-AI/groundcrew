import { signInWithProvider } from "@/app/actions";
import { Button } from "@/components/ui/button";

import { GitHubIcon, GoogleIcon } from "./auth-shell";

/** "Continue with Google / GitHub" for the providers this server has credentials for. */
export function ProviderButtons({ next, providers }: { next: string; providers: { github: boolean; google: boolean } }) {
  const list = [
    providers.google && { id: "google", label: "Continue with Google", icon: <GoogleIcon /> },
    providers.github && { id: "github", label: "Continue with GitHub", icon: <GitHubIcon /> },
  ].filter(Boolean) as { id: string; label: string; icon: React.ReactNode }[];
  if (list.length === 0) return null;
  return (
    <div className="space-y-2.5">
      {list.map((p) => (
        <form key={p.id} action={signInWithProvider}>
          <input type="hidden" name="provider" value={p.id} />
          <input type="hidden" name="next" value={next} />
          <Button type="submit" variant="outline" size="lg" className="h-10 w-full gap-2 text-[14px]">
            {p.icon}
            {p.label}
          </Button>
        </form>
      ))}
    </div>
  );
}

export function OrDivider() {
  return (
    <div className="my-5 flex items-center gap-3 text-[12px] text-muted-foreground" aria-hidden="true">
      <span className="h-px flex-1 bg-border" />
      or
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}
