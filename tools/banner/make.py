# Token Burn Gauge by soybean_alien
# https://github.com/soybean-alien/Token-Burn-Gauge - MIT License
# Writes hero.html beside this file (2560x1280). app.png is a capture of the real app window. Render with:
#   chrome --headless --screenshot=assets/hero.png --window-size=2560,1280 --virtual-time-budget=6000 tools/banner/hero.html
from pathlib import Path

BG, INK, DIM = "#14151C", "#EDE6D6", "#8E8C9C"


def text(cls, x, y, size, fill, body, extra=""):
    return f'<text class="{cls}" x="{x}" y="{y}" font-size="{size}" fill="{fill}" {extra}>{body}</text>\n'


page = f"""<!doctype html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500&display=block" rel="stylesheet">
<style>html,body{{margin:0;width:2560px;height:1280px;background:{BG};overflow:hidden}}
.mono{{font-family:'JetBrains Mono',monospace}}.serif{{font-family:'Instrument Serif',serif}}</style></head><body>
<svg width="2560" height="1280" viewBox="0 0 2560 1280" xmlns="http://www.w3.org/2000/svg">
<rect width="2560" height="1280" fill="{BG}"/>
"""
page += '<image href="../../assets/TokenGauge-256.png" x="136" y="118" width="104" height="104"/>\n'
page += text("mono", 262, 182, 33, DIM, "TOKEN BURN GAUGE · WINDOWS DESKTOP APP", 'letter-spacing="7"')
page += text("serif", 132, 400, 170, INK, "Token Burn Gauge", 'font-style="italic"')
page += text("serif", 144, 540, 84, INK, "Claude Code, Codex,")
page += text("serif", 144, 630, 84, INK, "GPU and CPU on one board.")
page += text("mono", 146, 740, 32, DIM, "Model and effort level in the titles.")
page += text("mono", 146, 788, 32, DIM, "Counters only. Nothing leaves this PC.")
page += ('<defs><clipPath id="win"><rect x="1330" y="150" width="1090" height="900"/></clipPath></defs>\n'
         '<image href="app.png" x="1330" y="150" width="1090" clip-path="url(#win)"/>\n'
         '<rect x="1330" y="150" width="1090" height="900" fill="none" stroke="#2A2D3A" stroke-width="4"/>\n')
page += text("mono", 144, 1150, 30, DIM, "NO MODEL CALLS · 127.0.0.1 ONLY · STALE, NEVER ZERO", 'letter-spacing="9"')
page += text("mono", 2420, 1150, 30, DIM, "by soybean_alien", 'text-anchor="end"')
page += "</svg></body></html>\n"
(Path(__file__).parent / "hero.html").write_text(page, encoding="utf-8")
