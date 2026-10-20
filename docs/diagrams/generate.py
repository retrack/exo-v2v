"""Generate the exo-v2v documentation diagrams (diagram-design skill, Exoscale skin).

Writes self-contained HTML files (the source of truth); SVGs are exported from them
with the skill's export_svg.py.
"""
import sys
from pathlib import Path

OUT = Path(sys.argv[1])

# --- Exoscale skin (style-guide.md, light) ---
PAPER, PAPER2, INK, MUTED, SOFT = "#fafafa", "#f2f2f2", "#212121", "#616161", "#5f6368"
ACCENT, LINK = "#da291c", "#2f5d8c"
ACCENT_TINT = "rgba(218,41,28,0.08)"
SANS = "'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
MONO = "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
SERIF = "'Instrument Serif', Georgia, 'Times New Roman', serif"

TREAT = {  # node type -> (fill, stroke, tag stroke, tag text)
    "focal": (ACCENT_TINT, ACCENT, "rgba(218,41,28,0.50)", ACCENT),
    "backend": ("#ffffff", INK, "rgba(33,33,33,0.40)", INK),
    "store": ("rgba(33,33,33,0.05)", MUTED, "rgba(97,97,97,0.50)", MUTED),
    "external": ("rgba(33,33,33,0.03)", "rgba(33,33,33,0.30)", "rgba(33,33,33,0.22)", SOFT),
    "input": ("rgba(97,97,97,0.10)", SOFT, "rgba(95,99,104,0.40)", SOFT),
    # chart series (bars): BIOS = muted, UEFI = link blue
    "bios": ("rgba(97,97,97,0.15)", MUTED, None, None),
    "uefi": ("rgba(47,93,140,0.18)", LINK, None, None),
}


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def lw(text, size=8):
    """Arrow-label mask width: 0.62em per mono char + 8px padding, rounded up to 4."""
    w = len(text) * size * 0.62 + 8
    return int(-(-w // 4) * 4)


def node(x, y, w, h, kind, tag, name, sub=None, sub2=None):
    fill, stroke, tstroke, ttext = TREAT[kind]
    tw = lw(tag, 7) if len(tag) > 3 else 28
    cx = x + w / 2
    ny = y + h / 2 - (-8 if sub is None else -4 if sub2 is None else 2) - 8  # name baseline - 8
    s = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{PAPER}"/>',
         f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1"/>',
         f'<rect x="{x + 8}" y="{y + 6}" width="{tw}" height="12" rx="2" fill="none" stroke="{tstroke}" stroke-width="0.8"/>',
         f'<text x="{x + 8 + tw / 2}" y="{y + 15}" fill="{ttext}" font-size="7" font-family="{MONO}" text-anchor="middle" letter-spacing="0.08em">{esc(tag)}</text>',
         f'<text x="{cx}" y="{ny + 8}" fill="{INK}" font-size="12" font-weight="600" font-family="{SANS}" text-anchor="middle">{esc(name)}</text>']
    if sub:
        s.append(f'<text x="{cx}" y="{ny + 24}" fill="{MUTED}" font-size="9" font-family="{MONO}" text-anchor="middle">{esc(sub)}</text>')
    if sub2:
        s.append(f'<text x="{cx}" y="{ny + 37}" fill="{MUTED}" font-size="9" font-family="{MONO}" text-anchor="middle">{esc(sub2)}</text>')
    return "\n".join(s)


def label(cx, y_line, text, color=SOFT, side="above", x_line=None):
    """Masked arrow label. above: centred over a horizontal stroke at y_line (8px gap).
    right/left: beside a vertical stroke at x_line, vertically centred on y_line."""
    w = lw(text)
    if side == "above":
        x, y = cx - w / 2, y_line - 20
    elif side == "below":
        x, y = cx - w / 2, y_line + 8
    elif side == "right":
        x, y = x_line + 8, y_line - 6
    else:
        x, y = x_line - 8 - w, y_line - 6
    return (f'<rect x="{x}" y="{y}" width="{w}" height="12" rx="2" fill="{PAPER}"/>'
            f'<text x="{x + w / 2}" y="{y + 9}" fill="{color}" font-size="8" font-family="{MONO}" text-anchor="middle" letter-spacing="0.06em">{esc(text)}</text>')


def path(d, kind="muted", dashed=False, width=None):
    color, marker = {"muted": (MUTED, "arrow"), "accent": (ACCENT, "arrow-accent"), "link": (LINK, "arrow-link")}[kind]
    w = width or (1.4 if kind == "accent" else 1 if dashed else 1.2)
    dash = ' stroke-dasharray="4,3"' if dashed else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{w}"{dash} marker-end="url(#{marker})"/>'


def zone(x, y, w, h, text):
    tw = lw(text, 7) + 4
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="rgba(33,33,33,0.02)" stroke="rgba(33,33,33,0.10)" stroke-width="0.8"/>'
            f'<rect x="{x + 12}" y="{y + 4}" width="{tw}" height="12" rx="2" fill="{PAPER}"/>'
            f'<text x="{x + 12 + tw / 2}" y="{y + 13}" fill="{SOFT}" font-size="7" font-family="{MONO}" text-anchor="middle" letter-spacing="0.14em">{esc(text)}</text>')


def legend(y, x0, width, items):
    s = [f'<line x1="{x0}" y1="{y - 8}" x2="{x0 + width}" y2="{y - 8}" stroke="rgba(33,33,33,0.10)" stroke-width="0.8"/>',
         f'<text x="{x0}" y="{y + 8}" fill="{MUTED}" font-size="8" font-family="{MONO}" letter-spacing="0.18em">LEGEND</text>']
    x = x0
    for kind, text in items:
        yy = y + 28
        if kind in TREAT:
            fill, stroke, _, _ = TREAT[kind]
            s.append(f'<rect x="{x}" y="{yy - 8}" width="14" height="10" rx="2" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
        else:
            k, dashed = (kind[:-7], True) if kind.endswith("-dashed") else (kind, False)
            s.append(path(f"M {x},{yy - 3} H {x + 28}", k, dashed))
        tx = x + (20 if kind in TREAT else 36)
        s.append(f'<text x="{tx}" y="{yy}" fill="{MUTED}" font-size="8.5" font-family="{SANS}">{esc(text)}</text>')
        x = tx + len(text) * 8.5 * 0.6 + 28
    return "\n".join(s)


def lifeline(cx, y0, y1):
    return f'<line x1="{cx}" y1="{y0}" x2="{cx}" y2="{y1}" stroke="rgba(33,33,33,0.20)" stroke-width="1" stroke-dasharray="3,3"/>'


def msg(x1, x2, y, text, kind="muted", ret=False, at=None):
    """Sequence message: horizontal arrow with its label above, centred on `at` (default: midpoint)."""
    s = path(f"M {x1},{y} H {x2}", kind, dashed=ret)
    color = {"muted": SOFT, "accent": ACCENT, "link": LINK}[kind]
    return s + label(at or (x1 + x2) / 2, y, text, color)


def selfmsg(cx, y, text):
    """Self-message: a U-loop to the right of the lifeline, label right of the loop."""
    d = f"M {cx + 4},{y} H {cx + 28} Q {cx + 36},{y} {cx + 36},{y + 8} Q {cx + 36},{y + 16} {cx + 28},{y + 16} H {cx + 6}"
    w = lw(text)
    return (path(d, "muted") +
            f'<rect x="{cx + 44}" y="{y + 2}" width="{w}" height="12" rx="2" fill="{PAPER}"/>'
            f'<text x="{cx + 44 + w / 2}" y="{y + 11}" fill="{SOFT}" font-size="8" font-family="{MONO}" text-anchor="middle" letter-spacing="0.06em">{esc(text)}</text>')


def actor(cx, y, w, kind, tag, name, sub):
    return node(cx - w / 2, y, w, 56, kind, tag, name, sub)


def page(slug, eyebrow, title, desc, vbw, vbh, body, caption, y0=0):
    """y0 trims the empty band above the content: the viewBox starts at y0."""
    defs = f"""<marker id="arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{MUTED}"/></marker>
          <marker id="arrow-accent" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{ACCENT}"/></marker>
          <marker id="arrow-link" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{LINK}"/></marker>"""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(title)}</title>
  <!-- diagram-design skill, Exoscale skin, font source: system (no network requests) -->
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --color-paper:  {PAPER};
      --color-ink:    {INK};
      --color-muted:  {MUTED};
      --color-accent: {ACCENT};
      --font-sans:  {SANS};
      --font-serif: {SERIF};
      --font-mono:  {MONO};
    }}
    body {{ font-family: var(--font-sans); background: var(--color-paper); color: var(--color-ink);
           min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 3rem 2rem; }}
    .frame {{ max-width: 1200px; width: 100%; }}
    .diagram-container {{ width: 100%; overflow-x: auto; }}
    .eyebrow {{ font-family: var(--font-mono); font-size: 0.66rem; font-weight: 500; letter-spacing: 0.18em;
               text-transform: uppercase; color: var(--color-muted); margin-bottom: 0.5rem; }}
    h1 {{ font-family: var(--font-serif); font-size: clamp(1.5rem, 2.4vw + 0.75rem, 2rem); font-weight: 400;
         letter-spacing: -0.02em; line-height: 1.15; color: var(--color-ink); margin-bottom: 1.5rem; }}
    .caption {{ font-family: var(--font-serif); font-style: italic; font-size: 1rem; color: var(--color-muted);
               margin-top: 1rem; max-width: 60ch; }}
    svg {{ width: 100%; min-width: {vbw}px; display: block; }}
    @media print {{ .diagram-container {{ overflow-x: visible; }} svg {{ min-width: 0; }} }}
  </style>
