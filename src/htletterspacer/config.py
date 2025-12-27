from __future__ import annotations

from typing import Tuple, List

import glyphsLib.glyphdata
from ufoLib2.objects import Glyph

GLYPHS_LEFT_METRICS_KEY = "com.schriftgestaltung.Glyphs.glyph.leftMetricsKey"
GLYPHS_RIGHT_METRICS_KEY = "com.schriftgestaltung.Glyphs.glyph.rightMetricsKey"
GLYPHS_SCRIPT_KEY = "com.schriftgestaltung.Glyphs.script"
GLYPHS_CATEGORY_KEY = "com.schriftgestaltung.Glyphs.category"
GLYPHS_SUBCATEGORY_KEY = "com.schriftgestaltung.Glyphs.subCategory"

DEFAULT_CONFIGURATION = """
# Reference
# Script, Category, Subcategory, factor, referenceGlyph, filter

# Letters
*,Letter,Uppercase,1.25,H,*,
*,Letter,Smallcaps,1.1,h.sc,*,
*,Letter,Lowercase,1,x,*,
*,Letter,Lowercase,0.7,m.sups,.sups,

# Numbers
*,Number,Decimal Digit,1.2,one,*,
*,Number,Decimal Digit,1.2,zero.osf,.osf,
*,Number,Fraction,1.3,*,*,
*,Number,*,0.8,*,.dnom,
*,Number,*,0.8,*,.numr,
*,Number,*,0.8,*,.inferior,
*,Number,*,0.8,*,superior,

# Punctuation
*,Punctuation,Other,1.4,*,*,
*,Punctuation,Parenthesis,1.2,*,*,
*,Punctuation,Quote,1.2,*,*,
*,Punctuation,Dash,1,*,*,
*,Punctuation,*,1,*,slash,
*,Punctuation,*,1.2,*,*,

# Symbols
*,Symbol,Currency,1.6,*,*,
*,Symbol,*,1.5,*,*,
*,Mark,*,1,*,*,

# Devanagari
devanagari,Letter,Other,1,devaHeight,*,
devanagari,Letter,Ligature,1,devaHeight,*,
"""

ConfigLine = Tuple[str, str, str, float, str, str]
ConfigList = List[ConfigLine]


def parse_config(config: str) -> ConfigList:
    array: ConfigList = []
    for line in config.split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            line_split = line.rstrip(",").split(",")
            script, category, subcategory, factor, reference_glyph, name_filter = (
                line_split
            )
            array.append(
                (
                    script,
                    category,
                    subcategory,
                    float(factor),
                    reference_glyph,
                    name_filter,
                )
            )
    return array


def find_exception(
    config: ConfigList, script: str, category: str, subcategory: str, glyph_name: str
) -> ConfigLine | None:
    exception = None
    for item in config:
        if script == item[0] or item[0] == "*":
            if category == item[1] or item[1] == "*":
                if subcategory == item[2] or item[2] == "*":
                    if not exception or item[5] in glyph_name:
                        exception = item
    return exception


def reference_and_factor(config: ConfigList, glyph: Glyph) -> tuple[str, float]:
    assert glyph.name is not None
    glyph_name = glyph.name

    # Some names have been replaced.
    if glyph_name == "ustrait-cy":
        glyph_name = "ustraight-cy"
    elif glyph_name == "Ustrait-cy":
        glyph_name = "Ustraight-cy"

    glyph_data = glyphsLib.glyphdata.get_glyph(glyph_name)
    script: str = glyph.lib.get(GLYPHS_SCRIPT_KEY, glyph_data.script) or "*"
    category: str = glyph.lib.get(GLYPHS_CATEGORY_KEY, glyph_data.category) or "*"
    subcategory: str = (
        glyph.lib.get(GLYPHS_SUBCATEGORY_KEY, glyph_data.subCategory) or "*"
    )

    if category == "Punctuation" and subcategory == "*":
        subcategory = "Other"

    if ".sc" in glyph_name or ".smcp" in glyph_name or ".c2sc" in glyph_name:
        subcategory = "Smallcaps"

    # TODO: glyphs does ("ustrait-cy", "x", 1.0),
    # here get  ('ustrait-cy', 'ustrait-cy', 1.0)

    exception = find_exception(config, script, category, subcategory, glyph_name)
    if exception is None:
        reference = glyph_name
        factor = 1.0
    else:
        _, _, _, factor, reference, _ = exception
        if reference == "*":
            reference = glyph_name
    return reference, factor
