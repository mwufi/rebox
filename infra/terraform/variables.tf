variable "env" {
  description = "Environment name, used in resource names."
  type        = string
  default     = "prod"
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "image_tag" {
  description = "Image tag (git sha) for every task definition. deploy.sh sets it."
  type        = string
  default     = "latest"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "enable_nat_gateway" {
  description = "One NAT gateway for private subnets. If false, tasks run in public subnets with public IPs (still no inbound access)."
  type        = bool
  default     = true
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "acm_certificate_arn" {
  description = "If set, adds an HTTPS listener and redirects HTTP to HTTPS."
  type        = string
  default     = ""
}

variable "api_desired_count" {
  type    = number
  default = 1
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "schedule_expression" {
  type    = string
  default = "rate(15 minutes)"
}

variable "optional_secrets" {
  description = "Optional worker secrets to inject. Set each secret's value out of band first (see RUNBOOK)."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for s in var.optional_secrets : contains(["GITHUB_TOKEN", "X_BEARER_TOKEN"], s)])
    error_message = "optional_secrets may contain GITHUB_TOKEN and X_BEARER_TOKEN."
  }
}

variable "user_agent" {
  type    = string
  default = "rebox-bot/0.1 (+https://github.com/rebox)"
}

variable "log_retention_days" {
  type    = number
  default = 30
}
