# Groundcrew's platform host: one EC2 instance with its encrypted root volume, an Elastic IP, a
# security group, and an instance role. Nothing else (docs/phase6-plan.md section 4).

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "platform" {
  vpc_id            = data.aws_vpc.default.id
  availability_zone = var.availability_zone
  default_for_az    = true
}

# Canonical's Ubuntu 24.04 LTS, amd64, gp3. Read once at create time; later images do not
# replace the instance (unattended-upgrades keeps it patched).
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

resource "aws_security_group" "platform" {
  name        = "groundcrew-platform"
  description = "Groundcrew platform host: HTTP and HTTPS from anywhere, SSH from the admin address"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.platform.id
  description       = "HTTP (redirects to HTTPS; ACME challenges)"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.platform.id
  description       = "HTTPS"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each          = toset(var.admin_cidrs)
  security_group_id = aws_security_group.platform.id
  description       = "SSH from an admin address"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.platform.id
  description       = "Outbound: package and image pulls, model and cloud APIs, customer accounts"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_instance" "platform" {
  ami                     = data.aws_ami.ubuntu.id
  instance_type           = var.instance_type
  subnet_id               = data.aws_subnet.platform.id
  vpc_security_group_ids  = [aws_security_group.platform.id]
  iam_instance_profile    = aws_iam_instance_profile.platform.name
  disable_api_termination = true
  monitoring              = false

  credit_specification {
    cpu_credits = "unlimited"
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
    # 2: the API and worker containers reach the instance role through IMDS. Which containers
    # may is decided by the host firewall (plan section 5.4), not by the hop limit.
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.volume_gb
    encrypted             = true
    delete_on_termination = false
    tags = {
      Name = "groundcrew-platform-root"
    }
  }

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    ssh_public_key = var.ssh_public_key
    admin_cidrs    = var.admin_cidrs
  })
  # cloud-init runs on first boot only; changing it later must not replace the host. A rebuild
  # is an explicit `terraform apply -replace=aws_instance.platform` (docs/operations.md).
  user_data_replace_on_change = false

  tags = {
    Name = "groundcrew-platform"
  }

  lifecycle {
    ignore_changes = [ami, user_data]
  }
}

resource "aws_eip" "platform" {
  domain   = "vpc"
  instance = aws_instance.platform.id

  tags = {
    Name = "groundcrew-platform"
  }
}
