from collections.abc import Mapping

from oauthclientbridge.settings import TelemetrySettings


def resource_attributes(settings: TelemetrySettings) -> dict[str, str]:
    attributes = {
        "service.name": settings.service_name,
        "service.namespace": settings.service_namespace,
        "service.version": settings.service_version,
        "deployment.environment": settings.deployment_environment,
    }
    if settings.service_instance_id:
        attributes["service.instance.id"] = settings.service_instance_id
    if settings.oauth_provider:
        attributes["oauth.provider"] = settings.oauth_provider
    if settings.vcs_revision:
        attributes["vcs.revision"] = settings.vcs_revision
    return attributes


def log_attributes(attributes: Mapping[str, str]) -> dict[str, str]:
    canonical_keys = {
        "service.name",
        "service.namespace",
        "service.version",
        "deployment.environment",
        "service.instance.id",
        "oauth.provider",
        "vcs.revision",
    }
    return {key: value for key, value in attributes.items() if key in canonical_keys}


def build_info_labels(settings: TelemetrySettings) -> dict[str, str]:
    attributes = resource_attributes(settings)
    return {
        "service_name": attributes["service.name"],
        "service_namespace": attributes["service.namespace"],
        "service_instance_id": attributes.get("service.instance.id", "unknown"),
        "deployment_environment": attributes["deployment.environment"],
        "oauth_provider": attributes.get("oauth.provider", "unknown"),
        "service_version": attributes["service.version"],
        "vcs_revision": attributes.get("vcs.revision", "unknown"),
    }
