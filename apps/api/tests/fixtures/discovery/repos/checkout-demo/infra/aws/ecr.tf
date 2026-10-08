resource "aws_ecr_repository" "app" {
  for_each = local.services

  name                 = "${var.name}-${each.key}"
  image_tag_mutability = "MUTABLE" # a re-run of the same commit pushes the same SHA tag again
  force_delete         = true      # so `make destroy` works with images present

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  for_each = aws_ecr_repository.app

  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}
