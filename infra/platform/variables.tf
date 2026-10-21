variable "admin_cidr" {
  description = "The only address SSH (22) is open to, as a /32."
  type        = string

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0)) && endswith(var.admin_cidr, "/32")
    error_message = "admin_cidr must be one address, like 203.0.113.7/32."
  }
}

variable "ssh_public_key" {
  description = "Public key for the ubuntu user (key-only SSH). Goes into cloud-init, not into an AWS key pair."
  type        = string

  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) ", var.ssh_public_key))
    error_message = "ssh_public_key must be an OpenSSH public key line."
  }
}

variable "instance_type" {
  description = "Web, API, worker, Postgres, and builds on one host (decision 6.6)."
  type        = string
  default     = "t3.large"
}

variable "volume_gb" {
  description = "Encrypted gp3 root volume: OS, images, Postgres, the build daemon's 30 GB filesystem, logs."
  type        = number
  default     = 80
}

variable "availability_zone" {
  description = "Where the instance and its volume live (the default VPC's default subnet there)."
  type        = string
  default     = "us-east-2a"
}
