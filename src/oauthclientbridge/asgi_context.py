from dataclasses import dataclass

from oauthclientbridge import bridge, observer
from oauthclientbridge.settings import Settings


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    oauth_bridge: bridge.Bridge
    fallback_observer: observer.FallbackObserver
    outcome_observer: observer.OAuthOutcomeObserver
