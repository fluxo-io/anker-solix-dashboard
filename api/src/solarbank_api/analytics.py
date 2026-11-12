"""Pure analytics labels and timestamp presentation helpers."""

import math
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SERIES_COLORS = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
]
OTHER_LABEL = "Andere"
OTHER_COLOR = "#8a8985"
MAX_BUCKETS = 400
RANGES = {
    "1": "Letzte 24 Stunden",
    "7": "Letzte 7 Tage",
    "30": "Letzte 30 Tage",
    "90": "Letzte 90 Tage",
    "all": "Gesamter Zeitraum",
}
BUCKETS = {"hour": "Stunde", "day": "Tag", "week": "Woche"}
DEFAULT_BUCKET_FOR_RANGE = {
    "1": "hour",
    "7": "day",
    "30": "day",
    "90": "week",
    "all": "week",
}


def excluded_usernames(value):
    values = value.split(",") if isinstance(value, str) else value
    return frozenset(
        str(name).strip().casefold() for name in values if str(name).strip()
    )


def get_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return UTC


def parse_timestamp(raw):
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


EVENT_LABELS = {
    "bootstrap": "Einrichtung",
    "login_success": "Anmeldung",
    "login_failed": "Anmeldung fehlgeschlagen",
    "logout": "Abmeldung",
    "user_create": "Benutzer angelegt",
    "password_reset": "Passwort zurückgesetzt",
    "password_change": "Passwort geändert",
    "page_view": "Seitenaufruf",
    "access_denied": "Zugriff abgewiesen",
    "export_csv": "CSV-Export",
}


def _floor(moment, bucket):
    if bucket == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    moment = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment - timedelta(days=moment.weekday()) if bucket == "week" else moment


def _boundaries(start, end, zone, bucket, stride=1):
    cursor = _floor(start.astimezone(zone), bucket)
    result = []
    while cursor.astimezone(UTC) <= end and len(result) <= MAX_BUCKETS:
        result.append(cursor.astimezone(UTC))
        if bucket == "hour":
            # Step on the UTC axis: autumn's repeated hour remains two buckets,
            # spring's nonexistent hour is never invented.
            cursor = (cursor.astimezone(UTC) + timedelta(hours=stride)).astimezone(zone)
        else:
            cursor += timedelta(days=stride * (7 if bucket == "week" else 1))
    return result


def _label(moment, bucket, zone):
    local = moment.astimezone(zone)
    if bucket == "hour":
        return local.strftime("%d.%m. %H:%M %Z")
    if bucket == "week":
        return local.strftime("KW %V (%d.%m.%Y)")
    return local.strftime("%d.%m.%Y")


