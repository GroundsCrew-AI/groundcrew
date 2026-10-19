import { redirect } from "next/navigation";

import { auth, signOut } from "@/auth";
import { signInternalToken } from "@/lib/internal-jwt";

const API_BASE = (process.env.API_INTERNAL_URL || process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");

/**
 * Where the app sends the browser when the API refuses the session (the password was changed
 * or reset elsewhere). It signs out only when the API really refuses it, so a link to this
 * page from another site cannot sign anyone out.
 */
export async function GET() {
  const session = await auth();
  if (session?.user?.id && API_BASE) {
    let status = 401;
    try {
      const token = await signInternalToken(
        { userId: session.user.id, authTime: session.authAt },
        process.env.INTERNAL_JWT_SECRET,
      );
      const res = await fetch(`${API_BASE}/me`, { headers: { authorization: `Bearer ${token}` }, cache: "no-store" });
      status = res.status;
    } catch {
      status = 0; // the API is down: keep the session
    }
    if (status !== 401) redirect("/");
  }
  await signOut({ redirectTo: "/signin?reason=ended" });
}
