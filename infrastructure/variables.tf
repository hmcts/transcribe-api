# ---------------------------------------------------------------------------
# Injected by the CNP pipeline via -var. Do NOT set these in a .tfvars file.
# ---------------------------------------------------------------------------
variable "product" {}

variable "component" {}

variable "env" {}

variable "subscription" {}

variable "location" {
  default = "UK South"
}

variable "common_tags" {
  type = map(string)
}

variable "jenkins_AAD_objectId" {
  description = "Object ID of the Jenkins identity, granted administrative access to the server."
}

variable "aks_subscription_id" {
  description = "Subscription holding the AKS VNet the database is delegated into."
}

# ---------------------------------------------------------------------------
# Component configuration
# ---------------------------------------------------------------------------
variable "pgsql_version" {
  description = "Major version of PostgreSQL Flexible Server to run."
  type        = string
  default     = "16"
}
