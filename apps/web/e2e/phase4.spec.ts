import { expect, type Page, test } from "@playwright/test";

/** Fake mode: the real web app, API, worker, and pipeline; scripted model, sandbox, cluster. */

async function signIn(page: Page) {
  await page.goto("/signin");
  await page.getByRole("button", { name: "Sign in as the test user (dev)" }).click();
  await expect(page).not.toHaveURL(/\/signin/);
}

test.describe.configure({ mode: "serial" });

test("signs in with the dev-only test user", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/signin/);
  await signIn(page);
  await expect(page.getByText("E2E Tester").or(page.getByText("e2e-tester")).first()).toBeVisible();
});

test("lists the workspace's connections", async ({ page }) => {
  await signIn(page);
  await page.goto("/connections");
  for (const name of ["GroundsCrew-AI", "kind e2e", "AWS 111122223333"]) {
    await expect(page.getByText(name, { exact: true }).first()).toBeVisible();
  }
});

test("shows the graph on the Map", async ({ page }) => {
  await signIn(page);
  await page.goto("/map");
  await expect(page.getByText("checkout-api", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("checkout-orders-db", { exact: true }).first()).toBeVisible();
});

test("deploys from the Deploy page through an approval to a live URL", async ({ page }) => {
  await signIn(page);
  await page.goto("/deploy");
  await expect(page.getByRole("heading", { name: "New deploy" })).toBeVisible();
  await page.getByRole("radio", { name: /kind e2e/ }).check();
  // The observe-tier account cannot be picked.
  await expect(page.getByRole("radio", { name: /AWS 111122223333/ })).toBeDisabled();
  await page.getByRole("button", { name: /add env var/i }).click();
  await page.getByLabel("Name").fill("DATABASE_URL");
  await page.getByLabel("Value of DATABASE_URL").fill("postgresql://e2e:not-a-secret@db.invalid/app");
  await page.getByRole("button", { name: /^deploy$/i }).click();

  await expect(page).toHaveURL(/\/deploy\/[0-9a-f-]{36}$/);
  // Builder: the first build fails on psycopg2, the automatic fix passes.
  await expect(page.getByText(/pg_config executable not found/).first()).toBeVisible({ timeout: 60_000 });
  const card = page.getByRole("region", { name: "Deploy plan approval" });
  await expect(card).toBeVisible({ timeout: 60_000 });
  await expect(card.getByText("push", { exact: true }).first()).toBeVisible();
  await expect(card).toContainText("********");
  await expect(card).not.toContainText("not-a-secret");

  await card.getByRole("button", { name: /approve and deploy/i }).click();
  await expect(page.getByText("http://legacy-flask-app.localhost").first()).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText(/Live on Kubernetes/)).toBeVisible();
  await expect(page.getByRole("link", { name: "View on Map" })).toBeVisible();
  await expect(page.getByText("2, fixed")).toBeVisible();

  await page.goto("/deploy");
  await expect(page.getByText("Live", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("GroundsCrew-AI/legacy-flask-app").first()).toBeVisible();
});
