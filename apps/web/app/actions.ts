"use server";

import { signIn, signOut } from "@/auth";
import { testAuthEnabled } from "@/lib/test-auth";

export async function signInWithGitHub(formData: FormData) {
  const next = formData.get("next");
  const redirectTo = typeof next === "string" && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  await signIn("github", { redirectTo });
}

/** Playwright's sign-in (APP_ENV=dev and E2E_TEST_AUTH=1 only). */
export async function signInAsTestUser() {
  if (!testAuthEnabled()) throw new Error("Test sign-in is off.");
  await signIn("e2e", { redirectTo: "/" });
}

export async function signOutAction() {
  await signOut({ redirectTo: "/signin" });
}
