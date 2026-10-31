"""今日のエピソードの要約と音声リンクを、LINE公式アカウントから送るスクリプト。

GitHub Actions でサイト公開の後に呼ばれます。
LINE_CHANNEL_ACCESS_TOKEN が未設定なら、何もせずに終わります。
LINE_USER_ID があればその人だけに、なければ友だち全員に送ります(社長も友だち登録済み)。
TEST_SEND=true(手動のテスト実行)のときは LINE_TEST_USER_ID の人だけに送り、なければ送りません。

見た目は「表紙(今日の要点＋目次＋音声ボタン)」と「種類ごとのカード(横にめくる)」。
各種類の1枚目には、その種類だけの音声を聞くボタンを付けます。
送る前に LINE に形式チェックを頼み、だめなら昔ながらの文字だけの形で送ります。
"""

import json
import os
import re
import sys
from pathlib import Path

import requests

SITE_URL = os.environ["SITE_URL"].rstrip("/")
LIMIT = 4900  # LINEのテキストは1通5000文字まで
API = "https://api.line.me/v2/bot/message"

# 種類ごとのカードの色(config.json にない名前はグレー)
COLORS = {"生成AI全般": "#2F6FDE", "住宅・不動産": "#2E9E5B", "岡山県の自治体ニュース": "#E07A1F"}
SHORT = {"生成AI全般": "生成AI", "住宅・不動産": "住宅・不動産", "岡山県の自治体ニュース": "岡山県"}


def audio_url(meta: dict, part_id: str = "") -> str:
    """音声のURL。LINEの中ではなく普段のブラウザで開く(画面を消しても聞けるように)。"""
    name = f"{meta['date']}-{part_id}" if part_id else meta["date"]
    return f"{SITE_URL}/audio/{name}.mp3?openExternalBrowser=1"


def split_point(p) -> tuple[str, str]:
    """要約1本を(見出し, 本文)に分ける(古い形式の文字列にも対応)。"""
    if isinstance(p, dict):
        return p.get("headline", ""), p.get("body", "")
    return str(p), ""


def paragraphs(body: str) -> list[str]:
    """本文を段落に分ける。改行がなければ2文ずつ区切る。"""
    parts = [s.strip() for s in body.split("\n") if s.strip()]
    if len(parts) > 1:
        return parts
    sentences = [s for s in re.split(r"(?<=。)", body) if s.strip()]
    return ["".join(sentences[i:i + 2]) for i in range(0, len(sentences), 2)] or [body]


def text(t: str, **kw) -> dict:
    return {"type": "text", "text": t or " ", "wrap": True, **kw}


def cover(meta: dict, digest: list[dict]) -> dict:
    """表紙: 今日の見出し一覧と音声ボタン。"""
    rows = []
    if meta.get("top"):
        rows.append({"type": "box", "layout": "vertical", "backgroundColor": "#FFF7E0", "cornerRadius": "8px",
                     "paddingAll": "12px", "margin": "md", "spacing": "sm",
                     "contents": [text("今日の要点", size="sm", weight="bold", color="#B45309")]
                     + [text(f"・{t}", size="sm", color="#333333") for t in meta["top"]]})
    for block in digest:
        color = COLORS.get(block["category"], "#666666")
        rows.append(text(f"■ {block['category']}", size="sm", weight="bold", color=color, margin="lg"))
        for n, p in enumerate(block.get("points", []), 1):
            rows.append(text(f"{n}. {split_point(p)[0]}", size="sm", color="#333333", margin="sm"))
    bubble = {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1F2937", "paddingAll": "16px",
                   "contents": [text(meta["title"], size="lg", weight="bold", color="#FFFFFF"),
                                text("今日の見出し(下のカードを横にめくって読めます)", size="xs", color="#D1D5DB")]},
        "body": {"type": "box", "layout": "vertical", "contents": rows},
        "footer": {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "primary", "color": "#1F2937",
             "action": {"type": "uri", "label": "全部通しで聞く", "uri": audio_url(meta)}},
            {"type": "button", "style": "link", "height": "sm",
             "action": {"type": "uri", "label": "元記事を見る", "uri": f"{SITE_URL}/#{meta['date']}"}}]},
    }
    return {"type": "flex", "altText": meta["title"], "contents": bubble}


