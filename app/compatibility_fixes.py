"""Runtime compatibility fixes for MyBlink and the pinned Blinkpy revision.

These fixes are intentionally kept outside the Blinkpy submodule so the
submodule can remain pinned to an upstream commit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import yaml


class BlinkRateLimitError(Exception):
    """Raised when Blink rate-limits an OAuth sign-in request."""

    def __init__(
        self,
        retry_after: str | None = None,
        retry_at: str | None = None,
        reset_source: str | None = None,
    ) -> None:
        self.retry_after = retry_after
        self.retry_at = retry_at
        self.reset_source = reset_source

        message = "Blink login rate-limited (HTTP 429)"
        if retry_at:
            message += f"; retry at {retry_at}"
            if retry_after:
                message += f" ({retry_after} seconds)"
        elif retry_after:
            message += f"; retry after {retry_after} seconds"
        else:
            message += "; Blink did not provide a reset time"
        super().__init__(message)


def _rate_limit_reset_info(headers) -> tuple[str | None, str | None, str | None]:
    """Extract a human-readable rate-limit reset time from response headers."""
    now = datetime.now().astimezone()

    retry_after = headers.get("Retry-After")
    if retry_after:
        value = retry_after.strip()
        try:
            seconds = max(0, int(float(value)))
            retry_at = now + timedelta(seconds=seconds)
            return str(seconds), retry_at.strftime("%Y-%m-%d %H:%M:%S %Z"), "Retry-After"
        except (TypeError, ValueError):
            try:
                retry_at_dt = parsedate_to_datetime(value)
                if retry_at_dt.tzinfo is None:
                    retry_at_dt = retry_at_dt.replace(tzinfo=timezone.utc)
                retry_at_local = retry_at_dt.astimezone()
                seconds = max(0, int((retry_at_local - now).total_seconds()))
                return str(seconds), retry_at_local.strftime("%Y-%m-%d %H:%M:%S %Z"), "Retry-After"
            except (TypeError, ValueError, OverflowError):
                pass

    for header_name in ("X-RateLimit-Reset", "RateLimit-Reset"):
        reset_value = headers.get(header_name)
        if not reset_value:
            continue
        try:
            reset_epoch = float(reset_value)
            retry_at_local = datetime.fromtimestamp(reset_epoch, tz=timezone.utc).astimezone()
            seconds = max(0, int((retry_at_local - now).total_seconds()))
            return str(seconds), retry_at_local.strftime("%Y-%m-%d %H:%M:%S %Z"), header_name
        except (TypeError, ValueError, OverflowError, OSError):
            continue

    return None, None, None


def _patch_blinkpy_oauth_signin() -> None:
    """Preserve Blinkpy login behavior while surfacing HTTP 429 explicitly."""
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
            retry_after, retry_at, reset_source = _rate_limit_reset_info(response.headers)
            if retry_at:
                logger.warning(
                    "Blink OAuth signin rate-limited (HTTP 429); retry at %s (%s seconds, source=%s)",
                    retry_at,
                    retry_after,
                    reset_source,
                )
            else:
                interesting_headers = {
                    key: value
                    for key, value in response.headers.items()
                    if "rate" in key.lower() or "retry" in key.lower()
                }
                if interesting_headers:
                    logger.warning(
                        "Blink OAuth signin rate-limited (HTTP 429); no parseable reset time; headers=%s",
                        interesting_headers,
                    )
                else:
                    logger.warning(
                        "Blink OAuth signin rate-limited (HTTP 429); Blink supplied no rate-limit reset headers"
                    )
            raise BlinkRateLimitError(retry_after, retry_at, reset_source)

        if not response_text:
            response_text = await response.text()

        logger.error(
            "OAuth signin failed: status=%s body=%s",
            response.status,
            response_text[:800],
        )
        return None

    api.oauth_signin = oauth_signin


def _patch_blink_handler() -> None:
    """Only enter the OTP flow after Blink explicitly requests 2FA."""
    from blinkpy.auth import Auth, BlinkTwoFARequiredError
    from blinkpy.blinkpy import Blink
    from modules.blink_handler import BlinkHandler
    from modules.config_manager import mask_email
    from modules.exceptions import AuthenticationError

    async def authenticate_with_credentials(self, session) -> None:
        self.logger.info("Performing fresh authentication")

        if self.credentials.blink.cached_credentials:
            self.credentials.blink.cached_credentials = None
            self.config.blink_cached_credentials = None
            self.config_manager.save_config()

        self.blink = Blink(session=session)
        auth_info = {
            "username": self.credentials.blink.username,
            "password": self.credentials.blink.password,
        }
        self.logger.debug(
            "Authenticating user: %s", mask_email(auth_info["username"])
        )
        self.blink.auth = Auth(auth_info, no_prompt=True, session=session)

        twofa_required = False
        try:
            result = await self.blink.start()
            self.logger.debug(
                "Authentication result: %s, available: %s",
                result,
                self.blink.available,
            )
        except BlinkTwoFARequiredError:
            twofa_required = True
            self.logger.debug("2FA required during initial authentication")
        except BlinkRateLimitError as error:
            await self.cleanup_session()
            raise AuthenticationError(str(error)) from error

        if twofa_required:
            await self._handle_2fa()
        elif not self.blink.available:
            await self.cleanup_session()
            raise AuthenticationError(
                "Blink authentication failed before a 2FA challenge was issued"
            )

        await self._verify_blink_connection()
        self._save_blink_credentials()

    BlinkHandler._authenticate_with_credentials = authenticate_with_credentials


def _patch_history_manager() -> None:
    """Add the media-history API expected by MediaManager."""
    from dateutil.parser import parse
    from modules.history_manager import HistoryManager

    def get_all_media_downloads(self):
        return self.get_media_download_history(limit=2_147_483_647)

    original_record_media_download = HistoryManager.record_media_download

    def record_media_download(
        self,
        camera_name,
        sync_name,
        media_type,
        media_id,
        file_path,
        file_size_bytes=None,
        created_at=None,
    ):
        if isinstance(created_at, str):
            try:
                created_at = parse(created_at)
            except (TypeError, ValueError, OverflowError):
                created_at = None
        return original_record_media_download(
            self,
            camera_name,
            sync_name,
            media_type,
            media_id,
            file_path,
            file_size_bytes,
            created_at,
        )

    HistoryManager.get_all_media_downloads = get_all_media_downloads
    HistoryManager.record_media_download = record_media_download


def _patch_blinkbridge_config() -> None:
    """Retain BlinkBridge's nested configuration in the AppConfig model."""
    from modules.config_manager import ConfigManager
    from modules.models import AppConfig

    def config_get(self, key: str, default: Any = None):
        return getattr(self, key, default)

    def config_getitem(self, key: str):
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    AppConfig.get = config_get
    AppConfig.__getitem__ = config_getitem

    original_load_config = ConfigManager.load_config
    original_save_config = ConfigManager.save_config

    def load_config(self):
        config = original_load_config(self)
        blinkbridge = {}
        try:
            if self._config_file.exists() and self._config_file.is_file():
                with open(self._config_file, "r") as handle:
                    raw_config = yaml.safe_load(handle) or {}
                blinkbridge = raw_config.get("blinkbridge", {}) or {}
        except Exception as error:
            self.logger.warning("Failed to load BlinkBridge config: %s", error)
        config.blinkbridge = blinkbridge
        return config

    def save_config(self) -> None:
        original_save_config(self)
        if not self.config:
            return
        try:
            if self._config_file.exists() and self._config_file.is_file():
                with open(self._config_file, "r") as handle:
                    raw_config = yaml.safe_load(handle) or {}
            else:
                raw_config = {}
            raw_config["blinkbridge"] = getattr(self.config, "blinkbridge", {}) or {}
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_file, "w") as handle:
                yaml.safe_dump(
                    raw_config,
                    handle,
                    default_flow_style=False,
                    sort_keys=False,
                )
        except Exception as error:
            self.logger.error("Failed to persist BlinkBridge config: %s", error)

    ConfigManager.load_config = load_config
    ConfigManager.save_config = save_config


def _patch_handler_replacement(myblink_module) -> None:
    """Close the previous Blink session before replacing its handler."""
    MyBlink = myblink_module.MyBlink
    original_set_credentials = MyBlink.set_credentials

    def set_credentials(self, creds_dict: dict) -> None:
        old_handler = self.blink_handler
        if old_handler is not None:
            try:
                if self._event_loop and self._event_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        old_handler.cleanup_session(), self._event_loop
                    )
                    future.result(timeout=5)
                elif getattr(old_handler, "_session", None) is not None:
                    asyncio.run(old_handler.cleanup_session())
            except Exception as error:
                self.logger.debug("Failed to close previous Blink session: %s", error)

        original_set_credentials(self, creds_dict)

    MyBlink.set_credentials = set_credentials


def apply_runtime_fixes(myblink_module) -> None:
    """Apply MyBlink compatibility fixes before the application is instantiated."""
    _patch_blinkpy_oauth_signin()
    _patch_blink_handler()
    _patch_history_manager()
    _patch_blinkbridge_config()
    _patch_handler_replacement(myblink_module)
