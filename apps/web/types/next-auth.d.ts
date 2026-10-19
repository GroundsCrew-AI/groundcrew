import type { DefaultSession } from "next-auth";

declare module "next-auth" {
  interface Session {
    /** The Groundcrew user's id (users.id in the API). Empty for a session from before it. */
    user: { id: string } & DefaultSession["user"];
    /** When this session signed in, in seconds; the API refuses sessions older than a password change. */
    authAt: number;
  }
}

declare module "@auth/core/jwt" {
  interface JWT {
    uid?: string;
    authAt?: number;
  }
}
