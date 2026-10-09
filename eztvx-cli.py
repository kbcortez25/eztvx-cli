#!/usr/bin/env python3
"""Browse a torrent RSS/Atom feed and open or save magnet links.

Use this only with feeds and content you are authorized to access.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_PROBE_WORKERS = 5
USER_AGENT = "torrent-feed-browser/1.0"
DEFAULT_EZTV_API_URL = "https://eztvx.to/api/get-torrents"
TVMAZE_SEARCH_URL = "https://api.tvmaze.com/search/shows"
MAGNET_PATTERN = re.compile(r"magnet:\?[^<>\s\"']+")


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


@dataclass(frozen=True)
class FeedItem:
    index: int
    title: str
    magnet: str
    published: str


@dataclass(frozen=True)
class SeriesMatch:
    name: str
    imdb_id: str
    premiered: str = ""
    network: str = ""


@dataclass(frozen=True)
class SearchRequest:
    query: str


def should_colorize(disabled: bool = False) -> bool:
    return not disabled and "NO_COLOR" not in os.environ and sys.stdout.isatty()


def color(text: object, code: str, enabled: bool) -> str:
    value = str(text)
    return f"{code}{value}{Colors.RESET}" if enabled else value


def fetch_feed(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def build_request_url(
    url: str, limit: int, page: int, imdb_id: Optional[str]
) -> str:
    """Add supported EZTV API parameters without discarding existing query values."""
    if not imdb_id and page == 1 and url != DEFAULT_EZTV_API_URL:
        return url

    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["limit"] = str(limit)
    query["page"] = str(page)
    if imdb_id:
        query["imdb_id"] = imdb_id
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def text_from(element: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        found = find_child(element, name)
        if found is not None and found.text:
            return html.unescape(found.text.strip())
    return ""


def find_child(element: ET.Element, local_name: str) -> Optional[ET.Element]:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return child
    return None


def extract_magnet_from_text(text: str) -> str:
    match = MAGNET_PATTERN.search(text)
    return html.unescape(match.group(0)) if match else ""


def extract_magnet(item: ET.Element) -> str:
    for child in item.iter():
        for attr in ("href", "url"):
            value = child.attrib.get(attr, "")
            if value.startswith("magnet:?"):
                return html.unescape(value)

        if child.text:
            magnet = extract_magnet_from_text(child.text)
            if magnet:
                return magnet

    return ""


def parse_feed(feed_xml: bytes) -> list[FeedItem]:
    root = ET.fromstring(feed_xml)
    raw_items = [
        element
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] in {"item", "entry"}
    ]

    items: list[FeedItem] = []
    for raw_item in raw_items:
        magnet = extract_magnet(raw_item)
        if not magnet:
            continue

        title = text_from(raw_item, ("title",)) or "(untitled)"
        published = text_from(raw_item, ("pubDate", "published", "updated"))
        items.append(
            FeedItem(
                index=len(items) + 1,
                title=title,
                magnet=magnet,
                published=published,
            )
        )

    return items


def parse_eztv_response(response_json: bytes) -> list[FeedItem]:
    payload = json.loads(response_json)
    torrents = payload.get("torrents", [])
    if not isinstance(torrents, list):
        raise ValueError("EZTV response has an invalid 'torrents' field")

    items: list[FeedItem] = []
    for torrent in torrents:
        if not isinstance(torrent, dict):
            continue
        magnet = str(torrent.get("magnet_url") or "")
        if not magnet.startswith("magnet:?"):
            continue

        published = str(torrent.get("date_released") or "")
        if not published and torrent.get("date_released_unix"):
            try:
                timestamp = int(torrent["date_released_unix"])
                published = dt.datetime.fromtimestamp(
                    timestamp, tz=dt.timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
            except (TypeError, ValueError, OSError):
                pass

        items.append(
            FeedItem(
                index=len(items) + 1,
                title=str(torrent.get("title") or "(untitled)"),
                magnet=magnet,
                published=published,
            )
        )
    return items


def parse_response(response: bytes) -> list[FeedItem]:
    if response.lstrip().startswith((b"{", b"[")):
        return parse_eztv_response(response)
    return parse_feed(response)


def is_eztv_api_url(url: str) -> bool:
    return urllib.parse.urlsplit(url).path.rstrip("/").endswith("/api/get-torrents")


def search_tvmaze(search: str, timeout: int) -> list[SeriesMatch]:
    request_url = f"{TVMAZE_SEARCH_URL}?{urllib.parse.urlencode({'q': search})}"
    payload = json.loads(fetch_feed(request_url, timeout))
    if not isinstance(payload, list):
        raise ValueError("TVmaze response is not a list")

    matches: list[SeriesMatch] = []
    seen_ids: set[str] = set()
    for result in payload:
        show = result.get("show") if isinstance(result, dict) else None
        if not isinstance(show, dict):
            continue
        externals = show.get("externals")
        imdb_id = externals.get("imdb") if isinstance(externals, dict) else None
        if not isinstance(imdb_id, str):
            continue
        imdb_id = imdb_id.casefold().removeprefix("tt")
        if not imdb_id.isdigit() or imdb_id in seen_ids:
            continue
        network_data = show.get("network") or show.get("webChannel") or {}
        network = network_data.get("name", "") if isinstance(network_data, dict) else ""
        matches.append(
            SeriesMatch(
                name=str(show.get("name") or "(untitled)"),
                imdb_id=imdb_id,
                premiered=str(show.get("premiered") or ""),
                network=str(network),
            )
        )
        seen_ids.add(imdb_id)
    return matches


def find_indexed_series(
    url: str,
    search: str,
    timeout: int,
    maximum: int = 10,
    workers: int = DEFAULT_PROBE_WORKERS,
) -> list[SeriesMatch]:
    """Return TVmaze title matches that have at least one torrent on EZTV."""
    candidates = search_tvmaze(search, timeout)[:maximum]

    def is_indexed(series: SeriesMatch) -> bool:
        probe_url = build_request_url(url, 1, 1, series.imdb_id)
        return bool(parse_eztv_response(fetch_feed(probe_url, timeout)))

    # Network latency dominates this step. Executor.map probes concurrently while
    # preserving TVmaze's relevance order in the returned results.
    worker_count = min(workers, len(candidates))
    if not worker_count:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        availability = executor.map(is_indexed, candidates)
        return [series for series, available in zip(candidates, availability) if available]


def print_series(matches: list[SeriesMatch], use_color: bool = False) -> None:
    if not matches:
        print(color("No matching TV series are indexed by EZTV.", Colors.YELLOW, use_color))
        return
    for index, series in enumerate(matches, 1):
        details = " · ".join(value for value in (series.premiered[:4], series.network) if value)
        suffix = color(f"  {details}", Colors.DIM, use_color) if details else ""
        number = color(f"{index}.", Colors.CYAN, use_color)
        print(f"{number} {color(series.name, Colors.BOLD, use_color)}{suffix}")


def choose_series(
    matches: list[SeriesMatch], interactive: bool, use_color: bool = False
) -> Optional[SeriesMatch]:
    if not matches:
        return None
    if len(matches) == 1 or not interactive:
        return matches[0]

    print_series(matches, use_color)
    print()
    while True:
        try:
            raw = input(color("Choose series [1]: ", Colors.CYAN, use_color)).strip()
        except EOFError:
            print()
            return matches[0]
        if not raw:
            return matches[0]
        if raw.isdigit() and 1 <= int(raw) <= len(matches):
            return matches[int(raw) - 1]
        print(color(f"Enter a number from 1 to {len(matches)}.", Colors.YELLOW, use_color))


def title_matches(title: str, search: str) -> bool:
    terms = search.casefold().split()
    normalized_title = title.casefold()
    return all(term in normalized_title for term in terms)


def filter_items_by_title(items: list[FeedItem], title_search: Optional[str]) -> list[FeedItem]:
    if not title_search:
        return items

    return [item for item in items if title_matches(item.title, title_search)]


def print_items(items: list[FeedItem], use_color: bool = False) -> None:
    if not items:
        print(color("No items with magnet links found.", Colors.YELLOW, use_color))
        return

    index_width = len(str(items[-1].index))
    for item in items:
        index = color(f"{item.index:>{index_width}}.", Colors.CYAN, use_color)
        title = color(item.title, Colors.BOLD, use_color)
        date = (
            color(f"  {item.published}", Colors.DIM, use_color)
            if item.published
            else ""
        )
        print(f"{index} {title}{date}")


def select_items(items: list[FeedItem], selections: list[int]) -> list[FeedItem]:
    by_index = {item.index: item for item in items}
    selected: list[FeedItem] = []
    missing: list[int] = []

    for selection in selections:
        item = by_index.get(selection)
        if item is None:
            missing.append(selection)
        else:
            selected.append(item)

    if missing:
        raise SystemExit(f"Unknown item number(s): {', '.join(map(str, missing))}")

    return selected


def parse_selections(raw_selection: str, items: list[FeedItem]) -> list[int]:
    value = raw_selection.strip().casefold()
    if not value:
        return []
    if value in {"a", "all"}:
        return [item.index for item in items]

    selections: list[int] = []
    for part in re.split(r"[,\s]+", value):
        if not part:
            continue

        if "-" in part:
            start_text, end_text = part.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"Invalid range: {part}")

            start = int(start_text)
            end = int(end_text)
            step = 1 if start <= end else -1
            selections.extend(range(start, end + step, step))
            continue

        if not part.isdigit():
            raise ValueError(f"Invalid selection: {part}")

        selections.append(int(part))

    return selections


def prompt_for_items(
    items: list[FeedItem], use_color: bool = False
) -> list[FeedItem] | SearchRequest:
    print()
    print(
        color(
            "Type item numbers to download, ranges like 2-5, 'all', "
            "'/query' to search ('/' for a search prompt), or press Enter to quit.",
            Colors.DIM,
            use_color,
        )
    )

    while True:
        try:
            prompt = color("Download selection: ", Colors.CYAN, use_color)
            raw_selection = input(prompt).strip()
            if raw_selection.startswith("/"):
                query = raw_selection[1:].strip()
                if not query:
                    query = input(color("Search: ", Colors.CYAN, use_color)).strip()
                if query:
                    return SearchRequest(query)
                continue
        except EOFError:
            print()
            return []

        try:
            selections = parse_selections(raw_selection, items)
            return select_items(items, selections) if selections else []
        except (SystemExit, ValueError) as exc:
            print(color(exc, Colors.YELLOW, use_color))


def save_magnets(items: list[FeedItem], output_path: Path, use_color: bool = False) -> None:
    output_path.write_text(
        "".join(f"{item.title}\n{item.magnet}\n\n" for item in items),
        encoding="utf-8",
    )
    print(
        color(
            f"Saved {len(items)} magnet link(s) to {output_path}",
            Colors.GREEN,
            use_color,
        )
    )


def open_magnets(items: list[FeedItem], use_color: bool = False) -> None:
    for item in items:
        print(f"{color('Opening:', Colors.GREEN, use_color)} {item.title}")
        webbrowser.open(item.magnet)


def bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be between {minimum} and {maximum}"
            )
        return number

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List EZTV API or RSS/Atom items and open or save magnet links.",
        epilog=(
            "Examples:\n"
            "  %(prog)s --search \"The Last of Us\"\n"
            "  %(prog)s --limit 10 --page 1\n"
            "  %(prog)s --imdb-id 6048596\n"
            "  %(prog)s https://example.com/feed.xml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "feed_url",
        nargs="?",
        default=DEFAULT_EZTV_API_URL,
        help=f"EZTV API or RSS/Atom URL (default: {DEFAULT_EZTV_API_URL}).",
    )
    title_group = parser.add_mutually_exclusive_group()
    title_group.add_argument(
        "-t",
        "--title",
        "--search",
        dest="title_search",
        help=(
            "Search TVmaze for a series name, then fetch that show's torrents "
            "from EZTV."
        ),
    )
    title_group.add_argument(
        "-q",
        "--query",
        dest="title_search",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=bounded_int(1, 100),
        default=50,
        help="Results per page, between 1 and 100 (default: 50).",
    )
    parser.add_argument(
        "-p",
        "--page",
        type=bounded_int(1, 100),
        default=1,
        help="EZTV results page, between 1 and 100 (default: 1).",
    )
    parser.add_argument(
        "--series-results",
        type=bounded_int(1, 25),
        default=10,
        metavar="N",
        help="Maximum TVmaze matches to check against EZTV (default: 10).",
    )
    parser.add_argument(
        "--probe-workers",
        type=bounded_int(1, 25),
        default=DEFAULT_PROBE_WORKERS,
        metavar="N",
        help=f"Concurrent EZTV series checks (default: {DEFAULT_PROBE_WORKERS}).",
    )
    parser.add_argument(
        "--imdb-id",
        metavar="ID",
        help="Return torrents for an exact IMDb title ID (for example: 6048596).",
    )
    parser.add_argument(
        "--open",
        nargs="+",
        type=int,
        metavar="N",
        help="Open the selected item number(s) with your magnet handler.",
    )
    parser.add_argument(
        "--save",
        type=Path,
        metavar="PATH",
        help="Save displayed or selected magnet links to a text file.",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="List feed items without prompting for a download selection.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Network timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored terminal output.",
    )
    return parser


def load_and_print_items(
    args: argparse.Namespace, imdb_id: Optional[str], use_color: bool = False
) -> list[FeedItem]:
    if args.title_search and not imdb_id and is_eztv_api_url(args.feed_url):
        series_matches = find_indexed_series(
            args.feed_url,
            args.title_search,
            args.timeout,
            args.series_results,
            args.probe_workers,
        )
        series = choose_series(
            series_matches,
            interactive=not args.no_interactive,
            use_color=use_color,
        )
        if series is None:
            print_series([], use_color)
            return []
        request_url = build_request_url(
            args.feed_url, args.limit, args.page, series.imdb_id
        )
        items = parse_eztv_response(fetch_feed(request_url, args.timeout))
        print(
            color(
                f"EZTV results for {series.name} (IMDb tt{series.imdb_id})",
                Colors.GREEN,
                use_color,
            )
        )
        title_search = None
    else:
        request_url = build_request_url(
            args.feed_url, args.limit, args.page, imdb_id
        )
        items = parse_response(fetch_feed(request_url, args.timeout))
        title_search = args.title_search

    items = filter_items_by_title(items, title_search)
    limited_items = items[: args.limit]
    print_items(limited_items, use_color)
    return limited_items


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    use_color = should_colorize(args.no_color)

    imdb_id = args.imdb_id
    if imdb_id:
        imdb_id = imdb_id.casefold().removeprefix("tt")
        if not imdb_id.isdigit():
            parser.error("--imdb-id must be numeric, optionally prefixed with 'tt'")

    while True:
        try:
            limited_items = load_and_print_items(args, imdb_id, use_color)
        except ET.ParseError as exc:
            print(
                color(f"Could not parse feed XML: {exc}", Colors.RED, use_color),
                file=sys.stderr,
            )
            return 1
        except (json.JSONDecodeError, ValueError) as exc:
            print(
                color(f"Could not parse API response: {exc}", Colors.RED, use_color),
                file=sys.stderr,
            )
            return 1
        except OSError as exc:
            print(
                color(f"Could not fetch feed: {exc}", Colors.RED, use_color),
                file=sys.stderr,
            )
            return 1

        target_items = limited_items
        if args.open:
            target_items = select_items(limited_items, args.open)
            open_magnets(target_items, use_color)
        elif not args.save and not args.no_interactive:
            selection = prompt_for_items(limited_items, use_color)
            if isinstance(selection, SearchRequest):
                args.title_search = selection.query
                imdb_id = None
                args.page = 1
                continue
            target_items = selection
            if target_items:
                open_magnets(target_items, use_color)

        if args.save:
            save_magnets(target_items, args.save, use_color)

        return 0


if __name__ == "__main__":
    raise SystemExit(main())
