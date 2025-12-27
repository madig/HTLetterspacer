from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable

import fontTools.misc.arrayTools as arrayTools
import fontTools.misc.bezierTools as bezierTools
import fontTools.pens.basePen as basePen
from fontTools.misc.transform import Identity, Transform
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPointPen
from ufoLib2.objects import Font, Glyph, Layer
from ufoLib2.objects.misc import BoundingBox
from ufoLib2.objects.point import Point as UfoPoint
from ufoLib2.typing import GlyphSet

LOGGER = logging.Logger(__name__)


@dataclass
class Point:
    __slots__ = "x", "y"
    x: float
    y: float


def space_main(
    layer: Glyph,
    reference_layer_bounds: BoundingBox,
    glyphset: Font | Layer,
    angle: float,
    compute_lsb: bool,
    compute_rsb: bool,
    factor: float,
    param_area: int,
    param_depth: int,
    param_freq: int,
    param_over: int,
    tabular_width: int | None,
    upm: int,
    xheight: int,
    debug_draw: Callable[[list[Point], list[Point]], None] | None = None,
) -> None:
    if not layer.contours and not layer.components:
        LOGGER.info("No paths in glyph %s.", layer.name)
        return

    if layer.components:
        dpen = DecomposingRecordingPen(glyphset)
        layer.draw(dpen)
        layer_decomposed = layer.copy()
        layer_decomposed.components.clear()
        dpen.replay(layer_decomposed.getPen())
        layer_measure = layer_decomposed
    else:
        layer_measure = layer

    assert layer.name is not None
    if tabular_width is None and (".tosf" in layer.name or ".tf" in layer.name):
        layer_width = layer.width
        assert layer_width is not None
        tabular_width = round(layer_width)

    new_left, new_right, new_width = calculate_spacing(
        layer_measure,
        reference_layer_bounds,
        angle,
        compute_lsb,
        compute_rsb,
        factor,
        param_area,
        param_depth,
        param_freq,
        param_over,
        tabular_width,
        upm,
        xheight,
        debug_draw,
    )
    set_sidebearings(
        layer,
        glyphset,
        new_left,
        new_right,
        new_width,
        angle,
        xheight,
    )


def calculate_spacing(
    layer: Glyph,
    reference_layer_bounds: BoundingBox,
    angle: float,
    compute_lsb: bool,
    compute_rsb: bool,
    factor: float,
    param_area: int,
    param_depth: int,
    param_freq: int,
    param_over: int,
    tabular_width: int | None,
    upm: int,
    xheight: int,
    debug_draw: Callable[[list[Point], list[Point]], None] | None = None,
) -> tuple[int, int, int]:
    # TODO: compute lsb/rsb separately?

    # The reference glyph provides the lower and upper bound of the vertical
    # zone to use for spacing. Overshoot lets us measure a bit above and below.
    overshoot = xheight * param_over / 100
    ref_ymin = reference_layer_bounds.yMin - overshoot
    ref_ymax = reference_layer_bounds.yMax + overshoot

    # Feel out the outer outline of the glyph from the left and the right (both
    # full and with the lower and upper bounds of the reference glyph). Take the
    # outermost reading as the extreme point from which to "test the depth" of
    # the glyph.
    margins_left_full, margins_right_full = sample_margins(
        layer, ref_ymin, ref_ymax, param_freq, angle, xheight
    )
    assert margins_left_full
    assert margins_right_full

    extreme_left_full, extreme_left, margins_left = extract_from_samples(
        margins_left_full, float.__lt__, ref_ymin, ref_ymax
    )
    extreme_right_full, extreme_right, margins_right = extract_from_samples(
        margins_right_full, float.__gt__, ref_ymin, ref_ymax
    )

    # create a closed polygon
    polygon_left, polygon_right = process_margins(
        margins_left,
        margins_right,
        extreme_left,
        extreme_right,
        xheight,
        ref_ymin,
        ref_ymax,
        param_depth,
        param_freq,
    )
    if debug_draw is not None:
        debug_draw(polygon_left, polygon_right)

    # dif between extremes full and zone
    distance_left = math.ceil(extreme_left.x - extreme_left_full.x)
    distance_right = math.ceil(extreme_right_full.x - extreme_right.x)

    # set new sidebearings
    new_left: int = math.ceil(
        -distance_left
        + calculate_sidebearing_value(
            factor,
            ref_ymax,
            ref_ymin,
            param_area,
            polygon_left,
            upm,
            xheight,
        )
    )
    new_right: int = math.ceil(
        -distance_right
        + calculate_sidebearing_value(
            factor,
            ref_ymax,
            ref_ymin,
            param_area,
            polygon_right,
            upm,
            xheight,
        )
    )
    new_width: int = 0

    if tabular_width is not None:
        width_shape = extreme_right_full.x - extreme_left_full.x
        width_actual = width_shape + new_left + new_right
        width_diff = round((tabular_width - width_actual) / 2)

        new_left += width_diff
        new_right += width_diff
        new_width = tabular_width

        LOGGER.info(
            "%s is tabular and adjusted at width = %s", layer.name, str(tabular_width)
        )
    # TODO: Decide earlier whether to compute lsb/rsb.
    else:
        if not compute_lsb:
            margin_left = layer.getLeftMargin()
            assert margin_left is not None
            new_left = round(margin_left)
        if not compute_rsb:
            margin_right = layer.getRightMargin()
            assert margin_right is not None
            new_right = round(margin_right)

    return new_left, new_right, new_width


