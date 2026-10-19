"use client";

import { cn } from "cn";
import {
  Check,
  ChevronsUpDown,
  Inbox,
  LayoutGrid,
  LogOut,
  type LucideIcon,
  Monitor,
  Moon,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  Rocket,
  Settings,
  Siren,
  Sun,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTheme } from "next-themes";

import { signOutAction } from "@/app/actions";
import { LogoMark } from "@/components/logo";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { UserAvatar } from "@/components/user-avatar";
import { useSummary } from "@/lib/queries";
import { useWorkspace } from "@/lib/workspace";

import { useShell } from "./shell-context";

type Badge = "approvals" | "incidents";
type NavItem = { href: string; label: string; icon: LucideIcon; badge?: Badge };

/** How a count reads to a screen reader, and its color: approvals wait on a person, incidents are outages. */
const BADGES: Record<Badge, { noun: string; color: string }> = {
  approvals: { noun: "pending", color: "var(--status-waiting)" },
  incidents: { noun: "open", color: "var(--status-failed)" },
};

const PRIMARY: NavItem[] = [
  { href: "/", label: "Overview", icon: LayoutGrid },
  { href: "/map", label: "Map", icon: Network },
  { href: "/deploy", label: "Deploy", icon: Rocket },
  { href: "/runs", label: "Runs", icon: Workflow },
  { href: "/incidents", label: "Incidents", icon: Siren, badge: "incidents" },
  { href: "/approvals", label: "Approvals", icon: Inbox, badge: "approvals" },
];
const SECONDARY: NavItem[] = [
  { href: "/connections", label: "Connections", icon: Plug },
  { href: "/settings", label: "Settings", icon: Settings },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

function NavLink({
  item,
  collapsed,
  count,
  onNavigate,
}: {
  item: NavItem;
  collapsed: boolean;
  count?: number;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const active = isActive(pathname, item.href);
  const Icon = item.icon;
  const badge = item.badge ? BADGES[item.badge] : undefined;
  const label = count && badge ? `${item.label}, ${count} ${badge.noun}` : item.label;
  const link = (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      aria-label={collapsed || count ? label : undefined}
      className={cn(
        "group relative flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] text-muted-foreground transition-colors",
        "hover:bg-sidebar-accent hover:text-foreground",
        active && "bg-sidebar-accent text-foreground",
        collapsed && "justify-center px-0",
      )}
    >
      {active && (
        <span aria-hidden="true" className="absolute top-1.5 bottom-1.5 left-0 w-[2px] rounded-full bg-primary" />
      )}
      <Icon aria-hidden="true" className={cn("size-4 shrink-0", active && "text-primary")} strokeWidth={1.75} />
      {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
      {count ? (
        <span
          aria-hidden="true"
          style={{ backgroundColor: badge?.color }}
          className={cn(
            "tabular rounded-full px-1.5 text-[10.5px] leading-[18px] font-semibold text-background",
            collapsed && "absolute -top-0.5 -right-0.5 min-w-4 px-1 text-center leading-4",
          )}
        >
          {count > 99 ? "99+" : count}
        </span>
      ) : null}
    </Link>
  );
  if (!collapsed) return link;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  );
}

function WorkspaceSwitcher({ collapsed }: { collapsed: boolean }) {
  const { workspace, workspaces, setWorkspaceId, isLoading } = useWorkspace();
  if (isLoading) return <Skeleton className="h-8 w-full" />;
  const name = workspace?.name ?? "No workspace";
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Workspace: ${name}. Switch workspace`}
          className={cn(
            "flex h-9 w-full items-center gap-2 rounded-md px-2 text-left hover:bg-sidebar-accent",
            collapsed && "justify-center px-0",
          )}
        >
          <LogoMark />
          {!collapsed && (
            <>
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{name}</span>
              <ChevronsUpDown aria-hidden="true" className="size-3.5 text-muted-foreground" />
            </>
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-60">
        <DropdownMenuLabel className="text-muted-foreground">Workspaces</DropdownMenuLabel>
        {workspaces.map((w) => (
          <DropdownMenuItem key={w.id} onSelect={() => setWorkspaceId(w.id)}>
            <span className="flex-1 truncate">{w.name}</span>
            {w.role === "owner" && <span className="text-[11px] text-muted-foreground">Owner</span>}
            {w.id === workspace?.id && <Check aria-label="Current" className="size-3.5" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function UserMenu({ collapsed }: { collapsed: boolean }) {
  const { me } = useWorkspace();
  const { theme, setTheme } = useTheme();
  const user = me?.user;
  const display = user?.name || user?.github_login || "Signed in";
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="Account and theme"
          className={cn(
            "flex h-9 w-full items-center gap-2 rounded-md px-2 text-left hover:bg-sidebar-accent",
            collapsed && "justify-center px-0",
          )}
        >
          <UserAvatar src={user?.avatar_url} name={display} />
          {!collapsed && (
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px]">{display}</span>
              {user?.github_login && (
                <span className="block truncate text-[11.5px] text-muted-foreground">@{user.github_login}</span>
              )}
            </span>
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="top" className="w-56">
        <DropdownMenuLabel className="text-muted-foreground">Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme ?? "dark"} onValueChange={setTheme}>
          <DropdownMenuRadioItem value="dark">
            <Moon aria-hidden="true" /> Dark
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="light">
            <Sun aria-hidden="true" /> Light
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="system">
            <Monitor aria-hidden="true" /> System
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <form action={signOutAction}>
          <DropdownMenuItem asChild>
            <button type="submit" className="w-full">
              <LogOut aria-hidden="true" /> Sign out
            </button>
          </DropdownMenuItem>
        </form>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function SidebarContent({
  collapsed,
  onNavigate,
}: {
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  const { workspace } = useWorkspace();
  // The workspace summary already polls (and pauses in hidden tabs), so the counts cost nothing extra.
  const summary = useSummary(workspace?.id);
  const counts: Record<Badge, number> = {
    approvals: summary.data?.pending_approvals ?? 0,
    incidents: summary.data?.open_incidents ?? 0,
  };
  return (
    <div className="flex h-full flex-col gap-1 p-2">
      <WorkspaceSwitcher collapsed={collapsed} />
      <nav aria-label="Main" className="mt-3 flex flex-1 flex-col gap-0.5">
        {PRIMARY.map((item) => (
          <NavLink
            key={item.href}
            item={item}
            collapsed={collapsed}
            count={item.badge ? counts[item.badge] : undefined}
            onNavigate={onNavigate}
          />
        ))}
        <div className="my-2 h-px bg-sidebar-border" role="separator" />
        {SECONDARY.map((item) => (
          <NavLink key={item.href} item={item} collapsed={collapsed} onNavigate={onNavigate} />
        ))}
      </nav>
      <UserMenu collapsed={collapsed} />
    </div>
  );
}

export function Sidebar() {
  const { collapsed, toggleCollapsed } = useShell();
  return (
    <aside
      className={cn(
        "sticky top-0 hidden h-dvh shrink-0 flex-col border-r border-sidebar-border bg-sidebar transition-[width] duration-200 md:flex",
        collapsed ? "w-[52px]" : "w-[232px]",
      )}
    >
      <SidebarContent collapsed={collapsed} />
      <div className={cn("px-2 pb-2", collapsed && "flex justify-center")}>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={toggleCollapsed}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="text-muted-foreground"
        >
          {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
        </Button>
      </div>
    </aside>
  );
}
