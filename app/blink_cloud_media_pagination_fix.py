"""Add pagination and duration accounting to Blink cloud media diagnostics.

Blink's v4 media endpoint returns a bounded page of clips and accepts a numeric
``pagination_key`` query parameter. The collector follows that cursor, combines
all pages in the requested time window, and deduplicates media ids.

Blink media entries can also contain a JSON-encoded ``metadata`` object with
``pts_length_ms``. That value is summed here so capped cloud storage can be
compared with the known 7,200-second legacy allocation without assuming the
account is necessarily using that specific quota.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import blink_cloud_media_diagnostics as cloud_diag


_MAX_MEDIA_PAGES = 50
_REFERENCE_LEGACY_CAP_SECONDS = 7200.0


def _media_identity(item: Any, page_number: int, item_index: int) -> tuple:
    """Return a stable identity for deduplicating clips across pages."""
    if isinstance(item, dict):
        media_id = item.get("id")
        if media_id is not None:
            return ("id", str(media_id))
        return (
            "fallback",
            str(item.get("created_at")),
            str(item.get("camera_id")),
            str(item.get("camera_name")),
            str(item.get("media")),
        )
    return ("opaque", page_number, item_index, repr(item))


def _numeric_cursor(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _numeric_duration_ms(value: Any) -> float | None:
    """Normalize Blink's pts_length_ms value to a non-negative float."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if duration < 0:
        return None
    return duration


def _extract_pts_length_ms(item: Any) -> float | None:
    """Extract pts_length_ms from a Blink media entry's metadata field."""
    if not isinstance(item, dict):
        return None

    metadata = item.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    if not isinstance(metadata, dict):
        return None

    return _numeric_duration_ms(metadata.get("pts_length_ms"))


def _build_duration_summary(media: list[Any]) -> dict[str, Any]:
    """Summarize clip duration metadata without exposing the raw metadata blob."""
    durations_ms: list[float] = []
    by_camera_ms: dict[str, float] = defaultdict(float)
    by_camera_clips: dict[str, int] = defaultdict(int)
    video_count = 0

    for item in media:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "video":
            video_count += 1

        duration_ms = _extract_pts_length_ms(item)
        if duration_ms is None:
            continue

        durations_ms.append(duration_ms)
        camera_name = (
            item.get("device_name")
            or item.get("camera_name")
            or "Unknown"
        )
        camera_key = str(camera_name).strip() or "Unknown"
        by_camera_ms[camera_key] += duration_ms
        by_camera_clips[camera_key] += 1

    media_count = len(media)
    measured_count = len(durations_ms)
    total_ms = sum(durations_ms)
    total_seconds = total_ms / 1000.0
    difference_seconds = total_seconds - _REFERENCE_LEGACY_CAP_SECONDS
    reference_utilization = (
        total_seconds / _REFERENCE_LEGACY_CAP_SECONDS * 100.0
        if _REFERENCE_LEGACY_CAP_SECONDS
        else None
    )

    by_camera = {
        camera: {
            "clips_with_duration": by_camera_clips[camera],
            "total_seconds": round(duration_ms / 1000.0, 3),
        }
        for camera, duration_ms in sorted(by_camera_ms.items())
    }

    return {
        "media_records": media_count,
        "video_records": video_count,
        "clips_with_duration": measured_count,
        "clips_without_duration": media_count - measured_count,
        "coverage_percent": round(measured_count / media_count * 100.0, 2)
        if media_count
        else 0.0,
        "total_duration_ms": round(total_ms, 3),
        "total_duration_seconds": round(total_seconds, 3),
        "total_duration_minutes": round(total_seconds / 60.0, 3),
        "average_duration_seconds": round(
            total_seconds / measured_count, 3
        )
        if measured_count
        else None,
        "minimum_duration_seconds": round(min(durations_ms) / 1000.0, 3)
        if durations_ms
        else None,
        "maximum_duration_seconds": round(max(durations_ms) / 1000.0, 3)
        if durations_ms
        else None,
        "by_camera": by_camera,
        "reference_legacy_cap_seconds": _REFERENCE_LEGACY_CAP_SECONDS,
        "difference_from_7200_seconds": round(difference_seconds, 3),
        "reference_7200_utilization_percent": round(reference_utilization, 2)
        if reference_utilization is not None
        else None,
        "reference_note": (
            "7,200 seconds is shown only as a comparison target for the known "
            "legacy Blink cloud allocation; this diagnostic does not assume "
            "that it is the active quota for this account."
        ),
    }