</head>
<body>
  <div class="frame">
    <p class="eyebrow">{esc(eyebrow)}</p>
    <h1>{esc(title)}</h1>
    <div class="diagram-container">
      <svg viewBox="0 {y0} {vbw} {vbh - y0}" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="{slug}-title {slug}-desc">
        <title id="{slug}-title">{esc(title)}</title>
        <desc id="{slug}-desc">{esc(desc)}</desc>
        <defs>
          {defs}
        </defs>
        <rect x="0" y="{y0}" width="{vbw}" height="{vbh - y0}" fill="{PAPER}"/>
{body}
      </svg>
    </div>
    <p class="caption">{esc(caption)}</p>
  </div>
</body>
</html>
"""
    (OUT / f"{slug}.html").write_text(html)


# ---------------------------------------------------------------- 1. architecture
def architecture():
    b = []
    b.append(zone(48, 80, 384, 360, "VSPHERE · ON-PREMISES"))
    b.append(zone(552, 80, 368, 360, "EXOSCALE ZONE"))
    # arrows first
    b.append(path("M 144,192 V 284"))                                   # VM -> datastore
    b.append(label(None, 238, "VMDK", side="right", x_line=144))
    b.append(path("M 256,160 H 220", "link"))                            # vSphere -> VM (snapshot)
    b.append(label(238, 160, "SNAP", LINK))
    b.append(path("M 336,288 V 196", "link"))                            # appliance -> vSphere API
    b.append(label(None, 240, "VSPHERE API", LINK, side="right", x_line=336))
    b.append(path("M 216,320 H 252"))                                     # datastore -> appliance (hot-add)
    b.append(label(236, 320, "READ", SOFT, side="below"))
    b.append(path("M 416,304 H 472 Q 480,304 480,296 V 168 Q 480,160 488,160 H 580", "link"))  # appliance -> Exoscale API
    b.append(label(None, 232, "API :443", LINK, side="right", x_line=480))
    b.append(path("M 416,336 H 580", "accent"))                           # tunnel
    b.append(label(498, 336, "SSH TUNNEL :22", ACCENT, side="below"))
    b.append(path("M 656,192 V 252", "link", dashed=True))               # API -> instance
    b.append(label(None, 222, "CREATE · SCALE", LINK, side="right", x_line=656))
    b.append(path("M 792,176 H 760 Q 752,176 752,184 V 260 Q 752,268 744,268 H 732", "muted", dashed=True))  # template -> instance
    b.append(label(None, 222, "BOOT IMAGE", SOFT, side="right", x_line=752))
    b.append(path("M 728,296 H 788"))                                     # instance -> root disk
    b.append(label(760, 296, "VDA", SOFT))
    b.append(path("M 728,392 H 788"))                                     # instance -> volumes
    b.append(label(760, 392, "VDB…", SOFT))
    # nodes
    b.append(node(72, 128, 144, 64, "input", "VM", "Source VM", "running workload"))
    b.append(node(256, 128, 160, 64, "external", "API", "vCenter / ESXi", "snapshots · CBT"))
    b.append(node(72, 288, 144, 64, "store", "DS", "Datastore", "VMDK files"))
    b.append(node(256, 288, 160, 64, "focal", "CLI", "Replication appliance", "exo-v2v · Linux VM"))
    b.append(node(584, 128, 144, 64, "external", "API", "Exoscale API", "compute"))
    b.append(node(584, 256, 144, 168, "backend", "VM", "Target instance", "agent in RAM", "NBD · convert"))
    b.append(node(792, 128, 112, 64, "store", "TPL", "Template", "exo-v2v receiver"))
    b.append(node(792, 264, 112, 64, "store", "DISK", "Root disk", "512 B sectors"))
    b.append(node(792, 360, 112, 64, "store", "VOL", "Volumes", "4 KiB sectors"))
    b.append(legend(472, 48, 872, [("focal", "exo-v2v"), ("input", "Source"), ("backend", "Target"), ("store", "Disk · image"), ("external", "Control API"),
                                    ("accent", "Replicated data"), ("link", "API call"), ("muted", "Disk access")]))
    page("architecture", "Architecture · exo-v2v", "How exo-v2v moves a running VM",
         "The replication appliance reads frozen snapshots of the source VM's disks inside vSphere and writes them "
         "through an SSH tunnel onto the disks of the target Exoscale instance, whose agent runs from RAM, "
         "booted from a receiver template or rebuilt from a stock template.",
         960, 528, "\n".join(b),
         "Nothing is staged: the bytes land on the disks the migrated VM will boot from. "
         "Only TCP 22 to the target instance is opened, and only from the appliance.", y0=56)


# ---------------------------------------------------------------- 2. lifecycle (state machine)
def lifecycle():
    b = []
    xs = [96, 272, 448, 624, 800]
    y, w, h = 120, 120, 64
    cy = y + h / 2
    b.append(f'<circle cx="56" cy="{cy}" r="6" fill="{INK}"/>')
    b.append(path(f"M 62,{cy} H 92"))
    names = ["CREATE", "SYNC", "CUTOVER", "CLEANUP"]
    for i, n in enumerate(names):
        x1, x2 = xs[i] + w, xs[i + 1]
        b.append(path(f"M {x1},{cy} H {x2 - 4}", "accent" if n == "CUTOVER" else "muted"))
        b.append(label((x1 + x2) / 2, cy, n, ACCENT if n == "CUTOVER" else SOFT))
    # self-loop above "Replicating"
    lx = xs[2] + w / 2
    b.append(path(f"M {lx - 20},{y} V {y - 28} Q {lx - 20},{y - 36} {lx - 12},{y - 36} H {lx + 12} Q {lx + 20},{y - 36} {lx + 20},{y - 28} V {y - 4}"))
    b.append(label(lx, y - 36, "SYNC --EVERY", SOFT))
    # rollback down from "Cut over"
    rx = xs[3] + w / 2
    b.append(path(f"M {rx},{y + h} V {y + h + 72}", "muted", dashed=True))
    b.append(label(None, y + h + 38, "ROLLBACK", SOFT, side="right", x_line=rx))
    # end: from Cleaned up down to the end ring
    ex = xs[4] + w / 2
    b.append(path(f"M {ex},{y + h} V {y + h + 70}"))
    b.append(f'<circle cx="{ex}" cy="{y + h + 84}" r="8" fill="none" stroke="{INK}" stroke-width="1.2"/>')
    b.append(f'<circle cx="{ex}" cy="{y + h + 84}" r="5" fill="{INK}"/>')
    # states
    b.append(node(xs[0], y, w, h, "backend", "PLAN", "Planned", "nothing created"))
    b.append(node(xs[1], y, w, h, "backend", "CREATE", "Receiver ready", "agent in RAM"))
    b.append(node(xs[2], y, w, h, "backend", "SYNC", "Replicating", "source running"))
    b.append(node(xs[3], y, w, h, "focal", "CUTOVER", "Cut over", "source off"))
    b.append(node(xs[4], y, w, h, "store", "DONE", "Cleaned up", "migration closed"))
    b.append(node(xs[3], y + h + 76, w, h, "input", "BACK", "Rolled back", "source on again"))
    # downtime callout (italic serif, editorial)
    b.append(f'<text x="96" y="300" fill="{MUTED}" font-size="14" font-style="italic" font-family="{SERIF}">'
             'Downtime starts at cutover: shutdown, last delta, conversion, boot.</text>')
    b.append(f'<text x="96" y="320" fill="{MUTED}" font-size="14" font-style="italic" font-family="{SERIF}">'
             'Measured: 2 to 9 minutes. Every step before it runs with the VM online.</text>')
    b.append(legend(368, 40, 880, [("focal", "Downtime state"), ("backend", "VM online"), ("store", "Closed"), ("input", "Source restored"),
                                    ("muted", "Command"), ("muted-dashed", "Fallback")]))
    page("lifecycle", "State machine · exo-v2v", "A migration from plan to cleanup",
         "The migration moves through planned, receiver ready, replicating and cut over states, driven by the "
         "exo-v2v commands create, sync, cutover and cleanup, with rollback available after the cutover.",
         960, 416, "\n".join(b),
         "Each step is resumable. Rollback works until cleanup; data written on Exoscale after the cutover is not copied back.", y0=40)


# ---------------------------------------------------------------- 3. replication cycle (sequence)
def replication():
    b = []
    A, V, R = 160, 480, 800
    top, bottom = 88, 520
    for cx in (A, V, R):
        b.append(lifeline(cx, top + 56, bottom))
    # loop fragment (per disk)
    b.append(f'<rect x="{A - 40}" y="160" width="{R - A + 80}" height="336" rx="4" fill="rgba(33,33,33,0.02)" stroke="rgba(33,33,33,0.22)" stroke-width="1"/>')
    b.append(f'<rect x="{A - 40}" y="160" width="40" height="16" rx="2" fill="{PAPER}" stroke="rgba(33,33,33,0.22)" stroke-width="1"/>')
    b.append(f'<text x="{A - 20}" y="172" fill="{MUTED}" font-size="8" font-family="{MONO}" text-anchor="middle" letter-spacing="0.12em">LOOP</text>')
    b.append(f'<text x="{A + 12}" y="172" fill="{MUTED}" font-size="8" font-family="{MONO}" letter-spacing="0.04em">[for each disk of the VM]</text>')
    # activation bars
    b.append(f'<rect x="{A - 4}" y="196" width="8" height="284" fill="rgba(33,33,33,0.06)" stroke="{MUTED}" stroke-width="0.8"/>')
    ys = [208, 240, 272, 304, 336, 384, 416, 448, 472]
    b.append(msg(A + 4, V - 4, ys[0], "SNAPSHOT", "link"))
    b.append(msg(V - 4, A + 8, ys[1], "FROZEN DISK", "muted", ret=True))
    b.append(msg(A + 4, V - 4, ys[2], "QUERY CBT", "link"))
    b.append(msg(V - 4, A + 8, ys[3], "CHANGED AREAS", "muted", ret=True))
    b.append(msg(A + 4, V - 4, ys[4], "HOT-ADD (R/O)", "link"))
    b.append(selfmsg(A, 352, "READ CHANGES"))
    b.append(msg(A + 4, R - 4, ys[6], "NBD WRITE", "accent", at=(A + V) / 2))
    b.append(msg(A + 4, R - 4, ys[7], "SAVE CHANGEID", "muted", at=(A + V) / 2))
    b.append(msg(A + 4, V - 4, ys[8], "DROP SNAPSHOT", "link"))
    b.append(actor(A, top, 176, "focal", "CLI", "Replication appliance", "exo-v2v sync"))
    b.append(actor(V, top, 176, "external", "API", "vCenter / ESXi", "vSphere API"))
    b.append(actor(R, top, 176, "backend", "VM", "Target instance", "agent in RAM"))
    b.append(legend(552, 40, 880, [("link", "vSphere call"), ("muted-dashed", "Return"), ("accent", "Data to Exoscale"),
                                    ("muted", "Agent call")]))
    page("replication", "Sequence · exo-v2v", "One replication cycle",
         "Each cycle snapshots the VM, asks vSphere which areas changed since the previous cycle, reads them from the "
         "frozen disk hot-added read-only to the appliance, writes them to the target instance over NBD, records the "
         "new change ID on the target and deletes the snapshot.",
         960, 600, "\n".join(b),
         "The first cycle copies every allocated area; later cycles copy only what changed. "
         "The VM keeps running; its snapshot exists only for the duration of the cycle.", y0=64)


# ---------------------------------------------------------------- 4. cutover (sequence)
def cutover():
    b = []
    A, S, R, E = 120, 360, 600, 840
    top, bottom = 88, 600
    for cx in (A, S, R, E):
        b.append(lifeline(cx, top + 56, bottom))
    # opt fragment (relayout)
    b.append(f'<rect x="{A - 32}" y="396" width="{E - A + 64}" height="88" rx="4" fill="rgba(33,33,33,0.02)" stroke="rgba(33,33,33,0.22)" stroke-width="1"/>')
    b.append(f'<rect x="{A - 32}" y="396" width="40" height="16" rx="2" fill="{PAPER}" stroke="rgba(33,33,33,0.22)" stroke-width="1"/>')
    b.append(f'<text x="{A - 12}" y="408" fill="{MUTED}" font-size="8" font-family="{MONO}" text-anchor="middle" letter-spacing="0.12em">OPT</text>')
    b.append(f'<text x="{A + 20}" y="408" fill="{MUTED}" font-size="8" font-family="{MONO}" letter-spacing="0.04em">[XFS-512 or ext 1K data volume]</text>')
    # downtime bracket on the left
    b.append(f'<line x1="40" y1="176" x2="40" y2="576" stroke="{ACCENT}" stroke-width="1.2"/>')
    b.append(f'<line x1="40" y1="176" x2="48" y2="176" stroke="{ACCENT}" stroke-width="1.2"/>')
    b.append(f'<line x1="40" y1="576" x2="48" y2="576" stroke="{ACCENT}" stroke-width="1.2"/>')
    b.append(f'<rect x="28" y="356" width="24" height="44" fill="{PAPER}"/>')
    b.append(f'<text x="40" y="372" fill="{ACCENT}" font-size="7" font-family="{MONO}" text-anchor="middle" letter-spacing="0.08em">DOWN</text>')
    b.append(f'<text x="40" y="384" fill="{ACCENT}" font-size="7" font-family="{MONO}" text-anchor="middle" letter-spacing="0.08em">TIME</text>')
    b.append(msg(A, S - 4, 176, "GUEST SHUTDOWN", "link"))
    b.append(msg(A, R - 4, 216, "FINAL DELTA", "muted", at=(A + S) / 2))
    b.append(msg(A, R - 4, 256, "POST /CONVERT", "muted", at=(A + S) / 2))
    b.append(selfmsg(R, 280, "VIRT-V2V"))
    b.append(selfmsg(R, 320, "FIX-UPS"))
    b.append(msg(R, A + 4, 368, "CONVERTED", "muted", ret=True, at=(A + S) / 2))
    b.append(msg(A, E - 4, 436, "SPARE VOLUME", "link"))
    b.append(msg(A, R - 4, 468, "POST /RELAYOUT", "muted", at=(A + S) / 2))
    b.append(msg(A, E - 4, 512, "SCALE · START", "link"))
    b.append(msg(E, A + 4, 560, "SSH/RDP OPEN", "accent", ret=True))
    b.append(actor(A, top, 160, "focal", "CLI", "Appliance", "exo-v2v cutover"))
    b.append(actor(S, top, 160, "input", "VM", "Source VM", "vSphere"))
    b.append(actor(R, top, 160, "backend", "VM", "Target instance", "agent in RAM"))
    b.append(actor(E, top, 160, "external", "API", "Exoscale API", "compute"))
    b.append(legend(632, 40, 880, [("link", "API call"), ("muted", "Agent call"), ("muted-dashed", "Return"),
                                    ("accent", "Instance up")]))
    page("cutover", "Sequence · exo-v2v", "What happens at cutover",
         "At cutover the appliance shuts the source VM down, copies the last changes, asks the target instance's agent "
         "to convert the guest for KVM and adapt it to Exoscale, rebuilds incompatible data volumes if needed, then "
         "scales and starts the instance until SSH or RDP answers.",
         960, 680, "\n".join(b),
         "The source VM stays off and untouched: exo-v2v rollback powers it back on. "
         "Fix-ups cover network names, 4 KiB-sector volumes and, on Windows, disk onlining.", y0=64)


# ---------------------------------------------------------------- 5. create with a receiver template (sequence)
def create_template():
    b = []
    A, E, R = 160, 480, 800
    top, bottom = 88, 488
    for cx in (A, E, R):
        b.append(lifeline(cx, top + 56, bottom))
    # opt fragment: Windows variant copies the drivers into RAM before SSH starts
    b.append(f'<rect x="{E + 40}" y="284" width="{R - E + 40}" height="48" rx="4" fill="rgba(33,33,33,0.02)" stroke="rgba(33,33,33,0.22)" stroke-width="1"/>')
    b.append(f'<rect x="{E + 40}" y="284" width="40" height="16" rx="2" fill="{PAPER}" stroke="rgba(33,33,33,0.22)" stroke-width="1"/>')
    b.append(f'<text x="{E + 60}" y="296" fill="{MUTED}" font-size="8" font-family="{MONO}" text-anchor="middle" letter-spacing="0.12em">OPT</text>')
    b.append(f'<text x="{E + 88}" y="296" fill="{MUTED}" font-size="8" font-family="{MONO}" letter-spacing="0.04em">[Windows template]</text>')
    b.append(msg(A + 4, E - 4, 176, "CREATE", "link"))
    b.append(msg(E + 4, R - 4, 208, "BOOT TEMPLATE", "link"))
    b.append(selfmsg(R, 228, "RAM ROOT"))
    b.append(selfmsg(R, 256, "USERDATA KEY"))
    b.append(selfmsg(R, 304, "VIRTIO-WIN"))
    b.append(msg(R - 4, A + 4, 368, "SSH UP", "muted", ret=True, at=(A + E) / 2))
    b.append(msg(A + 4, E - 4, 400, "ATTACH VOLUMES", "link"))
    b.append(msg(A + 4, R - 4, 432, "PUSH AGENT", "muted", at=(A + E) / 2))
    b.append(msg(R - 4, A + 4, 464, "AGENT READY", "accent", ret=True, at=(A + E) / 2))
    b.append(actor(A, top, 176, "focal", "CLI", "Replication appliance", "exo-v2v create"))
    b.append(actor(E, top, 176, "external", "API", "Exoscale API", "compute"))
    b.append(actor(R, top, 176, "backend", "VM", "Target instance", "receiver template"))
    b.append(legend(520, 40, 880, [("link", "API call"), ("muted", "SSH"), ("muted-dashed", "Return"), ("accent", "Ready to sync")]))
    page("create-template", "Sequence · exo-v2v", "Creating the receiver from a template",
         "With a receiver template, the target instance boots straight into the RAM-root receiver, installs the "
         "migration's SSH key from its user-data, then exo-v2v attaches the Block Storage volumes and starts the agent.",
         960, 568, "\n".join(b),
         "About one minute from create to ready, against 5 to 15 with a stock template, which installs the tools "
         "first. The disk is never mounted, so the first sync can overwrite it.", y0=64)


# ---------------------------------------------------------------- 6. downtime chart (grouped columns)
DOWNTIME = [  # guest, BIOS s, UEFI s (receiver templates, ch-gva-2, 2026-10-19)
    ("Ubuntu 24.04", 136, 138), ("Debian 13", 253, 306), ("AlmaLinux 9", 355, 396),
    ("Windows Server 2022", 325, 316), ("Windows Server 2025", 405, 513)]


def downtime():
    b = []
    x0, x1, y0, y1, vmax = 80, 920, 64, 400, 600          # plot area; axis 0..600 s
    sc = (y1 - y0) / vmax
    for t in range(0, vmax + 1, 120):                     # gridlines every 2 minutes
        y = y1 - t * sc
        if t:
            b.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="rgba(33,33,33,0.08)" stroke-width="0.8"/>')
        b.append(f'<text x="{x0 - 8}" y="{y + 3}" fill="{MUTED}" font-size="8" font-family="{MONO}" text-anchor="end">{t // 60} MIN</text>')
    b.append(f'<line x1="{x0}" y1="{y0 - 8}" x2="{x0}" y2="{y1}" stroke="rgba(33,33,33,0.25)" stroke-width="1"/>')
    b.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="rgba(33,33,33,0.25)" stroke-width="1"/>')
    pitch, bw, gap = (x1 - x0) / len(DOWNTIME), 52, 8
    for i, (name, bios, uefi) in enumerate(DOWNTIME):
        cx = x0 + pitch * (i + 0.5)
        for j, (v, kind) in enumerate(((bios, "bios"), (uefi, "uefi"))):
            fill, stroke, _, _ = TREAT[kind]
            x = cx - bw - gap / 2 if j == 0 else cx + gap / 2
            h = round(v * sc)
            b.append(f'<rect x="{x}" y="{y1 - h}" width="{bw}" height="{h}" fill="{PAPER}"/>')
            b.append(f'<rect x="{x}" y="{y1 - h}" width="{bw}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
            b.append(f'<text x="{x + bw / 2}" y="{y1 - h - 8}" fill="{MUTED}" font-size="8" font-family="{MONO}" text-anchor="middle">{v} S</text>')
        first, _, rest = name.partition(" ")
        if name.startswith("Windows"):
            first, rest = "Windows Server", name.rsplit(" ", 1)[1]
        b.append(f'<text x="{cx}" y="{y1 + 20}" fill="{INK}" font-size="11" font-weight="600" font-family="{SANS}" text-anchor="middle">{esc(first)}</text>')
        b.append(f'<text x="{cx}" y="{y1 + 34}" fill="{MUTED}" font-size="9" font-family="{MONO}" text-anchor="middle">{esc(rest)}</text>')
    b.append(legend(464, 40, 880, [("bios", "BIOS"), ("uefi", "UEFI")]))
    page("downtime", "Bar chart · exo-v2v", "Downtime at cutover",
         "Measured downtime of live migrations from vSphere to Exoscale, from source shutdown until SSH or RDP answered, "
         "per guest and firmware: Ubuntu about 2 minutes, Debian 4 to 5, AlmaLinux 6 to 7, Windows Server 5 to 9.",
         960, 520, "\n".join(b),
         "Live migrations through receiver templates, ESXi 7.0 U3 to ch-gva-2, up to 8 at once from one appliance. "
         "AlmaLinux includes rebuilding its XFS data volume for 4 KiB sectors.", y0=40)


architecture()
lifecycle()
replication()
cutover()
create_template()
downtime()
print("written:", ", ".join(p.name for p in sorted(OUT.glob("*.html"))))
