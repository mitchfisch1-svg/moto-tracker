"""Render a ~30s animated race-recap video (MP4) from the latest results.

Frames are drawn with Pillow (720x1280 portrait, 24 fps) and stitched with the
ffmpeg binary bundled by imageio-ffmpeg. Content: intro card, 450 podium,
250 podium, championship top-3s, outro with the next race. All from our own
database + the official rider headshots.

Usage (from the project root, DATABASE_URL set):
    python scripts/render_recap_video.py --out recap_out [--force]

Skips rendering when the published recap.json already covers the latest event
(so the scheduled CI run is a cheap no-op between race weekends).
"""

import argparse
import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import get_connection  # noqa: E402

W, H, FPS, DUR = 720, 1280, 24, 30.0
BG = (15, 17, 21)
CARD = (26, 30, 39)
CARD2 = (35, 40, 51)
ORANGE = (255, 90, 31)
DARK_ORANGE = (122, 44, 18)
WHITE = (242, 244, 248)
DIM = (154, 164, 178)
GOLD = (255, 209, 102)
SILVER = (200, 208, 218)
BRONZE = (208, 140, 74)

MEDIA_JSON_URL = (
    "https://raw.githubusercontent.com/mitchfisch1-svg/moto-tracker/media/recap.json"
)
VIDEO_URL = (
    "https://raw.githubusercontent.com/mitchfisch1-svg/moto-tracker/media/recap.mp4"
)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --- fonts (Windows locally, Liberation/DejaVu on CI runners) -----------------
def font(size, italic=False):
    names = (
        ["arialbi.ttf", "LiberationSans-BoldItalic.ttf", "DejaVuSans-BoldOblique.ttf"]
        if italic else
        ["arialbd.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"]
    )
    dirs = [
        "C:/Windows/Fonts",
        "/usr/share/fonts/truetype/liberation",
        "/usr/share/fonts/truetype/dejavu",
    ]
    for d in dirs:
        for n in names:
            try:
                return ImageFont.truetype(f"{d}/{n}", size)
            except OSError:
                continue
    return ImageFont.load_default()


def pos_color(p):
    return {1: GOLD, 2: SILVER, 3: BRONZE}.get(p, ORANGE)


def ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


