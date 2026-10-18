"""ニュースを集めて、原稿とLINE用の要約を作り、VOICEVOXで読み上げてmp3にするスクリプト。

GitHub Actions から毎朝呼ばれます。出力:
  out/episode.mp3   … 今日の音声
  out/episode.json  … タイトル・原稿・LINE用要約・元記事リンク

どの種類のニュースを集めるかは config.json(管理画面から変更できます)で決めます。
"""

import argparse
import datetime as dt
import io
import itertools
import json
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import feedparser
import requests

JST = dt.timezone(dt.timedelta(hours=9))
OUT = Path("out")

# ---- 設定(環境変数で上書きできます) ----
VOICEVOX_URL = os.getenv("VOICEVOX_URL", "http://localhost:50021")
SPEAKER_ID = int(os.getenv("VOICEVOX_SPEAKER", "8"))  # 8 = 春日部つむぎ(ノーマル)
SPEED = float(os.getenv("VOICEVOX_SPEED", "1.15"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "8"))
PER_CATEGORY = 12  # 1種類あたりAIに渡す記事の数
# 無料枠はFlash系のみ。名前が変わっても動くよう、上から順に試します
GEMINI_MODELS = os.getenv(
    "GEMINI_MODELS", "gemini-flash-latest,gemini-2.5-flash,gemini-2.5-flash-lite"
).split(",")


def load_categories() -> list[dict]:
    """config.json から、オンになっているニュースの種類だけを返す。"""
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    return [c for c in config["categories"] if c.get("enabled")]


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def fetch_news(categories: list[dict]) -> list[dict]:
    """種類ごとにRSSから直近の記事を集めて、重複を除いて返す。"""
    items, seen = [], set()
    for cat in categories:
        items += fetch_category(cat, seen)[:PER_CATEGORY]
    return items


def fetch_category(cat: dict, seen: set) -> list[dict]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cat.get("hours", 30))
    per_feed = []
    for url in cat["feeds"]:
        items = []
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "morning-news"})
        except Exception as e:  # 1つのサイトが落ちても止めない
            print(f"skip {url}: {e}", file=sys.stderr)
            continue
        for e in feed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            when = dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else None
            if when and when < since:
                continue
            title = clean(e.get("title", ""))
            key = re.sub(r"\W", "", title)[:30]
            if not title or key in seen:
                continue
            seen.add(key)
            items.append({
                "category": cat["name"],
                "title": title,
                "summary": clean(e.get("summary", ""))[:300],
                "link": e.get("link", ""),
                "when": when.isoformat() if when else "",
            })
        per_feed.append(items)
    # 検索条件ごとに上から1件ずつ交互に拾う(1つの条件の記事ばかりにならないように)
    mixed = []
    for row in itertools.zip_longest(*per_feed):
        mixed += [it for it in row if it]
    return mixed


PROMPT = """あなたは朝のニュース番組のナレーターです。
以下の記事一覧から重要度の高いものを選び、耳で聞いてわかる読み上げ原稿と、LINEで読む短い要約を作ってください。

ニュースの種類と、重視してほしい内容:
{focus}

ルール(読み上げ原稿 script):
- 全体で1500〜2000文字(約5分)。選ぶ記事は合計で最大{n}本。
- 種類ごとにまとめ、「続いて、住宅のニュースです。」のように区切りを入れる。記事がない種類は飛ばす。
- 冒頭は「おはようございます。{date}のニュースです。」で始める。
- 各ニュースは「何が起きたか」「なぜ重要か」を2〜3文で。
- 英字の製品名や略語は、読み方がわかるようカタカナで書く(例: OpenAI→オープンエーアイ)。
- 記号・箇条書き・URL・括弧は使わない。数字は読みやすい形で。
- 記事にない事実は足さない。
- 重視してほしい内容に合わない記事(スポーツの結果、事件・事故、他県の話題など)は選ばない。
- 最後は「以上、今日のニュースでした。」で締める。

ルール(LINE用要約 digest):
- 原稿で取り上げたニュースを、種類ごとに1本1〜2文(60文字程度)で書く。
- 製品名などは英字のままでよい。

出力はJSONのみ:
{{"script": "原稿", "digest": [{{"category": "種類名", "points": ["要約1", "要約2"]}}], "used": [使った記事の番号]}}

記事一覧:
{articles}
"""


