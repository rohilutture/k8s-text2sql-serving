variable "project_id" {
  description = "GCP project to create everything in."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  description = "A zonal cluster is cheaper than a regional one (one control plane, no cross-zone HA) and is fine for a demo."
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  type    = string
  default = "sql-serving"
}

variable "cpu_machine_type" {
  type    = string
  default = "e2-standard-4"
}

variable "gpu_machine_type" {
  description = "g2-standard-* machines carry NVIDIA L4 GPUs."
  type        = string
  default     = "g2-standard-4"
}

variable "gpu_type" {
  type    = string
  default = "nvidia-l4"
}

variable "max_cpu_nodes" {
  type    = number
  default = 3
}

variable "max_gpu_nodes" {
  description = "Upper bound on GPU nodes (and therefore on GPU spend)."
  type        = number
  default     = 1
}

variable "use_spot" {
  description = "Spot VMs are far cheaper but can be reclaimed at any time."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Left false so `terraform destroy` works for a short-lived demo cluster."
  type        = bool
  default     = false
}

variable "billing_account" {
  description = "Billing account ID. If set, a budget with email alerts at 50/90/100 percent is created."
  type        = string
  default     = ""
}

variable "budget_usd" {
  type    = number
  default = 50
}
