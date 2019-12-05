import os
import re
import time

import pyprometheus
import pyprometheus.contrib.uwsgi_features
import pyprometheus.registry
import pyprometheus.utils.exposition
from flask import Response, request

from oauthclientbridge import compat

if 'PROMETHEUS_UWSGI_SHAREDAREA' in os.environ:
    storage = pyprometheus.contrib.uwsgi_features.UWSGIStorage()
else:
    storage = pyprometheus.LocalMemoryStorage()

registry = pyprometheus.registry.BaseRegistry(storage=storage)


TIME_BUCKETS = (
    0.0001,
    0.00055,
    0.001,
    0.0028,
    0.0046,
    0.0064,
    0.0082,
    0.01,
    0.028,
    0.046,
    0.064,
    0.082,
    0.1,
    0.4,
    0.7,
    1.0,
    4.0,
    7.0,
    10.0,
    float('inf'),
)

BYTE_BUCKETS = (
    8,
    22,
    36,
    50,
    64,
    176,
    288,
    400,
    512,
    1408,
    2304,
    3200,
    4096,
    11264,
    18432,
    25600,
    32768,
    90112,
    147456,
    204800,
    float('inf'),
)

RETRY_BUCKETS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, float('inf'))

# Rest of these get populated lazily with http_%d as fallback.
HTTP_STATUS = {429: 'http_too_many_requests'}

DBErrorCounter = pyprometheus.Counter(
    'oauth_database_error_total',
    'Database errors.',
    ['query', 'error'],
    registry=registry,
)

DBLatencyHistorgram = pyprometheus.Histogram(
    'oauth_database_latency_seconds',
    'Database query latency.',
    ['query'],
    buckets=TIME_BUCKETS,
    registry=registry,
)

ServerErrorCounter = pyprometheus.Counter(
    'oauth_server_error_total',
    'OAuth errors returned to users.',
    ['endpoint', 'status', 'error'],
    registry=registry,
)

ServerLatencyHistogram = pyprometheus.Histogram(
    'oauth_server_latency_seconds',
    'Overall request latency.',
    ['endpoint', 'status'],
    buckets=TIME_BUCKETS,
    registry=registry,
)

ServerRequestSizeHistogram = pyprometheus.Histogram(
    'oauth_server_request_bytes',
    'Overall request size.',
    ['endpoint', 'status'],
    buckets=BYTE_BUCKETS,
    registry=registry,
)

ServerResponseSizeHistogram = pyprometheus.Histogram(
    'oauth_server_response_bytes',
    'Overall response size.',
    ['endpoint', 'status'],
    buckets=BYTE_BUCKETS,
    registry=registry,
)

ClientErrorCounter = pyprometheus.Counter(
    'oauth_client_error_total',
    'OAuth errors from upstream provider.',
    ['endpoint', 'status', 'error'],
    registry=registry,
)

ClientRetryHistogram = pyprometheus.Histogram(
    'oauth_client_retries',
    'OAuth fetch retries.',
    ['endpoint', 'status'],
    buckets=RETRY_BUCKETS,
    registry=registry,
)

ClientLatencyHistogram = pyprometheus.Histogram(
    'oauth_client_latency_seconds',
    'Overall request latency.',
    ['endpoint', 'status'],
    buckets=TIME_BUCKETS,
    registry=registry,
)

ClientResponseSizeHistogram = pyprometheus.Histogram(
    'oauth_client_response_bytes',
    'Overall response size.',
    ['endpoint', 'status'],
    buckets=BYTE_BUCKETS,
    registry=registry,
)


def status(code):
    if code not in HTTP_STATUS:
        text = compat.responses.get(code, str(code)).lower()
        HTTP_STATUS[code] = 'http_%s' % re.sub(r'[ -]', '_', text)
    return HTTP_STATUS[code]


def endpoint():
    return getattr(request.url_rule, 'endpoint', 'notfound')


def before_request():
    request._stats_latency_start_time = time.time()


def after_request(response):
    request_latency = time.time() - request._stats_latency_start_time
    labels = {'endpoint': endpoint(), 'status': status(response.status_code)}

    ServerLatencyHistogram.labels(**labels).observe(request_latency)
    if response.content_length is not None:
        ServerResponseSizeHistogram.labels(**labels).observe(
            response.content_length
        )
    if request.content_length is not None:
        ServerRequestSizeHistogram.labels(**labels).observe(
            request.content_length
        )
    return response


def export_metrics():
    text = pyprometheus.utils.exposition.registry_to_text(registry)
    return Response(text, mimetype='text/plain')
