"""ニュースを集めて、原稿とLINE用の要約を作り、音声で読み上げてmp3にするスクリプト。

読み上げの声は config.json の voice で選びます。
VOICEVOX(かわいい声のキャラクター)が基本で、使えなかったときだけ Microsoft Edge の音声に切り替えます。

GitHub Actions から毎朝呼ばれます。出力:
  out/episode.mp3   … 今日の音声(全部通し)
  out/episode-<種類のid>.mp3 … 種類ごとの音声(LINEの各ジャンルのボタンから聞く)
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
# VOICEVOX の声の番号と速さ。config.json の voice が優先されます
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


def load_config() -> dict:
    return json.loads(Path("config.json").read_text(encoding="utf-8"))


def load_categories() -> list[dict]:
    """config.json から、オンになっているニュースの種類だけを返す。"""
    return [c for c in load_config()["categories"] if c.get("enabled")]


def load_voice() -> dict:
    """config.json の voice(読み上げの声)を返す。書いていなければ VOICEVOX の春日部つむぎ。
    voice に rotation(声の一覧)があれば、日替わりで順番に使う。"""
    voice = {"engine": "voicevox", "speaker": SPEAKER_ID, "speed": SPEED}
    voice.update(load_config().get("voice", {}))
    rotation = voice.pop("rotation", [])
    if rotation:
        voice.update(rotation[dt.datetime.now(JST).toordinal() % len(rotation)])
    return voice


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
            if not title or key in seen or any(w in title for w in cat.get("exclude", [])):
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

- 種類ごとに上限の本数まで選ぶ。ただし重視してほしい内容に本当に合う記事だけを選び、合う記事が少ない種類は少なくてよい(0本でもよい)。数合わせで関係の薄い記事を選ばない。
- 自治体のニュースは、住民や事業者が使える補助金・助成・支援制度、住宅・移住・子育て・まちづくりの政策を優先し、業務委託の公募・入札・職員採用・イベント告知・施設の休館などは選ばない。
- 重視してほしい内容に合わない記事(芸能、スポーツの結果、事件・事故、他県だけの話題、海外だけの住宅・不動産の話題、個人ブログや広告記事など)は選ばない。
- 英語の記事も選んでよい。新しいAIモデルや新機能の発表は、公式ブログや英語の一次情報を優先する。
- 生成AIは「新しいAIモデルの発表」「ChatGPT・Claude・Geminiなど主要AIの新機能・アップデート」を最優先し、資金調達・パソコンなどのハードウェア製品・政治の話題は、それが足りないときだけ選ぶ。
- 同じ出来事を扱う記事は1本だけ選ぶ。

出力はJSONのみ: {{"used": [選んだ記事の番号]}}

記事一覧:
{articles}
"""

PROMPT = """あなたは新聞記者です。
以下の「{category}」の記事(本文つき)をもとに、LINEで読む新聞のような記事要約を作ってください。
この要約は目で読むためのものです(音声は別の仕組みで読み上げ用に直すので、読み上げ向けに書きかえない)。

この種類で重視してほしい内容: {focus}

ルール(要約 points):
- すべての記事について、新聞記事のように「見出し」と「本文」を書く。
- 見出しは20〜30文字で、何が起きたかが一目でわかるように。
- 本文は必ず400〜600文字(短すぎるのは不可)。最初の1文で要点(誰が・何を・いつ)を伝え、続けて具体的な数字・金額・期間・対象・条件・性能・料金・提供開始時期などの中身をできるだけ多く、最後に背景や影響(なぜ重要か、これまでとの違い、競合との比較)を書く。
- 英語の記事も日本語で書く。だ・である調で書く。
- 数字は算用数字で書く(例: 10月29日、0.25%、330万円、約1兆)。漢数字やカタカナの読みに書きかえない。
- 会社名・製品名・モデル名は元の表記のまま英字で書く(例: Mistral AI、Hugging Face、GLM-5.3)。カタカナに書きかえない。
- 本文は内容の切れ目で2〜3段落に分け、段落の間は改行(\\n)を1つ入れる。
- 「〜について発表しました」のような中身のない書き方はしない。本文に書かれた具体的な情報を必ず入れる。
- 本文が空の記事は、見出しと概要からわかる範囲で書き、推測で補わない。
- 本文がログイン画面・セキュリティチェック・エラー表示など記事でない場合は、その記事を要約から外す。「詳細は不明」のような書き方はしない。
- 箇条書き・URLは使わない。記事にない事実は足さない。
- 見出しと本文に出てきた英字の名前は、すべて readings にカタカナの読み方を入れる(音声の読み上げに使う)。
{point_rule}
出力はJSONのみ:
{{"points": [{{"headline": "見出し", "body": "本文"{point_field}}}], "used": [使った記事の番号], "readings": {{"Hugging Face": "ハギングフェイス"}}}}

記事一覧:
{articles}
"""


