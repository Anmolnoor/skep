"""v101-F5: the type and radius scales are a FLOOR, not a one-time cleanup.

v75 shipped colour, radius, shadow and motion tokens; the eighteen versions
since spent literals. Without this lint, v105 adds `12.5px` again and the sweep
was housekeeping. With it, the drift fails a gate.

A plain text check over the stylesheet — no framework, no parser, no build step
(I11: the UI is a no-build static app and its tests stay that way).
"""

from __future__ import annotations

import re
from pathlib import Path

STYLE = Path(__file__).resolve().parents[2] / "src/skep/supervisor/serve/static/style.css"

# The seven steps. A declaration may write the literal or the token; what it may
# not do is invent a thirteenth size.
TYPE_SCALE = {"11px", "12px", "13px", "15px", "17px", "22px", "32px"}
RADIUS_TOKENS = {
    "var(--radius-sm)",
    "var(--radius-md)",
    "var(--radius-lg)",
    "var(--radius-xl)",
    "var(--radius-pill)",
    "999px",
    "0",
    "inherit",
}
REQUIRED_TOKENS = (
    "--text-micro",
    "--text-sm",
    "--text-body",
    "--text-md",
    "--text-lg",
    "--text-xl",
    "--text-hero",
    "--space-1",
    "--space-2",
    "--space-3",
    "--space-4",
    "--space-5",
    "--space-6",
)


def _css() -> str:
    return STYLE.read_text(encoding="utf-8")


def _luminance(hex_colour: str) -> float:
    """WCAG relative luminance. Fifteen lines of sRGB maths beats a dependency
    for a check that runs on one file."""
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(fg: str, bg: str) -> float:
    lighter, darker = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _tokens() -> dict[str, str]:
    root = _css().split(":root {", 1)[1].split("\n}", 1)[0]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", root))


def test_every_font_size_is_on_the_scale() -> None:
    """12 distinct sizes became 7. `.card-kicker` at 12.5px beside `.field-help`
    at 12px is the drift this stops; `.timeline-time` at 10px was below the size
    the vendored mono is comfortably legible at."""
    css = _css()
    values = {match.group(1) for match in re.finditer(r"font-size:\s*([^;}]+)", css)}
    offenders = sorted(
        value.strip()
        for value in values
        if value.strip() not in TYPE_SCALE
        and value.strip() != "0"  # the icon-label idiom (font-size: 0)
        and not value.strip().startswith("var(--text-")
        # Relative sizes track whatever they inherit — they cannot drift off a
        # scale they never sit on.
        and not re.fullmatch(r"[\d.]+(em|rem|%)", value.strip())
        and "inherit" not in value
    )
    assert not offenders, f"font sizes off the scale: {offenders}"
    # And the scale really is seven steps, not seven plus whatever slipped in.
    literals = {v.strip() for v in values if v.strip().endswith("px")}
    assert literals <= TYPE_SCALE
    assert len(literals) <= len(TYPE_SCALE)


def test_every_border_radius_is_a_token() -> None:
    """24 hand-written radii shadowed four perfectly good tokens. `999px` stays:
    a dot or a pill is round by intent, not by accident."""
    css = _css()
    offenders = sorted(
        match.group(1).strip()
        for match in re.finditer(r"border-radius:\s*([^;}]+)", css)
        if match.group(1).strip() not in RADIUS_TOKENS
    )
    assert not offenders, f"hand-written border-radius values: {offenders}"


def test_root_defines_every_token_the_sweep_introduced() -> None:
    """A token the stylesheet references but never defines renders as nothing —
    the failure mode is silent, so the check is not (I8)."""
    css = _css()
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    missing = [token for token in REQUIRED_TOKENS if f"{token}:" not in root]
    assert not missing, f":root is missing {missing}"


def test_no_var_reference_is_undefined() -> None:
    """The general form of the same rule, over every token the file uses."""
    css = _css()
    # Tokens defined anywhere in the sheet count — :root, theme overrides and
    # scoped blocks alike — plus the handful app.js sets as an inline style
    # (`--chip-color`, the context meter): those are defined at runtime, and a
    # var() with no fallback that JS forgot to set is app.js's bug, not a
    # missing token here.
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    defined |= set(re.findall(r"(--[\w-]+)", (STYLE.parent / "app.js").read_text(encoding="utf-8")))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    assert not (used - defined), f"undefined tokens referenced: {sorted(used - defined)}"


def test_text_tokens_clear_aa_on_every_surface_they_are_painted_on() -> None:
    """v101-F6: --muted-2 was #6f695c — 3.25:1 on --panel, below the 4.5 bar,
    and it coloured .field-help, the text that teaches every policy knob (I9).
    Pinned from the token values themselves so a future palette tweak that
    darkens tertiary text fails a gate instead of shipping."""
    tokens = _tokens()
    surfaces = ("--bg", "--chrome", "--panel", "--panel-2", "--panel-3")
    failures = [
        f"{fg} on {bg}: {_contrast(tokens[fg], tokens[bg]):.2f}"
        for fg in ("--text", "--text-strong", "--muted", "--muted-2")
        for bg in surfaces
        if _contrast(tokens[fg], tokens[bg]) < 4.5
    ]
    assert not failures, f"below AA (4.5:1): {failures}"


def test_muted_2_stays_a_step_below_muted() -> None:
    """Legibility is not the only requirement — tertiary text that reads as
    body text has lost the hierarchy the token exists for."""
    tokens = _tokens()
    assert _luminance(tokens["--muted-2"]) < _luminance(tokens["--muted"])


