"""ニュースを集めて、原稿とLINE用の要約を作り、音声で読み上げてmp3にするスクリプト。

読み上げは Microsoft Edge の音声(edge-tts、無料・登録不要)を使い、
使えなかったときだけ VOICEVOX に切り替えます。

GitHub Actions から毎朝呼ばれます。出力:
  out/episode.mp3   … 今日の音声
  out/episode.json  … タイトル・原稿・LINE用要約・元記事リンク

どの種類のニュースを集めるかは config.json(管理画面から変更できます)で決めます。
"""

import argparse
import asyncio
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
import trafilatura

JST = dt.timezone(dt.timedelta(hours=9))
OUT = Path("out")

# ---- 設定(環境変数で上書きできます) ----
EDGE_VOICE = os.getenv("EDGE_VOICE", "ja-JP-NanamiNeural")  # 男性の声は ja-JP-KeitaNeural
EDGE_RATE = os.getenv("EDGE_RATE", "+5%")  # 読む速さ
VOICEVOX_URL = os.getenv("VOICEVOX_URL", "http://localhost:50021")
SPEAKER_ID = int(os.getenv("VOICEVOX_SPEAKER", "8"))  # 8 = 春日部つむぎ(ノーマル)
SPEED = float(os.getenv("VOICEVOX_SPEED", "1.15"))
DEFAULT_COUNT = 3  # config.json に count がない種類の記事数
PER_CATEGORY = 25  # 1種類あたりAIに渡す候補の数
BODY_CHARS = 4000  # AIに渡す本文の長さ(1記事あたり)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
# 無料枠はFlash系のみ。名前が変わっても動くよう、上から順に試します
GEMINI_MODELS = os.getenv(
    "GEMINI_MODELS", "gemini-flash-latest,gemini-3.8-flash,gemini-flash-lite-latest"
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


def resolve_link(link: str) -> str:
    """Googleニュースのリンクを元記事のURLに変える(それ以外はそのまま)。"""
    if "news.google.com" not in link:
        return link
    aid = link.split("?")[0].rstrip("/").split("/")[-1]
    page = requests.get(f"https://news.google.com/rss/articles/{aid}", headers=UA, timeout=15).text
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts):
        return link
    req = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None,
                           None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
           aid, int(ts.group(1)), sg.group(1)]
    res = requests.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data={"f.req": json.dumps([[["Fbv4je", json.dumps(req), None, "generic"]]])},
        timeout=15,
    )
    return json.loads(json.loads(res.text.split("\n\n")[1])[0][2])[1]


def fetch_body(item: dict):
    """記事のページを開いて本文を取り出し、item["body"] に入れる。失敗しても止めない。"""
    try:
        item["link"] = resolve_link(item["link"])
        page = requests.get(item["link"], headers=UA, timeout=15)
        if page.encoding in (None, "ISO-8859-1"):
            page.encoding = page.apparent_encoding
        item["body"] = (trafilatura.extract(page.text) or "")[:BODY_CHARS]
    except Exception as e:
        print(f"body skip {item['link'][:80]}: {e}", file=sys.stderr)
        item["body"] = ""


SELECT_PROMPT = """以下は今朝のニュース候補の見出し一覧です。
次の種類と重視してほしい内容に合う、重要度の高い記事を選んでください。

ニュースの種類と、重視してほしい内容(かっこ内は選ぶ本数の上限):
{focus}

- 種類ごとに上限の本数まで選ぶ(合う記事が少ない種類は少なくてよい)。
- 重視してほしい内容に合わない記事(芸能、スポーツの結果、事件・事故、他県だけの話題、海外だけの住宅・不動産の話題、個人ブログや広告記事など)は選ばない。
- 英語の記事も選んでよい。新しいAIモデルや新機能の発表は、公式ブログや英語の一次情報を優先する。
- 生成AIは「新しいAIモデルの発表」「ChatGPT・Claude・Geminiなど主要AIの新機能・アップデート」を最優先し、資金調達・パソコンなどのハードウェア製品・政治の話題は、それが足りないときだけ選ぶ。
- 同じ出来事を扱う記事は1本だけ選ぶ。

出力はJSONのみ: {{"used": [選んだ記事の番号]}}

記事一覧:
{articles}
"""

