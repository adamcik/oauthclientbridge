from typing import override

from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport


class FakeTransport(Transport):
    envelopes: list[Envelope]

    def __init__(self):
        self.envelopes = []
        super().__init__()

    @override
    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)
