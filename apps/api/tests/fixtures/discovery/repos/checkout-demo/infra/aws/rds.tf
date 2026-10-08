resource "random_password" "db" {
  length = 32
  # Letters and digits only: the password goes into a URL and RDS rejects / @ " and spaces.
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.name}-db"
  subnet_ids = aws_subnet.public[*].id

  tags = { Name = "${var.name}-db" }
}

resource "aws_db_instance" "orders" {
  identifier     = "${var.name}-orders-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "checkout"
  username = "checkout"
  password = random_password.db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  multi_az               = false
  # Only reachable from outside the VPC when the api runs on Azure, and then only from
  # allowed_db_cidrs (security group above).
  publicly_accessible = var.api_platform == "azure"

  backup_retention_period    = 1
  deletion_protection        = false
  skip_final_snapshot        = true
  auto_minor_version_upgrade = true
  apply_immediately          = true

  tags = { Name = "${var.name}-orders-db" }
}
