"""Visual system: dark navy intelligence-console look, applied once per run."""
from __future__ import annotations

import html

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, .stText, button, input, textarea { font-family: 'Inter', system-ui, sans-serif; }
.block-container { padding-top: 3.2rem; padding-bottom: 2.5rem; max-width: 1500px; }
[data-testid="stSidebar"] { background: #0d1628; border-right: 1px solid #1e2a44; }
[data-testid="stSidebar"] .block-container { padding-top: 1rem; }
h1, h2, h3 { letter-spacing: -0.01em; }
h2 { font-size: 1.35rem !important; margin-top: .4rem !important; }
h3 { font-size: 1.08rem !important; color: #cbd5e1; }
.px-top { display:flex; align-items:center; justify-content:space-between; gap:1rem; padding: .85rem 1.1rem;
  border:1px solid #1e2a44; border-radius:12px; background:#0f1a30; margin-bottom: 1rem; }
.px-top .brand { font-weight:700; font-size:1.25rem; color:#f8fafc; letter-spacing:.06em; }
.px-top .brand span { color:#22d3ee; }
.px-top .tag { color:#94a3b8; font-size:.85rem; }
.px-top .ctx { text-align:right; color:#cbd5e1; font-size:.88rem; }
.px-top .ctx b { color:#f8fafc; }
.px-chip { display:inline-block; padding:2px 9px; border-radius:999px; font-size:.72rem; font-weight:600;
  border:1px solid #1e3a5f; background:#0c2340; color:#7dd3fc; margin: 0 4px 4px 0; }
.px-chip.warn { background:#2a1f0a; border-color:#5c4410; color:#fbbf24; }
.px-chip.ok { background:#0a2a1f; border-color:#14532d; color:#4ade80; }
.px-chip.bad { background:#2a0f12; border-color:#5f1d24; color:#f87171; }
.px-chip.live { background:#0b2a2f; border-color:#155e75; color:#22d3ee; }
.px-cards { display:grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap:.7rem; margin:.2rem 0 1rem; }
.px-card { border:1px solid #1e2a44; border-radius:10px; padding:.75rem .9rem; background:#111a2e; }
.px-card .lbl { color:#94a3b8; font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; }
.px-card .val { color:#f8fafc; font-size:1.55rem; font-weight:700; margin-top:.15rem; line-height:1.2; }
.px-card .sub { color:#64748b; font-size:.75rem; margin-top:.2rem; }
.px-card.accent { border-left:3px solid #3b82f6; }
.px-card.alert { border-left:3px solid #f97316; }
.px-card.cyan { border-left:3px solid #22d3ee; }
.px-note { border:1px solid #1e2a44; border-left:3px solid #22d3ee; background:#0f1a30; border-radius:8px;
  padding:.6rem .85rem; color:#cbd5e1; font-size:.86rem; margin:.4rem 0 .8rem; }
.px-note.warn { border-left-color:#f59e0b; }
.px-find { border:1px solid #1e2a44; border-radius:10px; padding:.7rem .9rem; background:#111a2e; margin-bottom:.5rem; }
.px-find b { color:#f8fafc; }
.px-muted { color:#94a3b8; font-size:.82rem; }
[data-testid="stMetricValue"] { font-size: 1.5rem; }
div[data-testid="stDataFrame"] { border:1px solid #1e2a44; border-radius:8px; }
.stTabs [data-baseweb="tab-list"] { gap: .3rem; }
footer { visibility: hidden; }
</style>
"""


def esc(s) -> str:
    return html.escape(str(s))


def card(label, value, sub="", tone="accent") -> str:
    return (f'<div class="px-card {tone}"><div class="lbl">{esc(label)}</div><div class="val">{esc(value)}</div>'
            + (f'<div class="sub">{esc(sub)}</div>' if sub else "") + "</div>")


def cards(items) -> str:
    return '<div class="px-cards">' + "".join(card(*i) for i in items) + "</div>"


def chip(text, tone="") -> str:
    return f'<span class="px-chip {tone}">{esc(text)}</span>'


def note(text_html, warn=False) -> str:
    return f'<div class="px-note{" warn" if warn else ""}">{text_html}</div>'