def write_script(items: list[dict], categories: list[dict], date_label: str) -> dict:
    """Gemini(無料枠)で原稿と要約を作る。失敗したら見出し読み上げに切り替える。"""
    key = os.getenv("GEMINI_API_KEY")
    articles = "\n".join(
        f"[{i}]【{it['category']}】{it['title']} — {it['summary']}" for i, it in enumerate(items)
    )
    focus = "\n".join(f"- {c['name']}: {c.get('focus', '')}" for c in categories)
    if key:
        from google import genai

        client = genai.Client(api_key=key)
        prompt = PROMPT.format(n=MAX_ITEMS, date=date_label, focus=focus, articles=articles)
        for model in GEMINI_MODELS:
            try:
                res = client.models.generate_content(
                    model=model.strip(),
                    contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                data = json.loads(res.text)
                print(f"script by {model}")
                return {
                    "script": data["script"],
                    "digest": data.get("digest", []),
                    "used": [int(i) for i in data.get("used", [])],
                }
            except Exception as e:
                print(f"model {model} failed: {e}", file=sys.stderr)
    # 予備: AIなしで見出しを読み上げる(種類ごとに数本ずつ)
    print("fallback: headlines only", file=sys.stderr)
    per = max(1, MAX_ITEMS // max(1, len(categories)))
    picked = []
    for c in categories:
        picked += [i for i, it in enumerate(items) if it["category"] == c["name"]][:per]
    body = "".join(f"{n}本目。{items[i]['title']}。" for n, i in enumerate(picked, 1))
    script = f"おはようございます。{date_label}のニュースです。{body}以上、今日のニュースでした。"
    digest = [
        {"category": c["name"],
         "points": [items[i]["title"] for i in picked if items[i]["category"] == c["name"]]}
        for c in categories
    ]
    return {"script": script, "digest": [d for d in digest if d["points"]], "used": picked}


def split_sentences(text: str, limit: int = 120) -> list[str]:
    parts = re.split(r"(?<=[。！？!?])", text)
    out = []
    for p in (p.strip() for p in parts):
        while len(p) > limit:  # 長すぎる文は読点で切る
            cut = p.rfind("、", 0, limit)
            cut = cut + 1 if cut > 0 else limit
            out.append(p[:cut])
            p = p[cut:]
        if p:
            out.append(p)
    return out


def synth_voicevox(text: str) -> bytes:
    q = requests.post(f"{VOICEVOX_URL}/audio_query",
                      params={"text": text, "speaker": SPEAKER_ID}, timeout=60)
    q.raise_for_status()
    query = q.json()
    query["speedScale"] = SPEED
    query["postPhonemeLength"] = 0.35  # 文と文の間を少しあける
    s = requests.post(f"{VOICEVOX_URL}/synthesis",
                      params={"speaker": SPEAKER_ID}, json=query, timeout=120)
    s.raise_for_status()
    return s.content


def synth_dummy(text: str) -> bytes:
    """--dry-run 用: 文字数に比例した無音を返す。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x00\x00" * int(24000 * len(text) * 0.12))
    return buf.getvalue()


def wait_voicevox():
    for _ in range(60):
        try:
            if requests.get(f"{VOICEVOX_URL}/version", timeout=5).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    sys.exit("VOICEVOX engine did not start")


def to_mp3(chunks: list[bytes], path: Path):
    """WAVをつなげて、軽いmp3(モノラル48kbps)にする。容量節約のため低めの音質。"""
    frames, params = [], None
    for c in chunks:
        with wave.open(io.BytesIO(c)) as w:
            params = params or w.getparams()
            frames.append(w.readframes(w.getnframes()))
    wav = OUT / "all.wav"
    with wave.open(str(wav), "wb") as w:
        w.setparams(params)
        for f in frames:
            w.writeframes(f)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                    "-ac", "1", "-ar", "24000", "-b:a", "48k", str(path)], check=True)
    wav.unlink()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ネットとVOICEVOXなしで動作確認")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    today = dt.datetime.now(JST)
    date_label = f"{today.month}月{today.day}日"

    categories = load_categories()
    if not categories:
        sys.exit("ニュースの種類が1つもオンになっていません(管理画面で選んでください)")

    if args.dry_run:
        items = [{"category": c["name"], "title": f"{c['name']}のテスト記事{i}", "summary": "概要",
                  "link": f"https://example.com/{c['id']}/{i}", "when": ""}
                 for c in categories for i in range(3)]
        result = write_script(items, categories, date_label)
        synth = synth_dummy
    else:
        items = fetch_news(categories)
        if not items:
            sys.exit("ニュースが1件も取れませんでした")
        result = write_script(items, categories, date_label)
        wait_voicevox()
        synth = synth_voicevox

    script = result["script"]
    chunks = [synth(s) for s in split_sentences(script)]
    to_mp3(chunks, OUT / "episode.mp3")

    sources = [items[i] for i in result["used"] if 0 <= i < len(items)]
    meta = {
        "date": today.strftime("%Y-%m-%d"),
        "title": f"{date_label}の朝ニュース",
        "script": script,
        "digest": result["digest"],
        "sources": [{"category": s["category"], "title": s["title"], "link": s["link"]}
                    for s in sources],
    }
    (OUT / "episode.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: {len(script)} chars, {len(chunks)} sentences")


if __name__ == "__main__":
    main()
