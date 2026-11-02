#!/usr/bin/env bash
# Optional: create an S3 bucket for shared Terraform state (versioned, encrypted, private),
# then print the backend.hcl to use. Run once per AWS account.
#   infra/scripts/bootstrap-state.sh [region]
set -euo pipefail
region="${1:-us-west-2}"
account="$(aws sts get-caller-identity --query Account --output text)"
bucket="rebox-tfstate-$account"

if aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
  echo "bootstrap-state: $bucket exists"
else
  if [ "$region" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$bucket" --region "$region" >/dev/null
  else
    aws s3api create-bucket --bucket "$bucket" --region "$region" \
      --create-bucket-configuration "LocationConstraint=$region" >/dev/null
  fi
  echo "bootstrap-state: created $bucket"
fi
aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"},"BucketKeyEnabled":true}]}'

cat <<HCL

# infra/terraform/backend.hcl
bucket       = "$bucket"
key          = "prod/terraform.tfstate"
region       = "$region"
encrypt      = true
use_lockfile = true
HCL