def sample_margins(
    layer: Glyph,
    ref_ymin: float,
    ref_ymax: float,
    param_freq: int,
    angle: float,
    xheight: int,
) -> tuple[list[Point], list[Point]]:
    """Returns the left and right outline of the glyph, vertically scanned at param_freq
    intervals, sorted bottom to top.

    The italic angle is implicitly deslanted at origin = xheight / 2.
    """

    mline = xheight / 2
    tan_angle = math.tan(math.radians(angle))

    bounds = layer.getBounds()
    assert bounds is not None

    # A glyph can over- or undershoot its reference bounds. Measure the tallest
    # stretch.
    lower_bound = min(ref_ymin, bounds.yMin)
    upper_bound = max(ref_ymax, bounds.yMax)

    # XXX: handle case where glyph outside ref, e.g. fira sans 'prosgegrammeni'?!
    left: list[Point] = []
    right: list[Point] = []
    for y in range(round(lower_bound), round(upper_bound) + 1, param_freq):
        hits = sorted(intersections(layer, (bounds.xMin, y, bounds.xMax, y)))
        if hits:
            if angle:
                # Deslant angled glyphs implicitly.
                left.append(Point(hits[0][0] - (y - mline) * tan_angle, y))
                right.append(Point(hits[-1][0] - (y - mline) * tan_angle, y))
            else:
                left.append(Point(hits[0][0], y))
                right.append(Point(hits[-1][0], y))
        else:
            # If the glyph is shorter than the reference glyph top or bottom (i.e.
            # due to the reference having a round overshoot either or both sides),
            # fill up the margin samples top and bottom at maximum depth (infinity
            # into the opposite direction) to match. This also catches glyphs with
            # nothing in the middle, like "equal".
            left.append(Point(math.inf, y))
            right.append(Point(-math.inf, y))

    return left, right


def extract_from_samples(
    samples: list[Point],
    cmp: Callable[[float, float], bool],
    ref_ymin: float,
    ref_ymax: float,
) -> tuple[Point, Point, list[Point]]:
    extreme_full = None
    extreme = None
    margins: list[Point] = []
    for point in samples:
        if extreme_full is None or cmp(point.x, extreme_full.x):
            extreme_full = point
        if ref_ymin <= point.y <= ref_ymax:
            margins.append(point)
            if extreme is None or cmp(point.x, extreme.x):
                extreme = point
    assert extreme_full is not None
    assert extreme is not None
    assert margins
    return extreme_full, extreme, margins


