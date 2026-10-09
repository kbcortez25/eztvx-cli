import contextlib
import io
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import torrent_feed_browser as browser


def response(title, magnet):
    return json.dumps({"torrents": [{"title": title, "magnet_url": magnet}]}).encode()


class InteractiveSearchTests(unittest.TestCase):
    def run_browser(self, arguments, answers, feeds, matches=()):
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["torrent_feed_browser.py", "--no-color", *arguments]),
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
