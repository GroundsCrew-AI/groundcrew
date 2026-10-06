export { auth as middleware } from "@/auth";

// Everything is behind sign-in except the sign-in page, Auth.js itself, the API proxy
// (which answers 401 on its own so fetch callers get JSON, not a redirect), and static files.
export const config = {
  matcher: ["/((?!signin|api/auth|api/gc|_next/static|_next/image|favicon.ico).*)"],
};
