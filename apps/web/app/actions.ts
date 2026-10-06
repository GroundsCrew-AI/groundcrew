"use server";

import { signIn, signOut } from "@/auth";

export async function signInWithGitHub(formData: FormData) {
  const next = formData.get("next");
  const redirectTo = typeof next === "string" && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  await signIn("github", { redirectTo });
}

export async function signOutAction() {
  await signOut({ redirectTo: "/signin" });
}
