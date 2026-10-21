import path from "node:path";

import { loadEnvConfig } from "@next/env";
import type { NextConfig } from "next";

// One .env at the repo root serves the API, the worker, and the web app.
loadEnvConfig(path.resolve(__dirname, "../.."));

const nextConfig: NextConfig = {
  poweredByHeader: false,
  // The production image runs .next/standalone (Dockerfile); `next dev` and `next start` ignore it.
  output: "standalone",
  // The floating dev badge sits on top of the sidebar's user menu.
  devIndicators: false,
};

export default nextConfig;
