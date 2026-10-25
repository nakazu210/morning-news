"""今日のエピソードの要約と音声リンクを、LINE公式アカウントから自分に送るスクリプト。

GitHub Actions でサイト公開の後に呼ばれます。
LINE_CHANNEL_ACCESS_TOKEN が未設定なら、何もせずに終わります。
LINE_USER_ID があればその人だけに、なければ友だち全員(=自分だけ)に送ります。
"""

import json
import os
import sys
from pathlib import Path

import requests

SITE_URL = os.environ["SITE_URL"].rstrip("/")
LIMIT = 4900  # LINEのテキストは1通5000文字まで


def format_point(p) -> str:
    """要約1本を「◆見出し+本文」の形にする(古い形式の文字列にも対応)。"""
    if isinstance(p, dict):
        return f"◆{p.get('headline', '')}\n{p.get('body', '')}"
    return f"◆{p}"


def build_messages(meta: dict) -> list[str]:
    """種類ごとに1つの吹き出しにする(LINEは1回で最大5つまで送れる)。"""
    texts = []
    for block in meta.get("digest", []):
        lines = [f"■ {block['category']}"]
        lines += ["\n" + format_point(p) for p in block.get("points", [])]
        texts.append("\n".join(lines))
    texts = texts[:4] or ["今日は要約を作れませんでした。"]
    texts[0] = f"【{meta['title']}】\n\n" + texts[0]
    texts = [t if len(t) <= LIMIT else t[: LIMIT - 1] + "…" for t in texts]
    texts.append(f"🔊 音声で聞く・元記事を見る\n{SITE_URL}/#{meta['date']}")
    return texts


def main():
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")
    if not token:
        print("LINEの設定がないので送信をスキップしました")
        return

    meta = json.loads(Path("out/episode.json").read_text(encoding="utf-8"))
    body = {"messages": [{"type": "text", "text": t} for t in build_messages(meta)]}
    if user_id:
        url = "https://api.line.me/v2/bot/message/push"
        body["to"] = user_id
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"
    res = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=30,
    )
    if not res.ok:
        sys.exit(f"LINE送信に失敗しました: {res.status_code} {res.text}")
    print("LINEに送信しました")


if __name__ == "__main__":
    main()
