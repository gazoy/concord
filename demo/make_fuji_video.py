"""Render the Foliant demo video: title card, a terminal replaying the real Fuji run, closing card.

    python demo/make_fuji_video.py              # writes frames to demo/video/frames
    ffmpeg -framerate 30 -i demo/video/frames/f%05d.png -i narration.mp3 \
           -af "highpass=f=80,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5" \
           -c:v libx264 -crf 20 -pix_fmt yuv420p -movflags +faststart \
           -c:a aac -b:a 160k -shortest demo/video/foliant-fuji-voiced.mp4

The reveal is timed to the narration in demo/video/fuji-script.md: MARKS maps a time in the
voiceover to the number of terminal lines shown by then. Re-time by editing MARKS, which was
derived from the script's character counts and checked against the pauses ffmpeg's silencedetect
found in the recording.
"""
import io, math, os, sys
import cairosvg
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1920, 1080, 30
OUT = os.environ.get("FOLIANT_VIDEO_OUT", "demo/video/frames")
BG, PANEL, LINE, INK = (15, 27, 45), (24, 39, 66), (42, 58, 88), (246, 245, 240)
MUTED, DIM, ACCENT, SOFT = (184, 196, 214), (138, 151, 171), (47, 128, 237), (127, 176, 245)
GREEN, AMBER = (126, 200, 154), (232, 178, 94)

F = "/usr/share/fonts/truetype/dejavu/"
mono = lambda s: ImageFont.truetype(F + "DejaVuSansMono.ttf", s)
monob = lambda s: ImageFont.truetype(F + "DejaVuSansMono-Bold.ttf", s)
sans = lambda s: ImageFont.truetype(F + "DejaVuSans.ttf", s)
sansb = lambda s: ImageFont.truetype(F + "DejaVuSans-Bold.ttf", s)

LOCKUP = Image.open(io.BytesIO(cairosvg.svg2png(
    url="docs/assets/brand/foliant-lockup-dark-transparent.svg", output_width=440))).convert("RGBA")

# the real run, trimmed of the blank lines, with a colour per line
T = (INK,); M = (MUTED,); D = (DIM,); A = (SOFT,); G = (GREEN,); R = (AMBER,)
def L(text, col=MUTED, bold=False): return (text, col, bold)
SCRIPT = [
  L("$ pip install \"foliant-protocol[chain]\" httpx", DIM),
  L("$ curl -fsSLO .../examples/try_fuji.py", DIM),
  L("$ python try_fuji.py", INK, True),
  L(""),
  L("Foliant - https://fuji.foliant.network", MUTED),
  L(""),
  L("1. Ask the server what it offers", INK, True),
  L("   network   avalanche-fuji (chain id 43113)", MUTED),
  L("   price     0.10 per call", MUTED),
  L(""),
  L("2. Get a key and ask the tap to fund it", INK, True),
  L("   address   0xEEa9e3ce99ee0F003ecd6fC22CB4495A06336624", MUTED),
  L("   funded: testnet.snowtrace.io/tx/0x9ee24e43db0b...", SOFT),
  L("   funded: testnet.snowtrace.io/tx/0x677fe8c7b3d7...", SOFT),
  L("   holding   0.02 AVAX for gas, 1,000.00 tokens to spend", MUTED),
  L(""),
  L("3. Register the orchestrator's account", INK, True),
  L("   policy    500.00 per payment, 2,000.00 per hour", GREEN, True),
  L("   registered: testnet.snowtrace.io/tx/0xfe6d1b09c23f...", SOFT),
  L("   deposited:  testnet.snowtrace.io/tx/0x6f4a1b235f67...", SOFT),
  L(""),
  L("4. Give a worker its own budget inside the orchestrator's", INK, True),
  L("   policy    20.00 per payment, 60.00 per hour", GREEN, True),
  L("   delegated: testnet.snowtrace.io/tx/0x9487ff6f1214...", SOFT),
  L("   worker    funded with 60.00 tokens", MUTED),
  L(""),
  L("5. Commit the worker's budget to the pool - one transaction", INK, True),
  L("   joined: testnet.snowtrace.io/tx/0x92d8c2582bf4...", SOFT),
  L("   committed 20.00 - this is the payment the policy checks", MUTED),
  L(""),
  L("6. Make 25 paid API calls - none of these touch the chain", INK, True),
  L("   call 1   paid 0.10, answered 'call 1'", MUTED),
  L("   call 2   paid 0.10, answered 'call 2'", MUTED),
  L("   call 3   paid 0.10, answered 'call 3'", MUTED),
  L("   ...", MUTED),
  L("   call 25  paid 0.10, answered 'call 25'", MUTED),
  L("   25 signed receipts, 2.50 paid, and 0 transactions sent", GREEN, True),
  L(""),
  L("7. Try to commit more than the worker's budget allows", INK, True),
  L("   refused by the contract:", MUTED),
  L("   PolicyViolation: amount exceeds per_tx_max", AMBER, True),
  L("   nothing sent, no gas spent", MUTED),
  L(""),
  L("8. Settlement - one transaction for the whole session", INK, True),
  L("   25 calls settled in one transaction", GREEN, True),
]
# line index reached by each narration paragraph's end (timing model validated against the audio)
MARKS = [(1.4,0),(13.1,0),(16.0,0),(21.8,15),(27.5,20),(35.8,25),(43.1,29),(52.8,37),
         (57.6,41),(67.7,42),(71.6,45),(76.4,45),(82.9,45),(84.9,45)]

def ease(x): return 1 - (1 - x) ** 3

