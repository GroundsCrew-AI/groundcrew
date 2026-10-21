import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Inline, Step, Steps, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Notifications",
  description: "Tell your team in Slack or by email when an incident opens, waits for an approval, resolves, or fails.",
};

const TOC = [
  { id: "events", title: "What you hear about" },
  { id: "slack", title: "Slack" },
  { id: "email", title: "Email" },
  { id: "test", title: "Send a test" },
  { id: "contents", title: "What a message contains" },
  { id: "approve", title: "Why there is no approve link" },
  { id: "sign-in-mail", title: "Other email from Groundcrew" },
  { id: "trouble", title: "Troubleshooting" },
];

export default function NotificationsPage() {
  return (
    <DocsArticle
      path="/docs/notifications"
      title="Notifications"
      lead="Each workspace can tell its team about incidents in a Slack channel, by email, or both. An owner sets it up in Settings, Notifications; other members see the settings read-only."
      toc={TOC}
    >
      <DocSection id="events" title="What you hear about">
        <Table
          head={["Event", "Sent when"]}
          rows={[
            ["Incident opened", "The watcher or a person opens a new incident. A signal that joins an open incident does not send another."],
            ["Approval needed", "Medic has a fix waiting for an approval."],
            ["Incident resolved", "The fix worked and the signals are healthy again."],
            ["Incident failed", "No fix worked, none was approved, or the investigation was stopped."],
          ]}
        />
        <p>
          Each of the four can be turned off. Deploy plans waiting for approval are not notified yet; they show in the
          Approvals inbox and on the Overview page.
        </p>
      </DocSection>

      <DocSection id="slack" title="Slack">
        <Steps>
          <Step title="Create an incoming webhook">
            <p>
              In Slack, add the <strong>Incoming Webhooks</strong> app to your workspace, or create a Slack app with
              incoming webhooks turned on. Pick the channel the messages should go to.
            </p>
          </Step>
          <Step title="Copy the webhook URL">
            <p>
              It looks like <Inline>https://hooks.slack.com/services/…</Inline>.
            </p>
          </Step>
          <Step title="Paste it in Groundcrew">
            <p>
              In <strong>Settings</strong>, <strong>Notifications</strong>, paste the URL and save.
            </p>
          </Step>
        </Steps>
        <p>
          The webhook URL is stored encrypted and never shown again; the page only says <em>Saved</em>. To change the
          channel, paste a new URL. To stop Slack messages, remove the saved webhook.
        </p>
      </DocSection>

      <DocSection id="email" title="Email">
        <p>Choose who gets email, then <strong>Save notifications</strong>:</p>
        <ul>
          <li>
            <strong>Owners only</strong> (the default).
          </li>
          <li>
            <strong>Every member</strong> of the workspace.
          </li>
          <li>
            <strong>No email</strong>.
          </li>
        </ul>
        <p>
          Only confirmed addresses get mail. An address is confirmed when the person signed in with an email link, or
          with GitHub using an address GitHub verified.
        </p>
      </DocSection>

      <DocSection id="test" title="Send a test">
        <p>
          <strong>Send test</strong> posts a sample message to every channel that is set up and shows what each one
          answered, so you can see a wrong webhook or a bounced address straight away.
        </p>
      </DocSection>

      <DocSection id="contents" title="What a message contains">
        <ul>
          <li>The incident title and severity.</li>
          <li>The affected resources.</li>
          <li>A one-line summary, which becomes the root cause once Medic knows it.</li>
          <li>A link to the incident page.</li>
          <li>For an approval: each fix and its plan, as before and after lines.</li>
        </ul>
        <p>Messages never contain secret values or tokens.</p>
      </DocSection>

      <DocSection id="approve" title="Why there is no approve link">
        <p>
          Messages never carry approve or reject links. Mail security scanners and chat link previews open every link
          they see, and an approval must be a deliberate decision by a signed-in person. Approving always happens in
          Groundcrew, on the incident&apos;s approval card or in the Approvals inbox, where you see the full plan.
        </p>
        <Callout kind="safety">
          <p>
            A message is a pointer, not a permission. If someone forwards it, the link still requires them to sign in
            and to be an owner or approver of the workspace.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="sign-in-mail" title="Other email from Groundcrew">
        <p>Apart from notifications, Groundcrew sends:</p>
        <ul>
          <li>Sign-in links, when you ask for one. A link works once and expires after 15 minutes.</li>
          <li>Invitations, when an owner adds you to a workspace by email address.</li>
        </ul>
      </DocSection>

      <DocSection id="trouble" title="Troubleshooting">
        <ul>
          <li>
            <strong>Slack test fails with 404 or &quot;no_service&quot;.</strong> The webhook was removed or the app
            uninstalled in Slack. Create a new webhook and paste it.
          </li>
          <li>
            <strong>Slack test fails with &quot;channel_not_found&quot; or &quot;action_prohibited&quot;.</strong> The
            channel was archived or the webhook is restricted. Create a webhook for another channel.
          </li>
          <li>
            <strong>Nobody gets email.</strong> Check the audience is not <em>No email</em>, and that members have
            confirmed addresses. Look in spam folders the first time.
          </li>
          <li>
            <strong>A message arrived late.</strong> Messages are sent in the background with up to 5 attempts, so a
            slow Slack or mail server never holds up an investigation. A message can arrive a little after the
            incident page changed.
          </li>
        </ul>
        <p>
          Who can change these settings is on <Link href="/docs/roles">Members and roles</Link>.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