def card(category: str, n: int, total: int, p, listen: str | None = None) -> dict:
    """記事1本のカード。listen があれば下に「この種類だけ聞く」ボタンを付ける。"""
    headline, body = split_point(p)
    color = COLORS.get(category, "#666666")
    contents = [text(headline, size="lg", weight="bold", color="#111111"),
                {"type": "separator", "margin": "md", "color": color}]
    contents += [text(para, size="md", color="#333333", margin="lg", lineSpacing="6px")
                 for para in paragraphs(body)]
    if isinstance(p, dict) and p.get("point"):
        contents.append({"type": "box", "layout": "vertical", "backgroundColor": "#F3F4F6", "cornerRadius": "8px",
                         "paddingAll": "12px", "margin": "lg", "spacing": "xs",
                         "contents": [text("ポイント", size="xs", weight="bold", color=color),
                                      text(p["point"], size="sm", color="#333333", lineSpacing="4px")]})
    bubble = {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": color, "paddingAll": "10px",
                   "contents": [text(f"{SHORT.get(category, category)}  {n} / {total}",
                                     size="sm", weight="bold", color="#FFFFFF")]},
        "body": {"type": "box", "layout": "vertical", "contents": contents},
    }
    if listen:
        bubble["footer"] = {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "primary", "color": color,
             "action": {"type": "uri", "label": f"{SHORT.get(category, category)}だけ聞く", "uri": listen}}]}
    return bubble


def build_flex(meta: dict) -> list[dict]:
    """表紙1通＋種類ごとにカードを横に並べたもの(LINEは1回で最大5通、1通に最大12枚)。"""
    digest = meta.get("digest", [])[:4]
    messages = [cover(meta, digest)]
    has_audio = {pt["id"] for pt in meta.get("parts", [])}
    for block in digest:
        points = block.get("points", [])[:12]
        if not points:
            continue
        listen = audio_url(meta, block["id"]) if block.get("id") in has_audio else None
        cards = [card(block["category"], n, len(points), p, listen if n == 1 else None)
                 for n, p in enumerate(points, 1)]
        messages.append({"type": "flex", "altText": f"{block['category']}のニュース({len(points)}本)",
                         "contents": {"type": "carousel", "contents": cards}})
    return messages


def build_text(meta: dict) -> list[dict]:
    """予備: 文字だけの形(段落の間に空行を入れて読みやすく)。"""
    texts = []
    for block in meta.get("digest", []):
        lines = [f"■ {block['category']}"]
        for p in block.get("points", []):
            headline, body = split_point(p)
            lines.append(f"\n◆{headline}\n" + "\n\n".join(paragraphs(body)) if body else f"\n◆{headline}")
            if isinstance(p, dict) and p.get("point"):
                lines.append(f"\n▶ポイント:{p['point']}")
        texts.append("\n".join(lines))
    texts = texts[:4] or ["今日は要約を作れませんでした。"]
    top = "".join(f"・{t}\n" for t in meta.get("top", []))
    texts[0] = f"【{meta['title']}】\n\n" + (f"今日の要点\n{top}\n" if top else "") + texts[0]
    texts = [t if len(t) <= LIMIT else t[: LIMIT - 1] + "…" for t in texts]
    texts.append(f"音声で聞く\n{audio_url(meta)}\n\n元記事を見る\n{SITE_URL}/#{meta['date']}")
    return [{"type": "text", "text": t} for t in texts]


def main():
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")
    if not token:
        print("LINEの設定がないので送信をスキップしました")
        return
    if os.getenv("TEST_SEND") == "true":
        user_id = os.getenv("LINE_TEST_USER_ID")
        if not user_id:
            print("テスト実行ですが LINE_TEST_USER_ID がないので、全員には送らずに終わります")
            return
        print("テスト実行なので自分だけに送ります")

    meta = json.loads(Path("out/episode.json").read_text(encoding="utf-8"))
    kind = "push" if user_id else "broadcast"
    headers = {"Authorization": f"Bearer {token}"}

    messages = build_flex(meta)
    try:
        check = requests.post(f"{API}/validate/{kind}", headers=headers, json={"messages": messages}, timeout=30)
        problem = None if check.ok else f"{check.status_code} {check.text}"
    except requests.RequestException as e:
        problem = str(e)
    if problem:
        print(f"カード形式が使えないので文字だけで送ります: {problem}", file=sys.stderr)
        messages = build_text(meta)

    body = {"messages": messages}
    if user_id:
        body["to"] = user_id
    res = requests.post(f"{API}/{kind}", headers=headers, json=body, timeout=30)
    if not res.ok:
        sys.exit(f"LINE送信に失敗しました: {res.status_code} {res.text}")
    print("LINEに送信しました")


if __name__ == "__main__":
    main()
