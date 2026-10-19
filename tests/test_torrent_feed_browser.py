import contextlib
import importlib.util
import io
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "eztvx_cli", Path(__file__).resolve().parents[1] / "eztvx-cli.py"
)
browser = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = browser
spec.loader.exec_module(browser)


def response(title, magnet):
    return json.dumps({"torrents": [{"title": title, "magnet_url": magnet}]}).encode()


class RequestURLTests(unittest.TestCase):
    def test_api_urls_apply_limit_and_page_and_preserve_other_parameters(self):
        for base_url in (
            browser.DEFAULT_EZTV_API_URL,
            browser.DEFAULT_EZTV_API_URL + "/",
            "https://example.com/api/get-torrents",
        ):
            with self.subTest(base_url=base_url):
                url = base_url + "?limit=1&page=3&key=a%26b&empty=&imdb_id=00123"
                args = browser.build_parser().parse_args(
                    [url, "--limit", "100", "--no-interactive"]
                )
                with (
                    patch.object(browser, "fetch_feed", return_value=b'{"torrents": []}') as fetch,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    browser.load_and_print_items(args, None)
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(fetch.call_args.args[0]).query,
                    keep_blank_values=True,
                )
                self.assertEqual(query, {
                    "limit": ["100"], "page": ["1"], "key": ["a&b"],
                    "empty": [""], "imdb_id": ["00123"],
                })

    def test_generic_feed_url_is_unchanged(self):
        url = "https://example.com/feed.xml?limit=1&page=3&key=a%26b"
        self.assertEqual(browser.build_request_url(url, 100, 1, None), url)


