"""保存してある直近のエピソードから、Pages用のサイト(feed.xml / index.html)を作る。

入力: episodes/<日付>/episode.mp3, episode.json
出力: public/ (このフォルダだけがPagesに公開されます。管理画面 admin.html もここに置きます)
"""

import email.utils
import datetime as dt
import html
import json
import os
import shutil
import subprocess
from pathlib import Path

SITE_URL = os.environ["SITE_URL"].rstrip("/")  # 例: https://ユーザー名.github.io/morning-news
SHOW_TITLE = os.getenv("SHOW_TITLE", "毎朝のニュース")
REPO = os.getenv("GITHUB_REPOSITORY", "")  # 例: ユーザー名/morning-news
# 読み上げの声のクレジット(VOICEVOX は「VOICEVOX:キャラクター名」の表記が必要)
_voice = json.loads(Path("config.json").read_text(encoding="utf-8")).get("voice", {})
CREDIT = os.getenv("VOICE_CREDIT") or (
    "Microsoft Edge 音声読み上げ" if _voice.get("engine") == "edge"
    else f"VOICEVOX:{_voice.get('name', '春日部つむぎ').split('(')[0]}(予備: Microsoft Edge 音声読み上げ)")
JST = dt.timezone(dt.timedelta(hours=9))

src, pub = Path("episodes"), Path("public")
shutil.rmtree(pub, ignore_errors=True)
(pub / "audio").mkdir(parents=True)


def duration(mp3: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(mp3)], capture_output=True, text=True)
    try:
        return int(float(out.stdout.strip()))
    except ValueError:
        return 0


episodes = []
for d in sorted(src.iterdir(), reverse=True) if src.exists() else []:
    meta_file, mp3 = d / "episode.json", d / "episode.mp3"
    if not (meta_file.exists() and mp3.exists()):
        continue
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    name = f"{meta['date']}.mp3"
    shutil.copy(mp3, pub / "audio" / name)
    meta.update(url=f"{SITE_URL}/audio/{name}", size=mp3.stat().st_size, secs=duration(mp3))
    episodes.append(meta)


def notes(ep) -> str:
    links = "".join(f'<li>【{html.escape(s.get("category", ""))}】'
                    f'<a href="{html.escape(s["link"])}">{html.escape(s["title"])}</a></li>'
                    for s in ep["sources"])
    return f"<p>元記事:</p><ul>{links}</ul><p>{html.escape(CREDIT)}</p>"


items = ""
for ep in episodes:
    pub_date = dt.datetime.fromisoformat(ep["date"]).replace(hour=6, tzinfo=JST)
    items += f"""
  <item>
    <title>{html.escape(ep['title'])}</title>
    <description><![CDATA[{notes(ep)}]]></description>
    <enclosure url="{ep['url']}" length="{ep['size']}" type="audio/mpeg"/>
    <guid isPermaLink="false">{ep['date']}</guid>
    <pubDate>{email.utils.format_datetime(pub_date)}</pubDate>
    <itunes:duration>{ep['secs']}</itunes:duration>
  </item>"""

feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>{html.escape(SHOW_TITLE)}</title>
  <link>{SITE_URL}/</link>
  <language>ja</language>
  <description>生成AI・住宅・岡山県のニュースを毎朝自動要約。音声: {html.escape(CREDIT)}</description>
  <itunes:image href="{SITE_URL}/cover.png"/>
  <itunes:explicit>false</itunes:explicit>
  <itunes:block>Yes</itunes:block>{items}
</channel>
</rss>
"""
(pub / "feed.xml").write_text(feed, encoding="utf-8")

# カバー画像(Apple Podcastsは1400px以上の正方形を推奨)
from PIL import Image, ImageDraw, ImageFont

img = Image.new("RGB", (1400, 1400), "#14213d")
draw = ImageDraw.Draw(img)
font = ImageFont.load_default(size=260)
draw.text((700, 620), "AM", fill="#fca311", font=font, anchor="mm")
draw.text((700, 860), "NEWS", fill="#ffffff", font=ImageFont.load_default(size=150), anchor="mm")
img.save(pub / "cover.png")

# スマホで開いてそのまま聞けるページ
rows = "".join(
    f"<section id=\"{ep['date']}\"><h2>{html.escape(ep['title'])}</h2>"
    f'<audio controls preload="none" src="audio/{ep["date"]}.mp3"></audio>'
    f"<details><summary>原稿と元記事</summary><p>{html.escape(ep['script'])}</p>{notes(ep)}</details></section>"
    for ep in episodes
)
page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{html.escape(SHOW_TITLE)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:auto;padding:16px;background:#f7f7f5;color:#222}}
section{{background:#fff;border-radius:12px;padding:16px;margin:12px 0}}audio{{width:100%}}
h2{{font-size:1.05rem;margin:0 0 8px}}details{{margin-top:8px;font-size:.9rem;line-height:1.7}}
code{{background:#eee;padding:2px 6px;border-radius:4px;word-break:break-all}}</style></head><body>
<h1>{html.escape(SHOW_TITLE)}</h1>
<p><a href="admin.html">ニュースの種類を選ぶ(管理画面)</a></p>
<p>ポッドキャストアプリ登録用URL:<br><code>{SITE_URL}/feed.xml</code></p>
{rows or '<p>まだエピソードがありません。</p>'}
</body></html>"""
(pub / "index.html").write_text(page, encoding="utf-8")
print(f"built {len(episodes)} episodes")

# 管理画面(リポジトリ名だけ埋め込んでコピー)
admin = Path("admin.html").read_text(encoding="utf-8").replace("__REPO__", REPO)
(pub / "admin.html").write_text(admin, encoding="utf-8")
