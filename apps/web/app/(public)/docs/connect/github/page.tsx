import type { Metadata } from "next";

import { Article, Section } from "@/components/public/prose";

export const metadata: Metadata = {
  title: "Connect GitHub",
  description: "Install the Groundcrew GitHub App on the repositories it should read, build, and open fix pull requests on.",
};

export default function ConnectGitHubPage() {
  return (
    <Article
      title="Connect GitHub"
      lead="Groundcrew reads repositories through its GitHub App with short-lived installation tokens. You choose exactly which repositories it can see."
    >
      <Section id="install" title="Install the app">
        <ol>
          <li>
            In Groundcrew, open <strong>Connections</strong> and choose <strong>Pick repositories</strong>. You need to be a
            workspace owner.
          </li>
          <li>
            The first time, choose <strong>Install the GitHub App</strong>. On GitHub, pick your personal account or an
            organization you administer.
          </li>
          <li>
            Choose <strong>Only select repositories</strong> and pick the ones Groundcrew should work with. You can add or
            remove repositories later in the app&apos;s settings on GitHub.
          </li>
          <li>
            Back in Groundcrew, choose <strong>I installed it</strong>, tick the repositories, and choose{" "}
            <strong>Link</strong>. Then run discovery.
          </li>
        </ol>
      </Section>

      <Section id="permissions" title="What the app can do">
        <ul>
          <li>
            <strong>Repository contents, read and write.</strong> Reads files to see how an app is built and deployed
            (Dockerfiles, workflows, infrastructure code) and to build it. Writes only to new branches named{" "}
            <code>groundcrew/fix-*</code> for fix pull requests.
          </li>
          <li>
            <strong>Pull requests, read and write.</strong> Opens a fix pull request after you approve it, and follows its
            state. Groundcrew never merges a pull request and never pushes to your default branch.
          </li>
          <li>
            <strong>Actions, read.</strong> Reads workflow runs to link deploys to commits and to see when a merged fix was
            deployed.
          </li>
          <li>
            <strong>Metadata, read.</strong> Required by GitHub for every app.
          </li>
          <li>
            <strong>Email addresses, read</strong> (account permission). Used at sign-in to know which of your addresses
            GitHub verified, so invitations and accounts with the same address are matched.
          </li>
        </ul>
        <p>
          We recommend branch protection on your default branch with a required review, so a fix pull request always gets a
          person&apos;s review before it merges.
        </p>
      </Section>

      <Section id="disconnect" title="Disconnect">
        <p>
          Delete the GitHub connection on the Connections page, then uninstall the app on GitHub (your settings,{" "}
          <strong>Applications</strong>, <strong>Installed GitHub Apps</strong>, or the organization&apos;s settings).
          Uninstalling revokes access at once.
        </p>
      </Section>
    </Article>
  );
}
