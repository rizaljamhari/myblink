"""Guards that prevent scheduled Blink work before authentication succeeds."""

from __future__ import annotations

from blinkpy.auth import BlinkTwoFARequiredError

from modules.blink_handler import BlinkHandler
from modules.exceptions import AuthenticationError, TwoFactorAuthenticationError
from modules.schedule_executor import ScheduleExecutor


def _blink_authenticated(handler: BlinkHandler) -> bool:
    """Return True only after Blink has completed authenticated setup."""
    blink = getattr(handler, "blink", None)
    if blink is None:
        return False

    # Blink.urls and account_id are populated only after authentication/setup.
    return (
        getattr(blink, "urls", None) is not None
        and getattr(blink, "account_id", None) is not None
    )


def _patch_maintenance_scheduler() -> None:
    """Skip the five-minute maintenance cycle while Blink is unauthenticated."""
    original_run_scheduled_jobs = BlinkHandler.run_scheduled_jobs

    async def run_scheduled_jobs(self) -> None:
        if not _blink_authenticated(self):
            self.logger.warning(
                "Skipping scheduled jobs: Blink is not authenticated; "
                "no API calls or reauthentication will be attempted"
            )
            return

        await original_run_scheduled_jobs(self)

    BlinkHandler.run_scheduled_jobs = run_scheduled_jobs


def _patch_operation_retry() -> None:
    """Do not turn scheduled-operation failures into repeated login attempts."""

    async def execute_with_retry(
        self,
        operation,
        operation_name: str,
        max_retries=None,
    ):
        if not _blink_authenticated(self):
            raise AuthenticationError(
                f"{operation_name} skipped: Blink is not authenticated"
            )

        if max_retries is None:
            max_retries = self.config.blink_retry_limit

        last_exception = None

        for attempt in range(1, max_retries + 1):
            try:
                self.logger.debug(
                    "%s: attempt %s/%s", operation_name, attempt, max_retries
                )
                return await operation()

            except BlinkTwoFARequiredError as error:
                raise AuthenticationError(
                    f"{operation_name} paused: Blink requires 2FA"
                ) from error

            except (AuthenticationError, TwoFactorAuthenticationError):
                # Authentication failures are not operation-level transient errors.
                # Retrying here would create another OAuth login attempt.
                raise

            except Exception as error:
                last_exception = error
                self.logger.warning(
                    "%s failed (attempt %s/%s): %s",
                    operation_name,
                    attempt,
                    max_retries,
                    error,
                )

            if attempt >= max_retries:
                break

            # Only try to repair an established session. If authentication has
            # already been lost, stop instead of starting a fresh OAuth storm.
            if not _blink_authenticated(self):
                raise AuthenticationError(
                    f"{operation_name} aborted: Blink authentication is no longer available"
                ) from last_exception

            self.logger.info("Reinitializing Blink before retry %s", attempt + 1)
            try:
                await self.reinitialize()
            except (
                AuthenticationError,
                TwoFactorAuthenticationError,
                BlinkTwoFARequiredError,
            ) as error:
                raise AuthenticationError(
                    f"{operation_name} aborted: Blink reauthentication failed: {error}"
                ) from error

            if not _blink_authenticated(self):
                raise AuthenticationError(
                    f"{operation_name} aborted: Blink reauthentication did not complete"
                )

        error_msg = f"{operation_name} failed after {max_retries} attempts"
        self.logger.error(error_msg)
        raise Exception(error_msg) from last_exception

    BlinkHandler._execute_with_retry = execute_with_retry


def _patch_database_schedule_executor() -> None:
    """Pause DB-backed scheduled rules/timers while Blink is unauthenticated."""
    original_check_rules = ScheduleExecutor._check_and_execute_rules
    original_check_timers = ScheduleExecutor._check_and_execute_timers

    def should_skip(self) -> bool:
        if _blink_authenticated(self.blink_handler):
            self._blink_auth_skip_logged = False
            return False

        if not getattr(self, "_blink_auth_skip_logged", False):
            self.logger.info(
                "Schedule executor paused: Blink is not authenticated"
            )
            self._blink_auth_skip_logged = True
        return True

    async def check_and_execute_rules(self) -> None:
        if should_skip(self):
            return
        await original_check_rules(self)

    async def check_and_execute_timers(self) -> None:
        if should_skip(self):
            return
        await original_check_timers(self)

    ScheduleExecutor._check_and_execute_rules = check_and_execute_rules
    ScheduleExecutor._check_and_execute_timers = check_and_execute_timers


def apply_scheduling_guards() -> None:
    """Apply all authentication-aware scheduling guards."""
    _patch_maintenance_scheduler()
    _patch_operation_retry()
    _patch_database_schedule_executor()
