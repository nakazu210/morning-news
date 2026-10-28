"""聞き比べ用に、いくつかの声で同じ文を読み上げたmp3を作るスクリプト(Geminiは使いません)。

出力: samples/*.mp3 と samples/一覧.txt(ファイル名・声の名前・config.json に書く番号)
"""

import asyncio
from pathlib import Path

import requests

import generate as g

TEXT = ("おはようございます。10月28日の朝ニュースです。"
        "今日は、生成AIの新しいモデルと、住宅ローン金利の動きをお伝えします。"
        "それでは、1本目のニュースです。")

# (キャラクター名, スタイル名)。VOICEVOX から番号を調べて読み上げます
CANDIDATES = [
    ("春日部つむぎ", "ノーマル"),
    ("四国めたん", "あまあま"),
    ("ずんだもん", "ノーマル"),
    ("雨晴はう", "ノーマル"),
    ("冥鳴ひまり", "ノーマル"),
    ("九州そら", "あまあま"),
    ("もち子さん", "ノーマル"),
    ("WhiteCUL", "たのしい"),
    ("櫻歌ミコ", "ノーマル"),
    ("小夜/SAYO", "ノーマル"),
]

out = Path("samples")
out.mkdir(exist_ok=True)
g.OUT = out
g.wait_voicevox()
speakers = requests.get(f"{g.VOICEVOX_URL}/speakers", timeout=30).json()
ids = {(sp["name"], st["name"]): st["id"] for sp in speakers for st in sp["styles"]}

lines = []
for n, (name, style) in enumerate(CANDIDATES, 1):
    sid = ids.get((name, style))
    if sid is None:
        print(f"skip: {name}({style}) が見つかりません")
        continue
    chunks = [g.synth_voicevox(s, sid, g.SPEED) for s in g.split_sentences(TEXT)]
    file = f"{n:02d}_voicevox_{sid}.mp3"
    g.to_mp3(chunks, out / file)
    lines.append(f"{file}\t{name}({style})\tspeaker={sid}")

# 比較用に、今の Edge の声(ナナミ)と、少し高くした声も作る
import edge_tts
for file, pitch in [("90_edge_nanami.mp3", "+0Hz"), ("91_edge_nanami_high.mp3", "+25Hz")]:
    asyncio.run(edge_tts.Communicate(TEXT, g.EDGE_VOICE, rate=g.EDGE_RATE, pitch=pitch)
                .save(str(out / file)))
    lines.append(f"{file}\tEdge ナナミ(高さ{pitch})\tengine=edge")

(out / "一覧.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
