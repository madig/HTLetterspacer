from __future__ import annotations

import argparse
import collections
import functools
import logging
import sys
from os import PathLike
from pathlib import Path

import ufoLib2
import ufoLib2.objects

import htletterspacer.config
import htletterspacer.core

LOGGER = logging.Logger(__name__)

AREA_KEY = "com.ht.spacer.area"
DEPTH_KEY = "com.ht.spacer.depth"
OVERSHOOT_KEY = "com.ht.spacer.overshoot"


# TODO: respect metrics keys by skipping that side or by interpreting them?
# TODO: pull in glyphConstruction to rebuild components?
def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Respace all glyphs with contours.")
    parser.add_argument("ufo", type=ufoLib2.Font.open)
    parser.add_argument(
        "--area",
        type=int,
        help="Set the UFO-wide area parameter (can be overridden on the glyph level).",
    )
    parser.add_argument(
        "--depth",
        type=int,
        help="Set the UFO-wide depth parameter (can be overridden on the glyph level).",
    )
    parser.add_argument(
        "--overshoot",
        type=int,
        help="Set the UFO-wide overshoot parameter (can be overridden on the glyph level).",
    )
    parser.add_argument(
        "--debug-polygons-in-background",
        action="store_true",
        help="Draw the spacing polygons into the glyph background.",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output")
    parsed_args = parser.parse_args(args)

    space_ufo(
        parsed_args.ufo,
        area=parsed_args.area,
        depth=parsed_args.depth,
        overshoot=parsed_args.overshoot,
        config_file=parsed_args.config,
        debug_polygons_in_background=parsed_args.debug_polygons_in_background,
    )

    if parsed_args.output:
        parsed_args.ufo.save(parsed_args.output, overwrite=True)
    else:
        parsed_args.ufo.save()

    return None


def space_ufo(
    ufo: ufoLib2.Font,
    area: int | None = None,
    depth: int | None = None,
    overshoot: int | None = None,
    config_file: PathLike[str] | None = None,
    to_space: set[str] | None = None,
    debug_polygons_in_background: bool = False,
) -> None:
    italic_angle = ufo.info.italicAngle or 0
    assert isinstance(ufo.info.unitsPerEm, int)
    assert isinstance(ufo.info.xHeight, int)

    param_area: int = area or ufo.lib.get(AREA_KEY, 400)
    param_depth: int = depth or ufo.lib.get(DEPTH_KEY, 15)
    param_over: int = overshoot or ufo.lib.get(OVERSHOOT_KEY, 0)

    if config_file is not None:
        config = htletterspacer.config.parse_config(Path(config_file).read_text())
    else:
        config = htletterspacer.config.parse_config(
            htletterspacer.config.DEFAULT_CONFIGURATION
        )

    # Note down which glyphs are used as components in which other glyphs. Moving
    # them for spacing needs counter-moving them where they are used as components
    # to have them stay put.
    composite_graph: collections.defaultdict[str, set[str]]
    composite_graph = collections.defaultdict(set)
    for glyph in ufo:
        if glyph.name is None:
            continue
        for c in glyph.components:
            composite_graph[c.baseGlyph].add(glyph.name)

    background: ufoLib2.objects.Layer | None = None
    if debug_polygons_in_background:
        background = ufo.layers.get("public.background")
        if background is None:
            background = ufo.newLayer("public.background")

    # TODO: Make a Hypothesis test to see whether the order we space in makes a
    #       meaningful difference outside rounding errors for different float
    #       arithmetic order.
    for glyph in ufo:
        assert glyph.name is not None

        if to_space is not None and glyph.name not in to_space:
            continue

        if not glyph.contours and not glyph.components:
            LOGGER.info(
                "Skipping glyph %s because it has neither contours nor components.",
                glyph.name,
            )
            continue
        if glyph.width == 0 and any(
            a.name.startswith("_") for a in glyph.anchors if a.name is not None
        ):
            LOGGER.info("Skipping glyph %s because it is a mark.", glyph.name)
            continue

        ref_name, factor = htletterspacer.config.reference_and_factor(config, glyph)

        try:
            glyph_ref = ufo[ref_name]
        except KeyError:
            LOGGER.info(
                "Reference glyph %s does not exist, spacing %s with own bounds.",
                ref_name,
                glyph.name,
            )
            glyph_ref = glyph
        assert glyph_ref.name is not None
        ref_bounds = glyph_ref.getBounds(ufo)
        assert ref_bounds is not None

        glyph_param_area: int = glyph.lib.get(AREA_KEY, param_area)
        glyph_param_depth: int = glyph.lib.get(DEPTH_KEY, param_depth)
        glyph_param_over: int = glyph.lib.get(OVERSHOOT_KEY, param_over)

        left_before = glyph.getLeftMargin(ufo)
        assert left_before is not None

        if debug_polygons_in_background:
            assert background is not None
            debug_glyph = background.get(glyph.name)
            if debug_glyph is None:
                debug_glyph = background.newGlyph(glyph.name)
            assert debug_glyph is not None
            debug_draw = functools.partial(draw_samples, debug_glyph)
        else:
            debug_draw = None

        htletterspacer.core.space_main(
            glyph,
            ref_bounds,
            ufo,
            angle=-italic_angle,
            compute_lsb=True,
            compute_rsb=True,
            factor=factor,
            param_area=glyph_param_area,
            param_depth=glyph_param_depth,
            param_freq=5,
            param_over=glyph_param_over,
            tabular_width=None,
            upm=ufo.info.unitsPerEm,
            xheight=ufo.info.xHeight,
            debug_draw=debug_draw,
        )

        # If the glyph is used as a component in any other glyph, move that component
        # in the opposite direction (measured to the left, to the origin) to ensure
        # that existing components stay as before.
        if glyph.name in composite_graph:
            left_after = glyph.getLeftMargin(ufo)
            assert left_after is not None
            left_diff = left_before - left_after
            if isinstance(left_diff, float) and left_diff.is_integer():
                left_diff = round(left_diff)
            if not left_diff:
                continue
            for composite_name in composite_graph[glyph.name]:
                composite = ufo[composite_name]
                for c in composite.components:
                    if c.baseGlyph != glyph.name:
                        continue
                    c.transformation = c.transformation.translate(left_diff, 0)


def draw_samples(
    glyph: ufoLib2.objects.Glyph,
    margins_left: list[htletterspacer.core.Point],
    margins_right: list[htletterspacer.core.Point],
) -> None:
    glyph.clear()
    pen = glyph.getPointPen()
    pen.beginPath()
    for p in margins_left:
        pen.addPoint((p.x, p.y), segmentType="line")
    pen.endPath()
    pen.beginPath()
    for p in margins_right:
        pen.addPoint((p.x, p.y), segmentType="line")
    pen.endPath()


if __name__ == "__main__":
    main()
    sys.exit()
