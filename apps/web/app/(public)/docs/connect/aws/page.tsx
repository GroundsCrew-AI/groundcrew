import type { Metadata } from "next";
import Link from "next/link";

import { Callout, Code, DocSection, DocsArticle, Inline, Step, Steps, SubHeading } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Connect AWS",
  description:
    "Create Groundcrew's read-only role from a CloudFormation template, verify it, and opt into deploys or emergency fixes.",
};

const TOC = [
  { id: "before", title: "Before you start" },
  { id: "template", title: "1. Get the template and parameters" },
  { id: "console", title: "2. Create the stack" },
  { id: "verify", title: "3. Verify and discover" },
  { id: "reads", title: "What the role can do" },
  { id: "regions", title: "Regions" },
  { id: "deploy", title: "Optional: deploys" },
  { id: "operate", title: "Optional: emergency fixes" },
  { id: "trouble", title: "Troubleshooting" },
  { id: "change", title: "Change or remove" },
];

const CLI = `aws cloudformation create-stack \\
  --region <your region> \\
  --stack-name groundcrew-read-only \\
  --template-body file://aws-connect.yaml \\
  --capabilities CAPABILITY_NAMED_IAM \\
  --parameters \\
    ParameterKey=GroundcrewPrincipalArn,ParameterValue=<from the Connections page> \\
    ParameterKey=ExternalId,ParameterValue=<from the Connections page> \\
    ParameterKey=Tier,ParameterValue=observe

aws cloudformation wait stack-create-complete --region <your region> --stack-name groundcrew-read-only
aws cloudformation describe-stacks --region <your region> --stack-name groundcrew-read-only \\
  --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text`;