def test_the_focus_ring_is_for_keyboard_navigation() -> None:
    """14 bare :focus rules meant the ring fired on mouse clicks too, so it read
    as noise. Every rule that PAINTS a ring must be :focus-visible or
    :focus-within; rules that SUPPRESS inner chrome stay bare :focus, because a
    suppression has to hold on mouse focus as well."""
    painters = [
        selector.strip()
        for selector, body in re.findall(r"([^{}]*):focus[^{}-]*\{([^}]*)\}", _css())
        if "var(--shadow-focus)" in body
    ]
    bare = [p for p in painters if ":focus-visible" not in p and ":focus-within" not in p]
    assert not bare, f"rings painted on plain :focus: {bare}"


def test_reduced_motion_is_honoured() -> None:
    """v75 gave everything a transition; the OS setting that asks for stillness
    was never read."""
    assert "@media (prefers-reduced-motion: reduce)" in _css()


def test_two_breakpoints_declared_once() -> None:
    """v101-F7: five blocks at two widths in three places, and the .sidebar rule
    sets contradicted each other — below 640 all three applied and the winner
    was decided per-property by file order. One block per width, and a third
    width fails the gate rather than quietly becoming the design."""
    blocks = re.findall(r"@media \(max-width: (\d+)px\)", _css())
    assert sorted(blocks) == ["640", "720"], f"breakpoint blocks: {blocks}"


def test_the_narrow_block_narrows_rather_than_re_declares() -> None:
    """640 is a narrowing of 720, not a second layout: everything in the wide
    block still applies at a phone width. A selector setting the SAME property
    in both is the contradiction this fix removed."""
    css = _css()

    def declarations(width: str) -> set[tuple[str, str]]:
        body = css.split(f"@media (max-width: {width}px) {{", 1)[1]
        # Rules are one level deep inside the block; stop at its closing brace.
        body = body[: body.index("\n}")]
        return {
            (selector.strip(), declaration.split(":", 1)[0].strip())
            for selector, rules in re.findall(r"([^{}]+)\{([^}]*)\}", body)
            for declaration in rules.split(";")
            if ":" in declaration
        }

    clashes = sorted(declarations("720") & declarations("640"))
    assert not clashes, f"declared at both breakpoints: {clashes}"


def _html() -> str:
    return (STYLE.parent / "index.html").read_text(encoding="utf-8")


def test_hidden_means_hidden() -> None:
    """v104-F0. The UA stylesheet's `[hidden] { display: none }` loses to ANY
    author rule that sets display, and `.field`, `.command-suggest`,
    `.strip-pill` and `.queued-steer` all set `display: flex`. So
    `node.hidden = true` did nothing for them.

    The codebase found this twice and patched it per component
    (`.chat-working[hidden]`, `.command-suggest[hidden]`); v103-F1 hit it a
    third time and shipped a permanently-visible empty "queued" box above the
    composer. Two more were broken and unnoticed: the v101-F10 engine select
    never hid for a non-coding caste, and the composer strip pills never hid
    when empty. One global rule, so there is no fourth time."""
    css = _css()
    # Comments explain the rule and quote it, so match against the RULES only —
    # the first version of this test passed on its own explanatory comment.
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "[hidden] { display: none !important; }" in rules
    # `!important` is load-bearing — a class rule later in the file beats a
    # plain `[hidden]` on source order, which is how this kept recurring.
    rule = re.search(r"\[hidden\]\s*\{([^}]*)\}", rules)
    assert rule and "!important" in rule.group(1)
    # And the per-component patches are gone: one rule, not a growing list.
    assert "chat-working[hidden]" not in rules
    assert "command-suggest[hidden]" not in rules


def test_every_id_that_has_a_class_rule_also_carries_the_class() -> None:
    """v104-F0, the general form of the bug that made the home-page composer
    render as a default 20-character boxed input: `#dock-input` carried the id
    but no `class="dock-input"`, so every `.dock-input` rule — `flex: 1`, the
    transparent background, `border: 0` — matched nothing. Silent by
    construction: the element exists, the CSS exists, they simply never meet."""
    css, html = _css(), _html()
    declared = set(re.findall(r"\.([a-z][\w-]*)\s*[{,:\[]", css))
    orphans = []
    for tag in re.findall(r"<[^>]*\bid=\"[\w-]+\"[^>]*>", html):
        ident = re.search(r'id="([\w-]+)"', tag)
        assert ident
        name = ident.group(1)
        if name not in declared:
            continue  # no class rule of that name — nothing to miss
        attr = re.search(r'class="([^"]*)"', tag)
        if name not in (set(attr.group(1).split()) if attr else set()):
            orphans.append(name)
    assert not orphans, f"id has a matching .class rule but no class attribute: {orphans}"


def test_the_rail_active_indicator_stays_inside_the_rail() -> None:
    """v104-F0: the rail is --rail-w wide and its items are 42px, centred, so
    an item's left edge sits at (rail - 42) / 2. The indicator was at
    `left: -10px`, which put it past the rail's own left edge and the viewport
    clipped it. Computed from the tokens so a wider rail cannot silently
    re-break it."""
    css = _css()
    rail_match = re.search(r"--rail-w:\s*(\d+)px", css)
    offset_match = re.search(
        r"\.sidebar a\[data-ws\]\.active::after\s*\{[^}]*left:\s*(-?\d+)px", css, re.S
    )
    assert rail_match and offset_match, "rail width / active-bar offset not found"
    rail = int(rail_match.group(1))
    offset = int(offset_match.group(1))
    item_left = (rail - 42) // 2
    assert item_left + offset >= 0, (
        f"active bar renders at x={item_left + offset} — outside the {rail}px rail"
    )
