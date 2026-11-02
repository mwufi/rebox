terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Local state by default. For shared S3 state:
  #   terraform init -backend-config=backend.hcl   (copy backend.hcl.example; uncomment below)
  # backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { app = "rebox", env = var.env, managed_by = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" { state = "available" }

locals {
  name       = "rebox-${var.env}"
  account_id = data.aws_caller_identity.current.account_id
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
}
