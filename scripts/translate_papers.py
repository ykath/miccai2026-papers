import argparse
import json
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Tuple
from urllib.request import Request, urlopen


DEFAULT_API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
USER_AGENT = "MICCAI2026Translator/1.0"


def load_api_key_from_registry(env_name: Optional[str] = None) -> Optional[str]:
    """从 HKCU\\Environment 读取 DeepSeek API key（用户已配置）。"""
    try:
        output = subprocess.run(
            ["reg", "query", "HKCU\\Environment"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[1] not in ("REG_SZ", "REG_EXPAND_SZ"):
            continue
        name, value = parts[0], parts[2].strip()
        if not value:
            continue
        if env_name:
            if name.lower() == env_name.lower():
                return value
        elif "deepseek" in name.lower() and value.startswith("sk-"):
            return value
    return None


def resolve_api_key(args: argparse.Namespace) -> str:
    if args.api_key:
        return args.api_key
    key = load_api_key_from_registry(args.api_key_env)
    if not key:
        raise SystemExit(
            "未找到 API key：请检查注册表 HKCU\\Environment 中名称含 deepseek 的值，"
            "或用 --api-key / --api-key-env 指定。"
        )
    print(f"已从注册表读取 API key（{len(key)} 字符）。", flush=True)
    return key


def post_json(url: str, payload: Dict, api_key: str, timeout: int) -> Dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def init_db(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(papers)")}
    for column in ("title_zh", "title_zh_updated_at"):
        if column not in columns:
            connection.execute(f"ALTER TABLE papers ADD COLUMN {column} TEXT")
    connection.commit()


def iter_pending(
    connection: sqlite3.Connection, limit: Optional[int], overwrite: bool, mode: str
) -> Iterable[sqlite3.Row]:
    where = "COALESCE(abstract, '') <> ''"
    if not overwrite:
        if mode in ("both", "abstract"):
            where += " AND COALESCE(abstract_zh, '') = ''"
        if mode in ("both", "title"):
            where += " AND COALESCE(title_zh, '') = ''"
    query = f"SELECT id, title, abstract FROM papers WHERE {where} ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)}"
    yield from connection.execute(query)


def build_payload(model: str, title: str, abstract: str) -> Dict:
    user_content = (
        "论文题目：\n"
        f"{title}\n\n"
        "论文摘要：\n"
        f"{abstract}\n\n"
        "请输出 JSON，格式：{\"title_zh\": \"...\", \"abstract_zh\": \"...\"}"
    )
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是专业的医学影像计算（MICCAI）论文翻译助手。"
                    "把用户提供的论文题目和英文摘要翻译成简体中文。"
                    "要求：忠实准确、学术风格；专有名词、技术术语和英文缩写（如 CT、MRI、CNN）"
                    "保留原文或按学界惯例翻译；摘要中的换行合并为连贯段落；"
                    "不要添加任何解释、译注或原文。"
                    "只输出 JSON 对象：{\"title_zh\": \"...\", \"abstract_zh\": \"...\"}"
                ),
            },
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }


def parse_translation(content: str) -> Tuple[Optional[str], Optional[str]]:
    content = content.strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = __import__("re").search(r"\{[\s\S]*\}", content)
        if not match:
            return None, None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None, None
    title_zh = (data.get("title_zh") or "").strip() or None
    abstract_zh = (data.get("abstract_zh") or "").strip() or None
    return title_zh, abstract_zh


def translate_one(args: Tuple[sqlite3.Row, str, str, str, int, int]) -> Tuple[int, Optional[str], Optional[str]]:
    row, api_base, model, api_key, timeout, retries = args
    payload = build_payload(model, row["title"], row["abstract"])

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            data = post_json(f"{api_base.rstrip('/')}/chat/completions", payload, api_key, timeout)
            content = data["choices"][0]["message"]["content"].strip()
            if content:
                title_zh, abstract_zh = parse_translation(content)
                if title_zh or abstract_zh:
                    return row["id"], title_zh, abstract_zh
                last_error = RuntimeError("返回 JSON 中缺少有效字段")
            else:
                last_error = RuntimeError("返回内容为空")
        except Exception as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(min(attempt * 2, 10))
    raise RuntimeError(f"翻译失败: {last_error}") from last_error


def update_translation(
    connection: sqlite3.Connection, paper_id: int, title_zh: Optional[str], abstract_zh: Optional[str]
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if title_zh:
        connection.execute(
            "UPDATE papers SET title_zh=?, title_zh_updated_at=? WHERE id=?", (title_zh, now, paper_id)
        )
    if abstract_zh:
        connection.execute(
            "UPDATE papers SET abstract_zh=?, abstract_zh_updated_at=? WHERE id=?", (abstract_zh, now, paper_id)
        )
    connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="调用 DeepSeek 翻译 MICCAI 2026 论文题目与摘要。")
    parser.add_argument("--db", default="data/miccai2026.sqlite", help="SQLite 数据库路径。")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="DeepSeek API 根地址。")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型名，默认 deepseek-flash。")
    parser.add_argument("--api-key", help="直接传入 API key（优先于注册表）。")
    parser.add_argument("--api-key-env", help="注册表 HKCU\\Environment 中 key 的值名称；默认自动匹配名称含 deepseek 的值。")
    parser.add_argument("--limit", type=int, help="只翻译前 N 条，便于测试。")
    parser.add_argument("--workers", type=int, default=4, help="并发翻译线程数。")
    parser.add_argument("--timeout", type=int, default=180, help="单次请求超时秒数。")
    parser.add_argument("--retries", type=int, default=3, help="单篇失败重试次数。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有翻译。")
    args = parser.parse_args()

    api_key = resolve_api_key(args)
    api_base = args.api_base.rstrip("/")
    print(f"使用模型：{args.model} @ {api_base}", flush=True)

    with sqlite3.connect(args.db, timeout=60) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA journal_mode=WAL")
        init_db(connection)
        rows = list(iter_pending(connection, args.limit, args.overwrite, "both"))
        print(f"待翻译：{len(rows)} 篇", flush=True)

    if not rows:
        return

    completed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(translate_one, (row, api_base, args.model, api_key, args.timeout, args.retries)): row
            for row in rows
        }
        with sqlite3.connect(args.db, timeout=60) as write_connection:
            write_connection.execute("PRAGMA busy_timeout=60000")
            write_connection.execute("PRAGMA journal_mode=WAL")
            for future in as_completed(futures):
                row = futures[future]
                try:
                    paper_id, title_zh, abstract_zh = future.result()
                    update_translation(write_connection, paper_id, title_zh, abstract_zh)
                    completed += 1
                    if completed % 20 == 0:
                        print(f"已翻译 {completed}/{len(rows)} 篇", flush=True)
                except Exception as exc:
                    failed += 1
                    print(f"翻译失败 {row['id']}: {row['title'][:60]} -> {exc}", flush=True)

    print(f"完成：成功 {completed}，失败 {failed}", flush=True)


if __name__ == "__main__":
    main()
