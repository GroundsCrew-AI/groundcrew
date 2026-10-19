/** What a sign-in, sign-up, or email-link form shows after its action ran. */
export type FormState = {
  error?: string;
  /** The API's or Auth.js's code (invalid, unverified, locked, rate_limited, invalid_link, ...). */
  code?: string;
  /** The field `error` is about, shown under that field instead of under the form. */
  field?: "email" | "password";
  email?: string;
  name?: string;
  done?: boolean;
};

/** The field an API failure belongs under: a password the API turned down (too short, too common). */
export function failureField(code: string | undefined): FormState["field"] {
  return code === "weak_password" ? "password" : undefined;
}

/** The error to show under `field`, if the last attempt failed because of it. */
export function fieldError(state: FormState, field: NonNullable<FormState["field"]>): string | undefined {
  return state.field === field ? state.error : undefined;
}

/** A same-site path to go to after sign-in; anything else becomes "/". */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/";
  try {
    const url = new URL(next, "http://local");
    if (url.origin !== "http://local") return "/";
    if (/^\/(signin|signup|signout|verify-email|forgot-password|reset-password)\b/.test(url.pathname)) return "/";
    return url.pathname + url.search;
  } catch {
    return "/";
  }
}

const SIGN_IN: Record<string, string> = {
  invalid: "Email or password is incorrect.",
  unverified: "Confirm your email address first: open the link we sent you, or send a new one below.",
  locked: "Too many wrong passwords. Wait a few minutes, or reset your password.",
  rate_limited: "Too many attempts. Wait a few minutes and try again.",
  disabled: "Email sign-in is turned off on this server.",
  unreachable: "The server is not reachable. Try again in a moment.",
  config: "Sign-in is not configured on this server.",
};

export function signInMessage(code: string | undefined): string {
  return (code && SIGN_IN[code]) || "Sign-in failed. Try again.";
}

/** Messages for the sign-in page's ?error= (Auth.js) and ?reason= (ours). */
export function signInPageNotice(params: { error?: string; code?: string; reason?: string }): {
  tone: "error" | "info";
  text: string;
} | null {
  if (params.error === "CredentialsSignin") return { tone: "error", text: signInMessage(params.code) };
  if (params.error === "AccessDenied" || params.error === "OAuthCallbackError") {
    return { tone: "error", text: "Sign-in was cancelled or refused." };
  }
  if (params.error === "OAuthAccountNotLinked") {
    return { tone: "error", text: "That email belongs to another account. Sign in the way you did before." };
  }
  if (params.error === "Configuration") {
    return { tone: "error", text: "Sign-in is not available right now. Try again, or use another way to sign in." };
  }
  if (params.error) return { tone: "error", text: "Sign-in failed. Try again." };
  if (params.reason === "ended") return { tone: "info", text: "Your session ended. Sign in again." };
  if (params.reason === "password") return { tone: "info", text: "Password changed. Sign in with your new password." };
  return null;
}
