"""今日のエピソードの要約と音声リンクを、LINE公式アカウントから自分に送るスクリプト。

GitHub Actions でサイト公開の後に呼ばれます。
LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID が未設定なら、何もせずに終わります。
"""

import json
import os
import sys
from pathlib import Path

import requests

SITE_URL = os.environ["SITE_URL"].rstrip("/")
LIMIT = 4900  # LINEのテキストは1通5000文字まで


def build_message(meta: dict) -> str:
    lines = [f"【{meta['title']}】"]
    for block in meta.get("digest", []):
        lines += ["", f"■ {block['category']}"]
        lines += [f"・{p}" for p in block.get("points", [])]
    footer = ["", "🔊 音声で聞く・元記事を見る", f"{SITE_URL}/#{meta['date']}"]
    body = "\n".join(lines)
    tail = "\n".join(footer)
    if len(body) + len(tail) > LIMIT:
        body = body[: LIMIT - len(tail) - 2] + "…"
    return body + "\n" + tail


def main():
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")
    if not (token and user_id):
        print("LINEの設定がないので送信をスキップしました")
        return

    meta = json.loads(Path("out/episode.json").read_text(encoding="utf-8"))
    res = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"to": user_id, "messages": [{"type": "text", "text": build_message(meta)}]},
        timeout=30,
    )
    if not res.ok:
        sys.exit(f"LINE送信に失敗しました: {res.status_code} {res.text}")
    print("LINEに送信しました")


if __name__ == "__main__":
    main()
