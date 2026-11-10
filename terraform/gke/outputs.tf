output "cluster_name" {
  value = google_container_cluster.primary.name
}

output "get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.primary.name} --zone ${var.zone} --project ${var.project_id}"
}

output "registry" {
  description = "Push the gateway image here, e.g. docker push <registry>/gateway:v1"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
