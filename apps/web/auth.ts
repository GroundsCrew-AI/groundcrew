import NextAuth, { CredentialsSignin, type User } from "next-auth";
import Credentials from "next-auth/providers/credentials";
import GitHub, { type GitHubProfile } from "next-auth/providers/github";
import Google, { type GoogleProfile } from "next-auth/providers/google";

import { type AuthUser, authApi, type OAuthProfile } from "@/lib/auth-api";
import { enabledOAuthProviders } from "@/lib/auth-providers";
import { TEST_USER, testAuthEnabled } from "@/lib/test-auth";

/**
 * Sign-in for anyone: email and password, Google, and GitHub (each provider only when its OAuth
 * credentials are set). The account lives in the API (users, user_identities): every sign-in
 * goes through its /auth endpoints, which return the Groundcrew user. The session is an
 * encrypted cookie (AUTH_SECRET) holding that user's id and when they signed in; the API
 * learns who is calling only from the short-lived internal JWT the /api/gc proxy signs.
 */

/** A failed email sign-in; `code` reaches the sign-in page (invalid, unverified, locked, ...). */
export class SignInFailed extends CredentialsSignin {
  constructor(code: string) {
    super();
    this.code = code;
  }
}

function toUser(user: AuthUser): User {
  return { id: user.id, name: user.name, email: user.email, image: user.image };
}

async function oauthUser(profile: OAuthProfile): Promise<User> {
  const result = await authApi<AuthUser>("oauth", { body: profile });
  if (!result.ok) throw new Error(`Sign-in failed: ${result.detail}`);
  return toUser(result.data);
}

type GitHubEmail = { email: string; primary: boolean; verified: boolean };

/** The address GitHub gave us and whether GitHub verified it (only a verified one links
 * accounts or applies email invites). */
async function githubEmail(profile: GitHubProfile, accessToken: string | undefined) {
  if (accessToken) {
    try {
      const res = await fetch("https://api.github.com/user/emails", {
        headers: { Authorization: `Bearer ${accessToken}`, "User-Agent": "groundcrew" },
        signal: AbortSignal.timeout(5000),
      });
      if (res.ok) {
        const emails = (await res.json()) as GitHubEmail[];
        const pick = emails.find((e) => e.email === profile.email) ?? emails.find((e) => e.primary);
        if (pick) return { email: pick.email, verified: Boolean(pick.verified) };
      }
    } catch {
      // No email permission or GitHub is slow: the address stays unverified.
    }
  }
  return { email: profile.email ?? null, verified: false };
}

const oauth = enabledOAuthProviders();

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    ...(oauth.github
      ? [
          GitHub({
            clientId: process.env.AUTH_GITHUB_ID || process.env.GITHUB_APP_CLIENT_ID,
            clientSecret: process.env.AUTH_GITHUB_SECRET || process.env.GITHUB_APP_CLIENT_SECRET,
            async profile(profile, tokens) {
              const { email, verified } = await githubEmail(profile, tokens.access_token);
              return oauthUser({
                provider: "github",
                account_id: String(profile.id),
                email,
                email_verified: verified,
                login: profile.login,
                name: profile.name ?? null,
                avatar_url: profile.avatar_url ?? null,
              });
            },
          }),
        ]
      : []),
    ...(oauth.google
      ? [
          Google({
            async profile(profile: GoogleProfile) {
              return oauthUser({
                provider: "google",
                account_id: profile.sub,
                email: profile.email ?? null,
                email_verified: Boolean(profile.email_verified),
                name: profile.name ?? null,
                avatar_url: profile.picture ?? null,
              });
            },
          }),
        ]
      : []),
    Credentials({
      id: "password",
      name: "Email",
      credentials: { email: { type: "email" }, password: { type: "password" } },
      async authorize(credentials) {
        const email = typeof credentials?.email === "string" ? credentials.email : "";
        const password = typeof credentials?.password === "string" ? credentials.password : "";
        if (!email || !password) throw new SignInFailed("invalid");
        const result = await authApi<AuthUser>("sign-in", { body: { email, password } });
        if (!result.ok) throw new SignInFailed(result.code);
        return toUser(result.data);
      },
    }),
    // Playwright only (APP_ENV=dev and E2E_TEST_AUTH=1): a fixed test user, lib/test-auth.ts.
    ...(testAuthEnabled()
      ? [
          Credentials({
            id: "e2e",
            name: "Test user (dev)",
            credentials: {},
            async authorize() {
              if (!testAuthEnabled()) return null;
              return oauthUser({
                provider: "github",
                account_id: String(TEST_USER.githubId),
                email: null,
                email_verified: false,
                login: TEST_USER.login,
                name: TEST_USER.name,
              });
            },
          }),
        ]
      : []),
  ],
  pages: { signIn: "/signin", error: "/signin" },
  session: { strategy: "jwt" },
  callbacks: {
    jwt({ token, user, account }) {
      // `user` and `account` are only present right after sign-in; nothing a client sends
      // (session updates) changes who the token is for or when they signed in.
      if (account && user?.id) {
        token.uid = user.id;
        token.authAt = Math.floor(Date.now() / 1000);
        token.name = user.name;
        token.email = user.email;
        token.picture = user.image;
      }
      return token;
    },
    session({ session, token }) {
      session.user.id = token.uid ?? "";
      session.authAt = token.authAt ?? 0;
      return session;
    },
    authorized({ auth: session }) {
      return Boolean(session?.user?.id);
    },
  },
});
