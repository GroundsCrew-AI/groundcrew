# Full connection string in one secret. ECS injects it as DATABASE_URL through the task
# definition "secrets" block, never as a plain environment variable.
resource "aws_secretsmanager_secret" "database_url" {
  name        = "${var.name}/database-url"
  description = "DATABASE_URL for the checkout api and worker"

  # Delete immediately on destroy so a fresh apply can reuse the name.
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql://%s:%s@%s:%d/%s?sslmode=require",
    aws_db_instance.orders.username,
    random_password.db.result,
    aws_db_instance.orders.address,
    aws_db_instance.orders.port,
    aws_db_instance.orders.db_name,
  )
}
