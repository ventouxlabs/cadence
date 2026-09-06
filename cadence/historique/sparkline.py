"""The 30-day sparkline, built as an SVG string on the server.

No chart library and no new dependency (PRP-04 risk 8): two ``<polyline>`` elements in a 320x64
viewBox is the whole thing. Colours come from CSS custom properties applied through the two
classes, because ``var()`` does not resolve inside an SVG presentation attribute — the stylesheet
owns the palette either way.

Each series is normalised to its **own** min and max with a 5 % vertical pad, because kilograms and
percent share no scale. They share an x-axis of **dates**, not indices, so a gap in the data leaves
a gap in the line instead of compressing it.
"""

from __future__ import annotations

from datetime import date

from cadence.historique.trend import MIN_POINTS, Point, TrendSeries

WIDTH = 320
HEIGHT = 64
# 5 % of the height at each end, so a peak is not clipped by the stroke width.
PAD = HEIGHT * 0.05


def _extent(points: list[Point]) -> tuple[float, float]:
    values = [value for _, value in points]
    return min(values), max(values)


def _y(value: float, low: float, high: float) -> float:
    """Value to screen y, inverted. A flat series draws down the middle rather than dividing by zero."""
    if high <= low:
        return HEIGHT / 2
    span = high - low
    return HEIGHT - PAD - ((value - low) / span) * (HEIGHT - 2 * PAD)


def _x(day: date, first: date, last: date, index: int, count: int) -> float:
    """Date to screen x. Points that all share one date fall back to even spacing by index."""
    if last > first:
        return ((day - first).days / (last - first).days) * WIDTH
    if count < 2:
        return WIDTH / 2
    return (index / (count - 1)) * WIDTH


def _points_attr(points: list[Point], first: date, last: date) -> str:
    low, high = _extent(points)
    count = len(points)
    return " ".join(
        f"{_x(day, first, last, index, count):.2f},{_y(value, low, high):.2f}"
        for index, (day, value) in enumerate(points)
    )


def _drawable(trend: TrendSeries) -> list[tuple[str, list[Point]]]:
    """The series with enough points to draw, in stacking order."""
    candidates = (("weight", list(trend.weight_kg)), ("fat", list(trend.body_fat_pct)))
    return [(name, points) for name, points in candidates if len(points) >= MIN_POINTS]


def _number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _label(trend: TrendSeries, series: list[tuple[str, list[Point]]]) -> str:
    """What a screen reader hears: the first and last value of each drawn series."""
    parts: list[str] = []
    for name, points in series:
        unit = "kilograms" if name == "weight" else "percent"
        title = "Weight" if name == "weight" else "Body fat"
        parts.append(f"{title} {_number(points[0][1])} to {_number(points[-1][1])} {unit}")
    if trend.stale:
        parts.append("cached")
    return ". ".join(parts) + "."


def sparkline(trend: TrendSeries | None) -> str | None:
    """The SVG for this trend, or ``None`` when there is not enough of it to draw a line.

    ``None`` is the template's whole condition: fewer than two points in both series, no cached
    row at all, or a youth profile whose route nulled the trend before it got here.
    """
    if trend is None:
        return None
    series = _drawable(trend)
    if not series:
        return None

    days = [day for _, points in series for day, _ in points]
    first, last = min(days), max(days)
    lines = "".join(
        f'<polyline class="trend-line trend-{name}" fill="none" vector-effect="non-scaling-stroke" '
        f'points="{_points_attr(points, first, last)}"/>'
        for name, points in series
    )
    return (
        f'<svg class="trend-svg" data-role="trend" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'role="img" aria-label="{_label(trend, series)}">{lines}</svg>'
    )
