import argparse
import json
import re
import sqlite3
import webbrowser
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"

TOPIC_SPLIT = re.compile(r"\s*;\s*")


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def get_stats(connection: sqlite3.Connection) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT
          COUNT(*) AS papers,
          SUM(CASE WHEN COALESCE(abstract, '') <> '' THEN 1 ELSE 0 END) AS abstracts,
          SUM(CASE WHEN COALESCE(abstract_zh, '') <> '' THEN 1 ELSE 0 END) AS translated,
          SUM(CASE WHEN COALESCE(supp_url, '') <> '' THEN 1 ELSE 0 END) AS supplemental,
          SUM(CASE WHEN COALESCE(code_url, '') <> '' THEN 1 ELSE 0 END) AS code,
          SUM(CASE WHEN COALESCE(reviews, '') <> '' THEN 1 ELSE 0 END) AS reviewed
        FROM papers
        """
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def get_topics(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    counter: Counter = Counter()
    groups: Counter = Counter()
    for (topics,) in connection.execute("SELECT topics FROM papers WHERE COALESCE(topics, '') <> ''"):
        for topic in TOPIC_SPLIT.split(topics):
            if topic:
                counter[topic] += 1
                groups[topic.split(" -> ")[0].strip()] += 1
    return {
        "groups": [{"name": name, "count": count} for name, count in groups.most_common()],
        "topics": [{"name": name, "count": count} for name, count in counter.most_common()],
    }


class Handler(BaseHTTPRequestHandler):
    db_path: Path

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        import mimetypes

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 256):
                self.wfile.write(chunk)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api(parsed.path, parse_qs(parsed.query))
            return

        target = WEB_DIR / ("index.html" if parsed.path in ("/", "/index.html") else parsed.path.lstrip("/"))
        try:
            resolved = target.resolve()
            if WEB_DIR.resolve() != resolved and WEB_DIR.resolve() not in resolved.parents:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_file(resolved)

    def handle_api(self, path: str, query: dict[str, list[str]]) -> None:
        if not self.db_path.exists():
            self.send_json({"error": f"Database not found: {self.db_path}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        with connect(self.db_path) as connection:
            if path == "/api/papers":
                self.api_papers(connection, query)
                return
            if path == "/api/paper":
                self.api_paper(connection, query)
                return
            if path == "/api/stats":
                self.api_stats(connection)
                return
            if path == "/api/topics":
                self.send_json(get_topics(connection))
                return
        self.send_json({"error": "unknown endpoint"}, HTTPStatus.NOT_FOUND)

    def api_papers(self, connection: sqlite3.Connection, query: dict[str, list[str]]) -> None:
        q = query.get("q", [""])[0].strip()
        keywords = list(dict.fromkeys(k for k in q.split() if k))
        topic = query.get("topic", [""])[0].strip()
        has_code = query.get("code", [""])[0].strip() == "1"
        limit = min(max(int(query.get("limit", ["30"])[0] or 30), 1), 200)
        offset = max(int(query.get("offset", ["0"])[0] or 0), 0)

        clauses: list[str] = []
        params: list[Any] = []
        for keyword in keywords:
            like = f"%{keyword}%"
            clauses.append(
                "(title LIKE ? OR title_zh LIKE ? OR authors LIKE ? OR abstract LIKE ? OR abstract_zh LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        if topic:
            clauses.append("('; ' || topics || ';') LIKE ?")
            params.append(f"%; {topic};%")
        if has_code:
            clauses.append("COALESCE(code_url, '') <> ''")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""

        total = connection.execute(f"SELECT COUNT(*) FROM papers {where}", params).fetchone()[0]
        rows = rows_to_dicts(
            connection.execute(
                f"""
                SELECT id, paper_no, title, title_zh, authors, topics, pdf_url,
                       CASE WHEN COALESCE(abstract_zh, '') <> '' THEN 1 ELSE 0 END AS translated,
                       CASE WHEN COALESCE(code_url, '') <> '' THEN 1 ELSE 0 END AS has_code
                FROM papers {where}
                ORDER BY paper_no
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
        )
        self.send_json({"total": total, "rows": rows, "stats": get_stats(connection)})

    def api_paper(self, connection: sqlite3.Connection, query: dict[str, list[str]]) -> None:
        paper_id = query.get("id", [""])[0]
        if not paper_id.isdigit():
            self.send_json({"error": "invalid id"}, HTTPStatus.BAD_REQUEST)
            return
        row = connection.execute("SELECT * FROM papers WHERE id=?", (int(paper_id),)).fetchone()
        self.send_json(dict(row) if row else {"error": "not found"}, HTTPStatus.OK if row else HTTPStatus.NOT_FOUND)

    def api_stats(self, connection: sqlite3.Connection) -> None:
        top_words = rows_to_dicts(
            connection.execute(
                """
                WITH RECURSIVE words(word, rest) AS (
                  SELECT '', lower(group_concat(title, ' ')) || ' ' FROM papers
                  UNION ALL
                  SELECT substr(rest, 0, instr(rest, ' ')), substr(rest, instr(rest, ' ') + 1)
                  FROM words WHERE rest <> ''
                )
                SELECT trim(word, '.,:;!?()[]{}"''') AS word, COUNT(*) AS count
                FROM words
                WHERE length(trim(word, '.,:;!?()[]{}"''')) > 4
                  AND word NOT IN ('using','based','model','models','image','images','learning','framework','vision','language','medical')
                GROUP BY trim(word, '.,:;!?()[]{}"''')
                ORDER BY count DESC
                LIMIT 40
                """
            )
        )
        self.send_json({"totals": get_stats(connection), "topWords": top_words})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 MICCAI 2026 SQLite 本地浏览 Web 服务。")
    parser.add_argument("--db", default="data/miccai2026.sqlite", help="SQLite 数据库路径。")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址。")
    parser.add_argument("--port", default=8002, type=int, help="监听端口。")
    parser.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器。")
    args = parser.parse_args()

    Handler.db_path = Path(args.db).resolve()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"MICCAI 2026 论文库运行于 {url}")
    print(f"数据库: {Handler.db_path}")
    if not args.no_open:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
