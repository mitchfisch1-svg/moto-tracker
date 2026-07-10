"""Render a ~30s "State of the Series" video — the season-level catch-up.

Head-to-head title fights (leader vs chaser, with headshots) for each class of
the currently-active series, framed for a newcomer. Reuses the recap renderer's
helpers and the same imageio-ffmpeg stitching, publishes season.mp4 +
season.json to the media branch.

Usage (project root, DATABASE_URL set):
    python scripts/render_season_video.py --out season_out [--force]
"""

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

import requests
from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from render_recap_video import (  # noqa: E402  (shared helpers + constants)
    W, H, FPS, BG, ORANGE, WHITE, DIM, GOLD, SILVER,
    font, ease, stripes, center_text, wordmark, fetch_headshot, plate_avatar,
    fmt_date, series_long,
)
from src.db import get_connection  # noqa: E402

DUR = 30.0
SEASON_VIDEO_URL = (
    "https://raw.githubusercontent.com/mitchfisch1-svg/moto-tracker/media/season.mp4"
)
SEASON_META_URL = (
    "https://raw.githubusercontent.com/mitchfisch1-svg/moto-tracker/media/season.json"
)


def _first(name):
    return (name or "").split(" ")[0]


def fight_line(leader, chaser, gap, rounds_left):
    left = (f" with {rounds_left} to go" if rounds_left else "")
    if not chaser:
        return f"{_first(leader)} leads the championship."
    if gap <= 8:
        return (f"Just {gap} points split {_first(leader)} and {_first(chaser)}"
                f"{left} — this title is up for grabs.")
    if gap <= 25:
        return (f"{_first(leader)} leads by {gap}{left}, but {_first(chaser)} "
                f"is right there.")
    return f"{_first(leader)} has a commanding {gap}-point lead{left}."


def load_season():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.abbrev, e.venue, e.event_date FROM events e
                JOIN seasons se ON se.id = e.season_id
                JOIN series s ON s.id = se.series_id
                WHERE e.event_date >= CURRENT_DATE ORDER BY e.event_date LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                active = row[0]
                nxt = {"series": row[0], "venue": row[1], "date": str(row[2])}
            else:
                cur.execute(
                    """
                    SELECT s.abbrev FROM events e JOIN seasons se ON se.id=e.season_id
                    JOIN series s ON s.id=se.series_id
                    WHERE e.status='final' ORDER BY e.event_date DESC LIMIT 1
                    """
                )
                r = cur.fetchone()
                active, nxt = (r[0] if r else "MX"), None

            cur.execute(
                """
                SELECT count(*), count(*) FILTER (WHERE e.status='final')
                FROM events e JOIN seasons se ON se.id=e.season_id
                JOIN series s ON s.id=se.series_id WHERE s.abbrev=%s
                """,
                (active,),
            )
            total, done = cur.fetchone()

            cur.execute(
                """
                SELECT e.round_number, e.event_date FROM events e
                JOIN seasons se ON se.id=e.season_id JOIN series s ON s.id=se.series_id
                WHERE s.abbrev=%s AND e.status='final'
                  AND EXISTS (SELECT 1 FROM sessions x JOIN results r ON r.session_id=x.id
                              WHERE x.event_id=e.id)
                ORDER BY e.event_date DESC LIMIT 1
                """,
                (active,),
            )
            lr = cur.fetchone()
            key = f"{active}-{lr[0]}-{lr[1]}" if lr else f"{active}-pre"

            cur.execute(
                """
                SELECT st.class, st.position, r.full_name, r.number,
                       r.manufacturer, r.headshot_url, st.points
                FROM standings st JOIN seasons se ON se.id=st.season_id
                JOIN series s ON s.id=se.series_id JOIN riders r ON r.id=st.rider_id
                WHERE s.abbrev=%s AND st.position<=2
                ORDER BY st.class, st.position
                """,
                (active,),
            )
            rows = cur.fetchall()

    rounds_left = max(0, (total or 0) - (done or 0))
    by_class = {}
    for (cls, pos, name, num, make, shot, pts) in rows:
        by_class.setdefault(cls, []).append(
            dict(name=name, num=num, make=make, shot=shot, pts=pts, pos=pos))

    classes = []
    for cls in sorted(by_class, key=lambda c: (0 if c.startswith("450") else 1, c)):
        cr = by_class[cls]
        leader = cr[0]
        chaser = cr[1] if len(cr) > 1 else None
        gap = (leader["pts"] - chaser["pts"]) if chaser else 0
        classes.append({
            "class": cls, "leader": leader, "chaser": chaser, "gap": gap,
            "fight": fight_line(leader["name"], chaser["name"] if chaser else None,
                                gap, rounds_left),
        })

    return {"series": active, "done": done or 0, "total": total or 0,
            "rounds_left": rounds_left, "classes": classes, "next": nxt, "key": key}


def draw_wrapped(d, text, y, f, fill, alpha, max_w=620, line_h=38):
    if alpha <= 0:
        return
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if d.textbbox((0, 0), test, font=f)[2] <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    for i, ln in enumerate(lines):
        center_text(d, y + i * line_h, ln, f, fill, alpha)


def scene_intro(data, t):
    img = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(img)
    stripes(d)
    center_text(d, 360, "STATE OF THE", font(40), ORANGE, ease(t / 0.25))
    center_text(d, 420, series_long(data["series"]).upper(), font(70, True), WHITE,
                ease((t - 0.1) / 0.3))
    center_text(d, 560, f"Round {data['done']} of {data['total']}", font(30), DIM,
                ease((t - 0.35) / 0.3))
    wordmark(d, 1150, 30, ease((t - 0.45) / 0.3))
    return img


