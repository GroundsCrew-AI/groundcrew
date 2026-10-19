"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, type Me, unwrap } from "@/lib/api/client";
import { meKey } from "@/lib/workspace";

export const SIGN_IN_METHOD_LABEL: Record<string, string> = {
  password: "Email and password",
  google: "Google",
  github: "GitHub",
};

/** Change your display name. */
export function useUpdateProfile() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => unwrap(await api.PATCH("/me", { body: { name } })),
    onSuccess: (me) => client.setQueryData<Me>(meKey, me),
  });
}

/** Change (or add) your password. Every session ends afterwards, this one included. */
export function useChangePassword() {
  return useMutation({
    mutationFn: async (body: { current_password: string | null; new_password: string }) => {
      const result = await api.POST("/me/password", { body });
      if (!result.response.ok) {
        const detail = (result.error as { detail?: unknown } | undefined)?.detail;
        throw new ApiError(
          typeof detail === "string" ? detail : "Could not change the password. Try again.",
          result.response.status,
        );
      }
    },
  });
}

/** Why a new password cannot be used, before asking the API (which checks again). */
export function newPasswordProblem(password: string, confirm: string, minLength: number): string | null {
  if (password.length < minLength) return `Use at least ${minLength} characters.`;
  if (password !== confirm) return "The two passwords are different.";
  return null;
}