def build(
    entries,
    timezone_name="Europe/Berlin",
    days="30",
    bucket="",
    event="",
    *,
    now=None,
    excluded_users=(),
    user="",
):
    selected_user = user.strip()
    zone = get_zone(timezone_name)
    days = days if days in RANGES else "30"
    bucket = bucket if bucket in BUCKETS else DEFAULT_BUCKET_FOR_RANGE[days]
    event = event if event in EVENT_LABELS else ""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    excluded = excluded_usernames(excluded_users)
    raw_entries, entries = entries, []
    for raw in raw_entries:
        user = str(raw.get("user") or "-")
        if user.strip().casefold() in excluded:
            continue
        # Only API-verified actors become people in usage statistics.
        if not raw.get("actor_verified"):
            user = "-"
        moment = parse_timestamp(raw.get("ts"))
        if moment is not None and moment <= now:
            entries.append((moment, user, str(raw.get("event", ""))))
    start = (
        min((entry[0] for entry in entries), default=now)
        if days == "all"
        else now - timedelta(days=int(days))
    )
    effective = bucket
    boundaries = _boundaries(start, now, zone, effective)
    while len(boundaries) > MAX_BUCKETS and effective != "week":
        effective = {"hour": "day", "day": "week"}[effective]
        boundaries = _boundaries(start, now, zone, effective)
    stride = 1
    if len(boundaries) > MAX_BUCKETS:
        stride = max(
            2,
            math.ceil((now - start).total_seconds() / (7 * 86400 * (MAX_BUCKETS - 1))),
        )
        boundaries = _boundaries(start, now, zone, effective, stride)
    ticks = [moment.timestamp() for moment in boundaries]
    users = sorted({user for _moment, user, _name in entries if user != "-"})
    colors = {
        user: SERIES_COLORS[index]
        for index, user in enumerate(users[: len(SERIES_COLORS)])
    }
    counts, breakdowns, last_seen = {}, {}, {}
    for moment, user, name in entries:
        if (
            moment < start
            or (event and event != name)
            or (selected_user and user != selected_user)
        ):
            continue
        index = bisect_right(ticks, moment.timestamp()) - 1
        if index < 0:
            continue
        counts.setdefault(user, [0] * len(ticks))[index] += 1
        breakdown = breakdowns.setdefault(user, {})
        breakdown[name] = breakdown.get(name, 0) + 1
        last_seen[user] = max(last_seen.get(user, moment), moment)
    totals = {user: sum(values) for user, values in counts.items()}
    ranked = sorted(totals, key=lambda user: (-totals[user], user))
    active_users = [user for user in ranked if user != "-"]
    series = [
        {
            "user": user,
            "label": user,
            "color": colors[user],
            "counts": counts[user],
            "total": totals[user],
        }
        for user in sorted(counts)
        if user in colors
    ]
    other = [user for user in counts if user not in colors and user != "-"]
    if other:
        values = [
            sum(counts[user][index] for user in other) for index in range(len(ticks))
        ]
        series.append(
            {
                "user": OTHER_LABEL,
                "label": f"Andere ({len(other)})",
                "color": OTHER_COLOR,
                "counts": values,
                "total": sum(values),
            }
        )
    if "-" in counts:
        series.append(
            {
                "user": "-",
                "label": "Nicht zugeordnet",
                "color": OTHER_COLOR,
                "counts": counts["-"],
                "total": totals["-"],
            }
        )
    table = [
        {
            "user": user,
            "total": totals[user],
            "color": colors.get(user, OTHER_COLOR),
            "page_view": breakdowns[user].get("page_view", 0),
            "logins": breakdowns[user].get("login_success", 0),
            "exports": breakdowns[user].get("export_csv", 0)
            + breakdowns[user].get("attachment_download", 0),
            "denied": breakdowns[user].get("access_denied", 0),
            "last_seen": last_seen[user]
            .astimezone(zone)
            .strftime("%d.%m.%Y %H:%M:%S %Z (UTC%z)"),
        }
        for user in ranked
    ]
    labels = [_label(moment, effective, zone) for moment in boundaries]
    per_bucket = [
        sum(values[index] for values in counts.values()) for index in range(len(ticks))
    ]
    peak_index = max(range(len(per_bucket)), key=per_bucket.__getitem__, default=0)
    peak = per_bucket[peak_index] if per_bucket else 0
    # Fixed viewBox coordinates keep the server-rendered SVG independent of JS.
    plot_width, plot_height = 880, 230
    slot_width = plot_width / max(1, len(ticks))
    chart_rows = []
    bars = []
    for index, label in enumerate(labels):
        cumulative = 0
        for item in series:
            value = item["counts"][index]
            if not value:
                continue
            height = value / max(1, peak) * plot_height
            bars.append(
                {
                    "x": 55 + index * slot_width + slot_width * 0.15,
                    "y": 260 - cumulative - height,
                    "width": max(0.4, slot_width * 0.7),
                    "height": height,
                    "color": item["color"],
                    "label": f"{label}: {item['label']} – {value}",
                }
            )
            cumulative += height
        if per_bucket[index]:
            chart_rows.append(
                {
                    "label": label,
                    "total": per_bucket[index],
                    "counts": [item["counts"][index] for item in series],
                }
            )
    axis_indices = sorted({0, len(labels) // 2, max(0, len(labels) - 1)})
    axis = [
        {
            "x": 55 + (index + 0.5) * slot_width,
            "label": labels[index],
            "anchor": "start"
            if index == 0
            else "end"
            if index == len(labels) - 1
            else "middle",
        }
        for index in axis_indices
        if labels
    ]
    return {
        "range": days,
        "range_label": RANGES[days],
        "bucket": effective,
        "bucket_requested": bucket,
        "bucket_coarsened": effective != bucket or stride > 1,
        "bucket_stride": stride,
        "event": event,
        "user": selected_user,
        "users": users,
        "timezone": str(zone),
        "labels": labels,
        "series": series,
        "table": table,
        "bars": bars,
        "axis": axis,
        "chart_rows": chart_rows,
        "kpi": {
            "events": sum(totals.values()),
            "users": len(active_users),
            "busiest_user": active_users[0] if active_users else "",
            "peak_value": peak,
            "peak_label": labels[peak_index] if peak else "",
        },
    }


def activity(
    entries,
    timezone_name="Europe/Berlin",
    days="30",
    user="",
    event="",
    q="",
    page=1,
    *,
    now=None,
):
    zone = get_zone(timezone_name)
    days = days if days in RANGES else "30"
    now = now or datetime.now(UTC)
    start = None if days == "all" else now - timedelta(days=int(days))
    users = sorted({row["user"] for row in entries if row["actor_verified"]})
    selected = []
    query = q.strip().casefold()
    for row in entries:
        moment = parse_timestamp(row["ts"])
        if moment is None or moment > now or (start and moment < start):
            continue
        if user and row["user"] != user:
            continue
        if event and row["event"] != event:
            continue
        if (
            query
            and query
            not in " ".join(
                str(row.get(key, "")) for key in ("user", "event", "target")
            ).casefold()
        ):
            continue
        selected.append(
            {
                **row,
                "time": moment.astimezone(zone).strftime(
                    "%d.%m.%Y %H:%M:%S %Z (UTC%z)"
                ),
                "label": EVENT_LABELS.get(row["event"], row["event"]),
            }
        )
    selected.sort(key=lambda row: (row["ts"], row["id"]), reverse=True)
    pages = max(1, math.ceil(len(selected) / 50))
    page = min(max(1, page), pages)
    return {
        "rows": selected[(page - 1) * 50 : page * 50],
        "total": len(selected),
        "page": page,
        "pages": pages,
        "users": users,
        "range": days,
        "user": user,
        "event": event,
        "q": q,
        "timezone": str(zone),
    }
