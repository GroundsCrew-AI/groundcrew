"use client";

import { MailCheck } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useActionState } from "react";
import { useFormStatus } from "react-dom";

import {
  forgotPasswordAction,
  resendVerificationAction,
  resetPasswordAction,
  signInWithPassword,
  signUpAction,
  verifyEmailAction,
} from "@/app/actions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { FormState } from "@/lib/auth-forms";

function Submit({ children, pending: pendingText }: { children: ReactNode; pending: string }) {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" size="lg" className="h-10 w-full text-[14px]" disabled={pending} aria-busy={pending}>
      {pending ? pendingText : children}
    </Button>
  );
}

export function FormMessage({ tone = "error", children }: { tone?: "error" | "info"; children: ReactNode }) {
  return (
    <p
      role={tone === "error" ? "alert" : "status"}
      className={
        tone === "error"
          ? "rounded-md border px-3 py-2 text-[13px] text-[var(--status-failed)]"
          : "rounded-md border px-3 py-2 text-[13px] text-muted-foreground"
      }
    >
      {children}
    </p>
  );
}

function Field({
  id,
  label,
  hint,
  aside,
  ...input
}: React.ComponentProps<typeof Input> & { id: string; label: string; hint?: string; aside?: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between">
        <Label htmlFor={id} className="text-[13px]">
          {label}
        </Label>
        {aside}
      </div>
      <Input id={id} name={id} className="h-10 text-[14px]" aria-describedby={hint ? `${id}-hint` : undefined} {...input} />
      {hint && (
        <p id={`${id}-hint`} className="text-[12px] text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  );
}

/** "Check your email" after a link was sent, with a way to send it again. */
function CheckEmail({ email, children }: { email?: string; children: ReactNode }) {
  return (
    <div className="space-y-3 rounded-lg border bg-card px-4 py-4" role="status">
      <p className="flex items-center gap-2 text-[14px] font-medium">
        <MailCheck aria-hidden="true" className="size-4" />
        Check your email
      </p>
      <p className="text-[13px] text-muted-foreground">
        {children}
        {email && (
          <>
            {" "}
            We used <span className="font-medium text-foreground">{email}</span>.
          </>
        )}
      </p>
    </div>
  );
}

function ResendVerification({ email }: { email: string }) {
  const [state, action] = useActionState<FormState, FormData>(resendVerificationAction, {});
  return (
    <form action={action} className="space-y-2">
      <input type="hidden" name="email" value={email} />
      {state.done ? (
        <FormMessage tone="info">If that account still needs confirming, a new link is on its way.</FormMessage>
      ) : (
        <>
          {state.error && <FormMessage>{state.error}</FormMessage>}
          <ResendButton />
        </>
      )}
    </form>
  );
}

function ResendButton() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" variant="outline" size="sm" disabled={pending}>
      {pending ? "Sending..." : "Send a new confirmation link"}
    </Button>
  );
}

export function PasswordSignInForm({ next, defaultEmail }: { next: string; defaultEmail?: string }) {
  const [state, action] = useActionState<FormState, FormData>(signInWithPassword, { email: defaultEmail });
  return (
    <div className="space-y-3">
      <form action={action} className="space-y-4" noValidate={false}>
        <input type="hidden" name="next" value={next} />
        <Field id="email" label="Email" type="email" autoComplete="email" required defaultValue={state.email} />
        <Field
          id="password"
          label="Password"
          type="password"
          autoComplete="current-password"
          required
          aside={
            <Link href="/forgot-password" className="text-[12px] text-muted-foreground underline-offset-4 hover:underline">
              Forgot password?
            </Link>
          }
        />
        {state.error && <FormMessage>{state.error}</FormMessage>}
        <Submit pending="Signing in...">Sign in</Submit>
      </form>
      {state.code === "unverified" && state.email && <ResendVerification email={state.email} />}
    </div>
  );
}

export function SignUpForm({ defaultEmail, minLength }: { defaultEmail?: string; minLength: number }) {
  const [state, action] = useActionState<FormState, FormData>(signUpAction, { email: defaultEmail });
  if (state.done) {
    return (
      <div className="space-y-3">
        <CheckEmail email={state.email}>
          We sent a link to confirm your address. Open it to finish creating your account; it asks for the password
          you just chose.
        </CheckEmail>
        {state.email && <ResendVerification email={state.email} />}
      </div>
    );
  }
  return (
    <form action={action} className="space-y-4">
      <Field id="name" label="Your name" autoComplete="name" defaultValue={state.name} maxLength={200} />
      <Field id="email" label="Work email" type="email" autoComplete="email" required defaultValue={state.email} />
      <Field
        id="password"
        label="Password"
        type="password"
        autoComplete="new-password"
        required
        minLength={minLength}
        hint={`At least ${minLength} characters. A few unrelated words work well.`}
      />
      <Field id="confirm" label="Confirm password" type="password" autoComplete="new-password" required minLength={minLength} />
      {state.error && <FormMessage>{state.error}</FormMessage>}
      <Submit pending="Creating account...">Create account</Submit>
    </form>
  );
}

export function VerifyEmailForm({ token }: { token: string }) {
  const [state, action] = useActionState<FormState, FormData>(verifyEmailAction, {});
  if (state.done) {
    return (
      <FormMessage tone="info">
        Your address is confirmed. <Link href="/signin" className="underline underline-offset-4">Sign in</Link>.
      </FormMessage>
    );
  }
  const expired = state.code === "invalid_link";
  return (
    <form action={action} className="space-y-4">
      <input type="hidden" name="token" value={token} />
      <Field id="password" label="Password" type="password" autoComplete="current-password" required autoFocus />
      {state.error && (
        <FormMessage>
          {state.error}
          {expired && (
            <>
              {" "}
              <Link href="/signin" className="underline underline-offset-4">
                Sign in
              </Link>{" "}
              to get a new link.
            </>
          )}
        </FormMessage>
      )}
      <Submit pending="Confirming...">Confirm and sign in</Submit>
    </form>
  );
}

export function ForgotPasswordForm({ defaultEmail }: { defaultEmail?: string }) {
  const [state, action] = useActionState<FormState, FormData>(forgotPasswordAction, { email: defaultEmail });
  if (state.done) {
    return (
      <CheckEmail email={state.email}>
        If an account uses this address, we sent it a link to choose a new password. It expires in an hour.
      </CheckEmail>
    );
  }
  return (
    <form action={action} className="space-y-4">
      <Field id="email" label="Email" type="email" autoComplete="email" required defaultValue={state.email} />
      {state.error && <FormMessage>{state.error}</FormMessage>}
      <Submit pending="Sending...">Send reset link</Submit>
    </form>
  );
}

export function ResetPasswordForm({ token, minLength }: { token: string; minLength: number }) {
  const [state, action] = useActionState<FormState, FormData>(resetPasswordAction, {});
  if (state.done) {
    return (
      <FormMessage tone="info">
        Your password is changed. <Link href="/signin" className="underline underline-offset-4">Sign in</Link>.
      </FormMessage>
    );
  }
  return (
    <form action={action} className="space-y-4">
      <input type="hidden" name="token" value={token} />
      <Field
        id="password"
        label="New password"
        type="password"
        autoComplete="new-password"
        required
        minLength={minLength}
        hint={`At least ${minLength} characters. This signs you out on every other device.`}
      />
      <Field id="confirm" label="Confirm new password" type="password" autoComplete="new-password" required minLength={minLength} />
      {state.error && (
        <FormMessage>
          {state.error}
          {state.code === "invalid_link" && (
            <>
              {" "}
              <Link href="/forgot-password" className="underline underline-offset-4">
                Send a new link
              </Link>
              .
            </>
          )}
        </FormMessage>
      )}
      <Submit pending="Saving...">Set new password</Submit>
    </form>
  );
}