class FeedParsingTests(unittest.TestCase):
    def test_atom_magnet_after_inline_markup_is_preserved(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
            <title>Example</title><updated>2026-10-19</updated>
            <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
                <p><strong>Download:</strong> magnet:?xt=urn:btih:EXAMPLE&amp;dn=Example</p>
            </div></content>
        </entry></feed>'''
        self.assertEqual(browser.parse_response(feed), [browser.FeedItem(
            index=1, title="Example", published="2026-10-19",
            magnet="magnet:?xt=urn:btih:EXAMPLE&dn=Example",
        )])

    def test_text_outside_an_entry_is_not_used_as_its_magnet(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom">
            <entry><title>No magnet</title></entry>
            magnet:?xt=urn:btih:OUTSIDE
            <entry><title>Valid</title><link href="magnet:?xt=urn:btih:VALID"/></entry>
        </feed>'''
        self.assertEqual(browser.parse_response(feed), [browser.FeedItem(
            index=1, title="Valid", published="", magnet="magnet:?xt=urn:btih:VALID",
        )])


class APIResponseTests(unittest.TestCase):
    def test_nonobject_json_roots_are_rejected_with_value_error(self):
        for payload in ([], None, "unexpected", 1, True):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "not an object"):
                    browser.parse_eztv_response(json.dumps(payload).encode())

    def test_cli_reports_invalid_api_root_without_a_traceback(self):
        errors = io.StringIO()
        with (
            patch.object(sys, "argv", ["eztvx-cli.py", "--no-interactive", "--no-color"]),
            patch.object(browser, "fetch_feed", return_value=b"[]"),
            contextlib.redirect_stderr(errors),
        ):
            status = browser.main()
        self.assertEqual(status, 1)
        self.assertIn("Could not parse API response:", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_invalid_timestamps_do_not_drop_torrents_or_abort_parsing(self):
        for timestamp in (10**100, -(10**100), "invalid"):
            with self.subTest(timestamp=timestamp):
                payload = json.dumps({"torrents": [
                    {"title": "Bad date", "magnet_url": "magnet:?xt=urn:btih:BAD",
                     "date_released_unix": timestamp},
                    {"title": "Good date", "magnet_url": "magnet:?xt=urn:btih:GOOD",
                     "date_released_unix": 1700000000},
                ]}).encode()
                self.assertEqual(browser.parse_response(payload), [
                    browser.FeedItem(1, "Bad date", "magnet:?xt=urn:btih:BAD", ""),
                    browser.FeedItem(2, "Good date", "magnet:?xt=urn:btih:GOOD", "2023-11-14 22:13 UTC"),
                ])


class InteractiveSearchTests(unittest.TestCase):
    def run_browser(self, arguments, answers, feeds, matches=()):
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["eztvx-cli.py", "--no-color", *arguments]),
            patch("builtins.input", side_effect=answers),
            patch.object(browser, "fetch_feed", side_effect=feeds) as fetch,
            patch.object(browser, "find_indexed_series", side_effect=matches) as search,
            patch.object(browser.webbrowser, "open") as open_url,
            contextlib.redirect_stdout(output),
        ):
            status = browser.main()
        self.assertEqual(status, 0)
        return output.getvalue(), fetch, search, open_url

    def test_slash_prompt_searches_a_new_series_and_opens_its_selection(self):
        output, fetch, search, open_url = self.run_browser(
            ["--imdb-id", "tt12", "--page", "3"],
            ["/", "New Show", "1"],
            [
                response("Old release", "magnet:?xt=urn:btih:OLD"),
                response("Release S01E01", "magnet:?xt=urn:btih:NEW"),
            ],
            [[browser.SeriesMatch("New Show", "99")]],
        )
        self.assertEqual(search.call_args.args[1], "New Show")
        queries = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[0]).query)
            for call in fetch.call_args_list
        ]
        self.assertEqual(queries[0]["imdb_id"], ["12"])
        self.assertEqual(queries[0]["page"], ["3"])
        self.assertEqual(queries[1]["imdb_id"], ["99"])
        self.assertEqual(queries[1]["page"], ["1"])
        self.assertIn("Release S01E01", output)
        open_url.assert_called_once_with("magnet:?xt=urn:btih:NEW")

    def test_can_search_from_empty_results_and_retry_an_unmatched_query(self):
        output, _, search, open_url = self.run_browser(
            [],
            [" /Missing ", "/Found", "1"],
            [b'{"torrents": []}', response("Found", "magnet:?xt=urn:btih:FOUND")],
            [[], [browser.SeriesMatch("Found", "99")]],
        )
        self.assertEqual([call.args[1] for call in search.call_args_list], ["Missing", "Found"])
        self.assertIn("No matching TV series", output)
        open_url.assert_called_once_with("magnet:?xt=urn:btih:FOUND")

    def test_feed_search_replaces_the_filter_and_validates_current_selections(self):
        feed = b'''<rss><channel>
            <item><title>First</title><link>magnet:?xt=urn:btih:FIRST</link></item>
            <item><title>Second</title><link>magnet:?xt=urn:btih:SECOND</link></item>
        </channel></rss>'''
        output, _, search, open_url = self.run_browser(
            ["https://example.com/feed.xml"],
            ["/First", "/Second", "1", "2"],
            [feed, feed, feed],
        )
        search.assert_not_called()
        self.assertIn("Unknown item number(s): 1", output)
        open_url.assert_called_once_with("magnet:?xt=urn:btih:SECOND")

    def test_blank_search_returns_to_selection_and_eof_exits_cleanly(self):
        for answers in (["/", "", "1"], ["/", EOFError()]):
            with self.subTest(answers=answers):
                _, fetch, search, open_url = self.run_browser(
                    [], answers, [response("Original", "magnet:?xt=urn:btih:ORIGINAL")]
                )
                fetch.assert_called_once()
                search.assert_not_called()
                self.assertEqual(open_url.call_count, int(answers[-1] == "1"))

    def test_noninteractive_search_does_not_prompt(self):
        _, _, _, open_url = self.run_browser(
            ["--search", "Missing", "--no-interactive"], [], [], [[]]
        )
        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
