import os
import json
import re
import html
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


def fetch_feed_items(source):
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

        for item in root.findall(".//item")[:MAX_ITEMS_PER_SOURCE]:
            title_el = item.find("title")
            desc_el = item.find("description")
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            description = strip_html(desc_el.text) if desc_el is not None and desc_el.text else ""
            if title:
                items.append({"source": source["name"], "title": title, "description": description})

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


def get_market_analysis():
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

    return "\n".join(lines)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            result = get_market_analysis()

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