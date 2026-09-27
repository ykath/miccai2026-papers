import argparse
import gzip
import html
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import ProxyHandler, Request, build_opener, urlopen


BASE_URL = "https://papers.miccai.org"
SEARCH_JSON_URL = "https://papers.miccai.org/miccai-2026/js/search.json"
USER_AGENT = "Mozilla/5.0 (compatible; MICCAI2026LocalIndexer/1.0)"

# 本机代理（127.0.0.1:7892）对该站时快时挂且大响应易截断；直连稳定但慢。
# 策略：代理快速尝试（30s 超时），失败后切换直连（120s 超时），交替重试。
PROXY_OPENER = build_opener()
DIRECT_OPENER = build_opener(ProxyHandler({}))
ATTEMPT_OPENER = {1: PROXY_OPENER, 2: DIRECT_OPENER, 3: DIRECT_OPENER, 4: PROXY_OPENER, 5: DIRECT_OPENER}


def fetch_text(url: str, retries: int = 5, sleep_seconds: float = 1.0, timeout: int = 30) -> str:
    """下载并按 Content-Length 校验完整性（该站点大响应可能被截断）。"""
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        opener = ATTEMPT_OPENER.get(attempt, DIRECT_OPENER)
        attempt_timeout = timeout if opener is PROXY_OPENER else timeout * 4
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
            with opener.open(request, timeout=attempt_timeout) as response:
                data = response.read()
                expected = response.headers.get("Content-Length")
                if expected is not None and len(data) != int(expected):
                    raise IOError(f"响应被截断: {len(data)}/{expected} 字节")
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    data = gzip.decompress(data)
                charset = response.headers.get_content_charset() or "utf-8"
                return data.decode(charset, errors="replace")
        except (URLError, TimeoutError, IOError, IncompleteRead, RemoteDisconnected) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(sleep_seconds * attempt)
    raise RuntimeError(f"请求失败：{url}") from last_error


