import os
from collections.abc import Mapping
from threading import current_thread, get_ident
from typing import TypedDict

from opentelemetry import trace

from oauthclientbridge.settings import TelemetrySettings


def resource_attributes(settings: TelemetrySettings) -> dict[str, str | int]:
    attributes = {
        "service.name": settings.service_name,
        "service.namespace": settings.service_namespace,
        "service.version": settings.service_version,
        "deployment.environment": settings.deployment_environment,
        "process.pid": os.getpid(),
    }
    if settings.service_instance_id:
        attributes["service.instance.id"] = settings.service_instance_id
    if settings.oauth_provider:
        attributes["oauth.provider"] = settings.oauth_provider
    if settings.vcs_revision:
        attributes["vcs.revision"] = settings.vcs_revision
    return attributes


def runtime_log_attributes() -> dict[str, str | int]:
    thread = current_thread()
    return {
        "process.pid": os.getpid(),
        "process.thread.id": get_ident(),
        "process.thread.name": thread.name,
    }


def log_attributes(attributes: Mapping[str, str | int]) -> dict[str, str | int]:
    canonical_keys = {
        "service.name",
        "service.namespace",
        "service.version",
        "deployment.environment",
        "service.instance.id",
        "oauth.provider",
        "vcs.revision",
        "process.pid",
    }
    return {key: value for key, value in attributes.items() if key in canonical_keys}


def otel_log_attributes(span: trace.Span | None = None) -> dict[str, str | int | bool]:
    attributes: dict[str, str | int | bool] = runtime_log_attributes()
    current_span = span or trace.get_current_span()
    if not current_span.is_recording():
        return attributes

    context = current_span.get_span_context()
    if not context.is_valid:
        return attributes

    attributes["trace_id"] = format(context.trace_id, "032x")
    attributes["span_id"] = format(context.span_id, "016x")
    attributes["trace_sampled"] = context.trace_flags.sampled

    resource = getattr(trace.get_tracer_provider(), "resource", None)
    if resource is not None:
        attributes.update(log_attributes(resource.attributes))
    return attributes


class BuildInfoLabels(TypedDict):
    service_name: str
    service_namespace: str
    service_instance_id: str
    deployment_environment: str
    oauth_provider: str
    service_version: str
    vcs_revision: str


def build_info_labels(settings: TelemetrySettings) -> BuildInfoLabels:
    return {
        "service_name": settings.service_name,
        "service_namespace": settings.service_namespace,
        "service_instance_id": settings.service_instance_id or "unknown",
        "deployment_environment": settings.deployment_environment,
        "oauth_provider": settings.oauth_provider or "unknown",
        "service_version": settings.service_version,
        "vcs_revision": settings.vcs_revision or "unknown",
    }