EXPAND_PROMPT = """以下のニュース要約は短すぎます。元の記事をもとに、それぞれの本文を450〜600文字に書き直してください。
具体的な数字・金額・期間・対象・条件・性能・料金などの中身と、背景や影響(なぜ重要か、これまでとの違い)を足してください。
記事にない事実は足さない。だ・である調。見出しは変えない。
数字は算用数字、会社名・製品名は英字のまま書く。本文は2〜3段落に分け、段落の間は改行(\\n)を1つ入れる。
本文に出てきた英字の名前は、すべて readings にカタカナの読み方を入れる。

出力はJSONのみ: {{"points": [{{"headline": "見出し", "body": "本文"}}], "readings": {{"英字の名前": "カタカナの読み"}}}}

短い要約:
{points}

元の記事:
{articles}
"""
POINT_RULE = """- 各記事に point(60〜100文字)を付ける。{point}を、記事の事実にもとづいて具体的に書く(記事にない数字や事実は足さない)。
"""

TOP_PROMPT = """以下は今朝のニュース要約です。忙しい人が表紙だけ見ても大事なことがわかるように、
今日いちばん押さえておきたい要点を3つ、それぞれ40〜60文字で書いてください。
- 種類にかたよらず、仕事や生活への影響が大きいものを選ぶ。
- 数字は算用数字、会社名・製品名は英字のまま。だ・である調。記事にない事実は足さない。

出力はJSONのみ: {{"top": ["要点1", "要点2", "要点3"]}}

ニュース要約:
{digest}
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
    ids = {c["name"]: c.get("id", "") for c in categories}
    names = sorted(merged, key=lambda n: order.index(n) if n in order else len(order))
    return [{"category": n, "id": ids.get(n, ""), "points": merged[n]} for n in names if merged[n]]


# 読み上げで英字を正しく読ませるための読み方(LINEの文章はそのまま。音声だけ置きかえる)
READINGS = {
    "ChatGPT": "チャットジーピーティー", "OpenAI": "オープンエーアイ", "Anthropic": "アンソロピック",
    "Claude": "クロード", "Gemini": "ジェミニ", "DeepMind": "ディープマインド", "Google": "グーグル",
    "Microsoft": "マイクロソフト", "Copilot": "コパイロット", "Apple": "アップル", "Amazon": "アマゾン",
    "Meta": "メタ", "Llama": "ラマ", "NVIDIA": "エヌビディア", "Nvidia": "エヌビディア",
    "xAI": "エックスエーアイ", "Grok": "グロック", "Mistral": "ミストラル", "DeepSeek": "ディープシーク",
    "Sora": "ソラ", "Codex": "コーデックス", "Opus": "オーパス", "Sonnet": "ソネット", "Haiku": "ハイク",
    "Flash": "フラッシュ", "Pro": "プロ", "API": "エーピーアイ", "GPU": "ジーピーユー",
    "GPT": "ジーピーティー", "AI": "エーアイ", "ZEH": "ゼッチ", "GX": "ジーエックス",
}


def for_reading(text: str, extra: dict | None = None) -> str:
    """音声用に、英字の名前をカタカナにし、読まなくてよい記号や改行を外す。
    extra は Gemini がその日の記事について教えてくれた読み方(READINGS より優先)。"""
    readings = {**READINGS, **(extra or {})}
    text = re.sub(r"https?://\S+", "", text).replace("\\n", " ").replace("\n", " ")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)  # 1,500 → 1500(区切りの読み間違いを防ぐ)
    text = re.sub(r"(?<=[\d%])[〜~～](?=\d)", "から", text)  # 0.25〜0.5% → 0.25から0.5%
    for word in sorted(readings, key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", readings[word], text)
    return re.sub(r"[「」『』【】〈〉《》（）()\[\]・※*#]|(?<=ー)-", " ", text)


def digest_to_parts(digest: list[dict], date_label: str, readings: dict | None = None,
                    top: list[str] | None = None) -> list[dict]:
    """LINEの要約(見出し＋本文＋ポイント)を、全部そのまま読み上げる原稿にする。
    種類ごとに分けておき、全部通しの音声と、種類ごとの音声の両方を作れるようにする。"""
    intro = f"おはようございます。{date_label}のニュースです。"
    if top:
        intro += "まずは今日の要点です。" + "".join(
            f"{n}つ目。{t}" + ("" if t.endswith("。") else "。") for n, t in enumerate(top, 1))
    parts = [{"id": "", "text": intro}]
    for block in digest:
        lines = [f"{block['category']}のニュースです。"]
        for n, p in enumerate(block["points"], 1):
            if isinstance(p, dict):
                lines.append(f"{n}本目。{p.get('headline', '')}。{p.get('body', '')}")
                if p.get("point"):
                    lines.append(f"ポイント。{p['point']}")
            else:
                lines.append(f"{n}本目。{p}。")
        parts.append({"id": block.get("id", ""), "category": block["category"], "text": "".join(lines)})
    parts.append({"id": "", "text": "以上、今日のニュースでした。今日もいってらっしゃい。"})
    for part in parts:
        part["text"] = for_reading(part["text"], readings)
    return parts


def digest_to_script(digest: list[dict], date_label: str, readings: dict | None = None) -> str:
    """全部通しの原稿(声の聞き比べなどで使う)。"""
    return "".join(p["text"] for p in digest_to_parts(digest, date_label, readings))


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
        digest, done, readings = [], [], {}
        for c in categories:
            mine = [i for i in used if items[i]["category"] == c["name"]]
            if not mine:
                continue
            articles = "\n\n".join(
                f"[{i}]{items[i]['title']}\n概要: {items[i]['summary']}\n本文: {items[i].get('body', '')}"
                for i in mine
            )
            point = c.get("point")
            data = ask_gemini(client, PROMPT.format(
                category=c["name"], focus=c.get("focus", ""), articles=articles,
                point_rule=POINT_RULE.format(point=point) if point else "",
                point_field=', "point": "ポイント"' if point else ""))
            if not (data and data.get("points")):
                continue
            points = data.get("points", [])
            readings.update(data.get("readings") or {})
            if any(len(p.get("body", "")) < MIN_BODY for p in points):  # 軽いモデルは短く書きがち
                longer = ask_gemini(client, EXPAND_PROMPT.format(
                    points=json.dumps(points, ensure_ascii=False), articles=articles))
                new = (longer or {}).get("points", [])
                readings.update((longer or {}).get("readings") or {})
                if len(new) == len(points):
                    points = [{**o, **n} if len(n.get("body", "")) > len(o.get("body", "")) else o
                              for o, n in zip(points, new)]
            digest.append({"category": c["name"], "points": points})
            done += [int(i) for i in data.get("used", mine)]
        if digest:
            digest = merge_digest(digest, categories)
            readings = {k: v for k, v in readings.items()
                        if isinstance(k, str) and isinstance(v, str) and re.search(r"[A-Za-z]", k)}
            # 最後に: 全体から今日の要点を3つ(失敗しても要点なしで続ける)
            summary = "\n".join(f"【{b['category']}】{p.get('headline', '')}: {p.get('body', '')}"
                                for b in digest for p in b["points"] if isinstance(p, dict))
            top = [t for t in ((ask_gemini(client, TOP_PROMPT.format(digest=summary)) or {})
                               .get("top") or []) if isinstance(t, str) and t.strip()][:3]
            return {"parts": digest_to_parts(digest, date_label, readings, top),
                    "digest": digest, "top": top, "used": done}
    # 予備: AIなしで見出しを読み上げる(種類ごとに数本ずつ)
    print("fallback: headlines only", file=sys.stderr)
    picked = []
    for c in categories:
        picked += [i for i, it in enumerate(items) if it["category"] == c["name"]][:counts[c["name"]]]
    digest = [
        {"category": c["name"], "id": c.get("id", ""),
         "points": [items[i]["title"] for i in picked if items[i]["category"] == c["name"]]}
        for c in categories
    ]
    digest = [d for d in digest if d["points"]]
    return {"parts": digest_to_parts(digest, date_label), "digest": digest, "top": [], "used": picked}


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


def synth_voicevox(text: str, speaker: int = SPEAKER_ID, speed: float = SPEED) -> bytes:
    q = requests.post(f"{VOICEVOX_URL}/audio_query",
                      params={"text": text, "speaker": speaker}, timeout=60)
    q.raise_for_status()
    query = q.json()
    query["speedScale"] = speed
    query["postPhonemeLength"] = 0.35  # 文と文の間を少しあける
    s = requests.post(f"{VOICEVOX_URL}/synthesis",
                      params={"speaker": speaker}, json=query, timeout=120)
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
    raise RuntimeError("VOICEVOX engine did not start")


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


def find_speaker(voice: dict) -> int:
    """config.json の name(キャラクター名)と style から VOICEVOX の声の番号を調べる。
    見つからなければ speaker の番号を使う。"""
    name, style = voice.get("name", "").split("(")[0], voice.get("style", "ノーマル")
    for sp in requests.get(f"{VOICEVOX_URL}/speakers", timeout=30).json():
        if sp["name"] == name:
            styles = {st["name"]: st["id"] for st in sp["styles"]}
            return styles.get(style, sp["styles"][0]["id"])
    print(f"voice {name} not found, use speaker {voice['speaker']}", file=sys.stderr)
    return int(voice["speaker"])


def part_files(parts: list[dict]) -> dict:
    """種類ごとの音声ファイル名(id がない部分=あいさつ等は全部通しにだけ入る)。"""
    return {p["id"]: OUT / f"episode-{p['id']}.mp3" for p in parts if p.get("id")}


def speak_parts(parts: list[dict], synth):
    """部分ごとに読み上げ、全部通しの音声と種類ごとの音声を作る(1回の読み上げで両方作る)。"""
    chunks = [[synth(s) for s in split_sentences(p["text"])] for p in parts]
    to_mp3([c for cs in chunks for c in cs], OUT / "episode.mp3")
    for p, cs in zip(parts, chunks):
        if p.get("id"):
            to_mp3(cs, part_files(parts)[p["id"]])


def speak(parts: list[dict], voice: dict):
    """選んだ声で読み上げる。うまくいかなければもう一方の読み上げに切り替える。"""
    def by_voicevox():
        wait_voicevox()
        speaker = find_speaker(voice)
        speak_parts(parts, lambda s: synth_voicevox(s, speaker, float(voice["speed"])))
        print(f"voice: voicevox {voice.get('name')} {voice.get('style')} ({speaker})")

    def by_edge():
        synth_edge("".join(p["text"] for p in parts), OUT / "episode.mp3")
        for p in parts:
            if p.get("id"):
                synth_edge(p["text"], part_files(parts)[p["id"]])
        print(f"voice: edge {EDGE_VOICE}")

    first, backup = (by_edge, by_voicevox) if voice["engine"] == "edge" else (by_voicevox, by_edge)
    try:
        first()
    except Exception as e:
        print(f"voice failed, use backup: {e}", file=sys.stderr)
        backup()


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

    parts = result["parts"]
    script = "".join(p["text"] for p in parts)
    for old in OUT.glob("episode-*.mp3"):  # 前回の種類ごとの音声が残らないように
        old.unlink()
    if synth:
        speak_parts(parts, synth)
    else:
        speak(parts, load_voice())

    sources = [items[i] for i in result["used"] if 0 <= i < len(items)]
    meta = {
        "date": today.strftime("%Y-%m-%d"),
        "voice": load_voice().get("name", ""),
        "title": f"{date_label}の朝ニュース",
        "script": script,
        "top": result.get("top", []),
        "digest": result["digest"],
        "parts": [{"id": p["id"], "category": p["category"]} for p in parts if p.get("id")],
        "sources": [{"category": s["category"], "title": s["title"], "link": s["link"]}
                    for s in sources],
    }
    (OUT / "episode.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: {len(script)} chars")


if __name__ == "__main__":
    main()
