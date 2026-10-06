import type { DefaultSession } from "next-auth";

declare module "next-auth" {
  interface Session {
    user: {
      githubId: number;
      login: string;
      avatarUrl: string | null;
    } & DefaultSession["user"];
  }
}

declare module "@auth/core/jwt" {
  interface JWT {
    githubId?: number;
    login?: string;
    avatarUrl?: string | null;
  }
}
