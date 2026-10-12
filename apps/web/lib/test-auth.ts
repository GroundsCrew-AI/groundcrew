/**
 * Dev-only sign-in for Playwright (M8): a fixed test user, never a real account. On only when
 * APP_ENV=dev and E2E_TEST_AUTH=1 on the web server; everywhere else the provider is not even
 * registered and the button is not rendered.
 */
export const TEST_USER = { githubId: 990001, login: "e2e-tester", name: "E2E Tester" } as const;

export function testAuthEnabled(): boolean {
  return process.env.APP_ENV === "dev" && process.env.E2E_TEST_AUTH === "1";
}
