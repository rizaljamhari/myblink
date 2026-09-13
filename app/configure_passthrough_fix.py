"""Normalize /configure responses before the rate-limit UI injector runs.

Flask's static-file response uses direct passthrough mode. The injected
rate-limit UI needs to read and replace the small configure.html body, which
requires disabling passthrough first.
"""

from web_server import WebServer


def apply_configure_passthrough_fix() -> None:
    """Ensure /configure HTML can be safely read by later after_request hooks."""
    original_init = WebServer.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        @self.app.after_request
        def normalize_configure_response(response):
            from flask import request

            if request.path == "/configure" and response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if "text/html" in content_type and response.direct_passthrough:
                    # Flask/Werkzeug forbids get_data() while direct passthrough
                    # is enabled. configure.html is small, so buffering it is safe.
                    response.direct_passthrough = False

            return response

    WebServer.__init__ = patched_init
