# infra/platform: the Groundcrew platform host

One EC2 instance in 881050425883 / us-east-2 runs everything (docs/phase6-plan.md). Terraform here
creates only: the instance with its encrypted gp3 root volume, an Elastic IP, a security group, and
the `groundcrew-platform` instance role. The host itself is set up by `host/` (P6.5).

```bash
cd infra/platform
cp terraform.tfvars.example terraform.tfvars   # admin_cidr (your IP /32) and ssh_public_key
terraform init
terraform plan -lock=false -out=tfplan         # read-only
terraform apply tfplan                         # only after review
terraform output dns_records
```

Credentials: the `groundcrew-demo` profile (an admin of 881050425883); the provider refuses any other
account. State: `s3://groundcrew-checkout-tfstate-881050425883/groundcrew/platform.tfstate`
(encrypted, versioned, S3 lock file). It holds no secret: the public SSH key and the admin address.

What it sets, and why:

| | |
| --- | --- |
| Instance | `t3.large`, unlimited CPU credits, Ubuntu 24.04 LTS (Canonical's AMI, read once), IMDSv2 only with hop limit 2 (the API and worker containers use the role; the host firewall decides which containers may), termination protection, no detailed monitoring |
| Volume | 80 GiB gp3, encrypted with the AWS-managed key, kept if the instance is terminated |
| Security group | 80 and 443 from anywhere, 22 from `admin_cidr` only, all outbound |
| Role | trusted by EC2 only; one inline policy: `sts:AssumeRole` on `arn:aws:iam::*:role/groundcrew-*` |
| First boot (`cloud-init.yaml.tftpl`) | your key for `ubuntu`, no passwords or root login, ufw matching the security group, fail2ban, unattended security upgrades (reboot 04:00 UTC when needed), 4 GiB swap |

`tests/test_platform_terraform.py` (API tests) fails if any of that loosens.

## Changes later

- **Your IP changed.** Set the new `admin_cidr`, then `terraform apply`: the security group follows.
  ufw on the host still allows only the old address, so either SSH in from the old one first and run
  `sudo ufw allow from <new>/32 to any port 22 proto tcp`, or, if you cannot, stop the instance, set
  its user data to a `bootcmd` that runs that ufw command (cloud-init runs `bootcmd` on every boot),
  start it, SSH in, and put the user data back.
- **Rebuild from scratch.** `terraform apply -replace=aws_instance.platform`, then run `host/` again
  (docs/operations.md). The old root volume stays (it is not deleted on termination): delete it in EC2
  once the new host is serving, if you do not need its database.
- **Remove everything.** Turn off termination protection (`disable_api_termination = false`, apply),
  then `terraform destroy`, then delete the leftover root volume.
