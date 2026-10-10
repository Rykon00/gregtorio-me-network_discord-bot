import unittest

import discord_cleanup


class Stub:
    def __init__(self, messages):
        self.messages = messages
        self.deleted = []

    def request(self, method, path, body=None, query=None):
        if path.endswith("/channels"):
            return [{"id": "5", "name": "general", "type": 0}]
        if method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            return None
        before = (query or {}).get("before")
        ids = [m for m in self.messages if before is None or int(m["id"]) < int(before)]
        return ids[:100]


def msg(i, kind):
    return {"id": str(i), "type": kind, "author": {"username": "u%d" % i}}


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub([msg(i, 7 if i % 2 else 0) for i in range(300, 0, -1)])

    def test_dry_run_deletes_nothing(self):
        found = discord_cleanup.cleanup(self.stub, "1", "general")
        self.assertEqual(len(found), 150)
        self.assertEqual(self.stub.deleted, [])

    def test_only_join_messages_are_deleted(self):
        discord_cleanup.cleanup(self.stub, "1", "general", delete=True)
        self.assertEqual(len(self.stub.deleted), 150)
        self.assertTrue(all(int(i) % 2 for i in self.stub.deleted))


class WaveTest(unittest.TestCase):
    def test_orphan_wave_is_matched_but_other_replies_are_not(self):
        wave = {"id": "9", "type": 19, "sticker_items": [{"id": "1"}], "content": "", "referenced_message": None}
        self.assertTrue(discord_cleanup.is_orphan_wave(wave))
        self.assertFalse(discord_cleanup.is_orphan_wave({**wave, "content": "hi"}))
        self.assertFalse(discord_cleanup.is_orphan_wave({**wave, "referenced_message": {"id": "2"}}))
        self.assertFalse(discord_cleanup.is_orphan_wave({**wave, "sticker_items": []}))
