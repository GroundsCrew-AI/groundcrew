import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests (M8) against a running stack in fake mode: scripts/e2e.sh starts the API and
 * worker (APP_ENV=dev, PIPELINE_FAKE=1), seeds the test user, builds and starts the web app with
 * the dev-only test sign-in, then runs these. CI runs the same script.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3100",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