def process_margins(
    margins_left: list[Point],
    margins_right: list[Point],
    extreme_left: Point,
    extreme_right: Point,
    xheight: int,
    ref_ymin: float,
    ref_ymax: float,
    param_depth: int,
    param_freq: int,
) -> tuple[list[Point], list[Point]]:
    # Cap the margin samples to a maximum depth from the outermost point in to
    # get our depth cut-in.
    depth = xheight * param_depth / 100
    max_depth = extreme_left.x + depth
    min_depth = extreme_right.x - depth
    margins_left = [Point(min(p.x, max_depth), p.y) for p in margins_left]
    margins_right = [Point(max(p.x, min_depth), p.y) for p in margins_right]

    # Close open counterforms at 45 degrees to create a polygon.
    diagonize(margins_left, margins_right, param_freq)
    margins_left.insert(0, Point(extreme_left.x, ref_ymin))
    margins_left.append(Point(extreme_left.x, ref_ymax))
    margins_right.insert(0, Point(extreme_right.x, ref_ymin))
    margins_right.append(Point(extreme_right.x, ref_ymax))

    return margins_left, margins_right


def diagonize(
    margins_left: list[Point], margins_right: list[Point], param_freq: int
) -> None:
    """close counters at 45 degrees"""

    # This works by checking that the delta of point.x to next_point.x is <= param_freq,
    # which just so happens to work out to 45° angles.
    for i in range(len(margins_left) - 1):
        if margins_left[i + 1].x - margins_left[i].x > param_freq:
            margins_left[i + 1].x = margins_left[i].x + param_freq
        if margins_right[i + 1].x - margins_right[i].x < -param_freq:
            margins_right[i + 1].x = margins_right[i].x - param_freq
    for i in reversed(range(len(margins_left) - 1)):
        if margins_left[i].x - margins_left[i + 1].x > param_freq:
            margins_left[i].x = margins_left[i + 1].x + param_freq
        if margins_right[i].x - margins_right[i + 1].x < -param_freq:
            margins_right[i].x = margins_right[i + 1].x - param_freq


def calculate_sidebearing_value(
    factor: float,
    ref_ymax: float,
    ref_ymin: float,
    param_area: int,
    polygon: list[Point],
    upm: int,
    xheight: int,
) -> float:
    amplitude_y = ref_ymax - ref_ymin

    # recalculates area based on UPM
    area_upm = param_area * ((upm / 1000) ** 2)

    # calculates proportional area
    white_area = area_upm * factor * 100

    prop_area = (amplitude_y * white_area) / xheight
    valor = prop_area - area(polygon)
    return valor / amplitude_y


def area(points: list[Point]) -> float:
    # https://mathopenref.com/coordpolygonarea2.html
    return 0.5 * abs(
        sum(
            prev.x * next.y - next.x * prev.y
            for prev, next in ((points[i - 1], points[i]) for i in range(len(points)))
        )
    )


def set_sidebearings(
    layer: Glyph,
    glyphset: Font | Layer,
    new_left: float,
    new_right: float,
    width: float,
    angle: float,
    xheight: float,
) -> None:
    if angle:
        new_left, new_right = deslant_sidebearings(
            layer, glyphset, new_left, new_right, angle, xheight
        )
    set_left_margin_rounded(layer, new_left, glyphset)
    set_right_margin_rounded(layer, new_right, glyphset)

    # adjusts the tabular miscalculation
    if width:
        layer.width = width

    # TODO: Handle this outside the core.
    if "com.schriftgestaltung.Glyphs.originalWidth" in layer.lib:
        layer.lib["com.schriftgestaltung.Glyphs.originalWidth"] = layer.width
        layer.width = 0


def deslant_sidebearings(
    layer: Glyph,
    glyphset: Font | Layer,
    l: float,
    r: float,
    a: float,
    xheight: float,
) -> tuple[int, int]:
    bounds = layer.getBounds(glyphset)
    assert bounds is not None
    left, _, _, _ = bounds
    m = skew_matrix((-a, 0), offset=(left, xheight / 2))
    backslant = Glyph()
    backslant.width = layer.width
    layer.drawPoints(TransformPointPen(backslant.getPointPen(), m))
    backslant.setLeftMargin(l, glyphset)
    backslant.setRightMargin(r, glyphset)

    boundsback = backslant.getBounds(glyphset)
    assert boundsback is not None
    left, _, _, _ = boundsback
    mf = skew_matrix((a, 0), offset=(left, xheight / 2))
    forwardslant = Glyph()
    forwardslant.width = backslant.width
    backslant.drawPoints(TransformPointPen(forwardslant.getPointPen(), mf))

    left_margin = forwardslant.getLeftMargin(glyphset)
    assert left_margin is not None
    right_margin = forwardslant.getRightMargin(glyphset)
    assert right_margin is not None

    return round(left_margin), round(right_margin)


