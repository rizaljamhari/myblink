"""Attach opt-in HTTP tracing to the shared Blink aiohttp session."""

from contextlib import asynccontextmanager

from aiohttp import ClientSession

from modules.blink_handler import BlinkHandler
from modules.blink_http_debug import (
    blink_http_debug_enabled,
    create_blink_http_trace_config,
)


def apply_http_debug_tracing() -> None:
    """Patch BlinkHandler session creation to enable tracing when requested."""

    @asynccontextmanager
    async def get_session(self):
        # Reuse the existing Blink session whenever possible.
        if self._session and not self._session.closed:
            yield self._session
            return

        trace_configs = []
        if blink_http_debug_enabled():
            trace_configs.append(create_blink_http_trace_config())
            self.logger.info(
                "Blink HTTP debug tracing enabled "
                "(logger=blink.http, body limit controlled by BLINK_HTTP_DEBUG_BODY_LIMIT)"
            )

        self._session = ClientSession(trace_configs=trace_configs)
        try:
            yield self._session
        finally:
            # Keep the session alive for reuse. BlinkHandler.cleanup_session()
            # owns lifecycle cleanup on failures, replacement, and shutdown.
            pass

    BlinkHandler._get_session = get_session