export default function ConnectAwsPage() {
  return (
    <DocsArticle
      path="/docs/connect/aws"
      title="Connect AWS"
      lead="You create one IAM role in your account from Groundcrew's CloudFormation template. Only Groundcrew can assume it, only with your workspace's ExternalId, and it can read but not change anything. Groundcrew never stores AWS keys."
      toc={TOC}
    >
      <DocSection id="before" title="Before you start">
        <ul>
          <li>You are an owner of the Groundcrew workspace.</li>
          <li>
            You can create IAM roles with CloudFormation in the AWS account you want to connect (an administrator, or a
            role with <Inline>iam:CreateRole</Inline>, <Inline>iam:PutRolePolicy</Inline>, and CloudFormation rights).
          </li>
          <li>You know which region your workloads run in. You can add more regions later.</li>
          <li>About five minutes.</li>
        </ul>
        <p>
          Connect one AWS account per connection. For several accounts, repeat these steps in each; every connection gets
          its own ExternalId.
        </p>
      </DocSection>

      <DocSection id="template" title="1. Get the template and parameters">
        <Steps>
          <Step title="Open Connect AWS">
            <p>
              In Groundcrew, open <strong>Connections</strong>. On the AWS card, leave <strong>Observe</strong> selected
              (or choose <strong>Deploy</strong> to allow deploys, below) and choose <strong>Connect AWS</strong>.
            </p>
          </Step>
          <Step title="Download the template">
            <p>
              Download <a href="/aws-connect.yaml" download>aws-connect.yaml</a>. Read it if you like; it is the same file
              as in Groundcrew&apos;s repository, and it is the only thing that defines what the role can do.
            </p>
          </Step>
          <Step title="Keep the dialog open">
            <p>It shows the values for this workspace, each with a copy button:</p>
            <ul>
              <li>
                <Inline>GroundcrewPrincipalArn</Inline>: Groundcrew&apos;s service role, the only identity allowed to
                assume your role. Use the value on your Connections page.
              </li>
              <li>
                <Inline>ExternalId</Inline>: unique to this connection. Do not reuse it anywhere else.
              </li>
              <li>
                <Inline>Tier</Inline>: <Inline>observe</Inline> (read-only) or <Inline>deploy</Inline>.
              </li>
            </ul>
          </Step>
        </Steps>
      </DocSection>

      <DocSection id="console" title="2. Create the stack">
        <Steps>
          <Step title="Open CloudFormation">
            <p>
              Sign in to the AWS account you want to connect. Use <strong>Open CloudFormation</strong> in the dialog, or
              open CloudFormation yourself and check the region (top right).
            </p>
          </Step>
          <Step title="Upload the template">
            <p>
              Choose <strong>Create stack</strong>, then <strong>Choose an existing template</strong> and{" "}
              <strong>Upload a template file</strong>. Select <Inline>aws-connect.yaml</Inline> and choose{" "}
              <strong>Next</strong>.
            </p>
          </Step>
          <Step title="Name it and paste the parameters">
            <p>
              Stack name: <Inline>groundcrew-read-only</Inline>. Paste <Inline>GroundcrewPrincipalArn</Inline>,{" "}
              <Inline>ExternalId</Inline>, and <Inline>Tier</Inline> from the dialog. Leave the other parameters as they
              are unless you are turning on emergency fixes.
            </p>
          </Step>
          <Step title="Acknowledge and submit">
            <p>
              Choose <strong>Next</strong> twice. Tick{" "}
              <strong>I acknowledge that AWS CloudFormation might create IAM resources with custom names</strong> and
              choose <strong>Submit</strong>.
            </p>
          </Step>
          <Step title="Copy the RoleArn">
            <p>
              Wait for <Inline>CREATE_COMPLETE</Inline>, about a minute. Open the <strong>Outputs</strong> tab and copy{" "}
              <Inline>RoleArn</Inline>.
            </p>
          </Step>
        </Steps>
        <SubHeading>Or with the AWS CLI</SubHeading>
        <p>
          With credentials for the account you are connecting, in the folder where you saved the template. The dialog
          shows the same commands with your values filled in.
        </p>
        <Code>{CLI}</Code>
      </DocSection>

      <DocSection id="verify" title="3. Verify and discover">
        <p>
          Paste the <Inline>RoleArn</Inline> in the dialog and choose <strong>Verify</strong>. Groundcrew checks three
          things before it saves the connection:
        </p>
        <ul>
          <li>The role refuses Groundcrew without your ExternalId.</li>
          <li>It works with your ExternalId.</li>
          <li>The session belongs to the account in the ARN.</li>
        </ul>
        <p>
          Then choose <strong>Run discovery</strong>. If you close the dialog before verifying, the connection shows as
          pending; choose <strong>Finish connecting</strong> on it to continue with the same ExternalId.
        </p>
      </DocSection>

      <DocSection id="reads" title="What the role can do">
        <p>
          It reads resource configuration and tags (ECS, ECR, RDS, VPC, Lambda, load balancers, EC2, IAM, and others),
          CloudWatch logs and metrics, and secret metadata such as names and when a secret changed. It turns on a local
          Resource Explorer index in the region, so discovery is one search. AWS Config is not turned on. Set{" "}
          <Inline>CreateResourceExplorerIndex=false</Inline> if the region already has an index.
        </p>
        <Callout kind="safety">
          <p>
            An explicit deny blocks <Inline>secretsmanager:GetSecretValue</Inline>, <Inline>ssm:GetParameter*</Inline>,{" "}
            <Inline>kms:Decrypt</Inline>, <Inline>s3:GetObject</Inline>, and <Inline>s3:GetObjectVersion</Inline>. A
            deny wins over any allow, so no other policy can give Groundcrew those rights through this role. Sessions
            last at most one hour.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="regions" title="Regions">
        <p>
          Choose <strong>Regions</strong> on the connection to pick where Groundcrew discovers resources, watches for
          incidents, and runs approved fixes, up to 10 regions. IAM roles are global, so no stack update is needed.
          Groundcrew refreshes the map after you save.
        </p>
      </DocSection>

      <DocSection id="deploy" title="Optional: deploys">
        <p>
          With <Inline>Tier=deploy</Inline>, the stack also creates an ECS cluster <Inline>groundcrew-deploys</Inline>{" "}
          (Fargate, no running cost until something is deployed), the role <Inline>groundcrew-deploy</Inline>, and the
          task execution role <Inline>groundcrew-task-execution</Inline>. The deploy role may only create and change
          ECS services in that cluster, ECR repositories named <Inline>groundcrew-*</Inline>, log groups under{" "}
          <Inline>/groundcrew/</Inline>, security groups tagged <Inline>project=groundcrew</Inline>, and secrets under{" "}
          <Inline>groundcrew/</Inline>, which it can write but never read. Every deploy still waits for your approval of
          its plan.
        </p>
        <p>For an account that is already connected:</p>
        <ol>
          <li>
            Choose <strong>Enable deploys</strong> on the connection.
          </li>
          <li>
            In CloudFormation, select the <Inline>groundcrew-read-only</Inline> stack and choose <strong>Update</strong>,{" "}
            <strong>Replace existing template</strong>, <strong>Upload a template file</strong>, with the same file.
          </li>
          <li>
            Set <Inline>Tier</Inline> to <Inline>deploy</Inline>, leave the rest, and submit. Wait for{" "}
            <Inline>UPDATE_COMPLETE</Inline>, then choose <strong>Verify</strong> in the dialog.
          </li>
        </ol>
        <p>
          Deploys need subnets with public addresses; Groundcrew uses the region&apos;s default VPC. See{" "}
          <Link href="/docs/deploys">Deploys</Link>.
        </p>
      </DocSection>

      <DocSection id="operate" title="Optional: emergency fixes">
        <p>
          With <Inline>EnableOperate=true</Inline>, the stack creates <Inline>groundcrew-operate</Inline>, which can roll
          an ECS service back or restart it, make a secret&apos;s previous version current again, point a Lambda alias at
          an earlier version, or reboot an EC2 instance whose status checks fail. It acts only on the resources you list:
        </p>
        <ul>
          <li>
            <Inline>OperateServiceArns</Inline>: ECS services, for example all services in one cluster.
          </li>
          <li>
            <Inline>OperateSecretArns</Inline>: Secrets Manager secrets, by prefix.
          </li>
          <li>
            <Inline>OperateFunctionArns</Inline> and <Inline>OperateInstanceArns</Inline>: Lambda functions and EC2
            instances.
          </li>
          <li>
            <Inline>OperatePassRoleArns</Inline>: the task roles of services you want rolled back, if their task
            definitions have roles of their own.
          </li>
        </ul>
        <p>
          Update the stack with these, then choose <strong>Enable emergency fixes</strong> on the connection. Groundcrew
          checks the role before it uses it. Each fix needs an approval, by default from an owner.
        </p>
      </DocSection>

      <DocSection id="trouble" title="Troubleshooting">
        <ul>
          <li>
            <strong>&quot;Usually the stack was created with a different ExternalId&quot;.</strong> The stack has an
            ExternalId from an earlier attempt or another workspace. Update the stack with the ExternalId shown in the
            dialog, or delete it and create it again. If the stack is brand new, wait a minute and choose Verify again:
            IAM changes take a moment to reach every region.
          </li>
          <li>
            <strong>&quot;This role can be assumed without the ExternalId, which is unsafe&quot;.</strong> The role&apos;s
            trust policy was changed by hand or created without the template. Groundcrew refuses it. Delete the role and
            create the stack from the template.
          </li>
          <li>
            <strong>The stack fails with &quot;groundcrew-read-only already exists&quot;.</strong> IAM role names are
            global in an account, so a stack in another region, or from an earlier connection, already made the role.
            Update that stack instead, delete it first, or set the <Inline>RoleName</Inline> parameter to another name
            that starts with <Inline>groundcrew-</Inline>.
          </li>
          <li>
            <strong>Connected, but the map is empty.</strong> The stack&apos;s region is not where your workloads run.
            Choose <strong>Regions</strong> on the connection and add the right ones. The role works in every region.
          </li>
          <li>
            <strong>Connected to the wrong account.</strong> The RoleArn came from a stack in another account. Remove the
            connection, delete that stack, and start again signed in to the right account.
          </li>
          <li>
            <strong>&quot;Requires capabilities: [CAPABILITY_NAMED_IAM]&quot;.</strong> You did not tick the
            acknowledgement, or the CLI command is missing <Inline>--capabilities CAPABILITY_NAMED_IAM</Inline>.
          </li>
          <li>
            <strong>An emergency fix plan says it needs PassRole.</strong> The service&apos;s task definition uses a role
            that is not in <Inline>OperatePassRoleArns</Inline>. Add it and update the stack.
          </li>
        </ul>
      </DocSection>

      <DocSection id="change" title="Change or remove">
        <p>
          To change what the role can do, update the <Inline>groundcrew-read-only</Inline> stack with new parameters.
          Turning deploys or emergency fixes off in Groundcrew stops using those roles at once; update the stack too to
          delete them.
        </p>
        <p>
          To disconnect, remove the connection on the Connections page. That deletes everything Groundcrew discovered
          through it. Then delete the <Inline>groundcrew-read-only</Inline> stack in CloudFormation. If you used
          deploys, delete Groundcrew&apos;s deployed services in <Inline>groundcrew-deploys</Inline> first, so the
          cluster can be removed. The full model is on the <Link href="/security">Security</Link> page.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