def strip_tags(fragment: str, keep_breaks: bool = True) -> str:
    """去掉 HTML 标签；块级标签转换行，保持段落结构。"""
    if keep_breaks:
        fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
        fragment = re.sub(r"</p>", "\n", fragment, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    if keep_breaks:
        text = re.sub(r" ?\n ?", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_inline(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def strip_comments(page: str) -> str:
    return re.sub(r"<!--.*?-->", "", page, flags=re.S)


def extract_section(page: str, section_id: str) -> Optional[str]:
    """截取 <h1 id="..."> 之后到下一个 <h1>（或页面末尾）之间的内容。"""
    pattern = rf'<h1\s+id="{section_id}"[^>]*>.*?</h1>(.*?)(?=<h1[ >]|$)'
    match = re.search(pattern, page, flags=re.I | re.S)
    return match.group(1) if match else None


# ---------------------------------------------------------------- 阶段一：目录

def parse_catalog(search_json_text: str) -> List[Dict[str, Optional[str]]]:
    entries = json.loads(search_json_text)
    papers: List[Dict[str, Optional[str]]] = []
    for entry in entries:
        detail_url = urljoin(BASE_URL, entry.get("url", ""))
        no_match = re.search(r"/(\d{4})-Paper(\d+)\.html?$", detail_url)
        authors = "; ".join(
            clean_inline(a) for a in re.split(r"\s*;\s*", entry.get("tags", "")) if clean_inline(a)
        )
        # 主题名本身可能含逗号（如 Surgical Skills, Workflow & Team Dynamics），
        # 只在逗号后跟随新的 "组 -> 子题" 结构时切分
        topics = "; ".join(
            clean_inline(c) for c in re.split(r",\s*(?=[^,]*->)", entry.get("category", "")) if clean_inline(c)
        )
        papers.append(
            {
                "paper_no": no_match.group(1) if no_match else None,
                "submission_id": no_match.group(2) if no_match else None,
                "title": clean_inline(entry.get("title", "")),
                "authors": authors or None,
                "topics": topics or None,
                "detail_url": detail_url,
                "pdf_url": entry.get("pdflink") or None,
                "publish_date": (entry.get("date") or "")[:10] or None,
            }
        )
    return papers


# ---------------------------------------------------------------- 阶段二：详情

def parse_links_section(section: str) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {}
    for para in re.findall(r"<p>(.*?)</p>", section, flags=re.S):
        label_match = re.match(r"\s*([^:<]{2,40}):\s*", para)
        label = clean_inline(label_match.group(1)).lower() if label_match else ""
        link = re.search(r'<a\s+href="([^"]+)"', para)
        if "main paper" in label:
            result["pdf_url"] = html.unescape(link.group(1)) if link else None
        elif "sharedit" in label:
            result["sharedit_url"] = html.unescape(link.group(1)) if link else None
        elif "springerlink" in label:
            result["springer_url"] = html.unescape(link.group(1)) if link else None
        elif "supplementary" in label:
            result["supp_url"] = html.unescape(link.group(1)) if link else None
    return result


def parse_dataset_section(section: str) -> Optional[str]:
    items = []
    for para in re.findall(r"<p>(.*?)</p>", section, flags=re.S):
        urls = [html.unescape(u) for u in re.findall(r'<a\s+href="([^"]+)"', para)]
        text = strip_tags(para)
        if not text and not urls:
            continue
        items.append({"text": text, "urls": urls})
    return json.dumps(items, ensure_ascii=False) if items else None


def extract_paren_number(answer: str) -> Optional[int]:
    match = re.search(r"\((\d)\)", answer)
    return int(match.group(1)) if match else None


def parse_review_blocks(section: str, heading_pattern: str) -> List[Dict]:
    blocks = []
    for heading, chunk in re.findall(rf'<h[23] id="{heading_pattern}"[^>]*>([^<]*)</h[23]>(.*?)(?=<h[23][ >]|$)', section, flags=re.S):
        items = []
        review: Dict = {"heading": clean_inline(heading)}
        for question, answer_html in re.findall(
            r"<li>\s*<strong>(.*?)</strong>(.*?)</li>", chunk, flags=re.S
        ):
            question_text = clean_inline(question)
            answer_text = strip_tags(answer_html)
            if not answer_text:
                continue
            items.append({"q": question_text, "a": answer_text})
            lowered = question_text.lower()
            if lowered.startswith("rate the paper"):
                review["rating"] = extract_paren_number(answer_text)
                review["rating_text"] = re.sub(r"\s+", " ", answer_text.split("\n")[0]).strip()
            elif lowered.startswith("reviewer confidence"):
                review["confidence"] = extract_paren_number(answer_text)
            elif lowered.startswith("[post rebuttal]") and "final opinion" in lowered:
                review["final"] = re.sub(r"\s+", " ", answer_text.split("\n")[0]).strip()
        if items:
            review["items"] = items
            blocks.append(review)
    return blocks


def parse_detail(detail_html: str) -> Dict[str, Optional[str]]:
    page = strip_comments(detail_html)
    result: Dict[str, Optional[str]] = {}

    title_match = re.search(r'<div\s+class="post-title">\s*<h1>(.*?)</h1>', page, re.I | re.S)
    if title_match:
        title = clean_inline(re.sub(r"<[^>]+>", "", title_match.group(1)))
        if title:
            result["title"] = title

    abstract_section = extract_section(page, "abstract-id")
    if abstract_section:
        abstract = strip_tags(abstract_section)
        if abstract:
            result["abstract"] = re.sub(r"\n+", " ", abstract)

    author_section = re.search(
        r'<div\s+class="post-tags">(.*?)(?=</div>)', page, re.I | re.S
    )
    if author_section:
        # 锚点 class 属性总是紧邻标签收尾，href 中的 "->" 会破坏常规标签正则
        authors = [
            clean_inline(a)
            for a in re.findall(r'class="post-category"\s*>([\s\S]*?)</a>', author_section.group(1))
        ]
        authors = [a for a in authors if a]
        if authors:
            result["authors"] = "; ".join(authors)

    category_section = re.search(
        r'<div\s+class="post-categories">(.*?)(?=</div>)', page, re.I | re.S
    )
    if category_section:
        topics = [
            clean_inline(a)
            for a in re.findall(r'class="post-category"\s*>([\s\S]*?)</a>', category_section.group(1))
        ]
        topics = [t for t in topics if t]
        if topics:
            result["topics"] = "; ".join(topics)

    links_section = extract_section(page, "link-id")
    if links_section:
        result.update({k: v for k, v in parse_links_section(links_section).items() if v})

    code_section = extract_section(page, "code-id")
    if code_section:
        code_link = re.search(r'<a\s+href="([^"]+)"', code_section)
        result["code_url"] = html.unescape(code_link.group(1)) if code_link else None

    dataset_section = extract_section(page, "dataset-id")
    if dataset_section:
        result["dataset_info"] = parse_dataset_section(dataset_section)

    bibtex_section = extract_section(page, "bibtex-id")
    if bibtex_section:
        bibtex = re.search(r"<pre>\s*<code[^>]*>(.*?)</code>", bibtex_section, re.S)
        if bibtex:
            result["bibtex"] = html.unescape(bibtex.group(1)).strip()

    review_section = extract_section(page, "review-id")
    if review_section:
        reviews = parse_review_blocks(review_section, r"review-\d+")
        if reviews:
            result["reviews"] = json.dumps(reviews, ensure_ascii=False)

    feedback_section = extract_section(page, "authorFeedback-id")
    if feedback_section:
        blockquote = re.search(r"<blockquote>(.*?)</blockquote>", feedback_section, re.S)
        if blockquote:
            feedback = strip_tags(blockquote.group(1))
            if feedback:
                result["author_feedback"] = feedback

    meta_section = extract_section(page, "metareview-id")
    if meta_section:
        meta = parse_review_blocks(meta_section, r"meta-review-\d+")
        if meta:
            result["meta_review"] = json.dumps(meta, ensure_ascii=False)

    return result


# ---------------------------------------------------------------- 数据库

def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_no TEXT,
            submission_id TEXT,
            title TEXT NOT NULL,
            title_zh TEXT,
            title_zh_updated_at TEXT,
            authors TEXT,
            abstract TEXT,
            abstract_zh TEXT,
            abstract_zh_updated_at TEXT,
            topics TEXT,
            detail_url TEXT UNIQUE,
            pdf_url TEXT,
            supp_url TEXT,
            sharedit_url TEXT,
            springer_url TEXT,
            code_url TEXT,
            dataset_info TEXT,
            bibtex TEXT,
            reviews TEXT,
            author_feedback TEXT,
            meta_review TEXT,
            publish_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_papers_paper_no ON papers(paper_no);
        CREATE INDEX IF NOT EXISTS idx_papers_pdf_url ON papers(pdf_url);
        """
    )
    try:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts
            USING fts5(title, title_zh, authors, abstract, abstract_zh, content='papers', content_rowid='id');

            CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
              INSERT INTO papers_fts(rowid, title, title_zh, authors, abstract, abstract_zh)
              VALUES (new.id, new.title, new.title_zh, new.authors, new.abstract, new.abstract_zh);
            END;
            CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
              INSERT INTO papers_fts(papers_fts, rowid, title, title_zh, authors, abstract, abstract_zh)
              VALUES('delete', old.id, old.title, old.title_zh, old.authors, old.abstract, old.abstract_zh);
            END;
            CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE ON papers BEGIN
              INSERT INTO papers_fts(papers_fts, rowid, title, title_zh, authors, abstract, abstract_zh)
              VALUES('delete', old.id, old.title, old.title_zh, old.authors, old.abstract, old.abstract_zh);
              INSERT INTO papers_fts(rowid, title, title_zh, authors, abstract, abstract_zh)
              VALUES (new.id, new.title, new.title_zh, new.authors, new.abstract, new.abstract_zh);
            END;
            """
        )
    except sqlite3.OperationalError:
        pass


DETAIL_COLUMNS = [
    "title", "authors", "abstract", "topics", "pdf_url", "supp_url", "sharedit_url",
    "springer_url", "code_url", "dataset_info", "bibtex", "reviews",
    "author_feedback", "meta_review",
]


def upsert_paper(connection: sqlite3.Connection, paper: Dict[str, Optional[str]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    known = {row[1] for row in connection.execute("PRAGMA table_info(papers)")}
    fields = {key: value for key, value in paper.items() if key in known}
    catalog_columns = ["paper_no", "submission_id", "title", "authors", "topics",
                       "detail_url", "pdf_url", "publish_date"]
    catalog_fields = {k: v for k, v in fields.items() if k in catalog_columns}
    detail_fields = {k: v for k, v in fields.items() if k in DETAIL_COLUMNS}

    if catalog_fields:
        params = {**{k: None for k in catalog_columns}, **catalog_fields,
                  "created_at": now, "updated_at": now}
        connection.execute(
            f"""
            INSERT INTO papers ({', '.join(catalog_columns)}, created_at, updated_at)
            VALUES ({', '.join(':' + k for k in catalog_columns)}, :created_at, :updated_at)
            ON CONFLICT(detail_url) DO UPDATE SET
                paper_no=COALESCE(excluded.paper_no, papers.paper_no),
                submission_id=COALESCE(excluded.submission_id, papers.submission_id),
                title=COALESCE(NULLIF(excluded.title, ''), papers.title),
                authors=COALESCE(excluded.authors, papers.authors),
                topics=COALESCE(NULLIF(excluded.topics, ''), papers.topics),
                pdf_url=COALESCE(excluded.pdf_url, papers.pdf_url),
                publish_date=COALESCE(excluded.publish_date, papers.publish_date),
                updated_at=excluded.updated_at
            """,
            params,
        )

    if detail_fields:
        sets = ", ".join(f"{key}=COALESCE(NULLIF(?, ''), papers.{key})" for key in detail_fields)
        connection.execute(
            f"""
            UPDATE papers SET {sets}, updated_at=?
            WHERE detail_url=?
            """,
            [*(detail_fields[key] for key in detail_fields), now, paper.get("detail_url")],
        )


def completed_detail_urls(connection: sqlite3.Connection) -> set:
    rows = connection.execute(
        "SELECT detail_url FROM papers WHERE detail_url IS NOT NULL AND COALESCE(abstract, '') <> ''"
    ).fetchall()
    return {row[0] for row in rows}


def enrich_one(paper: Dict[str, Optional[str]], delay: float) -> Dict[str, Optional[str]]:
    detail = parse_detail(fetch_text(paper["detail_url"]))
    merged = dict(paper)
    for key, value in detail.items():
        if value:
            merged[key] = value
    if delay:
        time.sleep(delay)
    return merged


def enrich_papers(papers, delay: float, limit: Optional[int], workers: int):
    selected = list(papers)
    if limit:
        selected = selected[:limit]

    if workers <= 1:
        for paper in selected:
            try:
                yield enrich_one(paper, delay)
            except Exception as exc:
                print(f"详情页抓取失败: {paper.get('detail_url')} -> {exc}", flush=True)
        return

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(enrich_one, paper, delay): paper for paper in selected}
        for future in as_completed(futures):
            paper = futures[future]
            try:
                yield future.result()
            except Exception as exc:
                print(f"详情页抓取失败: {paper.get('detail_url')} -> {exc}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取 MICCAI 2026 开放评审站论文信息到 SQLite。")
    parser.add_argument("--db", default="data/miccai2026.sqlite", help="SQLite 数据库路径。")
    parser.add_argument("--delay", type=float, default=0.1, help="详情页请求间隔秒数。")
    parser.add_argument("--limit", type=int, help="只抓取前 N 篇详情，便于测试。")
    parser.add_argument("--workers", type=int, default=8, help="并发抓取详情页线程数。")
    parser.add_argument("--no-resume", action="store_true", help="不跳过已抓取摘要的论文。")
    parser.add_argument("--index-only", action="store_true", help="只写入 search.json 目录数据，不抓详情页。")
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    catalog = parse_catalog(fetch_text(SEARCH_JSON_URL))
    print(f"search.json 解析到 {len(catalog)} 篇论文。", flush=True)

    with sqlite3.connect(db_path, timeout=60) as connection:
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA journal_mode=WAL")
        init_db(connection)
        for paper in catalog:
            upsert_paper(connection, paper)
        connection.commit()
        print(f"目录已写入 {len(catalog)} 篇。", flush=True)
        if args.index_only:
            print(f"完成：目录已写入 {db_path}")
            return

        pending = catalog
        if not args.no_resume:
            completed = completed_detail_urls(connection)
            before = len(pending)
            pending = [p for p in pending if p.get("detail_url") not in completed]
            print(f"断点续跑：跳过已完成 {before - len(pending)} 篇，剩余 {len(pending)} 篇。", flush=True)

        processed = 0
        failed = 0
        for paper in enrich_papers(pending, args.delay, args.limit, args.workers):
            if paper.get("abstract"):
                upsert_paper(connection, paper)
                processed += 1
            else:
                failed += 1
                print(f"缺少摘要，未写入: {paper.get('detail_url')}", flush=True)
            connection.commit()
            if processed and processed % 50 == 0:
                print(f"已写入 {processed} 篇...", flush=True)

    print(f"完成：详情写入 {processed} 篇，失败 {failed} 篇 -> {db_path}", flush=True)


if __name__ == "__main__":
    main()