def scene_fight(c, avatars, t):
    img = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(img)
    stripes(d)
    center_text(d, 110, f"THE {c['class']} TITLE FIGHT", font(38), ORANGE, ease(t / 0.2))

    a1 = ease((t - 0.1) / 0.3)
    lead = avatars[(c["class"], "leader")]
    if t > 0.1:
        img.alpha_composite(lead, ((W - lead.width) // 2, 195))
    center_text(d, 520, c["leader"]["name"].upper(), font(46, True), WHITE, a1)
    center_text(d, 585, f"{c['leader']['pts']} PTS  ·  LEADER", font(26), GOLD, a1)

    if c["chaser"]:
        a2 = ease((t - 0.38) / 0.25)
        center_text(d, 660, f"—  {c['gap']} POINTS BACK  —", font(30), ORANGE, a2)
        a3 = ease((t - 0.48) / 0.3)
        ch = avatars[(c["class"], "chaser")]
        if t > 0.48:
            img.alpha_composite(ch, ((W - ch.width) // 2, 730))
        center_text(d, 945, c["chaser"]["name"].upper(), font(36, True), WHITE, a3)
        center_text(d, 1000, f"{c['chaser']['pts']} PTS", font(24), DIM, a3)

    draw_wrapped(d, c["fight"], 1085, font(27), DIM, ease((t - 0.62) / 0.3))
    return img


def scene_outro(data, t):
    img = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(img)
    stripes(d)
    a = ease(t / 0.3)
    pw, ph = 300, 240
    x0, y0 = (W - pw) // 2, 320
    d.rounded_rectangle([x0 - 8, y0 - 8, x0 + pw + 8, y0 + ph + 8], radius=52,
                        fill=(*ORANGE, int(255 * a)))
    d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=44, fill=(*WHITE, int(255 * a)))
    f = font(96, True)
    parts = [("M", (*BG, int(255 * a))), ("X", (*ORANGE, int(255 * a))),
             ("T", (*BG, int(255 * a)))]
    ws = [d.textbbox((0, 0), t2, font=f) for t2, _ in parts]
    tot = sum(b[2] - b[0] for b in ws)
    bb = d.textbbox((0, 0), "MXT", font=f)
    tx = x0 + (pw - tot) / 2
    ty = y0 + (ph - bb[3] + bb[1]) / 2 - bb[1]
    for (t2, color), b in zip(parts, ws):
        d.text((tx - b[0], ty), t2, font=f, fill=color)
        tx += b[2] - b[0]
    if data["rounds_left"]:
        center_text(d, 630, f"{data['rounds_left']} ROUNDS TO GO", font(30), ORANGE,
                    ease((t - 0.2) / 0.3))
    if data["next"]:
        center_text(d, 690, f"Next: {data['next']['venue']} · "
                    f"{fmt_date(data['next']['date'])}", font(30), WHITE,
                    ease((t - 0.3) / 0.3))
    return img


def build_video(data, out_dir):
    avatars = {}
    for c in data["classes"]:
        avatars[(c["class"], "leader")] = (
            fetch_headshot(c["leader"]["shot"], 300, GOLD)
            or plate_avatar(c["leader"]["num"], 300, GOLD))
        if c["chaser"]:
            avatars[(c["class"], "chaser")] = (
                fetch_headshot(c["chaser"]["shot"], 210, SILVER)
                or plate_avatar(c["chaser"]["num"], 210, SILVER))

    scenes = [(4.0, lambda t: scene_intro(data, t))]
    for c in data["classes"][:2]:
        scenes.append((9.0, lambda t, c=c: scene_fight(c, avatars, t)))
    scenes.append((DUR - sum(s[0] for s in scenes), lambda t: scene_outro(data, t)))

    bounds, acc = [], 0.0
    for dur, fn in scenes:
        bounds.append((acc, acc + dur, dur, fn))
        acc += dur

    frames = pathlib.Path(tempfile.mkdtemp(prefix="season_frames_"))
    total, fade = int(DUR * FPS), 0.5
    for i in range(total):
        T = i / FPS
        cur = next(b for b in bounds if b[0] <= T < b[1] or b is bounds[-1])
        start, end, dur, fn = cur
        img = fn((T - start) / dur)
        idx = bounds.index(cur)
        if idx + 1 < len(bounds) and T > end - fade:
            a = (T - (end - fade)) / fade
            img = Image.blend(img, bounds[idx + 1][3](0.0), ease(a))
        img.convert("RGB").save(frames / f"f{i:04d}.png")
        if i in (int(2 * FPS), int(9 * FPS)):
            img.convert("RGB").save(out_dir / f"preview_{i}.png")

    import imageio_ffmpeg
    out_mp4 = out_dir / "season.mp4"
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-framerate", str(FPS),
         "-i", str(frames / "f%04d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(out_mp4)],
        check=True, capture_output=True,
    )
    return out_mp4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="season_out")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    data = load_season()
    if not data["classes"]:
        print("No standings yet; nothing to render.")
        return
    if not args.force:
        try:
            cur = requests.get(SEASON_META_URL, timeout=15)
            if cur.status_code == 200 and cur.json().get("event_key") == data["key"]:
                print(f"Season video for {data['key']} already published; skipping.")
                return
        except Exception:
            pass

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Rendering season video ({data['key']})...")
    mp4 = build_video(data, out_dir)
    (out_dir / "season.json").write_text(json.dumps({
        "event_key": data["key"], "series": data["series"],
        "series_long": series_long(data["series"]),
        "video_url": SEASON_VIDEO_URL, "duration_seconds": DUR,
    }, indent=1), encoding="utf-8")
    print(f"Wrote {mp4} ({mp4.stat().st_size / 1e6:.1f} MB) + season.json")


if __name__ == "__main__":
    main()
