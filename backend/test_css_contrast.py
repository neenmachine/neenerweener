import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "index.css").read_text()


DARK = re.compile(r"@media \(prefers-color-scheme: dark\) \{\s*:root \{(.*?)\}\s*\}", re.S)
VAR = re.compile(r"--([\w-]+):\s*(#[0-9a-f]{6}|#[0-9a-f]{3});")


def scheme_vars(dark):
    """The `:root` custom properties for one color scheme."""
    light = {k: c for block in re.findall(r":root \{(.*?)\}", DARK.sub("", CSS), re.S) for k, c in VAR.findall(block)}
    if not dark:
        return light
    return light | {k: c for block in DARK.findall(CSS) for k, c in VAR.findall(block)}


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


def mix(a, b, weight):
    """sRGB `color-mix(in srgb, a weight, b)`; `transparent` for b means `a` at that alpha over the page."""
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(
        f"{round(int(a[i : i + 2], 16) * weight + int(b[i : i + 2], 16) * (1 - weight)):02x}" for i in (0, 2, 4)
    )


def code_backgrounds(v):
    """Every surface syntax tokens sit on: the page (file view), the `pre` block, and a highlighted file line."""
    pre = re.search(r"\npre \{[^}]*background: color-mix\(in srgb, var\(--fg\) (\d+)%, transparent\)", CSS)
    hl = re.search(r"\.code tr\.hl td \{\s*background: color-mix\(in srgb, (#[0-9a-f]{6}) (\d+)%, var\(--bg\)\)", CSS)
    return {
        "page": v["bg"],
        "pre": mix(v["fg"], v["bg"], int(pre.group(1)) / 100),
        "highlighted line": mix(hl.group(1), v["bg"], int(hl.group(2)) / 100),
    }


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_syntax_token_colors_meet_wcag_aa_on_code_backgrounds(dark):
    v = scheme_vars(dark)
    tokens = {k: c for k, c in v.items() if k.startswith("syn-")}
    assert {"syn-comment", "syn-keyword", "syn-string", "syn-number", "syn-func"} <= tokens.keys(), tokens
    ratios = {
        (token, surface): contrast(color, bg)
        for token, color in tokens.items()
        for surface, bg in code_backgrounds(v).items()
    }
    failing = {k: round(r, 2) for k, r in ratios.items() if r < 4.5}
    assert not failing, failing


ANCHOR_RULE = re.compile(r"article :is\(h2, h3, h4, h5, h6\)[^{]*\.anchor[^{]*\{([^}]*)\}")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_heading_anchor_glyph_meets_wcag_aa(dark):
    # The `#` is text once shown (hover, focus, touch), so it needs 4.5:1 on the page, at full opacity.
    rules = ANCHOR_RULE.findall(CSS)
    opacities = {o for rule in rules for o in re.findall(r"opacity: ([\d.]+);", rule)}
    assert opacities == {"0", "1"}, opacities
    v = scheme_vars(dark)
    color = re.search(r"\bcolor: var\(--([\w-]+)\);", rules[0]).group(1)
    ratio = contrast(v[color], v["bg"])
    assert ratio >= 4.5, (color, ratio)
