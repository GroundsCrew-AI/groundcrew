export { auth as middleware } from "@/auth";

// Everything is behind sign-in except the account pages (sign in, sign up, confirm email,
// forgot and reset password, sign out), Auth.js itself, the API proxy (which answers 401 on its
// own so fetch callers get JSON, not a redirect), and static files.
export const config = {
  matcher: [
    "/((?!signin|signup|verify-email|forgot-password|reset-password|signout|api/auth|api/gc|_next/static|_next/image|favicon.ico).*)",
  ],
};
