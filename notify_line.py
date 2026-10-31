"""今日のエピソードの要約と音声リンクを、LINE公式アカウントから送るスクリプト。

GitHub Actions でサイト公開の後に呼ばれます。
LINE_CHANNEL_ACCESS_TOKEN が未設定なら、何もせずに終わります。
LINE_USER_ID があればその人だけに、なければ友だち全員に送ります(社長も友だち登録済み)。

見た目は「表紙(目次＋音声ボタン)」と「種類ごとのカード(横にめくる)」。
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
             "action": {"type": "uri", "label": "音声で聞く・元記事を見る", "uri": f"{SITE_URL}/#{meta['date']}"}}]},
    }
    return {"type": "flex", "altText": meta["title"], "contents": bubble}


def card(category: str, n: int, total: int, p) -> dict:
    headline, body = split_point(p)
    color = COLORS.get(category, "#666666")
    contents = [text(headline, size="lg", weight="bold", color="#111111"),
                {"type": "separator", "margin": "md", "color": color}]
    contents += [text(para, size="md", color="#333333", margin="lg", lineSpacing="6px")
                 for para in paragraphs(body)]
    return {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": color, "paddingAll": "10px",
                   "contents": [text(f"{SHORT.get(category, category)}  {n} / {total}",
                                     size="sm", weight="bold", color="#FFFFFF")]},
        "body": {"type": "box", "layout": "vertical", "contents": contents},
    }


def build_flex(meta: dict) -> list[dict]:
    """表紙1通＋種類ごとにカードを横に並べたもの(LINEは1回で最大5通、1通に最大12枚)。"""
    digest = meta.get("digest", [])[:4]
    messages = [cover(meta, digest)]
    for block in digest:
        points = block.get("points", [])[:12]
        if not points:
            continue
        cards = [card(block["category"], n, len(points), p) for n, p in enumerate(points, 1)]
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
        texts.append("\n".join(lines))
    texts = texts[:4] or ["今日は要約を作れませんでした。"]
    texts[0] = f"【{meta['title']}】\n\n" + texts[0]
    texts = [t if len(t) <= LIMIT else t[: LIMIT - 1] + "…" for t in texts]
    texts.append(f"音声で聞く・元記事を見る\n{SITE_URL}/#{meta['date']}")
    return [{"type": "text", "text": t} for t in texts]


def main():
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")
    if not token:
        print("LINEの設定がないので送信をスキップしました")
        return

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
