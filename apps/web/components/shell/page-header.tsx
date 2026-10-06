"use client";

import { cn } from "cn";
import { Menu } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";

import { useShell } from "./shell-context";
import { SidebarContent } from "./sidebar";

/** The top bar: page title (with optional parent), and the page's own actions on the right. */
export function PageHeader({
  title,
  parent,
  actions,
  className,
}: {
  title: ReactNode;
  parent?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  const { mobileOpen, setMobileOpen } = useShell();
  return (
    <header
      className={cn(
        "sticky top-0 z-20 flex h-12 shrink-0 items-center gap-2 border-b bg-background/90 px-4 backdrop-blur-sm md:px-6",
        className,
      )}
    >
      <Button
        variant="ghost"
        size="icon-sm"
        className="-ml-1.5 md:hidden"
        aria-label="Open navigation"
        onClick={() => setMobileOpen(true)}
      >
        <Menu />
      </Button>
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-[260px] bg-sidebar p-0">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <SidebarContent collapsed={false} onNavigate={() => setMobileOpen(false)} />
        </SheetContent>
      </Sheet>
      <div className="flex min-w-0 flex-1 items-center gap-1.5 text-[13px]">
        {parent && (
          <>
            <span className="truncate text-muted-foreground">{parent}</span>
            <span aria-hidden="true" className="text-subtle">
              /
            </span>
          </>
        )}
        <h1 className="truncate font-medium">{title}</h1>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}

/** Page body with consistent gutters. */
export function PageBody({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("px-4 py-6 md:px-6", className)}>{children}</div>;
}
