"""聞き比べ用に、いくつかの声で同じ文を読み上げたmp3を作るスクリプト(Geminiは使いません)。

出力: samples/*.mp3 と samples/一覧.txt(ファイル名・声の名前・config.json に書く番号)
"""

import asyncio
from pathlib import Path

import requests

import generate as g

DIGEST = [{"category": "生成AI全般", "points": [{
    "headline": "OpenAIがChatGPTの新機能を発表",
    "body": "米OpenAIは27日、ChatGPTに新しい機能を追加したと発表した。GoogleのGeminiやAnthropicのClaudeと競う。"}]}]
TEXT = g.for_reading(g.digest_to_script(DIGEST, "10月29日"))

# (キャラクター名, スタイル名)。VOICEVOX から番号を調べて読み上げます
CANDIDATES = [
    ("猫使ビィ", None),  # None はそのキャラクターのスタイル全部
    ("春日部つむぎ", "ノーマル"),
]

out = Path("samples")
out.mkdir(exist_ok=True)
g.OUT = out
g.wait_voicevox()
speakers = requests.get(f"{g.VOICEVOX_URL}/speakers", timeout=30).json()
ids = {(sp["name"], st["name"]): st["id"] for sp in speakers for st in sp["styles"]}
targets = []
for name, style in CANDIDATES:
    found = [(n, s) for (n, s) in ids if n == name and style in (None, s)]
    if not found:
        print(f"skip: {name}({style}) が見つかりません")
    targets += found

lines = []
for n, (name, style) in enumerate(targets, 1):
    sid = ids[(name, style)]
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

# 本番と同じ流れ(config.json の voice)でも1本作って、正しく動くか確かめる
g.OUT = out
g.speak(TEXT, g.load_voice())
(out / "episode.mp3").rename(out / "00_honban.mp3")
lines.insert(0, f"00_honban.mp3\t本番の設定(config.json の voice)\t")

(out / "一覧.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