def _next_pagination_key(
    payload: dict[str, Any], media: list[Any]
) -> tuple[int | None, str | None]:
    """Resolve Blink's next media cursor, preferring any explicit server field."""
    for key in (
        "pagination_key",
        "next_pagination_key",
        "next_key",
        "next_cursor",
    ):
        cursor = _numeric_cursor(payload.get(key))
        if cursor is not None:
            return cursor, f"response.{key}"

    # The Android API accepts a Long pagination_key but response variants do
    # not always expose a continuation field. Media ids are numeric and ordered,
    # so the final item id is the practical fallback continuation cursor.
    for item in reversed(media):
        if isinstance(item, dict):
            cursor = _numeric_cursor(item.get("id"))
            if cursor is not None:
                return cursor, "last_media.id"

    return None, None


async def _fetch_all_media_pages(
    auth,
    *,
    root: str,
    account_id: int,
    start_text: str,
    end_text: str,
) -> tuple[dict[str, Any] | None, str | None]:
    all_media: list[Any] = []
    seen_media: set[tuple] = set()
    seen_cursors: set[int] = set()
    page_summaries: list[dict[str, Any]] = []

    pagination_key: int | None = None
    first_metadata: dict[str, Any] = {}
    last_metadata: dict[str, Any] = {}
    stop_reason = "unknown"
    page_limit_reached = False

    for page_number in range(1, _MAX_MEDIA_PAGES + 1):
        params: dict[str, Any] = {
            "start_time": start_text,
            "end_time": end_text,
        }
        if pagination_key is not None:
            params["pagination_key"] = pagination_key

        media_url = (
            f"{root}/api/v4/accounts/{account_id}/media?{urlencode(params)}"
        )
        payload, error = await cloud_diag._query_json(
            auth,
            url=media_url,
            method="post",
            body={},
        )
        if error:
            return None, f"page {page_number}: {error}"
        if not isinstance(payload, dict):
            return None, (
                f"page {page_number}: unexpected media response type "
                f"{type(payload).__name__}"
            )

        raw_media = payload.get("media")
        page_media = raw_media if isinstance(raw_media, list) else []
        metadata = {key: value for key, value in payload.items() if key != "media"}
        if page_number == 1:
            first_metadata = dict(metadata)
        last_metadata = dict(metadata)

        added_count = 0
        for item_index, item in enumerate(page_media):
            identity = _media_identity(item, page_number, item_index)
            if identity in seen_media:
                continue
            seen_media.add(identity)
            all_media.append(item)
            added_count += 1

        # Current Blink responses use page_size=200. Keep support for older
        # response variants that used limit.
        server_limit = _numeric_cursor(payload.get("page_size"))
        if server_limit is None:
            server_limit = _numeric_cursor(payload.get("limit"))

        next_key, cursor_source = _next_pagination_key(payload, page_media)

        page_summaries.append(
            {
                "page": page_number,
                "pagination_key_used": pagination_key,
                "returned_count": len(page_media),
                "new_unique_count": added_count,
                "server_limit": server_limit,
                "purge_id": payload.get("purge_id"),
                "refresh_count": payload.get("refresh_count"),
                "next_pagination_key": next_key,
                "cursor_source": cursor_source,
            }
        )

        if not page_media:
            stop_reason = "empty_page"
            break

        if server_limit and len(page_media) < server_limit:
            stop_reason = "short_page"
            break

        if added_count == 0:
            stop_reason = "no_new_media"
            break

        if next_key is None:
            stop_reason = "no_pagination_key"
            break

        if next_key == pagination_key or next_key in seen_cursors:
            stop_reason = "repeated_pagination_key"
            break

        seen_cursors.add(next_key)
        pagination_key = next_key
    else:
        page_limit_reached = True
        stop_reason = "page_safety_cap"

    combined: dict[str, Any] = dict(first_metadata)
    combined.update(last_metadata)
    combined["media"] = all_media
    combined["duration_summary"] = _build_duration_summary(all_media)
    combined["pagination"] = {
        "pages_fetched": len(page_summaries),
        "unique_media_count": len(all_media),
        "page_safety_cap": _MAX_MEDIA_PAGES,
        "page_limit_reached": page_limit_reached,
        "stop_reason": stop_reason,
        "last_pagination_key": pagination_key,
        "pages": page_summaries,
    }
    return combined, None