def skew_matrix(
    angle: tuple[float, float], offset: tuple[float, float] = (0, 0)
) -> Transform:
    dx, dy = offset
    x, y = angle
    x, y = math.radians(x), math.radians(y)
    sT = Identity.translate(dx, dy).skew(x, y).translate(-dx, -dy)
    return sT


####
# Helper functionality that is not part of the spacer algorithm.
####


def segments(contour: list[UfoPoint]) -> list[list[UfoPoint]]:
    if not contour:
        return []

    points = list(contour)
    segments: list[list[UfoPoint]] = []

    # If we have 2 points or more, locate the first on-curve point, and rotate the
    # point list so that it _ends_ with an on-curve point.
    if len(points) > 1:
        first_oncurve = next(
            (i for i, point in enumerate(points) if point.type is not None), None
        )
        if first_oncurve is not None:
            points = points[first_oncurve + 1 :] + points[: first_oncurve + 1]

    current_segment: list[UfoPoint] = []
    for point in points:
        current_segment.append(point)
        if point.type is None:
            continue
        segments.append(current_segment)
        current_segment = []
    # If the segment consists of only 1 or more off-curves, the above loop would have
    # ended without appending it, so append it whole.
    if current_segment:
        segments.append(current_segment)

    return segments


def intersections(
    layer: Glyph, measurement_line: tuple[float, float, float, float]
) -> list[tuple[float, float]]:
    intersections: list[tuple[float, float]] = []
    x1, y1, x2, y2 = measurement_line
    for contour in layer.contours:
        for segment_pair in arrayTools.pairwise(segments(contour.points)):
            last, curr = segment_pair
            curr_type = curr[-1].type
            if curr_type == "line":
                i = line_intersection(x1, y1, x2, y2, last[-1], curr[-1])
                if i is not None:
                    intersections.append(i)
            elif curr_type == "curve":
                i = curve_intersections(
                    x1, y1, x2, y2, last[-1], curr[0], curr[1], curr[2]
                )
                if i:
                    intersections.extend(i)
            elif curr_type == "qcurve":
                i = qcurve_intersections(x1, y1, x2, y2, last[-1], *curr)
                if i:
                    intersections.extend(i)
            else:
                raise ValueError(
                    f"Glyph '{layer.name}': Cannot deal with segment type {curr_type}"
                )
    return intersections


def curve_intersections(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    p1: UfoPoint,
    p2: UfoPoint,
    p3: UfoPoint,
    p4: UfoPoint,
) -> list[tuple[float, float]]:
    """
    Computes intersection between a line and a cubic curve.
    Adapted from: https://www.particleincell.com/2013/cubic-line-intersection/
    Takes four scalars describing line parameters and four points describing
    curve.
    Returns a list of intersections in Tuples of the format (coordinate_x,
    coordinate_y, term, subsegment_index == 0). The subsegment_index is only
    used for quadratic curves.
    """
    bx, by = x1 - x2, y2 - y1
    m = x1 * (y1 - y2) + y1 * (x2 - x1)
    a, b, c, d = bezierTools.calcCubicParameters(
        (p1.x, p1.y), (p2.x, p2.y), (p3.x, p3.y), (p4.x, p4.y)
    )
    pc0 = by * a[0] + bx * a[1]
    pc1 = by * b[0] + bx * b[1]
    pc2 = by * c[0] + bx * c[1]
    pc3 = by * d[0] + bx * d[1] + m
    r = bezierTools.solveCubic(pc0, pc1, pc2, pc3)
    sol = []
    for t in r:
        if t < 0 or t > 1:
            continue
        s0 = ((a[0] * t + b[0]) * t + c[0]) * t + d[0]
        s1 = ((a[1] * t + b[1]) * t + c[1]) * t + d[1]
        if (x2 - x1) != 0:
            s = (s0 - x1) / (x2 - x1)
        else:
            s = (s1 - y1) / (y2 - y1)
        if s < 0 or s > 1:
            continue
        sol.append((s0, s1))
    return sol


