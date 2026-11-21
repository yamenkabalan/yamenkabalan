#!/usr/bin/env python3
"""Render the profile README as a stack of animated SVG cards.

    python scripts/render_cards.py            -> cards/*.svg      (animated, what you commit)
    STATIC=1 python scripts/render_cards.py   -> cards-static/    (final frame + preview.html)

Everything here has to survive being shown through an <img> tag, so the cards use
only an inline <style> block (CSS keyframes), SMIL <animate> elements and fonts
subset with pyftsubset and embedded as base64 data URIs. No scripts, no links,
no external requests.

Inputs
    data/contributions.json   written daily by scripts/fetch-data.sh (the Action)
    data/repos.json           same
    featured.txt              repo names, one per line
    assets/portrait.*         optional hero portrait
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from html import escape
from pathlib import Path

try:
    from fontTools.ttLib import TTFont
except ImportError:  # pragma: no cover
    sys.exit("fonttools is missing: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC") == "1"
OUT = ROOT / ("cards-static" if STATIC else "cards")
DATA = ROOT / "data"

# ───────────────────────────── config: edit here ─────────────────────────────

NAME = "Yamen"
TAGLINE = ("Software engineering student building clinic SaaS, "
           "AI advisors and data-driven decision systems.")

# Placeholder palette and type: swap these for the values from your website.
THEME = {
    "surface": "#121A26",   # card fill
    "line":    "#223046",   # card outline, baselines
    "text":    "#EAF0F7",
    "muted":   "#8B99AE",
    "brand":   "#4FD1B5",   # accent: rules, bars, contribution graph
}

# role -> font files, tried in order per character (latin first, then latin-ext).
FONTS = {
    "display": {"family": "Newsreader", "weight": 600, "fallback": "Georgia, 'Times New Roman', serif",
                "files": ["fonts/newsreader-latin-600-normal.woff2",
                          "fonts/newsreader-latin-ext-600-normal.woff2"]},
    "text":    {"family": "Hanken Grotesk", "weight": 400, "fallback": "system-ui, Arial, sans-serif",
                "files": ["fonts/hanken-grotesk-latin-400-normal.woff2",
                          "fonts/hanken-grotesk-latin-ext-400-normal.woff2"]},
    "strong":  {"family": "Hanken Grotesk", "weight": 600, "fallback": "system-ui, Arial, sans-serif",
                "files": ["fonts/hanken-grotesk-latin-600-normal.woff2",
                          "fonts/hanken-grotesk-latin-ext-600-normal.woff2"]},
}

# File name = tile-<key>.svg. The links live in README.md (an <img> can't link).
TILES = [
    {"key": "linkedin",  "title": "LinkedIn",     "sub": "in/yam3n"},
    {"key": "instagram", "title": "Instagram",    "sub": "@yam3n__"},
    {"key": "repos",     "title": "Repositories", "sub": "github.com/yamenkabalan"},
]

# Timeline in seconds: the page plays top to bottom.
T_HERO, T_TILES, T_CHART, T_REPOS, REPO_STEP = 0.0, 0.9, 1.7, 2.4, 0.30

W = 830  # README column width; every card is drawn at this scale

# ───────────────────────────────── helpers ───────────────────────────────────

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ROLE_CLASS = {"display": "d", "text": "t", "strong": "s"}


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    """Blend colour a toward b by t (0..1), returned as hex."""
    ra, rb = rgb(a), rgb(b)
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ra, rb))


def fmt_date(d):
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def num(n):
    return f"{n:,}"


def plural(n, word):
    return f"{num(n)} {word}" + ("" if n == 1 else "s")


class Role:
    """Font role with glyph-advance measurement, so text can be fitted and wrapped."""

    def __init__(self, key):
        self.key = key
        self.spec = FONTS[key]
        self.tables = []
        for p in self.spec["files"]:
            f = TTFont(ROOT / p)
            self.tables.append((f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm))

    def has(self, ch):
        return any(ord(ch) in cm for cm, _, _ in self.tables)

    def adv(self, ch):
        for cm, hm, upm in self.tables:
            if ord(ch) in cm:
                return hm[cm[ord(ch)]][0] / upm
        return 0.55

    def width(self, s, size):
        return sum(self.adv(c) for c in s) * size * 1.02  # small margin for kerning/rounding


ROLES = {k: Role(k) for k in FONTS}


def wrap(role, s, size, max_w, max_lines):
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if ROLES[role].width(trial, size) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and ROLES[role].width(last + "…", size) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def fit(role, s, size, max_w, min_size=11):
    while size > min_size and ROLES[role].width(s, size) > max_w:
        size -= 0.5
    return size


# ───────────────────────────── font subsetting ───────────────────────────────

_subset_cache = {}


def run_pyftsubset(path, chars):
    key = (path, chars)
    if key in _subset_cache:
        return _subset_cache[key]
    exe = shutil.which("pyftsubset")
    cmd = [exe] if exe else [sys.executable, "-m", "fontTools.subset"]
    with tempfile.TemporaryDirectory() as td:
        text_file, out = Path(td, "chars.txt"), Path(td, "font.out")
        text_file.write_text(chars, encoding="utf-8")
        for flavor in ("woff2", "woff"):
            r = subprocess.run(
                cmd + [str(ROOT / path), f"--text-file={text_file}", f"--flavor={flavor}",
                       f"--output-file={out}", "--layout-features=kern,liga,tnum,lnum",
                       "--no-hinting"],
                capture_output=True, text=True)
            if r.returncode == 0:
                break
        else:
            sys.exit(f"pyftsubset failed on {path}:\n{r.stderr}")
        result = (flavor, out.read_bytes())
    _subset_cache[key] = result
    return result


def unicode_range(chars):
    cps = sorted({ord(c) for c in chars})
    out, i = [], 0
    while i < len(cps):
        j = i
        while j + 1 < len(cps) and cps[j + 1] == cps[j] + 1:
            j += 1
        out.append(f"U+{cps[i]:X}" if i == j else f"U+{cps[i]:X}-{cps[j]:X}")
        i = j + 1
    return ",".join(out)


def font_face_css(role, used):
    """One @font-face per source file that actually supplies glyphs, each with its own unicode-range."""
    spec, remaining, css = FONTS[role], set(used) | {" "}, []
    for path, (cmap, _, _) in zip(spec["files"], ROLES[role].tables):
        mine = {c for c in remaining if ord(c) in cmap}
        remaining -= mine
        if not mine:
            continue
        chars = "".join(sorted(mine))
        flavor, data = run_pyftsubset(path, chars)
        b64 = base64.b64encode(data).decode()
        css.append(f"@font-face{{font-family:'pf-{role}';font-weight:{spec['weight']};font-style:normal;"
                   f"src:url(data:font/{flavor};base64,{b64}) format('{flavor}');"
                   f"unicode-range:{unicode_range(chars)}}}")
    if remaining:
        print(f"  warning: no glyph for {''.join(sorted(remaining))!r} in role '{role}', fallback font used",
              file=sys.stderr)
    return "".join(css)


# ──────────────────────────────── card model ─────────────────────────────────

ANIM_CSS = """
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes grow{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes draw{from{stroke-dashoffset:var(--len)}to{stroke-dashoffset:0}}
@keyframes sweep{from{opacity:0;transform:scale(.55)}to{opacity:1;transform:scale(1)}}
.rise{animation:rise .8s cubic-bezier(.22,.8,.24,1) var(--d,0s) both}
.fade{animation:fade .7s ease-out var(--d,0s) both}
.grow{transform-box:fill-box;transform-origin:50% 100%;animation:grow .9s cubic-bezier(.22,.8,.24,1) var(--d,0s) both}
.draw{stroke-dasharray:var(--len);animation:draw 1s ease-out var(--d,0s) both}
.sweep{transform-box:fill-box;transform-origin:50% 50%;animation:sweep .5s ease-out var(--d,0s) both}
@media (prefers-reduced-motion:reduce){*{animation:none!important}.smil{display:none}}
"""


class Card:
    def __init__(self, name, h, title, desc, w=W):
        self.name, self.w, self.h, self.title, self.desc = name, w, h, title, desc
        self.parts, self.defs = [], []
        self.used = {k: set() for k in FONTS}

    # generic element; kind = animation class, delay in seconds, var = css custom props
    def node(self, tag, kind=None, delay=0.0, cls="", var=None, body=None, **attrs):
        classes, style = ([cls] if cls else []), []
        if kind and not STATIC:
            classes.append(kind)
            style.append(f"--d:{delay:.2f}s")
            style += [f"--{k}:{v}" for k, v in (var or {}).items()]
        a = " ".join(f'{k.rstrip("_").replace("_", "-")}="{escape(str(v), quote=True)}"'
                     for k, v in attrs.items())
        s = f"<{tag}"
        if classes:
            s += f' class="{" ".join(classes)}"'
        if style:
            s += f' style="{";".join(style)}"'
        if a:
            s += " " + a
        return s + (f">{body}</{tag}>" if body is not None else "/>")

    def add(self, *items):
        self.parts.extend(items)

    def text(self, x, y, s, role, size, fill, anchor="start", kind=None, delay=0.0, tabular=False):
        self.used[role].update(s)
        cls = ROLE_CLASS[role] + (" n" if tabular else "")
        self.add(self.node("text", kind, delay, cls=cls, body=escape(s), x=x, y=y,
                           font_size=size, fill=fill, text_anchor=anchor))

    def smil(self, svg):
        """Decorative SMIL; hidden under prefers-reduced-motion, absent from STATIC output."""
        if not STATIC:
            self.add(f'<g class="smil">{svg}</g>')

    def frame(self, rx):
        self.add(self.node("rect", x=0.5, y=0.5, width=self.w - 1, height=self.h - 1, rx=rx,
                           fill=THEME["surface"], stroke=THEME["line"]))

    def render(self):
        css = "".join(font_face_css(r, u) for r, u in self.used.items() if u)
        for r, d in ROLE_CLASS.items():
            spec = FONTS[r]
            css += f".{d}{{font-family:'pf-{r}',{spec['fallback']};font-weight:{spec['weight']}}}"
        css += ".n{font-variant-numeric:tabular-nums}"
        if not STATIC:
            css += ANIM_CSS
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="t d">'
                f'<title id="t">{escape(self.title)}</title><desc id="d">{escape(self.desc)}</desc>'
                f"<style>{css}</style>{defs}{''.join(self.parts)}</svg>\n")


# ───────────────────────────────── data ──────────────────────────────────────

def load_json(name):
    p = DATA / name
    if not p.exists():
        sys.exit(f"{p} not found. Run `bash scripts/fetch-data.sh` (needs gh auth) "
                 f"or the 'Refresh profile cards' workflow first.")
    return json.loads(p.read_text(encoding="utf-8"))


def load_days():
    raw = load_json("contributions.json")
    days = sorted((dt.date.fromisoformat(d["date"]), int(d["count"])) for d in raw["days"])
    if not days:
        sys.exit("data/contributions.json has no days")
    return days


def compute_stats(days):
    counts = [c for _, c in days]
    run = longest = 0
    for c in counts:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    i = len(days) - 1
    if counts[i] == 0:        # today may not have a contribution yet; don't break the streak for that
        i -= 1
    current = 0
    while i >= 0 and counts[i] > 0:
        current += 1
        i -= 1
    months = OrderedDict()
    for d, c in days:
        months[(d.year, d.month)] = months.get((d.year, d.month), 0) + c
    last12 = [(y, m, v) for (y, m), v in list(months.items())[-12:]]
    return {
        "total": sum(counts),
        "active": sum(1 for c in counts if c > 0),
        "span": len(days),
        "busiest": max(days, key=lambda x: x[1]),
        "current": current,
        "longest": longest,
        "months": last12,
        "start": days[0][0],
        "end": days[-1][0],
    }


def load_featured():
    names = [ln.strip() for ln in (ROOT / "featured.txt").read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    by_name = {r["name"]: r for r in load_json("repos.json")}
    out = []
    for n in names:
        if n in by_name:
            out.append(by_name[n])
        else:
            print(f"  warning: {n} is in featured.txt but not in data/repos.json", file=sys.stderr)
    return out


def portrait_uri():
    for p in sorted((ROOT / "assets").glob("portrait.*")):
        try:
            from PIL import Image
            im = Image.open(p).convert("RGB")
            s = min(im.size)
            l, t = (im.width - s) // 2, (im.height - s) // 2
            im = im.crop((l, t, l + s, t + s)).resize((360, 360), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except ImportError:
            mime = {"png": "image/png", "webp": "image/webp"}.get(p.suffix[1:].lower(), "image/jpeg")
            return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    return None


# ──────────────────────────────── the cards ──────────────────────────────────

def build_hero(base):
    T = THEME
    h = 250
    c = Card("hero", h, NAME, TAGLINE)
    c.frame(24)
    uri = portrait_uri()
    right = W - 48
    if uri:
        cx, cy, r = W - 48 - 94, h / 2, 86
        right = cx - r - 8 - 32
        c.defs.append(f'<clipPath id="pc"><circle cx="{cx}" cy="{cy}" r="{r}"/></clipPath>')
        c.add(c.node("image", "fade", base + 0.3, href=uri, x=cx - r, y=cy - r, width=2 * r, height=2 * r,
                     preserveAspectRatio="xMidYMid slice", clip_path="url(#pc)"))
        c.add(c.node("circle", "draw", base + 0.4, var={"len": round(2 * math.pi * (r + 8)) + 2},
                     cx=cx, cy=cy, r=r + 8, fill="none", stroke=T["brand"], stroke_width=2.5))
    size = fit("display", NAME, 108, right - 48)
    c.text(48, 124, NAME, "display", size, T["text"], kind="rise", delay=base + 0.05)
    c.add(c.node("path", "draw", base + 0.5, var={"len": 122}, d="M48 150H168", fill="none",
                 stroke=T["brand"], stroke_width=4, stroke_linecap="round"))
    c.add(c.node("circle", "fade", base + 1.4, cx=186, cy=150, r=5, fill=T["brand"]))
    c.smil(f'<circle cx="186" cy="150" r="5" fill="none" stroke="{T["brand"]}" stroke-width="1.5" opacity="0">'
           f'<animate attributeName="r" values="5;15" dur="2.6s" begin="{base + 1.7:.1f}s" repeatCount="indefinite"/>'
           f'<animate attributeName="opacity" values="0.7;0" dur="2.6s" begin="{base + 1.7:.1f}s" repeatCount="indefinite"/>'
           f"</circle>")
    sub = mix(T["text"], T["muted"], 0.3)
    for i, line in enumerate(wrap("text", TAGLINE, 20, right - 48, 2)):
        c.text(48, 196 + i * 28, line, "text", 20, sub, kind="rise", delay=base + 0.65 + i * 0.1)
    return c


def build_tile(spec, base):
    T = THEME
    w, h = 270, 104
    c = Card(f"tile-{spec['key']}", h, f"{spec['title']}: {spec['sub']}",
             f"Link tile for {spec['title']}", w=w)
    c.frame(16)
    c.add(c.node("path", "draw", base + 0.15, var={"len": 54}, d="M24 26V78", fill="none",
                 stroke=T["brand"], stroke_width=3, stroke_linecap="round"))
    c.text(44, 55, spec["title"], "display", 26, T["text"], kind="rise", delay=base + 0.1)
    size = fit("text", spec["sub"], 14, w - 44 - 20)
    c.text(44, 79, spec["sub"], "text", size, T["muted"], kind="rise", delay=base + 0.2)
    ax, ay = w - 38, 24   # corner arrow, drawn as a shape so no glyph is needed
    c.add(c.node("path", "fade", base + 0.35, d=f"M{ax} {ay + 12}L{ax + 12} {ay}M{ax + 2} {ay}H{ax + 12}V{ay + 10}",
                 fill="none", stroke=T["brand"], stroke_width=2, stroke_linecap="round", stroke_linejoin="round"))
    return c


def build_chart(st, base):
    T = THEME
    h = 340
    months = st["months"]
    c = Card("contributions", h, "Contributions per month",
             f"{num(st['total'])} contributions from {fmt_date(st['start'])} to {fmt_date(st['end'])}. "
             f"Current streak {st['current']} days, {st['active']} active days, "
             f"busiest day {st['busiest'][1]} contributions on {fmt_date(st['busiest'][0])}.")
    c.frame(14)
    c.text(48, 58, "Contributions per month", "display", 26, T["text"], kind="rise", delay=base)
    c.text(48, 82, f"{num(st['total'])} from {fmt_date(st['start'])} to {fmt_date(st['end'])}",
           "text", 14, T["muted"], kind="rise", delay=base + 0.1)

    x0, x1, by, hmax = 48, 572, 290, 150
    pitch = (x1 - x0) / len(months)
    bw = min(28, pitch * 0.62)
    maxv = max(v for *_, v in months) or 1
    best = max(range(len(months)), key=lambda i: months[i][2])
    c.add(c.node("line", "fade", base + 0.2, x1=x0, x2=x1, y1=by + 0.5, y2=by + 0.5, stroke=T["line"]))
    dim = mix(T["surface"], T["brand"], 0.55)
    for i, (y, m, v) in enumerate(months):
        cx = x0 + pitch * (i + 0.5)
        d0 = base + 0.3 + i * 0.05
        if v:
            bh = max(6.0, v / maxv * hmax)
            xl, xr, ty, r = cx - bw / 2, cx + bw / 2, by - bh, 5
            c.add(c.node("path", "grow", d0, fill=T["brand"] if i == best else dim,
                         d=f"M{xl:.1f} {by}V{ty + r:.1f}Q{xl:.1f} {ty:.1f} {xl + r:.1f} {ty:.1f}"
                           f"H{xr - r:.1f}Q{xr:.1f} {ty:.1f} {xr:.1f} {ty + r:.1f}V{by}Z"))
            ly = ty - 8
        else:
            ly = by - 8
        c.text(round(cx, 1), round(ly, 1), num(v), "text", 12, T["text"] if i == best else T["muted"],
               anchor="middle", kind="fade", delay=d0 + 0.6, tabular=True)
        c.text(round(cx, 1), by + 22, MONTHS[m - 1], "text", 13, T["muted"], anchor="middle",
               kind="fade", delay=d0 + 0.2)
        if i == 0 or m == 1:
            c.text(round(cx, 1), by + 38, str(y), "text", 11, mix(T["muted"], T["surface"], 0.35),
                   anchor="middle", kind="fade", delay=d0 + 0.2)

    c.add(c.node("line", "fade", base + 0.3, x1=600, x2=600, y1=108, y2=312, stroke=T["line"]))
    bdate, bd = st["busiest"]
    blocks = [
        (plural(st["current"], "day"), f"Current streak, longest {st['longest']}"),
        (num(st["active"]), f"Active days of {st['span']}"),
        (num(bd), f"Busiest day, {MONTHS[bdate.month - 1]} {bdate.day}, {bdate.year}"),
    ]
    for i, (value, label) in enumerate(blocks):
        y0 = 138 + i * 76
        d0 = base + 0.5 + i * 0.14
        c.text(632, y0, value, "display", 38, T["brand"], kind="rise", delay=d0, tabular=True)
        c.text(632, y0 + 24, label, "text", 13, T["muted"], kind="rise", delay=d0 + 0.08)
    return c


def build_repo(repo, base):
    T = THEME
    h = 156
    name = repo["name"]
    desc = repo.get("description") or "No description yet."
    c = Card(f"repo-{name}", h, name, desc)
    c.frame(14)
    c.add(c.node("path", "draw", base + 0.1, var={"len": 94}, d="M24 30V124", fill="none",
                 stroke=T["brand"], stroke_width=3, stroke_linecap="round"))
    c.text(48, 56, name, "display", fit("display", name, 28, W - 96), T["text"], kind="rise", delay=base)
    for i, line in enumerate(wrap("text", desc, 16, 640, 2)):
        c.text(48, 84 + i * 22, line, "text", 16, mix(T["text"], T["muted"], 0.35),
               kind="rise", delay=base + 0.12 + i * 0.07)

    x, my, d0 = 48, 134, base + 0.35
    if repo.get("language"):
        c.add(c.node("circle", "fade", d0, cx=x + 5, cy=my - 4, r=5, fill=repo.get("languageColor") or T["brand"]))
        c.text(x + 18, my, repo["language"], "strong", 13, T["text"], kind="fade", delay=d0)
        x += 18 + ROLES["strong"].width(repo["language"], 13) + 26
    # star
    star = "M8 .8l2.3 4.7 5.2.8-3.8 3.6.9 5.1L8 12.5l-4.6 2.5.9-5.1L.5 6.3l5.2-.8z"
    c.add(c.node("path", "fade", d0 + 0.05, d=star, transform=f"translate({x} {my - 13}) scale(.8)",
                 fill="none", stroke=T["muted"], stroke_width=1.4, stroke_linejoin="round"))
    stars = num(int(repo.get("stars", 0)))
    c.text(x + 18, my, stars, "text", 13, T["muted"], kind="fade", delay=d0 + 0.05, tabular=True)
    x += 18 + ROLES["text"].width(stars, 13) + 22
    # fork
    c.add(c.node("path", "fade", d0 + 0.1, fill="none", stroke=T["muted"], stroke_width=1.4,
                 transform=f"translate({x} {my - 13})", stroke_linecap="round",
                 d="M3 3m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0M11 3m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0"
                   "M7 13m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0M3 5v1.5Q3 8 5 8h4q2 0 2-1.5V5M7 8v3"))
    forks = num(int(repo.get("forks", 0)))
    c.text(x + 18, my, forks, "text", 13, T["muted"], kind="fade", delay=d0 + 0.1, tabular=True)
    pushed = repo.get("pushedAt", "")[:10]
    if pushed:
        pd = dt.date.fromisoformat(pushed)
        c.text(W - 48, my, f"Updated {MONTHS[pd.month - 1]} {pd.year}", "text", 13, T["muted"],
               anchor="end", kind="fade", delay=d0 + 0.15)
    return c


def build_graph(days, st, base):
    T = THEME
    h = 270
    c = Card("graph", h, "Contribution graph",
             f"Daily contributions from {fmt_date(st['start'])} to {fmt_date(st['end'])}, "
             f"{num(st['total'])} in total.")
    c.frame(14)
    c.text(48, 58, "Contribution graph", "display", 26, T["text"], kind="rise", delay=base)
    c.text(48, 82, f"{num(st['total'])} contributions, one square per day", "text", 14, T["muted"],
           kind="rise", delay=base + 0.1)

    cell, pitch, gx, gy = 10, 13, 92, 126
    nz = sorted(cnt for _, cnt in days if cnt)
    q = [nz[int(len(nz) * f)] for f in (0.25, 0.5, 0.75)] if nz else [1, 2, 3]
    shades = [mix(T["surface"], T["text"], 0.07)] + [mix(T["surface"], T["brand"], t) for t in (0.3, 0.55, 0.78, 1.0)]

    def level(cnt):
        return 0 if cnt == 0 else 1 + sum(cnt > t for t in q)

    off = (days[0][0].weekday() + 1) % 7      # grid starts on Sunday
    ncols = (len(days) + off + 6) // 7
    cols = [[] for _ in range(ncols)]
    for i, (d, cnt) in enumerate(days):
        cols[(i + off) // 7].append(((i + off) % 7, d, cnt))

    last_label_col, last_month = -9, None
    for ci, col in enumerate(cols):
        gxc = gx + ci * pitch
        rects = "".join(
            f'<rect x="{gxc}" y="{gy + row * pitch}" width="{cell}" height="{cell}" rx="2.5" '
            f'fill="{shades[level(cnt)]}"/>' for row, d, cnt in col)
        c.add(c.node("g", "sweep", base + 0.3 + ci * 0.016, body=rects))
        month = col[0][1].month
        if month != last_month and ci - last_label_col >= 3 and ci <= ncols - 3:
            c.text(gxc, gy - 12, MONTHS[month - 1], "text", 12, T["muted"], kind="fade", delay=base + 0.3 + ci * 0.016)
            last_label_col = ci
        last_month = month
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.text(48, gy + row * pitch + 9, label, "text", 11, T["muted"], kind="fade", delay=base + 0.3)

    last_col = ncols - 1       # pulse marks the most recent day
    px, py = gx + last_col * pitch, gy + cols[-1][-1][0] * pitch
    c.smil(f'<rect x="{px - 2}" y="{py - 2}" width="{cell + 4}" height="{cell + 4}" rx="4" fill="none" '
           f'stroke="{T["brand"]}" stroke-width="1.5" opacity="0">'
           f'<animate attributeName="opacity" values="0;1;0" dur="2.6s" begin="{base + 1.6:.1f}s" repeatCount="indefinite"/>'
           f"</rect>")

    ly, right = 246, W - 48
    more_w = ROLES["text"].width("More", 11)
    sq0 = right - more_w - 8 - 5 * pitch + (pitch - cell)
    c.text(right, ly + 9, "More", "text", 11, T["muted"], anchor="end", kind="fade", delay=base + 0.5)
    c.text(sq0 - 8, ly + 9, "Less", "text", 11, T["muted"], anchor="end", kind="fade", delay=base + 0.5)
    for i, col in enumerate(shades):
        c.add(c.node("rect", "fade", base + 0.5, x=round(sq0 + i * pitch, 1), y=ly, width=cell, height=cell,
                     rx=2.5, fill=col))
    return c


# ────────────────────────────────── output ───────────────────────────────────

def write_preview(names):
    tiles = " ".join(f'<img src="{n}.svg" width="32.5%" alt="">' for n in names if n.startswith("tile-"))
    stack = "".join(f'<p><img src="{n}.svg" width="100%" alt=""></p>' for n in names
                    if not n.startswith("tile-") and n != "hero")
    html = (f'<!doctype html><meta charset="utf-8"><title>Profile preview (final frame)</title>'
            f'<body style="margin:0;background:#0d1117;font:16px/1.5 system-ui">'
            f'<main style="width:{W}px;margin:0 auto;padding:24px 0">'
            f'<p><img src="hero.svg" width="100%" alt=""></p><p style="font-size:16px">{tiles}</p>{stack}'
            f"</main>")
    (OUT / "preview.html").write_text(html, encoding="utf-8")


def main():
    days = load_days()
    st = compute_stats(days)
    repos = load_featured()

    cards = [build_hero(T_HERO)]
    cards += [build_tile(t, T_TILES + i * 0.15) for i, t in enumerate(TILES)]
    cards.append(build_chart(st, T_CHART))
    cards += [build_repo(r, T_REPOS + i * REPO_STEP) for i, r in enumerate(repos)]
    cards.append(build_graph(days, st, T_REPOS + len(repos) * REPO_STEP + 0.1))

    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.svg"):
        old.unlink()
    for card in cards:
        path = OUT / f"{card.name}.svg"
        path.write_text(card.render(), encoding="utf-8")
        print(f"  {path.relative_to(ROOT)}  {path.stat().st_size / 1024:.1f} KB")
    if STATIC:
        write_preview([c.name for c in cards])
        print(f"STATIC=1: final frame written to {OUT.relative_to(ROOT)}/ (open preview.html to screenshot)")


if __name__ == "__main__":
    main()