PROMPT = """あなたは新聞記者です。
以下の「{category}」の記事(本文つき)をもとに、耳で聞いてわかる読み上げ原稿と、LINEで読む新聞のような記事要約を作ってください。

この種類で重視してほしい内容: {focus}

ルール(読み上げ原稿 script):
- 「{category}のニュースです。」で始め、記事ごとに300〜400文字で伝える(あいさつや締めの言葉は不要)。
- 各ニュースは「誰が・何を・いつ・どこで」「具体的な数字や中身」「背景・なぜ重要か」「今後の見通し」を伝える。
- 英字の製品名や略語は、読み方がわかるようカタカナで書く(例: OpenAI→オープンエーアイ)。
- 記号・箇条書き・URL・括弧は使わない。数字は読みやすい形で。
- 記事にない事実は足さない。

ルール(LINE用要約 points):
- すべての記事について、新聞記事のように「見出し」と「本文」を書く。
- 見出しは20〜30文字で、何が起きたかが一目でわかるように。
- 本文は必ず400〜600文字(短すぎるのは不可)。最初の1文で要点(誰が・何を・いつ)を伝え、続けて具体的な数字・金額・期間・対象・条件・性能・料金・提供開始時期などの中身をできるだけ多く、最後に背景や影響(なぜ重要か、これまでとの違い、競合との比較)を書く。
- 英語の記事も日本語で書く。製品名などは英字のままでよい。だ・である調で書く。
- 「〜について発表しました」のような中身のない書き方はしない。本文に書かれた具体的な情報を必ず入れる。
- 本文が空の記事は、見出しと概要からわかる範囲で書き、推測で補わない。
- 本文がログイン画面・セキュリティチェック・エラー表示など記事でない場合は、その記事を原稿からも要約からも外す。「詳細は不明」のような書き方はしない。

出力はJSONのみ:
{{"script": "原稿", "points": [{{"headline": "見出し", "body": "本文"}}], "used": [使った記事の番号]}}

記事一覧:
{articles}
"""


EXPAND_PROMPT = """以下のニュース要約は短すぎます。元の記事をもとに、それぞれの本文を450〜600文字に書き直してください。
具体的な数字・金額・期間・対象・条件・性能・料金などの中身と、背景や影響(なぜ重要か、これまでとの違い)を足してください。
記事にない事実は足さない。だ・である調。見出しは変えない。

出力はJSONのみ: {{"points": [{{"headline": "見出し", "body": "本文"}}]}}

短い要約:
{points}

元の記事:
{articles}
"""
MIN_BODY = 380  # これより短い要約は書き直してもらう


