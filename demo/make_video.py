"""Render the demo as a captioned terminal video with a timed voiceover script.

Output: demo/video/foliant-demo.mp4 (1920x1080, 30 fps), foliant-demo.gif, script.md
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "video"
FRAMES = OUT / "frames"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H, FPS = 1920, 1080, 30
BG, TERM, INK, DIM, ACCENT, CAP_BG = "#0F1B2D", "#0B1424", "#E6E9EF", "#8A97AB", "#7FB0F5", "#182742"

# Each segment: seconds, terminal lines to reveal (typed or printed), caption, script line.
SEGMENTS = [
    (5.0, [("$", "python demo/run_demo.py")], "Foliant: payments for AI agents, with the spend cap on the account.",
     "This is the Foliant reference implementation. One command runs five scenarios in about a second. I'll talk through what each one shows."),
    (7.0, [("h", "A. one agent, one channel, 20 calls"),
           ("o", "  channel 9310996d: deposit 100, on-chain settled 0, off-chain owed 60"),
           ("o", "  chain transfers so far: 2  (open only; no per-call transactions)")],
     "A. Twenty API calls. Two chain transactions in total.",
     "Scenario A: one agent, one payment channel, twenty API calls. Look at the third line: two chain transfers in total, the channel open and, later, the settlement. Twenty calls, zero per-call transactions."),
    (6.0, [("o", "  provider settles once: +60 USDC; provider balance 60; receipts held by agent: 20")],
     "The provider settles once. The agent holds 20 signed receipts.",
     "The provider settles once for sixty units, and the agent is holding twenty signed receipts, one per call, that prove what it bought."),
    (8.0, [("h", "B. three agents share the provider's pool, 10 calls each"),
           ("o", "  pool members: 3; on-chain paid: [0, 0, 0]"),
           ("o", "  one settlement transaction: +90 USDC across 3 members; on-chain paid now [30, 30, 30]; transfers added: 3")],
     "B. Three agents, thirty calls, one settlement transaction.",
     "Scenario B: three agents share the provider's pool, ten calls each. Thirty calls settle in one transaction, and the pool tracks what each member owes. This is a crew of agents paying one provider."),
    (9.0, [("h", "C. policy: per_window_max 200 of committed value stops the enclave signing"),
           ("o", "  after 66 calls (two 100-unit channel deposits) the signer refused a third deposit: amount 100 would exceed per_window_max 200 (already spent 200)"),
           ("o", "  committed in window per signer: 200; per ledger: 200")],
     "C. The spend policy. The signer refuses; the ledger agrees exactly.",
     "Scenario C is the one that matters. The operator set a cap of two hundred units per hour. Sixty-six calls in, the agent's signer refuses to commit any more, and the ledger's view agrees to the unit. A crew gets a budget, not a hot wallet."),
    (8.0, [("h", "D. unilateral exit: coordinator goes silent, bob leaves the pool"),
           ("o", "  exit opened at t+60s; bob's paid on-chain 30 of deposit 100"),
           ("o", "  bob refunded 70; exited=True")],
     "D. The provider vanishes. Bob leaves the pool alone and gets his 70 back.",
     "Scenario D: what if the provider vanishes? Bob exits the pool on his own and gets his unspent seventy back after the timeout. Nobody's funds depend on the coordinator being honest or online."),
    (12.0, [("h", "E. a crew: orchestrator delegates budgets to three workers; the tree bounds the crew"),
            ("o", "  each worker may commit 100; the crew as a whole may commit 150. after 48 calls: amount 50 would exceed per_window_max 150 (already spent 150)"),
            ("o", "  committed: workers [50, 50, 50], orchestrator 150 = crew total"),
            ("o", "  orchestrator revokes worker 3 with its own signer, no human in the loop: policy expired"),
            ("o", "  and recalls its unspent 950; provider settles every open channel, the crew included, in one call: +342")],
     "E. A crew. Each worker could spend 100; the crew stops at 150. The orchestrator revokes a worker itself.",
     "Scenario E is a crew. The orchestrator has a budget of one hundred and fifty and delegates one hundred to each of three workers, with its own key, no human involved. Each worker could spend a hundred, but the crew as a whole stops at one hundred and fifty: the next deposit is refused because every spend is checked against every level of the tree. Then the orchestrator revokes worker three itself and takes back the unspent balance. That is what a crew budget means."),
    (5.0, [("o", "supply check: 61000 == minted 61000")],
     "Every unit accounted for.",
     "And the supply check: every unit is accounted for."),
    (7.0, [("$", "pytest -q"), ("o", "..................................                                       [100%]"), ("o", "34 passed in 2.11s")],
     "34 tests, including property-based invariants over random budget trees. foliant.network",
     "Thirty-four tests back this, including property-based checks over hundreds of random sessions and random budget trees: a payee never gets more than the payer signed, a payer always gets the rest back, and no branch of a crew ever exceeds any ancestor's cap. The code is public at foliant.network."),
]


def wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render(term_lines, caption, seg_index, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_mono = ImageFont.truetype(MONO, 26)
    f_cap = ImageFont.truetype(SANS_B, 40)
    f_small = ImageFont.truetype(SANS, 24)
    # header
    d.text((72, 40), "FOLIANT · agent-layer reference implementation", font=ImageFont.truetype(SANS_B, 26), fill=ACCENT)
    d.text((W - 72 - 300, 44), f"scenario {seg_index}/{total}", font=f_small, fill=DIM)
    # terminal panel
    d.rounded_rectangle((72, 96, W - 72, 800), radius=16, fill=TERM, outline="#2A3A58", width=2)
    for i, c in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
        d.ellipse((96 + i * 28, 116, 112 + i * 28, 132), fill=c)
    # wrap everything, then show the last rows that fit: a terminal scrolls
    rows: list[tuple[str, str]] = []
    for kind, text in term_lines:
        color = {"$": ACCENT, "h": INK, "o": "#B8C4D6"}[kind]
        prefix = "$ " if kind == "$" else ""
        for line in wrap(d, prefix + text, f_mono, W - 200):
            rows.append((line, color))
    max_rows = (800 - 160 - 20) // 34
    y = 160
    for line, color in rows[-max_rows:]:
        d.text((104, y), line, font=f_mono, fill=color)
        y += 34
    # caption band
    d.rounded_rectangle((72, 832, W - 72, 1040), radius=16, fill=CAP_BG)
    lines = wrap(d, caption, f_cap, W - 240)
    cy = 936 - (len(lines) * 50) // 2
    for line in lines:
        d.text((W // 2, cy), line, font=f_cap, fill=INK, anchor="ma")
        cy += 50
    return img


def main():
    if len(sys.argv) > 1:
        # boundaries in seconds from a voiceover; last value = total length
        b = [0.0] + [float(v) for v in sys.argv[1].split(",")]
        durs = [b[i + 1] - b[i] for i in range(len(b) - 1)]
        assert len(durs) == len(SEGMENTS), f"need {len(SEGMENTS)} durations, got {len(durs)}"
        for i, d in enumerate(durs):
            SEGMENTS[i] = (d,) + SEGMENTS[i][1:]
    FRAMES.mkdir(parents=True, exist_ok=True)
    for f in FRAMES.glob("*.png"):
        f.unlink()
    n = 0
    shown: list[tuple[str, str]] = []
    script = ["# Foliant demo — voiceover script\n", "Read each line when its timestamp appears; the caption on screen matches.\n"]
    t = 0.0
    total = len(SEGMENTS)
    for si, (secs, lines, caption, say) in enumerate(SEGMENTS, 1):
        script.append(f"\n**{int(t // 60)}:{int(t % 60):02d}** — {say}\n")
        frames = int(secs * FPS)
        # reveal lines progressively over the first 40% of the segment
        reveal_frames = max(1, int(frames * 0.4))
        for fi in range(frames):
            k = min(len(lines), 1 + (fi * len(lines)) // reveal_frames) if fi < reveal_frames else len(lines)
            img = render(shown + lines[:k], caption, si, total)
            img.save(FRAMES / f"f{n:05d}.png")
            n += 1
        shown += lines
        t += secs
    script.append(f"\nTotal length: {int(t // 60)}:{int(t % 60):02d}. Record in one take with Voice Memos or QuickTime while the video plays; send me the audio file and I'll merge it.\n")
    (OUT / "script.md").write_text("".join(script))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(FRAMES / "f%05d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(OUT / "foliant-demo.mp4")], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(OUT / "foliant-demo.mp4"),
                    "-vf", "fps=5,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=64[p];[s1][p]paletteuse",
                    str(OUT / "foliant-demo.gif")], check=True)
    for f in FRAMES.glob("*.png"):
        f.unlink()
    FRAMES.rmdir()
    print(f"{n} frames, {t:.0f}s")


if __name__ == "__main__":
    main()
