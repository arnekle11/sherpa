"""Inline SVG bar charts (no JavaScript, no inline styles: colours come from CSS classes)."""
from __future__ import annotations

import html
import math
from collections.abc import Callable, Sequence

_e = html.escape


def nice_top(v: float) -> float:
    """Smallest 1/2/5 x 10^k that is >= v (and > 0)."""
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        top = m * 10 ** exp
        if top >= v - 1e-9:
            return float(top)
    return float(10 ** (exp + 1))


def _fmt(v: float, unit: str) -> str:
    if unit == "%":
        return f"{v:g}%"
    return f"{v:g}{unit}"


def vbars(values: Sequence[float | None], *, labels: Sequence[str] | None = None, label_every: int = 1,
          titles: Sequence[str] | None = None, unit: str = "", top: float | None = None,
          cls: Callable[[float], str] | None = None, none_title: str = "no data",
          width: int = 680, height: int = 160, aria: str = "bar chart") -> str:
    """Vertical bars. None = no data: drawn as a full-height pale bar so gaps are visible, not hidden."""
    L, R, T, B = 44, 8, 10, 22 if labels else 8
    pw, ph = width - L - R, height - T - B
    real = [v for v in values if v is not None]
    top = top or nice_top(max(real) if real else 1)
    wide = " wide" if width >= 600 else ""  # phone CSS enlarges text on charts that get scaled down a lot
    out = [f'<svg class="chart{wide}" viewBox="0 0 {width} {height}" role="img" aria-label="{_e(aria)}">']
    for frac in (0, .5, 1):
        y = T + ph * (1 - frac)
        out.append(f'<line class="grid" x1="{L}" x2="{width - R}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="ax" x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{_e(_fmt(round(top * frac, 6), unit))}</text>')
    n = max(len(values), 1)
    slot = pw / n
    bw = max(1.0, slot * (0.7 if n < 40 else 0.84))
    for i, v in enumerate(values):
        x = L + i * slot + (slot - bw) / 2
        tip = titles[i] if titles else ""
        if v is None:
            out.append(f'<rect class="bar none" x="{x:.1f}" y="{T}" width="{bw:.1f}" height="{ph:.1f}" rx="1">'
                       f'<title>{_e(tip + (": " if tip else "") + none_title)}</title></rect>')
            continue
        h = max(1.5, ph * min(max(v, 0), top) / top) if v > 0 else 0
        c = cls(v) if cls else ""
        if h:
            out.append(f'<rect class="bar {c}" x="{x:.1f}" y="{T + ph - h:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="1.5">'
                       f'<title>{_e(tip + (": " if tip else "") + _fmt(round(v, 2), unit))}</title></rect>')
        if labels and i % label_every == 0:
            out.append(f'<text class="ax" x="{x + bw / 2:.1f}" y="{height - 6}" text-anchor="middle">{_e(labels[i])}</text>')
    out.append("</svg>")
    return "".join(out)


def hbars(rows: Sequence[tuple[str, float]], *, unit: str = "", width: int = 680, row_h: int = 28,
          label_w: int = 230, aria: str = "bar chart") -> str:
    """Horizontal bars with the label on the left and the value at the end."""
    if not rows:
        return ""
    top = max(v for _, v in rows) or 1
    pw = width - label_w - 56
    h = row_h * len(rows) + 4
    wide = " wide" if width >= 600 else ""
    out = [f'<svg class="chart{wide}" viewBox="0 0 {width} {h}" role="img" aria-label="{_e(aria)}">']
    for i, (label, v) in enumerate(rows):
        y = 2 + i * row_h
        bw = max(2.0, pw * v / top)
        fit = max(6, int((label_w - 14) / 7.4))  # measured: ~7 px per character at 12 px
        short = label if len(label) <= fit else label[:fit - 1].rstrip() + "…"
        out.append(f'<text class="lbl" x="{label_w - 10}" y="{y + row_h / 2 + 4:.1f}" text-anchor="end"><title>{_e(label)}</title>{_e(short)}</text>'
                   f'<rect class="bar" x="{label_w}" y="{y + 5}" width="{bw:.1f}" height="{row_h - 10}" rx="3"/>'
                   f'<text class="val" x="{label_w + bw + 8:.1f}" y="{y + row_h / 2 + 4:.1f}">{_e(_fmt(round(v, 2), unit))}</text>')
    out.append("</svg>")
    return "".join(out)
