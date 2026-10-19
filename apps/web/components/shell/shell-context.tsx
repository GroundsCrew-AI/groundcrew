"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

const STORAGE_KEY = "groundcrew.sidebar-collapsed";

type ShellContextValue = {
  collapsed: boolean;
  toggleCollapsed: () => void;
  mobileOpen: boolean;
  setMobileOpen: (open: boolean) => void;
  /** Read `text` to screen readers through the shell's one live region. */
  announce: (text: string) => void;
};

const ShellContext = createContext<ShellContextValue | null>(null);

export function ShellProvider({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(STORAGE_KEY) === "1");
    } catch {
      // Storage blocked: start expanded.
    }
  }, []);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((value) => {
      try {
        window.localStorage.setItem(STORAGE_KEY, value ? "0" : "1");
      } catch {
        // The choice just won't persist.
      }
      return !value;
    });
  }, []);

  const announce = useCallback((text: string) => {
    // Clear first so the same text twice is read twice.
    setMessage("");
    window.setTimeout(() => setMessage(text), 50);
  }, []);

  const value = useMemo(
    () => ({ collapsed, toggleCollapsed, mobileOpen, setMobileOpen, announce }),
    [collapsed, toggleCollapsed, mobileOpen, announce],
  );
  return (
    <ShellContext.Provider value={value}>
      {children}
      {/* Mounted once and kept: screen readers ignore a live region added with its text. */}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {message}
      </div>
    </ShellContext.Provider>
  );
}

/**
 * Announce `text` whenever it changes (a run or incident status), skipping the first value so
 * opening a page reads nothing. A no-op outside the app shell (tests, sign-in pages).
 */
export function useAnnounceChange(text: string | null | undefined): void {
  const announce = useContext(ShellContext)?.announce;
  const [previous, setPrevious] = useState(text);
  useEffect(() => {
    if (text === previous) return;
    setPrevious(text);
    if (text && previous !== undefined && announce) announce(text);
  }, [text, previous, announce]);
}

export function useShell(): ShellContextValue {
  const value = useContext(ShellContext);
  if (!value) throw new Error("useShell must be used inside ShellProvider");
  return value;
}
