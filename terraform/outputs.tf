output "app_name" {
  value = juju_application.admission_webhook.name
}

output "provides" {
  value = {
    pod_defaults     = "pod-defaults",
    provide_cmr_mesh = "provide-cmr-mesh"
  }
}

output "requires" {
  value = {
    logging          = "logging",
    require_cmr_mesh = "require-cmr-mesh",
    service_mesh     = "service-mesh"
  }
}
