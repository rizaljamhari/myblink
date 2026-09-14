"""Correct camera snooze semantics and scheduling.

Blink camera snooze pauses notifications while motion detection remains enabled.
MyBlink previously used ScheduleAction.SNOOZE for motion detection disable in the
scheduler even though the direct camera snooze endpoint already called Blink's
real snooze API. This patch separates the two behaviors without modifying the
pinned BlinkPy submodule.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any

from modules.blink_handler import BlinkHandler
from modules.db_models import ScheduleAction, ScheduleTarget
from modules.schedule_executor import ScheduleExecutor
from web_server import WebServer


LOGGER = logging.getLogger(__name__)


def _ensure_motion_disable_action() -> None:
    """Add ScheduleAction.MOTION_DISABLE while remaining backward compatible."""
    if hasattr(ScheduleAction, "MOTION_DISABLE"):
        return

    member = object.__new__(ScheduleAction)
    member._name_ = "MOTION_DISABLE"
    member._value_ = "motion_disable"

    ScheduleAction._member_names_.append("MOTION_DISABLE")
    ScheduleAction._member_map_["MOTION_DISABLE"] = member
    ScheduleAction._value2member_map_["motion_disable"] = member
    type.__setattr__(ScheduleAction, "MOTION_DISABLE", member)


def _patch_blink_handler() -> None:
    """Add explicit per-camera notification snooze helpers."""
    original_unsnooze_camera = BlinkHandler.unsnooze_camera

    async def set_camera_notification_snooze(
        self,
        camera_name: str,
        duration_seconds: int,
    ) -> bool:
        camera = self._find_camera_in_blink(camera_name)
        if not camera:
            self.logger.error("Camera '%s' not found for notification snooze", camera_name)
            return False

        seconds = max(0, int(duration_seconds))
        self.logger.info(
            "Setting notification snooze for camera '%s' to %s seconds",
            camera_name,
            seconds,
        )

        try:
            result = await camera.async_snooze(seconds)
            if result is None:
                self.logger.warning(
                    "Notification snooze returned no result for camera '%s'",
                    camera_name,
                )
                return False
            return True
        except Exception as error:
            self.logger.error(
                "Failed to set notification snooze for camera '%s': %s",
                camera_name,
                error,
            )
            return False

    async def unsnooze_camera(self, camera_name: str) -> bool:
        """Clear notification snooze directly, falling back to the legacy reset."""
        camera = self._find_camera_in_blink(camera_name)
        if camera:
            try:
                self.logger.info(
                    "Clearing notification snooze for camera '%s' with snooze_time=0",
                    camera_name,
                )
                result = await camera.async_snooze(0)
                if result is not None:
                    return True
            except Exception as error:
                self.logger.warning(
                    "Direct notification unsnooze failed for '%s': %s; using legacy fallback",
                    camera_name,
                    error,
                )

        return await original_unsnooze_camera(self, camera_name)

    BlinkHandler.set_camera_notification_snooze = set_camera_notification_snooze
    BlinkHandler.unsnooze_camera = unsnooze_camera


def _patch_schedule_executor() -> None:
    """Make SNOOZE mean notification snooze and add MOTION_DISABLE separately."""
    original_execute_camera_action = ScheduleExecutor._execute_camera_action
    original_revert_timer_action = ScheduleExecutor._revert_timer_action

    async def execute_camera_action(self, rule) -> None:
        camera = self.blink_handler._find_camera_in_blink(rule.target_name)
        if not camera:
            self.logger.warning(
                "Camera '%s' not found for rule %s", rule.target_name, rule.id
            )
            return

        if rule.action == ScheduleAction.SNOOZE:
            duration_hours = rule.duration_hours
            if not duration_hours or float(duration_hours) <= 0:
                duration_hours = getattr(
                    self.blink_handler.config, "default_duration_hours", 1
                ) or 1
            duration_seconds = max(60, int(float(duration_hours) * 3600))
            success = await self.blink_handler.set_camera_notification_snooze(
                rule.target_name,
                duration_seconds,
            )
            if not success:
                raise RuntimeError(
                    f"Failed to snooze notifications for camera '{rule.target_name}'"
                )
            return

        if rule.action == ScheduleAction.MOTION_DISABLE:
            success = await self.blink_handler.set_camera_motion_detect(
                rule.target_name,
                enable=False,
            )
            if not success:
                raise RuntimeError(
                    f"Failed to disable motion detection for camera '{rule.target_name}'"
                )

            if rule.duration_hours and float(rule.duration_hours) > 0:
                self.history_manager.create_duration_timer(
                    target_type=ScheduleTarget.CAMERA,
                    target_name=rule.target_name,
                    action=ScheduleAction.MOTION_DISABLE,
                    duration_hours=float(rule.duration_hours),
                )
            return

        if rule.action == ScheduleAction.ARM:
            success = await self.blink_handler.set_camera_motion_detect(
                rule.target_name,
                enable=True,
            )
            if not success:
                raise RuntimeError(
                    f"Failed to enable motion detection for camera '{rule.target_name}'"
                )

            if rule.duration_hours and float(rule.duration_hours) > 0:
                self.history_manager.create_duration_timer(
                    target_type=ScheduleTarget.CAMERA,
                    target_name=rule.target_name,
                    action=ScheduleAction.ARM,
                    duration_hours=float(rule.duration_hours),
                )
            return

        return await original_execute_camera_action(self, rule)

    async def revert_timer_action(self, timer) -> None:
        if timer.target_type == ScheduleTarget.CAMERA:
            if timer.action == ScheduleAction.SNOOZE:
                await self.blink_handler.unsnooze_camera(timer.target_name)
                return

            if timer.action == ScheduleAction.MOTION_DISABLE:
                await self.blink_handler.set_camera_motion_detect(
                    timer.target_name,
                    enable=True,
                )
                return

            if timer.action == ScheduleAction.ARM:
                await self.blink_handler.set_camera_motion_detect(
                    timer.target_name,
                    enable=False,
                )
                return

        return await original_revert_timer_action(self, timer)

    ScheduleExecutor._execute_camera_action = execute_camera_action
    ScheduleExecutor._revert_timer_action = revert_timer_action


def _find_endpoint(app, rule_path: str, method: str) -> str | None:
    for rule in app.url_map.iter_rules():
        if rule.rule == rule_path and method.upper() in rule.methods:
            return rule.endpoint
    return None


def _patch_camera_snooze_route() -> None:
    """Make the camera snooze HTTP endpoint honor the requested duration."""
    original_init = WebServer.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        endpoint = _find_endpoint(
            self.app,
            "/api/camera/<camera_name>/snooze",
            "POST",
        )
        if not endpoint:
            self.logger.warning("Camera snooze endpoint not found; semantic patch skipped")
            return

        from flask import jsonify, request

        def camera_snooze(camera_name: str):
            try:
                data = request.get_json(silent=True) or {}
                enabled = bool(data.get("enabled", False))
                duration_hours = data.get("duration_hours")

                handler = self.myblink_app.blink_handler
                if not handler or not self.myblink_app._event_loop:
                    return jsonify({"error": "Blink handler or event loop not available"}), 500

                # Remove timers created by the old implementation. Blink itself
                # owns notification-snooze expiry once this patch is active.
                if self.myblink_app.history_manager:
                    self.myblink_app.history_manager.delete_duration_timers_for_target(
                        target_type=ScheduleTarget.CAMERA,
                        target_name=camera_name,
                        action=ScheduleAction.SNOOZE,
                    )

                if enabled:
                    if duration_hours is None:
                        duration_seconds = 300  # Preserve old quick-toggle default.
                    else:
                        duration_seconds = max(60, int(float(duration_hours) * 3600))

                    future = asyncio.run_coroutine_threadsafe(
                        handler.set_camera_notification_snooze(
                            camera_name,
                            duration_seconds,
                        ),
                        self.myblink_app._event_loop,
                    )
                    success = future.result(timeout=30)
                    if not success:
                        return jsonify({"error": "Failed to snooze camera notifications"}), 400

                    return jsonify(
                        {
                            "success": True,
                            "message": "Camera notifications snoozed",
                            "notification_snoozed": True,
                            "duration_seconds": duration_seconds,
                        }
                    )

                future = asyncio.run_coroutine_threadsafe(
                    handler.unsnooze_camera(camera_name),
                    self.myblink_app._event_loop,
                )
                success = future.result(timeout=30)
                if not success:
                    return jsonify({"error": "Failed to clear camera notification snooze"}), 400

                return jsonify(
                    {
                        "success": True,
                        "message": "Camera notification snooze cleared",
                        "notification_snoozed": False,
                    }
                )

            except Exception as error:
                self.logger.error(
                    "Error updating camera notification snooze for %s: %s",
                    camera_name,
                    error,
                    exc_info=True,
                )
                return jsonify({"error": str(error)}), 500

        # Preserve the route's normal authentication behavior.
        self.app.view_functions[endpoint] = self._require_auth(camera_snooze)

    WebServer.__init__ = patched_init


def apply_notification_snooze_fix() -> None:
    """Apply notification-snooze semantics before MyBlink is instantiated."""
    _ensure_motion_disable_action()
    _patch_blink_handler()
    _patch_schedule_executor()
    _patch_camera_snooze_route()
