"use client";

import { SlidersHorizontal } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { type MapFilters as Filters, PROVIDER_LABELS } from "@/lib/graph";

export function MapFilters({
  providers,
  filters,
  includeStale,
  onChange,
  onIncludeStale,
}: {
  providers: string[];
  filters: Filters;
  includeStale: boolean;
  onChange: (filters: Filters) => void;
  onIncludeStale: (value: boolean) => void;
}) {
  const active =
    filters.providers.length + Number(filters.showIac) + Number(includeStale);
  function toggleProvider(provider: string, on: boolean) {
    const current = filters.providers.length ? filters.providers : providers;
    const next = on ? [...new Set([...current, provider])] : current.filter((p) => p !== provider);
    onChange({ ...filters, providers: next.length === providers.length ? [] : next });
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm">
          <SlidersHorizontal aria-hidden="true" />
          Filters
          {active > 0 && <span className="tabular text-muted-foreground">{active}</span>}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuLabel>Providers</DropdownMenuLabel>
        {providers.map((provider) => (
          <DropdownMenuCheckboxItem
            key={provider}
            checked={filters.providers.length === 0 || filters.providers.includes(provider)}
            onCheckedChange={(on) => toggleProvider(provider, on === true)}
            onSelect={(e) => e.preventDefault()}
          >
            {PROVIDER_LABELS[provider] ?? provider}
          </DropdownMenuCheckboxItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Show</DropdownMenuLabel>
        <DropdownMenuCheckboxItem
          checked={filters.showIac}
          onCheckedChange={(on) => onChange({ ...filters, showIac: on === true })}
          onSelect={(e) => e.preventDefault()}
        >
          Lines to where it is defined (IaC)
        </DropdownMenuCheckboxItem>
        <DropdownMenuCheckboxItem
          checked={includeStale}
          onCheckedChange={(on) => onIncludeStale(on === true)}
          onSelect={(e) => e.preventDefault()}
        >
          Not seen in the last discovery
        </DropdownMenuCheckboxItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
