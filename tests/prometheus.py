from collections.abc import Mapping

from prometheus_client.parser import text_string_to_metric_families


def sample_value(
    content: bytes,
    name: str,
    labels: Mapping[str, str] | None = None,
) -> float:
    """Return one exported sample value, or zero before its label set exists."""
    expected_labels = dict(labels or {})
    for family in text_string_to_metric_families(content.decode()):
        for sample in family.samples:
            if sample.name == name and sample.labels == expected_labels:
                return float(sample.value)
    return 0.0
