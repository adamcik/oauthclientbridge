import httplib
import os
import re
import time

import pyprometheus
import pyprometheus.contrib.uwsgi_features
import pyprometheus.registry

from flask import request, Response

from pyprometheus.utils.exposition import registry_to_text

if 'PROMETHEUS_UWSGI_SHAREDAREA' in os.environ:
    storage = pyprometheus.contrib.uwsgi_features.UWSGIStorage()
else:
    storage = pyprometheus.LocalMemoryStorage()

registry = pyprometheus.registry.BaseRegistry(storage=storage)


TIME_BUCKETS = (0.001, 0.003, 0.005, 0.010, 0.020, 0.030, 0.050, 0.075, 0.100,
                0.250, 0.500, 0.750, 1.0, 2.5, 5, 7.5, 10, 15, float('inf'))

BYTE_BUCKETS = (0, 8, 16, 64, 256, 512, 1024, 2048, 4096, float('inf'))

RETRY_BUCKETS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, float('inf'))

# Rest of these get populated lazily with http_%d as fallback.
HTTP_STATUS = {429: 'http_too_many_requests'}

DBErrorCounter = pyprometheus.Counter(
    'oauth_database_error_total', 'Database errors.',
    ['query', 'error'], registry=registry)

DBLatencyHistorgram = pyprometheus.Histogram(
    'oauth_database_latency_seconds', 'Database query latency.',
    ['query'], buckets=[v / 100.0 for v in TIME_BUCKETS], registry=registry)

TokenGauge = pyprometheus.Gauge(
    'oauth_tokens_count', 'Number of tokens.', ['state'], registry=registry)

ServerErrorCounter = pyprometheus.Counter(
    'oauth_server_error_total', 'OAuth errors returned to users.',
    ['method', 'endpoint', 'status', 'error'], registry=registry)

ServerLatencyHistogram = pyprometheus.Histogram(
    'oauth_server_latency_seconds', 'Overall request latency.',
    ['method', 'endpoint', 'status'], buckets=TIME_BUCKETS, registry=registry)

ServerResponseSizeHistogram = pyprometheus.Histogram(
    'oauth_server_response_bytes', 'Overall response size.',
    ['method', 'endpoint', 'status'], buckets=BYTE_BUCKETS, registry=registry)

ClientErrorCounter = pyprometheus.Counter(
    'oauth_client_error_total', 'OAuth errors from upstream provider.',
    ['method', 'endpoint', 'status', 'error'], registry=registry)

ClientRetryHistogram = pyprometheus.Histogram(
    'oauth_client_retries', 'OAuth fetch retries.',
    ['method', 'endpoint', 'status'], buckets=RETRY_BUCKETS, registry=registry)

ClientLatencyHistogram = pyprometheus.Histogram(
    'oauth_client_latency_seconds', 'Overall request latency.',
    ['method', 'endpoint', 'status'], buckets=TIME_BUCKETS, registry=registry)

ClientResponseSizeHistogram = pyprometheus.Histogram(
    'oauth_client_response_bytes', 'Overall response size.',
    ['method', 'endpoint', 'status'], buckets=BYTE_BUCKETS, registry=registry)


def status(code):
    if code not in HTTP_STATUS:
        text = httplib.responses.get(code, str(code)).lower()
        HTTP_STATUS[code] = 'http_%s' % re.sub(r'[ -]', '_', text)
    return HTTP_STATUS[code]


def endpoint():
    return getattr(request.url_rule, 'endpoint', 'notfound')


def before_request():
    request._stats_latency_start_time = time.time()


def after_request(response):
    request_latency = time.time() - request._stats_latency_start_time
    content_length = response.content_length

    labels = {'method': request.method, 'endpoint': endpoint(),
              'status': status(response.status_code)}

    ServerLatencyHistogram.labels(**labels).observe(request_latency)
    if content_length >= 0:
        ServerResponseSizeHistogram.labels(**labels).observe(content_length)
    return response


def export_metrics():
    return Response(registry_to_text(registry), mimetype="text/plain")
