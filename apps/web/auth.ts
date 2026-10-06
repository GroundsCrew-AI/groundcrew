import NextAuth from "next-auth";
import GitHub from "next-auth/providers/github";

/**
 * GitHub sign-in with our GitHub App's OAuth credentials. The session lives in an encrypted
 * cookie (AUTH_SECRET); nothing is stored in the database by Auth.js. The API learns who the
 * user is only from the short-lived internal JWT the /api/gc proxy signs per request.
 */
export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    GitHub({
      clientId: process.env.AUTH_GITHUB_ID || process.env.GITHUB_APP_CLIENT_ID,
      clientSecret: process.env.AUTH_GITHUB_SECRET || process.env.GITHUB_APP_CLIENT_SECRET,
    }),
  ],
  pages: { signIn: "/signin" },
  session: { strategy: "jwt" },
  callbacks: {
    jwt({ token, profile }) {
      // `profile` is only present right after sign-in.
      if (profile) {
        token.githubId = Number(profile.id);
        token.login = String(profile.login ?? "");
        token.avatarUrl = typeof profile.avatar_url === "string" ? profile.avatar_url : null;
      }
      return token;
    },
    session({ session, token }) {
      session.user.githubId = token.githubId ?? 0;
      session.user.login = token.login ?? "";
      session.user.avatarUrl = token.avatarUrl ?? null;
      return session;
    },
    authorized({ auth: session }) {
      return Boolean(session?.user?.githubId);
    },
  },
});