async def _collect_paginated_cloud_media_diagnostics(
    blink, hours: int
) -> dict[str, Any]:
    auth = getattr(blink, "auth", None)
    urls = getattr(blink, "urls", None)
    base_url = getattr(urls, "base_url", None)
    account_id = getattr(blink, "account_id", None)

    if auth is None or not base_url or account_id is None:
        raise RuntimeError("Blink authentication session is unavailable")

    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(hours=hours)
    start_text = start_at.isoformat().replace("+00:00", "Z")
    end_text = end_at.isoformat().replace("+00:00", "Z")

    root = base_url.rstrip("/")
    media_settings_url = f"{root}/api/v4/accounts/{account_id}/media_settings"
    unwatched_url = f"{root}/api/v4/accounts/{account_id}/unwatched_media"

    settings, settings_error = await cloud_diag._query_json(
        auth,
        url=media_settings_url,
        method="get",
    )
    unwatched, unwatched_error = await cloud_diag._query_json(
        auth,
        url=unwatched_url,
        method="get",
    )
    media, media_error = await _fetch_all_media_pages(
        auth,
        root=root,
        account_id=account_id,
        start_text=start_text,
        end_text=end_text,
    )

    errors: dict[str, str] = {}
    if settings_error:
        errors["media_settings"] = settings_error
    if unwatched_error:
        errors["unwatched_media"] = unwatched_error
    if media_error:
        errors["media_list"] = media_error

    settings_safe = cloud_diag._sanitize(settings)
    unwatched_safe = cloud_diag._sanitize(unwatched)
    media_summary = cloud_diag._summarize_media_response(media)

    # duration_summary is intentionally duplicated at the top media_list level
    # for easy consumption, while remaining in response_metadata for backwards
    # compatibility with the existing Settings UI.
    if isinstance(media, dict) and isinstance(media.get("duration_summary"), dict):
        media_summary["duration_summary"] = cloud_diag._sanitize(
            media["duration_summary"]
        )

    homescreen = getattr(blink, "homescreen", {}) or {}
    video_stats = (
        cloud_diag._sanitize(homescreen.get("video_stats", {}))
        if isinstance(homescreen, dict)
        else {}
    )

    return {
        "success": True,
        "read_only": True,
        "account_id": account_id,
        "region_id": getattr(auth, "region_id", None),
        "window": {
            "hours": hours,
            "start_at": start_text,
            "end_at": end_text,
        },
        "homescreen_video_stats": video_stats,
        "media_settings": settings_safe,
        "media_settings_signals": cloud_diag._collect_signals(
            settings_safe,
            "media_settings",
        ),
        "unwatched_media": unwatched_safe,
        "media_list": media_summary,
        "errors": errors,
        "notes": [
            "The media-list POST is a read-only retrieval call in Blink's current API.",
            "Media diagnostics paginate until Blink returns a partial/empty page, no progress is made, or the 50-page safety cap is reached.",
            "Current page_size responses are honored, avoiding an unnecessary extra request after a partial final page.",
            "When Blink does not return an explicit continuation field, the final media id from the current page is used as pagination_key.",
            "Media ids are deduplicated across pages so a repeated cursor cannot inflate counts.",
            "Clip duration is read from metadata.pts_length_ms when present; raw metadata is not exposed in the diagnostic response.",
            "The 7,200-second comparison is diagnostic only and does not assert that this account is using the legacy quota.",
            "A deleted_count above zero means Blink explicitly returned clip records marked deleted in this diagnostic window.",
        ],
    }


def apply_blink_cloud_media_pagination_fix() -> None:
    """Replace the cloud-media collector with the paginated implementation."""
    cloud_diag._collect_cloud_media_diagnostics = (
        _collect_paginated_cloud_media_diagnostics
    )
