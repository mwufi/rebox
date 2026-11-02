resource "aws_ecr_repository" "this" {
  for_each             = toset(["rebox-api", "rebox-worker"])
  name                 = each.key
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 30 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}

locals {
  api_image    = "${aws_ecr_repository.this["rebox-api"].repository_url}:${var.image_tag}"
  worker_image = "${aws_ecr_repository.this["rebox-worker"].repository_url}:${var.image_tag}"
}