def at(t):
    """How many script lines are revealed at time t."""
    if t <= MARKS[0][0]: return 0
    for (t0, l0), (t1, l1) in zip(MARKS, MARKS[1:]):
        if t <= t1:
            f = (t - t0) / max(t1 - t0, 1e-6)
            return l0 + (l1 - l0) * f
    return MARKS[-1][1]

def card(d, t):
    """Title card, fading out as the terminal takes over."""
    a = 1.0 if t < 13.4 else max(0.0, 1 - (t - 13.4) / 1.6)
    if a <= 0: return
    def c(col, k=1.0): return tuple(int(BG[i] + (col[i] - BG[i]) * a * k) for i in range(3))
    lx, ly = (W - LOCKUP.width) // 2, 300
    lk = LOCKUP.copy()
    lk.putalpha(lk.getchannel("A").point(lambda v: int(v * a)))
    d._image.paste(lk, (lx, ly), lk)
    f = sansb(72); txt = "Give agents a budget,"; txt2 = "not a hot wallet."
    for i, s in enumerate((txt, txt2)):
        w = d.textlength(s, font=f)
        d.text(((W - w) / 2, 470 + i * 92), s, font=f, fill=c(INK))
    f2 = sans(34); s = "An open protocol for AI agent crews"
    d.text(((W - d.textlength(s, font=f2)) / 2, 680), s, font=f2, fill=c(MUTED))

def terminal(d, t):
    """The terminal panel, sliding in and revealing the run."""
    if t < 13.0: return
    a = min(1.0, (t - 13.0) / 1.8)
    x0, y0, x1, y1 = 150, int(120 + (1 - ease(a)) * 80), W - 150, H - 150
    def c(col): return tuple(int(BG[i] + (col[i] - BG[i]) * a) for i in range(3))
    d.rounded_rectangle([x0, y0, x1, y1], 14, fill=c(PANEL), outline=c(LINE), width=2)
    d.rounded_rectangle([x0, y0, x1, y0 + 44], 14, fill=c(LINE))
    d.rectangle([x0, y0 + 30, x1, y0 + 44], fill=c(LINE))
    for i, dot in enumerate(((235, 110, 100), (232, 190, 100), (126, 200, 154))):
        d.ellipse([x0 + 22 + i * 26, y0 + 15, x0 + 34 + i * 26, y0 + 27], fill=c(dot))
    d.text((x0 + 120, y0 + 12), "try_fuji.py - fuji.foliant.network", font=mono(19), fill=c(DIM))

    n = at(t); shown = SCRIPT[:math.ceil(n)]
    fm, fb, lh = mono(25), monob(25), 33
    total = len(shown) * lh
    avail = (y1 - y0 - 80)
    scroll = max(0, total - avail)
    yy = y0 + 64 - scroll
    for i, (text, col, bold) in enumerate(shown):
        if not text: yy += lh; continue
        la = 1.0
        if i == len(shown) - 1: la = min(1.0, (n - int(n)) * 2.2 + 0.15)
        if yy > y0 + 40 and yy < y1 - 30:
            cc = tuple(int(PANEL[k] + (col[k] - PANEL[k]) * la * a) for k in range(3))
            d.text((x0 + 44, yy), text, font=fb if bold else fm, fill=cc)
        yy += lh

def closing(d, t):
    """Closing card over the terminal."""
    if t < 76.0: return
    a = min(1.0, (t - 76.0) / 1.2)
    ov = Image.new("RGBA", (W, H), BG + (int(251 * a),))
    d._image.paste(ov, (0, 0), ov)
    def c(col): return tuple(int(BG[i] + (col[i] - BG[i]) * a) for i in range(3))
    lk = LOCKUP.resize((330, int(LOCKUP.height * 330 / LOCKUP.width)))
    lk.putalpha(lk.getchannel("A").point(lambda v: int(v * a)))
    d._image.paste(lk, ((W - lk.width) // 2, 170), lk)
    rows = [("25", "paid API calls"), ("2", "on-chain transactions"), ("0", "over budget")]
    bx = (W - (3 * 330 + 2 * 40)) // 2
    for i, (big, small) in enumerate(rows):
        x = bx + i * 370
        d.rounded_rectangle([x, 330, x + 330, 520], 12, fill=c(PANEL), outline=c(LINE), width=2)
        f = sansb(84); d.text((x + (330 - d.textlength(big, font=f)) / 2, 358), big, font=f, fill=c(SOFT))
        f2 = sans(24); d.text((x + (330 - d.textlength(small, font=f2)) / 2, 462), small, font=f2, fill=c(MUTED))
    f = sansb(44); s = "Live on Avalanche Fuji. Independently audited."
    d.text(((W - d.textlength(s, font=f)) / 2, 600), s, font=f, fill=c(INK))
    f2 = mono(36); s2 = "foliant.network"
    d.text(((W - d.textlength(s2, font=f2)) / 2, 690), s2, font=f2, fill=c(ACCENT))
    f3 = sans(27); s3 = "Run it yourself in ten minutes"
    d.text(((W - d.textlength(s3, font=f3)) / 2, 760), s3, font=f3, fill=c(MUTED))

def frame(i):
    t = i / FPS
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im); d._image = im
    terminal(d, t); card(d, t); closing(d, t)
    return im

if __name__ == "__main__":
    DUR = 85.0
    n = int(DUR * FPS)
    os.makedirs(OUT, exist_ok=True)
    for i in range(n):
        frame(i).save(f"{OUT}/f{i:05d}.png")
        if i % 300 == 0: print("frame", i, "/", n, flush=True)
    print("done")