def qcurve_intersections(
    x1: float, y1: float, x2: float, y2: float, *pts: UfoPoint
) -> list[tuple[float, float]]:
    """
    Computes intersection between a quadratic spline and a line segment.
    Adapted from curveIntersections(). Takes four scalars describing line and an
    Iterable of points describing a quadratic curve, including the first (==
    anchor) point. Quadatric curves are special in that they can consist of
    implied on-curve points, which is why this function returns a
    `subsegment_index` to associate a `t` with the correct subsegment.
    Returns a list of intersections in Tuples of the format (coordinate_x,
    coordinate_y, term, subsegment_index).
    """

    sol = []
    points = [(p.x, p.y) for p in pts]

    nx, ny = x1 - x2, y2 - y1
    m = x1 * (y1 - y2) + y1 * (x2 - x1)
    # Decompose a segment with potentially implied on-curve points into subsegments.
    # p1 is the anchor, p2 the control handle, p3 the (implied) on-curve point in the
    # subsegment.
    p1 = points[0]
    # TODO: skia pathops also has a SegmentPenIterator
    # https://github.com/googlefonts/nanoemoji/blob/9adfff414b1ba32a816d722936421c52d4827d8a/src/nanoemoji/svg_path.py#L98-L101
    for p2, p3 in basePen.decomposeQuadraticSegment(points[1:]):
        (ax, ay), (bx, by), (cx, cy) = bezierTools.calcQuadraticParameters(p1, p2, p3)
        p1 = p3  # prepare for next turn

        pc0 = ny * ax + nx * ay
        pc1 = ny * bx + nx * by
        pc2 = ny * cx + nx * cy + m
        r = bezierTools.solveQuadratic(pc0, pc1, pc2)

        for t in r:
            if t < 0 or t > 1:
                continue
            sx = (ax * t + bx) * t + cx
            sy = (ay * t + by) * t + cy
            if (x2 - x1) != 0:
                s = (sx - x1) / (x2 - x1)
            else:
                s = (sy - y1) / (y2 - y1)
            if s < 0 or s > 1:
                continue
            sol.append((sx, sy))
    return sol


def line_intersection(
    x1: float, y1: float, x2: float, y2: float, p3: UfoPoint, p4: UfoPoint
) -> tuple[float, float] | None:
    """
    Computes intersection between two lines.
    Adapted from Andre LaMothe, "Tricks of the Windows Game Programming Gurus".
    G. Bach, http://stackoverflow.com/a/1968345
    Takes four scalars describing line parameters and two points describing
    line.
    Returns a list of intersections in Tuples of the format (coordinate_x,
    coordinate_y, term, subsegment_index == 0). The subsegment_index is only
    used for quadratic curves.
    """
    x3, y3 = p3.x, p3.y
    x4, y4 = p4.x, p4.y
    Bx_Ax = x4 - x3
    By_Ay = y4 - y3
    Dx_Cx = x2 - x1
    Dy_Cy = y2 - y1
    determinant = -Dx_Cx * By_Ay + Bx_Ax * Dy_Cy
    if abs(determinant) < 1e-12:
        return None
    s = (-By_Ay * (x3 - x1) + Bx_Ax * (y3 - y1)) / determinant
    t = (Dx_Cx * (y3 - y1) - Dy_Cy * (x3 - x1)) / determinant
    if 0 <= s <= 1 and 0 <= t <= 1:
        return (x3 + (t * Bx_Ax), y3 + (t * By_Ay))
    return None


def set_left_margin_rounded(
    glyph: Glyph, value: float, layer: GlyphSet | None = None
) -> None:
    """Sets the the rounded space in font units from the point of origin to the
    left side of the glyph.

    Args:
        value: The desired left margin in font units.
        layer: The layer of the glyph to look up components, if any. Not needed for
            pure-contour glyphs.
    """
    bounds = glyph.getBounds(layer)
    if bounds is None:
        return None
    diff = round(value - bounds.xMin)
    if diff:
        glyph.width += diff
        glyph.move((diff, 0))


def set_right_margin_rounded(
    glyph: Glyph, value: float, layer: GlyphSet | None = None
) -> None:
    """Sets the the rounded space in font units from the glyph's advance width to
    the right side of the glyph.

    Args:
        value: The desired right margin in font units.
        layer: The layer of the glyph to look up components, if any. Not needed for
            pure-contour glyphs.
    """
    bounds = glyph.getBounds(layer)
    if bounds is None:
        return None
    glyph.width = round(bounds.xMax + value)
