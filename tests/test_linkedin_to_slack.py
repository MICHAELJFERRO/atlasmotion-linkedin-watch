import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import linkedin_to_slack as bot


HTML = r"""
<html><head>
<script type="application/ld+json">not valid json</script>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "DiscussionForumPosting",
      "datePublished": "2026-08-27T23:11:55.861Z",
      "mainEntityOfPage": "https://www.linkedin.com/posts/atlasmotion_newest-activity-200-Bbbb",
      "text": "Newest post\nwith whitespace",
      "url": "https://www.linkedin.com/posts/atlasmotion_newest-activity-200-Bbbb"
    },
    {
      "@type": ["Thing", "DiscussionForumPosting"],
      "datePublished": "2026-08-20T00:00:00.000Z",
      "text": "Older post",
      "url": "https://www.linkedin.com/posts/atlasmotion_older-activity-100-Aaaa"
    },
    {"@type": "Organization", "name": "Atlas Motion"}
  ]
}
</script>
</head></html>
"""


class ExtractPostsTests(unittest.TestCase):
    def test_extracts_deduplicates_and_orders_json_ld_posts(self):
        posts = bot.extract_posts(HTML)
        self.assertEqual([post.activity_id for post in posts], ["100", "200"])
        self.assertEqual(posts[-1].text, "Newest post\nwith whitespace")

    def test_refuses_page_without_posts(self):
        with self.assertRaises(bot.BotError):
            bot.extract_posts("<html><script type='application/ld+json'>{}</script></html>")

    def test_formats_alert_with_300_character_excerpt(self):
        post = bot.Post("1", "x" * 301, "2026-01-01", "https://example.test/post")
        message = bot.alert_text(post)
        self.assertEqual(message.splitlines()[0], "New Atlas post on LinkedIn:")
        self.assertEqual(message.splitlines()[1], "x" * 300 + "…")
        self.assertEqual(message.splitlines()[2], post.url)


class StateSafetyTests(unittest.TestCase):
    def test_records_only_successfully_delivered_posts(self):
        posts = [
            bot.Post("2", "second", "2026-01-02", "https://example.test/2"),
            bot.Post("3", "third", "2026-01-03", "https://example.test/3"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "last_seen.txt"
            state.write_text("1\n", encoding="utf-8")
            with patch.object(bot, "fetch_posts", return_value=posts), patch.object(
                bot, "post_to_slack", side_effect=[None, bot.BotError("Slack failed")]
            ):
                with self.assertRaises(bot.BotError):
                    bot.run_regular("https://example.test", state, "secret")
            self.assertEqual(bot.read_seen(state), {"1", "2"})

    def test_fetch_failure_does_not_touch_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "last_seen.txt"
            state.write_text("1\n", encoding="utf-8")
            with patch.object(bot, "fetch_posts", side_effect=bot.BotError("blocked")):
                with self.assertRaises(bot.BotError):
                    bot.run_regular("https://example.test", state, "secret")
            self.assertEqual(state.read_text(encoding="utf-8"), "1\n")


if __name__ == "__main__":
    unittest.main()
