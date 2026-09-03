#!/usr/bin/env python3
"""Send new public Atlas Motion LinkedIn posts to a Slack webhook."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, List, Set
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_PAGE_URL = "https://www.linkedin.com/company/atlasmotion"
DEFAULT_STATE_FILE = Path(__file__).with_name("last_seen.txt")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
ACTIVITY_ID_RE = re.compile(r"(?:activity-|urn:li:activity:)(\d+)")
MAX_PAGE_BYTES = 5_000_000


class BotError(RuntimeError):
    """An expected error that should make the workflow fail safely."""


@dataclass(frozen=True)
class Post:
    activity_id: str
    text: str
    published: str
    url: str


class JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._in_json_ld = False
        self._chunks: List[str] = []
        self.blocks: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        if tag.lower() != "script":
            return
        attributes = {str(key).lower(): value for key, value in attrs}
        script_type = (attributes.get("type") or "").lower().split(";", 1)[0].strip()
        if script_type == "application/ld+json":
            self._in_json_ld = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_json_ld:
            self.blocks.append("".join(self._chunks))
            self._in_json_ld = False
            self._chunks = []


def _walk_json(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _is_post(node: dict) -> bool:
    value = node.get("@type")
    if isinstance(value, list):
        return "DiscussionForumPosting" in value
    return value == "DiscussionForumPosting"


def _canonical_url(node: dict) -> str:
    for value in (node.get("url"), node.get("mainEntityOfPage")):
        if isinstance(value, str) and value.startswith("https://www.linkedin.com/posts/"):
            return value
        if isinstance(value, dict):
            candidate = value.get("@id") or value.get("url")
            if isinstance(candidate, str) and candidate.startswith(
                "https://www.linkedin.com/posts/"
            ):
                return candidate
    return ""


def extract_posts(html: str) -> List[Post]:
    parser = JsonLdParser()
    parser.feed(html)

    posts_by_id = {}
    for block in parser.blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            # An unrelated malformed JSON-LD block should not hide valid post data.
            continue
        for node in _walk_json(data):
            if not _is_post(node):
                continue
            url = _canonical_url(node)
            match = ACTIVITY_ID_RE.search(url)
            text = node.get("text")
            published = node.get("datePublished")
            if not match or not isinstance(text, str) or not isinstance(published, str):
                continue
            activity_id = match.group(1)
            posts_by_id.setdefault(
                activity_id,
                Post(
                    activity_id=activity_id,
                    text=text.strip(),
                    published=published,
                    url=url,
                ),
            )

    if not posts_by_id:
        raise BotError(
            "LinkedIn returned no usable posts in JSON-LD; refusing to advance state"
        )
    return sorted(
        posts_by_id.values(), key=lambda post: (post.published, post.activity_id)
    )


def fetch_posts(page_url: str, timeout: int = 30) -> List[Post]:
    request = Request(
        page_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise BotError(f"LinkedIn returned unexpected content type: {content_type}")
            raw = response.read(MAX_PAGE_BYTES + 1)
            if len(raw) > MAX_PAGE_BYTES:
                raise BotError("LinkedIn response exceeded the safety limit")
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        raise BotError(f"LinkedIn fetch failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise BotError(f"LinkedIn fetch failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise BotError("LinkedIn fetch timed out") from exc

    try:
        html = raw.decode(charset)
    except (LookupError, UnicodeDecodeError) as exc:
        raise BotError("LinkedIn response could not be decoded") from exc
    return extract_posts(html)


def read_seen(path: Path) -> Set[str]:
    if not path.exists():
        raise BotError(f"State file does not exist: {path}")
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if not value.isdigit():
            raise BotError(f"Invalid activity ID in {path} at line {line_number}")
        seen.add(value)
    return seen


def write_seen(path: Path, seen: Set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = "".join(f"{activity_id}\n" for activity_id in sorted(seen, reverse=True))
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def alert_text(post: Post) -> str:
    normalized = " ".join(post.text.split())
    excerpt = normalized[:300]
    if len(normalized) > 300:
        excerpt = excerpt.rstrip() + "…"
    return f"New Atlas post on LinkedIn:\n{excerpt}\n{post.url}"


def post_to_slack(webhook_url: str, post: Post, timeout: int = 30) -> None:
    if not webhook_url.startswith("https://hooks.slack.com/services/"):
        raise BotError("SLACK_WEBHOOK_URL is missing or is not a Slack incoming webhook URL")
    payload = json.dumps({"text": alert_text(post)}).encode("utf-8")
    request = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read(1024)
            if not 200 <= response.status < 300:
                raise BotError(f"Slack webhook failed with HTTP {response.status}")
    except HTTPError as exc:
        raise BotError(f"Slack webhook failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise BotError(f"Slack webhook failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise BotError("Slack webhook timed out") from exc


def run_regular(page_url: str, state_file: Path, webhook_url: str) -> None:
    # Fetch and parse completely before reading or touching the state file.
    posts = fetch_posts(page_url)
    seen = read_seen(state_file)
    new_posts = [post for post in posts if post.activity_id not in seen]
    if not new_posts:
        print(f"No new posts ({len(posts)} currently visible; {len(seen)} IDs seen).")
        return

    for post in new_posts:
        post_to_slack(webhook_url, post)
        seen.add(post.activity_id)
        # Persist each confirmed delivery. The workflow commits even if a later send fails.
        write_seen(state_file, seen)
        print(f"Alerted activity {post.activity_id}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-url", default=DEFAULT_PAGE_URL)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument(
        "--test-latest",
        action="store_true",
        help="send the latest visible post even though it is already in state",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fetch and parse the page without sending alerts or changing state",
    )
    args = parser.parse_args()

    try:
        if args.check:
            posts = fetch_posts(args.page_url)
            print(f"Parsed {len(posts)} posts; latest activity is {posts[-1].activity_id}.")
        elif args.test_latest:
            posts = fetch_posts(args.page_url)
            post_to_slack(os.environ.get("SLACK_WEBHOOK_URL", ""), posts[-1])
            print(f"Sent test alert for latest activity {posts[-1].activity_id}.")
        else:
            run_regular(
                args.page_url,
                args.state_file,
                os.environ.get("SLACK_WEBHOOK_URL", ""),
            )
    except BotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
