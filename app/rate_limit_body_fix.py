"""Blink OAuth 429 handling using Blink's JSON response body.

Blink may return next_time_in_secs in the JSON body for 2FA rate limits
without sending Retry-After or other standard rate-limit headers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from compatibility_fixes import BlinkRateLimitError, _rate_limit_reset_info


def apply_rate_limit_body_fix() -> None:
    """Patch Blinkpy OAuth signin to honor body.next_time_in_secs on HTTP 429."""
    from blinkpy import api
    from blinkpy.helpers.constants import OAUTH_SIGNIN_URL, OAUTH_USER_AGENT

    logger = logging.getLogger("blinkpy.api")

    async def oauth_signin(auth, email, password, csrf_token):
        headers = {
            "User-Agent": OAUTH_USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://api.oauth.blink.com",
            "Referer": OAUTH_SIGNIN_URL,
        }
        data = {
            "username": email,
            "password": password,
            "csrf-token": csrf_token,
        }

        response = await auth.session.post(
            OAUTH_SIGNIN_URL,
            headers=headers,
            data=data,
            allow_redirects=False,
        )

        response_text = ""

        if response.status == 412:
            return "2FA_REQUIRED"

        if response.status == 202:
            response_text = await response.text()
            try:
                response_json = json.loads(response_text)
            except json.JSONDecodeError:
                response_json = {}

            if (
                response_json.get("tsv_state")
                or response_json.get("tsv_methods")
                or response_json.get("next_time_in_secs")
            ):
                return "2FA_REQUIRED"

        elif response.status in [301, 302, 303, 307, 308]:
            return "SUCCESS"

        if response.status == 429:
            # Blink currently returns the useful reset window in the JSON body,
            # e.g. {"error_cause":"2fa_rate_limit_exceeded", "next_time_in_secs":86400}.
            response_text = await response.text()
            try:
                response_json = json.loads(response_text) if response_text else {}
            except json.JSONDecodeError:
                response_json = {}

            retry_after = None
            retry_at = None
            reset_source = None

            body_next_time = response_json.get("next_time_in_secs")
            if body_next_time is not None:
                try:
                    seconds = max(0, int(float(body_next_time)))
                    retry_after = str(seconds)
                    retry_at_dt = datetime.now().astimezone() + timedelta(seconds=seconds)
                    retry_at = retry_at_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                    reset_source = "body.next_time_in_secs"
                except (TypeError, ValueError, OverflowError):
                    pass

            # Fall back to standard/common rate-limit headers if Blink does not
            # provide a usable next_time_in_secs value.
            if retry_at is None:
                retry_after, retry_at, reset_source = _rate_limit_reset_info(
                    response.headers
                )

            error_cause = response_json.get("error_cause")
            error_description = response_json.get("error_description")

            if retry_at:
                logger.warning(
                    "Blink OAuth signin rate-limited (HTTP 429); cause=%s; retry at %s (%s seconds, source=%s)%s",
                    error_cause or "unknown",
                    retry_at,
                    retry_after,
                    reset_source,
                    f"; {error_description}" if error_description else "",
                )
            else:
                logger.warning(
                    "Blink OAuth signin rate-limited (HTTP 429); cause=%s; Blink supplied no usable reset time%s",
                    error_cause or "unknown",
                    f"; {error_description}" if error_description else "",
                )

            error = BlinkRateLimitError(retry_after, retry_at, reset_source)
            if error_cause:
                error.args = (f"{error.args[0]}; cause={error_cause}",)
            raise error

        if not response_text:
            response_text = await response.text()

        logger.error(
            "OAuth signin failed: status=%s body=%s",
            response.status,
            response_text[:800],
        )
        return None

    api.oauth_signin = oauth_signin
