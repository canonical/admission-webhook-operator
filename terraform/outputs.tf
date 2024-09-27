output "app_name" {
  value = juju_application.admission_webhook.name
}

output "provides" {
  value = {
    pod_defaults = "pod-defaults",
  }
}

output "requires" {
  value = {
    "logging" = "logging",
  }
}
