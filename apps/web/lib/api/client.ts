import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

/** Typed client for the API, through the server-side proxy at /api/gc. */
export const api = createClient<paths>({ baseUrl: "/api/gc" });

// The API refuses a session that started before a password change or reset: sign out once,
// through /signout (which checks with the API first), instead of failing every request.
let signingOut = false;
api.use({
  onResponse({ response }) {
    if (response.status === 401 && typeof window !== "undefined" && !signingOut) {
      signingOut = true;
      window.location.assign("/signout");
    }
    return response;
  },
});

export type Schemas = components["schemas"];
export type Me = Schemas["MeOut"];
export type Workspace = Schemas["WorkspaceOut"];
export type Run = Schemas["RunOut"];
export type RunDetail = Schemas["RunDetail"];
export type Step = Schemas["StepOut"];
export type Approval = Schemas["ApprovalOut"];
export type RunStatusEvent = Schemas["RunStatusEvent"];
export type ApprovalStepEvent = Schemas["ApprovalStepEvent"];
export type Summary = Schemas["WorkspaceSummary"];
export type RunStatus = Schemas["RunStatus"];
export type StopReason = Schemas["StopReason"];
export type ToolPolicy = Schemas["ToolPolicy"];
export type Decision = Schemas["Decision"];

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type Detail = { detail?: unknown } | undefined;

function messageFrom(error: unknown, status: number): string {
  const detail = (error as Detail)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return "The request was not valid.";
  if (status === 401) return "Your session ended. Sign in again.";
  if (status >= 500) return "The server had a problem. Try again.";
  return `Request failed (${status}).`;
}

/** Unwrap an openapi-fetch result or throw an ApiError with a readable message. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined && result.response.ok) return result.data;
  throw new ApiError(messageFrom(result.error, result.response.status), result.response.status);
}
