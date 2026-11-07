import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "index.css").read_text()


def scheme_vars(dark):
    """The `:root` custom properties for one color scheme."""
    light = dict(re.findall(r"--(\w+):\s*(#[0-9a-f]{6}|#[0-9a-f]{3});", CSS.split("@media")[0]))
    if not dark:
        return light
    block = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*:root \{(.*?)\}", CSS, re.S)
    return light | dict(re.findall(r"--(\w+):\s*(#[0-9a-f]{6}|#[0-9a-f]{3});", block.group(1)))


def luminance(hex_color):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted([luminance(a), luminance(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_post_list_and_tag_text_meets_wcag_aa(dark):
    v = scheme_vars(dark)
    # faint: post-list tags; muted: excerpts, tag filter, article tags; accent: hover/current.
    ratios = {name: contrast(v[name], v["bg"]) for name in ("faint", "muted", "accent")}
    assert all(r >= 4.5 for r in ratios.values()), ratios
    # Keep the hierarchy: faint stays lighter than muted, and hover/current stay distinct.
    assert ratios["faint"] < ratios["muted"] < ratios["accent"], ratios