def ask_gemini(client, prompt: str) -> dict | None:
    """Geminiに聞いてJSONを返す。混雑していたら少し待って高性能なモデルでやり直し、
    それでもだめなら最後に軽いモデル(lite)で書く。全部だめなら None。"""
    main = [m for m in GEMINI_MODELS if "lite" not in m]
    lite = [m for m in GEMINI_MODELS if "lite" in m]
    for wait in (0, 45, 120):
        if wait:
            print(f"retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
        data = ask_once(client, prompt, main)
        if data is not None:
            return data
    return ask_once(client, prompt, lite)


def ask_once(client, prompt: str, models: list[str]) -> dict | None:
    for model in models:
        try:
            res = client.models.generate_content(
                model=model.strip(),
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            data = json.loads(res.text)
            print(f"answered by {model}")
            return data
        except Exception as e:
            print(f"model {model} failed: {e}", file=sys.stderr)
    return None


def merge_digest(digest: list[dict], categories: list[dict]) -> list[dict]:
    """同じ種類がばらばらに出てきても1つにまとめ、config.json の順に並べる。"""
    merged = {}
    for block in digest:
        merged.setdefault(block.get("category", ""), []).extend(block.get("points", []))
    order = [c["name"] for c in categories]
    names = sorted(merged, key=lambda n: order.index(n) if n in order else len(order))
    return [{"category": n, "points": merged[n]} for n in names if merged[n]]


def write_script(items: list[dict], categories: list[dict], date_label: str, fetch=True) -> dict:
    """Gemini(無料枠)で記事を選び、本文を読んで原稿と要約を作る。失敗したら見出し読み上げに切り替える。"""
    key = os.getenv("GEMINI_API_KEY")
    counts = {c["name"]: c.get("count", DEFAULT_COUNT) for c in categories}
    total = sum(counts.values())
    focus = "\n".join(f"- {c['name']}({counts[c['name']]}本): {c.get('focus', '')}" for c in categories)
    if key:
        from google import genai

        client = genai.Client(api_key=key)
        # 1回目: 見出しから記事を選ぶ
        headlines = "\n".join(
            f"[{i}]【{it['category']}】{it['title']} — {it['summary']}" for i, it in enumerate(items)
        )
        picked = ask_gemini(client, SELECT_PROMPT.format(focus=focus, articles=headlines))
        used, taken = [], {}
        for i in (picked or {}).get("used", []):
            i = int(i)
            cat = items[i]["category"] if 0 <= i < len(items) else None
            if cat and i not in used and taken.get(cat, 0) < counts.get(cat, 0):
                used.append(i)
                taken[cat] = taken.get(cat, 0) + 1
        if not used:
            used = list(range(min(total, len(items))))
        # 選んだ記事だけ本文を取りに行く
        for i in used:
            if fetch:
                fetch_body(items[i])
        print(f"bodies: {sum(1 for i in used if items[i].get('body'))}/{len(used)}")
        # 2回目以降: 種類ごとに本文を読んで原稿と要約を書く(一度に書く量を減らして長さを守らせる)
        parts, digest, done = [], [], []
        for c in categories:
            mine = [i for i in used if items[i]["category"] == c["name"]]
            if not mine:
                continue
            articles = "\n\n".join(
                f"[{i}]{items[i]['title']}\n概要: {items[i]['summary']}\n本文: {items[i].get('body', '')}"
                for i in mine
            )
            data = ask_gemini(client, PROMPT.format(
                category=c["name"], focus=c.get("focus", ""), articles=articles))
            if not (data and data.get("script")):
                continue
            points = data.get("points", [])
            if any(len(p.get("body", "")) < MIN_BODY for p in points):  # 軽いモデルは短く書きがち
                longer = ask_gemini(client, EXPAND_PROMPT.format(
                    points=json.dumps(points, ensure_ascii=False), articles=articles))
                new = (longer or {}).get("points", [])
                if len(new) == len(points):
                    points = [n if len(n.get("body", "")) > len(o.get("body", "")) else o
                              for o, n in zip(points, new)]
            parts.append(data["script"])
            digest.append({"category": c["name"], "points": points})
            done += [int(i) for i in data.get("used", mine)]
        if parts:
            script = (f"おはようございます。{date_label}のニュースです。"
                      + "".join(parts) + "以上、今日のニュースでした。")
            return {"script": script, "digest": merge_digest(digest, categories), "used": done}
    # 予備: AIなしで見出しを読み上げる(種類ごとに数本ずつ)
    print("fallback: headlines only", file=sys.stderr)
    picked = []
    for c in categories:
        picked += [i for i, it in enumerate(items) if it["category"] == c["name"]][:counts[c["name"]]]
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


def synth_edge(text: str, path: Path):
    """Edgeの音声で原稿全体を読み上げ、軽いmp3にする。"""
    import edge_tts

    raw = OUT / "edge.mp3"
    asyncio.run(edge_tts.Communicate(text, EDGE_VOICE, rate=EDGE_RATE).save(str(raw)))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                    "-ac", "1", "-ar", "24000", "-b:a", "48k", str(path)], check=True)
    raw.unlink()


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
        result = write_script(items, categories, date_label, fetch=False)
        synth = synth_dummy
    else:
        items = fetch_news(categories)
        if not items:
            sys.exit("ニュースが1件も取れませんでした")
        result = write_script(items, categories, date_label)
        synth = None

    script = result["script"]
    try:
        if synth:
            raise RuntimeError("dry-run")
        synth_edge(script, OUT / "episode.mp3")
        print(f"voice: edge {EDGE_VOICE}")
    except Exception as e:
        if not synth:  # 本番でEdgeが使えなかったときは VOICEVOX で読む
            print(f"edge-tts failed, use VOICEVOX: {e}", file=sys.stderr)
            wait_voicevox()
            synth = synth_voicevox
        to_mp3([synth(s) for s in split_sentences(script)], OUT / "episode.mp3")

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
    print(f"done: {len(script)} chars")


if __name__ == "__main__":
    main()
