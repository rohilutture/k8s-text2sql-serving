terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

# No credentials here and none in this repo. When you choose to run this, authenticate with
# `gcloud auth application-default login` (or a service account via GOOGLE_APPLICATION_CREDENTIALS).
provider "google" {
  project = var.project_id
  region  = var.region
}
