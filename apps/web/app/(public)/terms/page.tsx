import type { Metadata } from "next";
import Link from "next/link";

import { CONTACT, POLICIES_UPDATED } from "@/components/public/contact";
import { Article, Section } from "@/components/public/prose";

export const metadata: Metadata = {
  title: "Terms of Service",
  description: "The terms for using Groundcrew.",
};

export default function TermsPage() {
  return (
    <Article
      title="Terms of Service"
      updated={POLICIES_UPDATED}
      lead="These terms apply when you use Groundcrew at groundkrew.com. By creating an account or using the service you agree to them. If you use Groundcrew for a company, you agree for that company and confirm you may do so."
    >
      <Section id="service" title="The service">
        <p>
          Groundcrew connects to infrastructure you choose (cloud accounts, Kubernetes clusters, GitHub repositories,
          Vercel projects), maps it into a resource graph, deploys applications, and investigates incidents. Software
          agents built on large language models do much of this work. Their output can be wrong, which is why every
          change needs a person&apos;s approval.
        </p>
      </Section>

      <Section id="accounts" title="Accounts and workspaces">
        <ul>
          <li>You sign in with GitHub or with a link sent to your email address. Keep access to that account secure.</li>
          <li>
            Each account gets a workspace. Owners invite others as owners, approvers, or viewers and are responsible for
            who has which role.
          </li>
          <li>You must be at least 16 years old and give accurate information.</li>
        </ul>
      </Section>

      <Section id="responsibilities" title="Your responsibilities">
        <ul>
          <li>Connect only accounts, clusters, and repositories you are allowed to give Groundcrew access to.</li>
          <li>
            Give the approver role only to people who should change your production systems, and read each plan before
            you approve it. An approved action runs as planned.
          </li>
          <li>Keep your own backups and recovery plans. Groundcrew does not replace them.</li>
          <li>Follow the terms of the providers you connect (AWS, GitHub, Vercel, your Kubernetes host).</li>
        </ul>
      </Section>

      <Section id="what-groundcrew-does" title="What Groundcrew does and does not do">
        <ul>
          <li>It reads configuration, metadata, logs, and metrics through the access you grant.</li>
          <li>
            It changes your systems only through typed actions you approve (a deploy plan, a rollback, a restart) or
            through pull requests on your repositories. It does not merge pull requests or push to your default branch.
          </li>
          <li>It never asks for long-lived cloud keys and does not read secret values.</li>
          <li>
            Recommendations, diagnoses, and generated code are suggestions. You decide whether to apply them, and you are
            responsible for that decision.
          </li>
        </ul>
      </Section>

      <Section id="acceptable-use" title="Acceptable use">
        <p>Do not use Groundcrew to:</p>
        <ul>
          <li>access systems or data you are not authorized to access;</li>
          <li>attack, scan, or disrupt Groundcrew, its users, or anyone else;</li>
          <li>build or run malware, or code meant to escape the build sandbox;</li>
          <li>get around quotas, rate limits, or spend limits, for example with many accounts;</li>
          <li>break the law or the rights of others.</li>
        </ul>
        <p>
          Each workspace has limits on model spend, web research, builds, runs at once, and connections. Settings shows
          them. We may change limits to keep the service available for everyone.
        </p>
      </Section>

      <Section id="availability" title="Availability and changes to the service">
        <p>
          Groundcrew is provided as is and as available. We do not promise any level of uptime, and features may change,
          pause, or end. We may pause agents for every workspace when platform limits are reached, and we will say so in
          the app.
        </p>
      </Section>

      <Section id="data" title="Your data">
        <p>
          You keep all rights to your data. You let us process it to run the service as described in the{" "}
          <Link href="/privacy">Privacy Policy</Link>, which also explains how to delete connections, workspaces, and your
          account. The service keeps no backups of its database, so keep your own copy of anything you need.
        </p>
      </Section>

      <Section id="liability" title="Disclaimers and limitation of liability">
        <p>
          To the extent the law allows, Groundcrew is provided without warranties of any kind, express or implied,
          including fitness for a particular purpose and non-infringement. We are not liable for indirect, incidental,
          special, or consequential damages, or for lost profits, revenue, or data, arising from your use of the service,
          including from actions you approved. Our total liability for any claim is limited to the amount you paid us for
          the service in the twelve months before the claim, or USD 50 if you paid nothing.
        </p>
      </Section>

      <Section id="termination" title="Ending your use">
        <p>
          You can stop at any time and delete your account in Settings. We may suspend or close accounts that break these
          terms or put the service or others at risk, and we will tell you why when we can. To remove Groundcrew&apos;s
          access on your side, delete the CloudFormation stack, uninstall the GitHub App, and delete the Kubernetes service
          account you created.
        </p>
      </Section>

      <Section id="changes" title="Changes to these terms">
        <p>
          We may update these terms. The date at the top shows the latest version. If a change matters, we will tell you in
          the app or by email before it applies. Using the service after that means you accept the new terms.
        </p>
      </Section>

      <Section id="contact" title="Contact">
        <p>
          Questions about these terms: <a href={`mailto:${CONTACT.privacy}`}>{CONTACT.privacy}</a>. Security issues:{" "}
          <a href={`mailto:${CONTACT.security}`}>{CONTACT.security}</a>.
        </p>
      </Section>
    </Article>
  );
}
