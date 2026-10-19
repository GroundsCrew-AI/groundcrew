import { toast } from "sonner";

/**
 * Await a mutation and toast how it went. Awaiting, rather than mutate() callbacks, still
 * toasts when the row or form that started it is gone (a removed member, a cancelled invite,
 * a form remounted with the saved values). API refusals show their own reason: 403 for a
 * member trying an owner-only action, 409 for the last owner.
 */
export async function toastResult<T>(promise: Promise<T>, success: (result: T) => string): Promise<T | null> {
  try {
    const result = await promise;
    toast.success(success(result));
    return result;
  } catch (error) {
    toast.error(error instanceof Error ? error.message : "Something went wrong. Try again.");
    return null;
  }
}
