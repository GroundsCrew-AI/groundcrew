output "elastic_ip" {
  description = "The platform's public address."
  value       = aws_eip.platform.public_ip
}

output "admin_cidrs" {
  description = "Where SSH is allowed from (host/push.sh applies the same list to ufw)."
  value       = var.admin_cidrs
}

output "instance_id" {
  value = aws_instance.platform.id
}

output "platform_role_arn" {
  description = "GroundcrewPrincipalArn: what customers' connect stacks trust."
  value       = aws_iam_role.platform.arn
}

output "ssh" {
  value = "ssh -i ~/.ssh/groundkrew ubuntu@${aws_eip.platform.public_ip}"
}

output "dns_records" {
  description = "Add these at the registrar (TTL 300)."
  value = [
    "groundkrew.com      A    ${aws_eip.platform.public_ip}",
    "www.groundkrew.com  A    ${aws_eip.platform.public_ip}",
    "api.groundkrew.com  A    ${aws_eip.platform.public_ip}",
    "groundkrew.com      CAA  0 issue \"letsencrypt.org\"",
  ]
}
