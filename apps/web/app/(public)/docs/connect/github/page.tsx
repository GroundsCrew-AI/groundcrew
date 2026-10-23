import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Inline, Step, Steps } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Connect GitHub",
  description:
    "Install the Groundcrew GitHub App on the repositories it should read, build, and open fix pull requests on, and link them to your workspace.",
};

const TOC = [
  { id: "before", title: "Before you start" },
  { id: "install", title: "1. Install the app" },
  { id: "find", title: "2. Find my installations" },
  { id: "link", title: "3. Link repositories" },
  { id: "permissions", title: "What the app can do" },
  { id: "trouble", title: "Troubleshooting" },
  { id: "change", title: "Change or remove" },
];

const INSTALL_URL = "https://github.com/apps/groundcrew-app";

export default function ConnectGitHubPage() {
  return (
    <DocsArticle
      path="/docs/connect/github"
      title="Connect GitHub"
      lead="Groundcrew reads repositories through its GitHub App with short-lived installation tokens. You choose exactly which repositories it can see, and you link the ones each workspace should use."
      toc={TOC}
    >
      <DocSection id="before" title="Before you start">
        <ul>
          <li>You are an owner of the Groundcrew workspace.</li>
          <li>
            You can install GitHub Apps on the account that owns the repositories: your personal account, or an
            organization where you are an owner (or an organization owner approves the request).
          </li>
          <li>You know which repositories Groundcrew should work with. You can change the list at any time.</li>
        </ul>
      </DocSection>

      <DocSection id="install" title="1. Install the app">
        <Steps>
          <Step title="Open the picker">
            <p>
              In Groundcrew, open <strong>Connections</strong> and choose <strong>Pick repositories</strong> on the
              GitHub card.
            </p>
          </Step>
          <Step title="Install on GitHub">
            <p>
              Choose <strong>Install the GitHub App</strong>, or open <a href={INSTALL_URL}>github.com/apps/groundcrew-app</a>{" "}
              and choose <strong>Install</strong>. Pick your personal account or the organization.
            </p>
          </Step>
          <Step title="Choose repositories">
            <p>
              Choose <strong>Only select repositories</strong> and pick the ones Groundcrew should work with. Installing
              on all repositories works too, but selecting is safer.
            </p>
          </Step>
        </Steps>
      </DocSection>

      <DocSection id="find" title="2. Find my installations">
        <p>
          The Groundcrew GitHub App is installed on many accounts. A workspace can only use installations that one of its
          owners has shown they can reach on GitHub. Back in Groundcrew, choose <strong>Find my installations</strong>:
        </p>
        <ol>
          <li>GitHub asks you to authorize Groundcrew. This confirms who you are on GitHub.</li>
          <li>
            Groundcrew asks GitHub which installations of its app you can reach, keeps that list for this workspace, and
            drops the one-time GitHub token at once.
          </li>
          <li>You come back to the Connections page with the repository picker open.</li>
        </ol>
        <Callout kind="safety">
          <p>
            Groundcrew keeps no GitHub password or personal token. The authorization is bound to you and to this
            workspace and expires after 10 minutes, so it cannot be replayed into another workspace or by another
            person. The installations you made available are recorded in the workspace&apos;s activity log.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="link" title="3. Link repositories">
        <p>
          The picker lists each installation you can reach and its repositories (private ones have a lock). Tick the
          repositories this workspace should use and choose <strong>Link repositories</strong>. Then run discovery from the
          Connections page.
        </p>
        <p>
          A repository has to be both shared with the app on GitHub and linked here. Sharing decides what Groundcrew
          could read; linking decides what this workspace uses.
        </p>
      </DocSection>

      <DocSection id="permissions" title="What the app can do">
        <ul>
          <li>
            <strong>Repository contents, read and write.</strong> Reads files to see how an app is built and deployed
            (Dockerfiles, workflows, infrastructure code) and to build it. Writes only to new branches named{" "}
            <Inline>groundcrew/fix-*</Inline> for fix pull requests you approved.
          </li>
          <li>
            <strong>Pull requests, read and write.</strong> Opens a fix pull request after you approve it, and follows
            its state. Groundcrew never merges a pull request and never pushes to your default branch.
          </li>
          <li>
            <strong>Actions, read.</strong> Reads workflow runs to link deploys to commits and to see when a merged fix
            was deployed.
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
          Installation tokens expire after at most an hour, and Groundcrew asks GitHub for new ones as it needs them.
          We recommend branch protection on your
          default branch with a required review, so a fix pull request always gets a person&apos;s review before it
          merges.
        </p>
      </DocSection>

      <DocSection id="trouble" title="Troubleshooting">
        <ul>
          <li>
            <strong>Find my installations shows none.</strong> GitHub only lists installations you can access. Check the
            app is installed on that account (in GitHub, <strong>Settings</strong>, <strong>Applications</strong>, or the
            organization&apos;s <strong>GitHub Apps</strong>). For an organization, you need to be a member with access
            to the installation; an organization owner may still need to approve your install request.
          </li>
          <li>
            <strong>The installation is there but has no repositories.</strong> The picker says no repositories are
            shared with the app on that account yet. Use <strong>Install the app on more repositories</strong> to add
            some on GitHub, then open the picker again.
          </li>
          <li>
            <strong>A repository is missing.</strong> It is not shared with the app. Add it in the app&apos;s settings on
            GitHub (<strong>Configure</strong>, <strong>Repository access</strong>).
          </li>
          <li>
            <strong>&quot;This GitHub authorization expired or is not valid&quot;.</strong> More than 10 minutes passed,
            or the page was reloaded with an old link. Choose Find my installations again.
          </li>
          <li>
            <strong>&quot;This GitHub authorization was started by someone else&quot;.</strong> You opened a link another
            person started. Start it yourself.
          </li>
          <li>
            <strong>The repositories are on another account.</strong> Choose <strong>Find installations on another
            account</strong> at the bottom of the picker.
          </li>
          <li>
            <strong>The connection shows as suspended or uninstalled.</strong> Someone suspended or uninstalled the app on
            GitHub. Groundcrew stops the connection at once. Install or unsuspend it, then link the repositories again.
          </li>
        </ul>
      </DocSection>

      <DocSection id="change" title="Change or remove">
        <p>
          To add or remove repositories, change them in the app&apos;s settings on GitHub and open the picker in
          Groundcrew again. A repository you remove on GitHub leaves the connection automatically.
        </p>
        <p>
          To disconnect, remove the GitHub connection on the Connections page, then uninstall the app on GitHub (your
          settings, <strong>Applications</strong>, <strong>Installed GitHub Apps</strong>, or the organization&apos;s
          settings). Uninstalling revokes access at once. Pull requests Groundcrew opened stay on GitHub until you close
          them. See <Link href="/docs/data">Delete data</Link>.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
