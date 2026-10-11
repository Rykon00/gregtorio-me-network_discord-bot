import unittest

import discord_poll


class Stub:
    def __init__(self, recent=()):
        self.recent = list(recent)
        self.posted = []

    def request(self, method, path, body=None, query=None):
        if path.endswith("/channels"):
            return [{"id": "5", "name": "ideas", "type": 0}]
        if path == "/users/@me":
            return {"id": "42"}
        if method == "POST":
            self.posted.append(body)
            return {}
        return self.recent


class PollTest(unittest.TestCase):
    def poll(self):
        return discord_poll.build_poll("Which?", ["a", "b"], 7, False)

    def test_build(self):
        poll = self.poll()
        self.assertEqual(poll["duration"], 168)
        self.assertEqual([a["poll_media"]["text"] for a in poll["answers"]], ["a", "b"])

    def test_limits(self):
        with self.assertRaises(discord_poll.PollError):
            discord_poll.build_poll("q", ["only one"], 7, False)
        with self.assertRaises(discord_poll.PollError):
            discord_poll.build_poll("q", ["a", "b" * 56], 7, False)
        with self.assertRaises(discord_poll.PollError):
            discord_poll.build_poll("q", ["a", "b"], 40, False)

    def test_posts_once(self):
        stub = Stub()
        discord_poll.post(stub, "1", "ideas", self.poll())
        self.assertEqual(len(stub.posted), 1)
        again = Stub([{"author": {"id": "42"}, "poll": {"question": {"text": "Which?"}}}])
        self.assertIn("already", discord_poll.post(again, "1", "ideas", self.poll()))
        self.assertEqual(again.posted, [])

    def test_dry_run_posts_nothing(self):
        stub = Stub()
        discord_poll.post(stub, "1", "ideas", self.poll(), dry_run=True)
        self.assertEqual(stub.posted, [])
