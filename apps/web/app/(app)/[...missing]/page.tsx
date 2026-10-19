import { notFound } from "next/navigation";

/**
 * Unknown URLs inside the app land here (every real route is more specific), so they get the
 * app's 404 with the sidebar instead of the bare root one. Signed-out visitors are sent to sign
 * in first, like every other app page.
 */
export default function MissingPage() {
  notFound();
}
