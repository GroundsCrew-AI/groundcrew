import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FormState } from "@/lib/auth-forms";

import { PasswordSignInForm, SignUpForm } from "./forms";

const signUpAction = vi.fn<(prev: FormState, data: FormData) => Promise<FormState>>();
const signInWithPassword = vi.fn<(prev: FormState, data: FormData) => Promise<FormState>>();
vi.mock("@/app/actions", () => ({
  signUpAction: (prev: FormState, data: FormData) => signUpAction(prev, data),
  signInWithPassword: (prev: FormState, data: FormData) => signInWithPassword(prev, data),
  resendVerificationAction: vi.fn(async () => ({})),
  forgotPasswordAction: vi.fn(),
  resetPasswordAction: vi.fn(),
  verifyEmailAction: vi.fn(),
}));

beforeEach(() => {
  signUpAction.mockReset();
  signInWithPassword.mockReset();
});

async function signUp() {
  render(<SignUpForm minLength={8} />);
  await userEvent.type(screen.getByLabelText("Work email"), "ada@example.com");
  await userEvent.type(screen.getByLabelText("Password"), "correct horse");
  await userEvent.type(screen.getByLabelText("Confirm password"), "correct house");
  await userEvent.click(screen.getByRole("button", { name: "Create account" }));
}

describe("SignUpForm", () => {
  it("says a password mismatch under the password field, tied to it", async () => {
    signUpAction.mockResolvedValue({ error: "The two passwords are different.", field: "password", email: "ada@example.com" });
    await signUp();
    const password = screen.getByLabelText("Password");
    await waitFor(() => expect(password).toHaveAttribute("aria-invalid", "true"));
    expect(password).toHaveAccessibleDescription(expect.stringContaining("The two passwords are different."));
    // Said once, under the field, not again under the form.
    expect(screen.getAllByText("The two passwords are different.")).toHaveLength(1);
    expect(screen.getByLabelText("Work email")).not.toHaveAttribute("aria-invalid");
  });

  it("keeps an error about no one field under the form", async () => {
    signUpAction.mockResolvedValue({ error: "Too many attempts.", code: "rate_limited", email: "ada@example.com" });
    await signUp();
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts.");
    expect(screen.getByLabelText("Password")).not.toHaveAttribute("aria-invalid");
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription(/At least 8 characters/);
  });
});

describe("PasswordSignInForm", () => {
  it("marks the password field when the password is wrong", async () => {
    signInWithPassword.mockResolvedValue({
      error: "Email or password is incorrect.",
      code: "invalid",
      field: "password",
      email: "ada@example.com",
    });
    render(<PasswordSignInForm next="/" />);
    await userEvent.type(screen.getByLabelText("Email"), "ada@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const password = screen.getByLabelText("Password");
    await waitFor(() => expect(password).toHaveAttribute("aria-invalid", "true"));
    expect(password).toHaveAccessibleDescription("Email or password is incorrect.");
  });
});