# --- data ---------------------------------------------------------------------
def load_recap():
    """Latest completed event + per-class podiums + standings top3 + next race."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.id, s.abbrev, e.round_number, e.round_label, e.venue,
                       e.city, e.state, e.event_date
                FROM events e
                JOIN seasons se ON se.id = e.season_id
                JOIN series  s  ON s.id  = se.series_id
                WHERE e.status = 'final'
                  AND EXISTS (SELECT 1 FROM sessions x
                              JOIN results r ON r.session_id = x.id
                              WHERE x.event_id = e.id)
                ORDER BY e.event_date DESC LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return None
            (eid, series, rnd, label, venue, city, state, date) = row
            ev = {"series": series, "round_number": rnd, "round_label": label,
                  "venue": venue, "city": city, "state": state,
                  "event_date": str(date)}

            cur.execute(
                """
                SELECT sess.class, sess.id, r.rider_id, ri.full_name, ri.number,
                       ri.manufacturer, ri.headshot_url, r.position, r.points
                FROM sessions sess
                JOIN results r ON r.session_id = sess.id
                JOIN riders ri ON ri.id = r.rider_id
                WHERE sess.event_id = %s
                """,
                (eid,),
            )
            rows = cur.fetchall()

            cur.execute(
                """
                SELECT st.class, st.position, r.full_name, st.points
                FROM standings st
                JOIN seasons se ON se.id = st.season_id
                JOIN series  s  ON s.id  = se.series_id
                JOIN riders  r  ON r.id  = st.rider_id
                WHERE s.abbrev = %s AND st.position <= 3
                ORDER BY st.class, st.position
                """,
                (series,),
            )
            champ = [{"class": c, "position": p, "full_name": n, "points": pts}
                     for c, p, n, pts in cur.fetchall()]

            cur.execute(
                """
                SELECT s.abbrev, e.venue, e.event_date
                FROM events e
                JOIN seasons se ON se.id = e.season_id
                JOIN series  s  ON s.id  = se.series_id
                WHERE e.event_date >= CURRENT_DATE
                ORDER BY e.event_date LIMIT 1
                """
            )
            nxt = cur.fetchone()
            next_ev = ({"series": nxt[0], "venue": nxt[1], "date": str(nxt[2])}
                       if nxt else None)

    classes = []
    by_class = {}
    for (cls, sid, rid, name, num, make, shot, pos, pts) in rows:
        by_class.setdefault(cls, []).append(
            dict(sid=sid, rid=rid, name=name, num=num, make=make,
                 shot=shot, pos=pos, pts=pts or 0))
    for cls, cr in sorted(by_class.items(), reverse=True):  # 450 first
        last_sid = max(r["sid"] for r in cr)
        agg = {}
        for r in cr:
            a = agg.setdefault(r["rid"], dict(
                name=r["name"], num=r["num"], make=r["make"], shot=r["shot"],
                pts=0, last=999, fins=[]))
            a["pts"] += r["pts"]
            if r["pos"]:
                a["fins"].append(r["pos"])
                if r["sid"] == last_sid:
                    a["last"] = r["pos"]
        ranked = sorted(agg.values(), key=lambda a: (-a["pts"], a["last"]))[:3]
        for i, a in enumerate(ranked, 1):
            a["overall"] = i
            a["fins"] = "-".join(str(f) for f in a["fins"])
        classes.append({"class": cls, "podium": ranked})

    return {"event": ev, "classes": classes, "champ": champ, "next": next_ev}


def fetch_headshot(url, size, border_color):
    """Circular headshot (or None) with a colored ring."""
    if not url:
        return None
    try:
        raw = requests.get(url, timeout=15).content
        src = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None
    src = src.resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(src, (0, 0), mask)
    ImageDraw.Draw(out).ellipse([3, 3, size - 3, size - 3],
                                outline=border_color, width=7)
    return out


def plate_avatar(num, size, border_color):
    """Fallback avatar: number plate circle."""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    d.ellipse([0, 0, size, size], fill=CARD2)
    d.ellipse([3, 3, size - 3, size - 3], outline=border_color, width=7)
    f = font(int(size * 0.34))
    txt = str(num or "?")
    bb = d.textbbox((0, 0), txt, font=f)
    d.text(((size - bb[2] + bb[0]) / 2 - bb[0], (size - bb[3] + bb[1]) / 2 - bb[1]),
           txt, font=f, fill=WHITE)
    return out


# --- drawing helpers ----------------------------------------------------------
def stripes(d):
    d.polygon([(0, int(H * .82)), (W, int(H * .68)), (W, int(H * .705)),
               (0, int(H * .845))], fill=ORANGE)
    d.polygon([(0, int(H * .865)), (W, int(H * .725)), (W, int(H * .74)),
               (0, int(H * .88))], fill=DARK_ORANGE)


def center_text(d, y, text, f, fill, alpha=1.0):
    if alpha <= 0:
        return
    c = (*fill, int(255 * alpha)) if len(fill) == 3 else fill
    bb = d.textbbox((0, 0), text, font=f)
    d.text(((W - (bb[2] - bb[0])) / 2 - bb[0], y), text, font=f, fill=c)


def wordmark(d, y, size=34, alpha=1.0):
    f = font(size, italic=True)
    t1, t2 = "MOTO ", "TRACKER"
    b1 = d.textbbox((0, 0), t1, font=f)
    b2 = d.textbbox((0, 0), t2, font=f)
    total = (b1[2] - b1[0]) + (b2[2] - b2[0])
    x = (W - total) / 2
    d.text((x, y), t1, font=f, fill=(*WHITE, int(255 * alpha)))
    d.text((x + b1[2] - b1[0], y), t2, font=f, fill=(*ORANGE, int(255 * alpha)))


def fmt_date(iso):
    y, m, dd = iso.split("-")
    return f"{MONTHS[int(m) - 1]} {int(dd)}"


def series_long(ab):
    return {"MX": "Pro Motocross", "SX": "Supercross"}.get(ab, "SuperMotocross")


# --- scenes (each draws onto a fresh RGBA canvas for time t in [0,1]) ---------
def scene_intro(data, t):
    img = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(img)
    stripes(d)
    ev = data["event"]
    center_text(d, 300, "RACE RECAP", font(40), ORANGE, ease(t / 0.25))
    slide = int((1 - ease((t - 0.1) / 0.3)) * 60)
    center_text(d, 420 + slide, ev["venue"].upper(), font(74, True), WHITE,
                ease((t - 0.1) / 0.3))
    a2 = ease((t - 0.3) / 0.3)
    center_text(d, 560, f"{series_long(ev['series'])} · "
                        f"{ev['round_label'] or 'Round ' + str(ev['round_number'])}",
                font(30), DIM, a2)
    loc = ", ".join(x for x in (ev["city"], ev["state"]) if x)
    center_text(d, 610, f"{loc} · {fmt_date(ev['event_date'])}", font(30), DIM, a2)
    wordmark(d, 1150, 30, ease((t - 0.4) / 0.3))
    return img


def scene_podium(cdata, avatars, t):
    img = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(img)
    stripes(d)
    center_text(d, 130, f"{cdata['class']} OVERALL", font(40), ORANGE, ease(t / 0.2))

    win = cdata["podium"][0] if cdata["podium"] else None
    if win:
        size = 360
        rise = int((1 - ease((t - 0.05) / 0.3)) * 70)
        av = avatars.get((cdata["class"], 1))
        if av is not None and t > 0.05:
            img.alpha_composite(av, ((W - size) // 2, 230 + rise))
        a = ease((t - 0.2) / 0.3)
        center_text(d, 640, win["name"].upper(), font(52, True), WHITE, a)
        sub = win["make"] or ""
        if win["fins"] and "-" in win["fins"]:
            sub = f"{sub} · Motos {win['fins']}" if sub else f"Motos {win['fins']}"
        center_text(d, 715, sub, font(30), DIM, a)

    for i, p in enumerate(cdata["podium"][1:], start=2):
        base_y = 820 + (i - 2) * 170
        slide_t = ease((t - (0.35 + (i - 2) * 0.12)) / 0.25)
        if slide_t <= 0:
            continue
        x_off = int((1 - slide_t) * 500)
        row_x = 70 + x_off
        av = avatars.get((cdata["class"], i))
        size = 120
        if av is not None:
            img.alpha_composite(av, (row_x, base_y))
        d.text((row_x + 150, base_y + 18), f"{i}", font=font(44),
               fill=pos_color(i))
        d.text((row_x + 210, base_y + 14), p["name"], font=font(34), fill=WHITE)
        d.text((row_x + 210, base_y + 66), p["make"] or "", font=font(26), fill=DIM)
    return img


def scene_champ(data, t):
    img = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(img)
    stripes(d)
    ev = data["event"]
    center_text(d, 120, "CHAMPIONSHIP", font(40), ORANGE, ease(t / 0.2))
    center_text(d, 180, f"after {ev['venue']}", font(28), DIM, ease(t / 0.25))

    groups = {}
    for r in data["champ"]:
        groups.setdefault(r["class"], []).append(r)
    y = 280
    gi = 0
    for cls, rows in groups.items():
        a = ease((t - 0.15 - gi * 0.15) / 0.25)
        if a > 0:
            d.text((80, y), cls.upper(), font=font(30),
                   fill=(*ORANGE, int(255 * a)))
            for j, r in enumerate(rows):
                ry = y + 55 + j * 62
                d.text((80, ry), str(r["position"]), font=font(34),
                       fill=(*pos_color(r["position"]), int(255 * a)))
                d.text((140, ry), r["full_name"], font=font(32),
                       fill=(*WHITE, int(255 * a)))
                pts = f"{r['points']} pts"
                bb = d.textbbox((0, 0), pts, font=font(30))
                d.text((W - 80 - (bb[2] - bb[0]), ry), pts, font=font(30),
                       fill=(*DIM, int(255 * a)))
        y += 55 + len(rows) * 62 + 45
        gi += 1
    return img


def scene_outro(data, t):
    img = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(img)
    stripes(d)
    # MT plate
    a = ease(t / 0.3)
    pw, ph = 300, 240
    x0, y0 = (W - pw) // 2, 330
    d.rounded_rectangle([x0 - 8, y0 - 8, x0 + pw + 8, y0 + ph + 8],
                        radius=52, fill=(*ORANGE, int(255 * a)))
    d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=44,
                        fill=(*WHITE, int(255 * a)))
    f = font(120, True)
    bb = d.textbbox((0, 0), "MT", font=f)
    d.text((x0 + (pw - bb[2] + bb[0]) / 2 - bb[0],
            y0 + (ph - bb[3] + bb[1]) / 2 - bb[1]),
           "MT", font=f, fill=(*BG, int(255 * a)))
    wordmark(d, 640, 44, ease((t - 0.15) / 0.3))
    if data["next"]:
        nx = data["next"]
        center_text(d, 760, "NEXT UP", font(26), ORANGE, ease((t - 0.3) / 0.3))
        center_text(d, 805, f"{nx['venue']} · {fmt_date(nx['date'])}",
                    font(34), WHITE, ease((t - 0.3) / 0.3))
    return img


# --- timeline -----------------------------------------------------------------
def build_video(data, out_dir):
    avatars = {}
    for c in data["classes"]:
        for p in c["podium"]:
            size = 360 if p["overall"] == 1 else 120
            av = (fetch_headshot(p["shot"], size, pos_color(p["overall"]))
                  or plate_avatar(p["num"], size, pos_color(p["overall"])))
            avatars[(c["class"], p["overall"])] = av

    scenes = [(4.0, lambda t: scene_intro(data, t))]
    for c in data["classes"][:2]:
        scenes.append((9.0, lambda t, c=c: scene_podium(c, avatars, t)))
    scenes.append((5.0, lambda t: scene_champ(data, t)))
    scenes.append((30.0 - sum(s[0] for s in scenes), lambda t: scene_outro(data, t)))

    bounds = []
    acc = 0.0
    for dur, fn in scenes:
        bounds.append((acc, acc + dur, dur, fn))
        acc += dur

    frames_dir = pathlib.Path(tempfile.mkdtemp(prefix="recap_frames_"))
    total = int(DUR * FPS)
    fade = 0.5
    for i in range(total):
        T = i / FPS
        cur = next(b for b in bounds if b[0] <= T < b[1] or b is bounds[-1])
        start, end, dur, fn = cur
        img = fn((T - start) / dur)
        idx = bounds.index(cur)
        if idx + 1 < len(bounds) and T > end - fade:
            nxt = bounds[idx + 1]
            a = (T - (end - fade)) / fade
            img = Image.blend(img, nxt[3](0.0), ease(a))
        img.convert("RGB").save(frames_dir / f"f{i:04d}.png")
        # Save two mid-scene stills for visual QA.
        if i in (int(6.5 * FPS), int(24 * FPS)):
            img.convert("RGB").save(out_dir / f"preview_{i}.png")

    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    out_mp4 = out_dir / "recap.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-framerate", str(FPS),
         "-i", str(frames_dir / "f%04d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         str(out_mp4)],
        check=True, capture_output=True,
    )
    return out_mp4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="recap_out")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    data = load_recap()
    if not data:
        print("No completed event with results; nothing to render.")
        return
    ev = data["event"]
    key = f"{ev['series']}-{ev['round_number']}-{ev['event_date']}"

    if not args.force:
        try:
            cur = requests.get(MEDIA_JSON_URL, timeout=15)
            if cur.status_code == 200 and cur.json().get("event_key") == key:
                print(f"Recap for {key} already published; skipping.")
                return
        except Exception:
            pass  # can't check -> render anyway

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Rendering recap for {ev['venue']} ({key})...")
    mp4 = build_video(data, out_dir)
    meta = {
        "event_key": key,
        "venue": ev["venue"],
        "series": ev["series"],
        "round_label": ev["round_label"],
        "event_date": ev["event_date"],
        "video_url": VIDEO_URL,
        "duration_seconds": DUR,
    }
    (out_dir / "recap.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"Wrote {mp4} ({mp4.stat().st_size / 1e6:.1f} MB) + recap.json")


if __name__ == "__main__":
    main()
