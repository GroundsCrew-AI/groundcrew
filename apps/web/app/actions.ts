"use server";

import { AuthError, CredentialsSignin } from "next-auth";

import { signIn, signOut } from "@/auth";
import { type AuthUser, authApi } from "@/lib/auth-api";
import { enabledOAuthProviders } from "@/lib/auth-providers";
import { failureField, type FormState, safeNext, signInMessage } from "@/lib/auth-forms";
import { testAuthEnabled } from "@/lib/test-auth";

function field(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value : "";
}

/** Email and password. Redirects on success; otherwise says what to do next. */
async function passwordSignIn(email: string, password: string, next: string): Promise<FormState> {
  try {
    await signIn("password", { email, password, redirectTo: next });
  } catch (error) {
    if (error instanceof CredentialsSignin) {
      // A wrong password is said under the password field; the other refusals are about the account.
      const under = error.code === "invalid" ? ("password" as const) : undefined;
      return { error: signInMessage(error.code), code: error.code, field: under, email };
    }
    if (error instanceof AuthError) return { error: signInMessage("error"), email };
    throw error; // the redirect after a successful sign-in
  }
  return {};
}

export async function signInWithPassword(_prev: FormState, formData: FormData): Promise<FormState> {
  const email = field(formData, "email").trim();
  const password = field(formData, "password");
  if (!email || !password) return { error: "Enter your email and password.", email };
  return passwordSignIn(email, password, safeNext(field(formData, "next")));
}

export async function signInWithProvider(formData: FormData) {
  const provider = field(formData, "provider");
  const enabled = enabledOAuthProviders();
  if (provider !== "github" && provider !== "google") throw new Error("Unknown sign-in provider.");
  if (!enabled[provider]) throw new Error(`${provider} sign-in is not configured.`);
  await signIn(provider, { redirectTo: safeNext(field(formData, "next")) });
}

export async function signUpAction(_prev: FormState, formData: FormData): Promise<FormState> {
  const email = field(formData, "email").trim();
  const name = field(formData, "name").trim();
  const password = field(formData, "password");
  if (password !== field(formData, "confirm")) {
    return { error: "The two passwords are different.", field: "password", email, name };
  }
  const result = await authApi<{ status: string }>("sign-up", {
    body: { email, password, name: name || null },
  });
  if (!result.ok) return { error: result.detail, code: result.code, field: failureField(result.code), email, name };
  return { done: true, email };
}

export async function resendVerificationAction(_prev: FormState, formData: FormData): Promise<FormState> {
  const email = field(formData, "email").trim();
  const result = await authApi("resend-verification", { body: { email } });
  if (!result.ok) return { error: result.detail, email };
  return { done: true, email };
}

/** Confirm the address with the link's token and the account's password, then sign in. */
export async function verifyEmailAction(_prev: FormState, formData: FormData): Promise<FormState> {
  const token = field(formData, "token");
  const password = field(formData, "password");
  const result = await authApi<AuthUser>("verify-email", { body: { token, password } });
  // "invalid" here is the wrong password for the link's account.
  if (!result.ok) return { error: result.detail, code: result.code, field: result.code === "invalid" ? "password" : undefined };
  if (!result.data.email) return { done: true };
  return passwordSignIn(result.data.email, password, "/");
}

export async function forgotPasswordAction(_prev: FormState, formData: FormData): Promise<FormState> {
  const email = field(formData, "email").trim();
  const result = await authApi("forgot-password", { body: { email } });
  if (!result.ok) return { error: result.detail, email };
  return { done: true, email };
}

/** Set the new password from the link, then sign in with it. */
export async function resetPasswordAction(_prev: FormState, formData: FormData): Promise<FormState> {
  const token = field(formData, "token");
  const password = field(formData, "password");
  if (password !== field(formData, "confirm")) return { error: "The two passwords are different.", field: "password" };
  const result = await authApi<AuthUser>("reset-password", { body: { token, password } });
  if (!result.ok) return { error: result.detail, code: result.code, field: failureField(result.code) };
  if (!result.data.email) return { done: true };
  return passwordSignIn(result.data.email, password, "/");
}

/** Playwright's sign-in (APP_ENV=dev and E2E_TEST_AUTH=1 only). */
export async function signInAsTestUser() {
  if (!testAuthEnabled()) throw new Error("Test sign-in is off.");
  await signIn("e2e", { redirectTo: "/" });
}

export async function signOutAction() {
  await signOut({ redirectTo: "/signin" });
}

/** After a password change every session ends, this one too: sign in again with the new one. */
export async function signOutAfterPasswordChange() {
  await signOut({ redirectTo: "/signin?reason=password" });
}
