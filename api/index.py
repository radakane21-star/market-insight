import os
import json
import re
import html
import base64
import datetime
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler
import requests
import google.generativeai as genai

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    """RSSのdescriptionに含まれるHTMLタグ・エンティティを安全に除去する。
    XMLパーサーを使わないため、不正なHTML構造でも例外を起こさない。"""
    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    return html.unescape(no_tags).strip()

RSS_SOURCES = [
    {
        "name": "Hatena Bookmark - IT",
        "url": "https://b.hatena.ne.jp/hotentry/it.rss",
    },
    {
        "name": "Hatena Bookmark - Economics",
        "url": "https://b.hatena.ne.jp/hotentry/economics.rss",
    },
    {
        "name": "Hatena Bookmark - Life",
        "url": "https://b.hatena.ne.jp/hotentry/life.rss",
    },
]

REQUEST_TIMEOUT = 8
MAX_ITEMS_PER_SOURCE = 8
MAX_HISTORY = 24
HISTORY_PATH = "data/history.json"


def _github_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "MarketInsight/1.0",
    }


def load_history():
    """GitHub上のdata/history.jsonを読み込む。
    存在しない/読み込み失敗時は (空リスト, None) を返す（クラッシュさせない）。"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return [], None

    url = f"https://api.github.com/repos/{repo}/contents/{HISTORY_PATH}"
    try:
        resp = requests.get(url, headers=_github_headers(token), timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return [], None
        if resp.status_code != 200:
            return [], None

        data = resp.json()
        sha = data.get("sha")
        content = base64.b64decode(data["content"]).decode("utf-8")
        history = json.loads(content)
        if not isinstance(history, list):
            return [], sha
        return history, sha

    except Exception:
        return [], None


def save_history(history, sha):
    """history(list)をGitHub上のdata/history.jsonに保存する。
    失敗してもクラッシュさせず、呼び出し元に成功/失敗だけを返す。"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return False, "GITHUB_TOKEN/GITHUB_REPOが未設定"

    url = f"https://api.github.com/repos/{repo}/contents/{HISTORY_PATH}"
    body_text = json.dumps(history, ensure_ascii=False, indent=2)
    payload = {
        "message": "Update market insight history",
        "content": base64.b64encode(body_text.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha

    try:
        resp = requests.put(
            url,
            headers=_github_headers(token),
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (200, 201):
            return True, None
        if resp.status_code == 409:
            return False, "競合のため今回はスキップしました"
        return False, f"GitHub保存エラー: HTTP {resp.status_code}"

    except Exception as e:
        return False, f"GitHub保存エラー: {e}"


def fetch_feed_items(source):
    """1つのRSSソースから記事タイトル・概要を抽出する。失敗時は例外を投げず空リストを返す。"""
    items = []
    try:
        resp = requests.get(
            source["url"],
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "MarketInsight/1.0 (+https://vercel.com)"},
        )
        if resp.status_code != 200:
            return items, f"{source['name']}: HTTPステータス {resp.status_code}"

        root = ET.fromstring(resp.content)

        all_items_el = [el for el in root.iter() if el.tag.split("}")[-1] == "item"]

        for item in all_items_el[:MAX_ITEMS_PER_SOURCE]:
            title_el = None
            desc_el = None
            for child in item:
                local_name = child.tag.split("}")[-1]
                if local_name == "title" and title_el is None:
                    title_el = child
                elif local_name == "description" and desc_el is None:
                    desc_el = child

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            description = strip_html(desc_el.text) if desc_el is not None and desc_el.text else ""
            if title:
                items.append({"source": source["name"], "title": title, "description": description})

        if not items:
            return items, f"{source['name']}: 0件のアイテムが見つかりませんでした（root tag: {root.tag}）"

        return items, None

    except ET.ParseError as e:
        return items, f"{source['name']}: XMLパースエラー ({e})"
    except requests.exceptions.RequestException as e:
        return items, f"{source['name']}: 通信エラー ({e})"
    except Exception as e:
        return items, f"{source['name']}: 予期しないエラー ({e})"


def build_context_text(all_items):
    lines = []
    for it in all_items:
        line = f"・[{it['source']}] {it['title']}"
        if it["description"]:
            desc = it["description"][:200]
            line += f"\n  概要: {desc}"
        lines.append(line)
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "あなたは優秀なビジネスデータアナリストです。提供された最新のトレンド・関心事データから、"
    "現在市場や世の中のビジネスパーソンがどのような業務やテーマに高い関心を持っているかを客観的に分析してください。\n\n"
    "【分析すべき項目】\n"
    "1. 現在のトレンドから読み取れる、人々の関心・関心事の主要テーマ（上位3つ）\n"
    "2. それらのテーマに関連して、世の中でどのような「業務効率化」や「デジタル自動化ツール」の潜在的需要が考えられるか、客観的な考察\n"
    "3. ユーザーに対して健全かつ高い価値を提供できる、具体的なサービス・ツール企画のヒント\n\n"
    "【制約事項】\n"
    "特定の個人の心理的な弱みや不安を煽るような表現、または商業的な搾取を助長するような提案は一切排除し、"
    "一貫して「技術的な不便の解消」や「業務効率の改善」という客観的かつ健全な視点でレポートを、Markdown形式で作成してください。"
)


def get_result_with_history():
    """
    1時間以内に最新の履歴があればそれを再利用し、なければ新規に分析して
    履歴(GitHub上のdata/history.json、最大MAX_HISTORY件)に追記保存する。
    GitHub未設定・取得失敗時は、履歴機能なしで毎回新規分析する。
    """
    history, sha = load_history()
    now = datetime.datetime.now(datetime.timezone.utc)

    if history:
        try:
            latest = history[0]
            collected_at = datetime.datetime.fromisoformat(latest["collected_at"])
            age = now - collected_at
            if age < datetime.timedelta(hours=1):
                result = dict(latest["result"])
                result["history"] = history
                result["from_cache"] = True
                return result
        except Exception:
            pass

    result = get_market_analysis()
    result["from_cache"] = False

    entry = {
        "collected_at": now.isoformat(),
        "result": {
            "status": result["status"],
            "sources_used": result["sources_used"],
            "source_errors": result["source_errors"],
            "analysis": result["analysis"],
            "error": result["error"],
        },
    }
    new_history = [entry] + history
    new_history = new_history[:MAX_HISTORY]

    saved, save_err = save_history(new_history, sha)
    result["history_save"] = "ok" if saved else (save_err or "保存スキップ")
    result["history"] = new_history

    return result


def get_market_analysis():
    """
    複数の公式RSSからデータを取得し、Gemini APIで客観的な市場分析を行う。
    どのステップで失敗しても、エラー情報を含むJSON文字列を返す（クラッシュさせない）。
    """
    result = {
        "status": "ok",
        "sources_used": [],
        "source_errors": [],
        "analysis": None,
        "error": None,
    }

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        result["status"] = "error"
        result["error"] = (
            "環境変数 'GEMINI_API_KEY' が設定されていません。"
            "Vercelのダッシュボードで設定してください。"
        )
        return result

    all_items = []
    for source in RSS_SOURCES:
        items, err = fetch_feed_items(source)
        if items:
            all_items.extend(items)
            result["sources_used"].append(source["name"])
        if err:
            result["source_errors"].append(err)

    if not all_items:
        result["status"] = "error"
        result["error"] = "全てのデータソースから有効な情報を取得できませんでした。"
        return result

    context_text = build_context_text(all_items)

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")

        full_prompt = f"{SYSTEM_PROMPT}\n\n--- 分析対象データ ---\n{context_text}"

        response = model.generate_content(full_prompt)

        analysis_text = getattr(response, "text", None)
        if not analysis_text:
            result["status"] = "error"
            result["error"] = "Geminiからの応答が空でした（セーフティブロック等の可能性があります）。"
            return result

        result["analysis"] = analysis_text
        return result

    except Exception as e:
        result["status"] = "error"
        result["error"] = f"Gemini APIの呼び出し中にエラーが発生しました: {e}"
        return result


def render_markdown(result):
    """分析結果を読みやすいMarkdownにレンダリングする"""
    lines = []
    lines.append("# MarketInsight レポート\n")

    if result["status"] == "error":
        lines.append("## ⚠️ エラー\n")
        lines.append(result["error"] or "不明なエラーが発生しました。")
        if result["source_errors"]:
            lines.append("\n## データソースの個別エラー\n")
            for e in result["source_errors"]:
                lines.append(f"- {e}")
        return "\n".join(lines)

    lines.append(f"**使用したデータソース**: {', '.join(result['sources_used'])}\n")

    if result["source_errors"]:
        lines.append("**一部のソースで取得に失敗しました（処理は継続しています）**\n")
        for e in result["source_errors"]:
            lines.append(f"- {e}")
        lines.append("")

    lines.append("---\n")
    lines.append(result["analysis"])

    cache_note = "（キャッシュ済みデータ）" if result.get("from_cache") else "（新規取得）"
    lines.append(f"\n\n---\n*{cache_note}*")

    history = result.get("history") or []
    if len(history) > 1:
        lines.append("\n## 📚 過去の履歴\n")
        for entry in history[1:]:
            ts = entry.get("collected_at", "?")
            r = entry.get("result", {})
            status_mark = "✅" if r.get("status") == "ok" else "⚠️"
            lines.append(f"- {status_mark} {ts}")

    return "\n".join(lines)


class handler(BaseHTTPRequestHandler):
    """Vercelがリクエストを受け取るためのハンドラークラス"""

    def do_GET(self):
        try:
            result = get_result_with_history()

            wants_json = "format=json" in (self.path or "")

            if wants_json:
                body = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                body = render_markdown(result).encode("utf-8")
                content_type = "text/markdown; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-type", content_type)
            self.end_headers()
            self.wfile.write(body)

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"システムレベルの予期しないエラー: {e}".encode("utf-8"))
            