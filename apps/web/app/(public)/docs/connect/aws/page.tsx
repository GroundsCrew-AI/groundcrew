import type { Metadata } from "next";
import Link from "next/link";

import { Article, Code, Inline, Section, SubHeading } from "@/components/public/prose";

export const metadata: Metadata = {
  title: "Connect AWS",
  description: "Create Groundcrew's read-only role from a CloudFormation template, verify it, and opt into deploys or emergency fixes.",
};

const CLI = `aws cloudformation create-stack \\
  --stack-name groundcrew-read-only \\
  --template-body file://aws-connect.yaml \\
  --capabilities CAPABILITY_NAMED_IAM \\
  --parameters \\
    ParameterKey=GroundcrewPrincipalArn,ParameterValue=<from the Connections page> \\
    ParameterKey=ExternalId,ParameterValue=<from the Connections page>

aws cloudformation wait stack-create-complete --stack-name groundcrew-read-only
aws cloudformation describe-stacks --stack-name groundcrew-read-only \\
  --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text`;

export default function ConnectAwsPage() {
  return (
    <Article
      title="Connect AWS"
      lead="You create one IAM role in your account from Groundcrew's CloudFormation template. Only Groundcrew can assume it, only with your workspace's ExternalId, and it can read but not change anything."
    >
      <Section id="template" title="1. Get the template and your parameters">
        <ol>
          <li>
            In Groundcrew, open <strong>Connections</strong> and choose <strong>Connect AWS</strong>. You need to be a
            workspace owner.
          </li>
          <li>
            Download the template: <a href="/aws-connect.yaml" download>aws-connect.yaml</a>. Read it if you like; it is
            the same file as in our public repository.
          </li>
          <li>
            Keep the Connections page open. It shows the parameter values with copy buttons:
            <ul>
              <li>
                <Inline>GroundcrewPrincipalArn</Inline>: Groundcrew&apos;s platform identity, the only principal allowed to
                assume the role.
              </li>
              <li>
                <Inline>ExternalId</Inline>: unique to your workspace. Do not reuse it anywhere else.
              </li>
              <li>
                <Inline>Tier</Inline>: <Inline>observe</Inline> (read-only, the default) or <Inline>deploy</Inline>.
              </li>
              <li>
                <Inline>EnableOperate</Inline>: <Inline>false</Inline> unless you want emergency fixes (below).
              </li>
            </ul>
          </li>
        </ol>
      </Section>

      <Section id="console" title="2. Create the stack in the AWS console">
        <ol>
          <li>Sign in to the AWS account you want to connect and open CloudFormation.</li>
          <li>Choose the region where your workloads run (top right).</li>
          <li>
            Choose <strong>Create stack</strong>, then <strong>With new resources (standard)</strong>.
          </li>
          <li>
            Under <strong>Specify template</strong>, choose <strong>Upload a template file</strong>, select{" "}
            <Inline>aws-connect.yaml</Inline>, and choose <strong>Next</strong>.
          </li>
          <li>
            Stack name: <Inline>groundcrew-read-only</Inline>. Paste <Inline>GroundcrewPrincipalArn</Inline> and{" "}
            <Inline>ExternalId</Inline> from the Connections page. Leave the other parameters as they are unless you are
            turning on deploys or emergency fixes.
          </li>
          <li>
            On the last page, tick <strong>I acknowledge that AWS CloudFormation might create IAM resources with custom
            names</strong> and choose <strong>Submit</strong>.
          </li>
          <li>
            Wait for <Inline>CREATE_COMPLETE</Inline> (about a minute), open the <strong>Outputs</strong> tab, and copy{" "}
            <Inline>RoleArn</Inline>.
          </li>
        </ol>
        <SubHeading>Or with the AWS CLI</SubHeading>
        <Code>{CLI}</Code>
      </Section>

      <Section id="verify" title="3. Verify">
        <p>
          Paste the <Inline>RoleArn</Inline> on the Connections page and choose <strong>Verify</strong>. Groundcrew checks
          that the role refuses access without your ExternalId, works with it, and belongs to the account you expect. Then
          run discovery.
        </p>
        <p>
          If Verify says the ExternalId does not match, the stack was created with an older value: delete the stack and
          start again from the Connections page.
        </p>
      </Section>

      <Section id="reads" title="What the role can do">
        <p>
          It reads resource configuration, tags, CloudWatch logs and metrics, and secret metadata, and turns on a local
          Resource Explorer index in the region (set <Inline>CreateResourceExplorerIndex=false</Inline> if the region
          already has one). An explicit deny blocks <Inline>secretsmanager:GetSecretValue</Inline>,{" "}
          <Inline>ssm:GetParameter*</Inline>, <Inline>kms:Decrypt</Inline>, <Inline>s3:GetObject</Inline>, and{" "}
          <Inline>s3:GetObjectVersion</Inline>. Sessions last at most one hour.
        </p>
      </Section>

      <Section id="deploy" title="Optional: deploys">
        <p>
          With <Inline>Tier=deploy</Inline>, the stack also creates an ECS cluster <Inline>groundcrew-deploys</Inline>, the
          role <Inline>groundcrew-deploy</Inline>, and the task execution role <Inline>groundcrew-task-execution</Inline>.
          The deploy role may only create and change resources named <Inline>groundcrew-*</Inline> and tagged{" "}
          <Inline>project=groundcrew</Inline> (ECR repositories, task definitions, services, security groups, log groups
          under <Inline>/groundcrew/</Inline>, secrets under <Inline>groundcrew/</Inline>). It can write a deploy&apos;s
          secrets and is denied reading any secret value. Every deploy still waits for your approval of its plan.
        </p>
        <p>
          For an account that is already connected, update the stack (<strong>Update</strong>,{" "}
          <strong>Replace existing template</strong>, upload the same file) with <Inline>Tier=deploy</Inline>, then choose{" "}
          <strong>Enable deploys</strong> on the Connections page.
        </p>
      </Section>

      <Section id="operate" title="Optional: emergency fixes">
        <p>
          With <Inline>EnableOperate=true</Inline>, the stack creates <Inline>groundcrew-operate</Inline>, which can roll an
          ECS service back or restart it, move a secret&apos;s current version back to the previous one, point a Lambda
          alias at an earlier version, or reboot an EC2 instance whose status checks fail. It acts only on the resources
          you list in <Inline>OperateServiceArns</Inline>, <Inline>OperateSecretArns</Inline>,{" "}
          <Inline>OperateFunctionArns</Inline>, and <Inline>OperateInstanceArns</Inline>. Rollbacks of task definitions
          with their own roles also need those roles in <Inline>OperatePassRoleArns</Inline>. Then turn on{" "}
          <strong>Emergency fixes</strong> for the connection. Each fix needs an approval.
        </p>
      </Section>

      <Section id="disconnect" title="Disconnect">
        <p>
          Delete the connection on the Connections page (this removes everything Groundcrew discovered through it), then
          delete the <Inline>groundcrew-read-only</Inline> stack in CloudFormation. Delete Groundcrew&apos;s deployed
          services first if you used deploys. See <Link href="/security">Security</Link> for the full model.
        </p>
      </Section>
    </Article>
  );
}
