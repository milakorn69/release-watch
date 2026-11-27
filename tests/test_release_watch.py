import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import release_watch as rw  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def pb_varint(n):
    out = bytearray()
    while True:
        c = n & 0x7F
        n >>= 7
        out.append(c | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb(fields):
    """Encode {field: int | bytes | str} as protobuf (varint or length-delimited)."""
    out = b""
    for fn, v in fields.items():
        if isinstance(v, int):
            out += pb_varint(fn << 3) + pb_varint(v)
        else:
            v = v.encode() if isinstance(v, str) else v
            out += pb_varint(fn << 3 | 2) + pb_varint(len(v)) + v
    return out


def plan_output(height, cur=1000, plan=True):
    """What the `plan` shell helper prints for a node: STATUS, PLAN and OLD lines."""
    status = {"result": {"sync_info": {"latest_block_height": str(cur), "latest_block_time": "2026-11-26T12:00:00.123456789Z"}}}
    value = ""
    if plan:
        inner = pb({1: "v29.1.0", 3: height, 4: '{"binaries":{}}'})
        value = base64.b64encode(pb({1: inner})).decode()
    query = {"result": {"response": {"value": value}}}
    old = {"result": {"block_metas": [{"header": {"height": str(cur - 1000), "time": "2026-11-26T10:00:00Z"}}]}}
    return "STATUS %s\nPLAN %s\nOLD %s\n" % (json.dumps(status), json.dumps(query), json.dumps(old))


class Versions(unittest.TestCase):
    def test_parse_ver_order(self):
        tags = ["v29.0.0", "v29.1.0-rc0", "v29.1.0", "v29.1.0-beta.2", "v29.0.10", "v1.2"]
        ordered = sorted(tags, key=lambda t: rw.parse_ver(t)[0])
        self.assertEqual(ordered, ["v1.2", "v29.0.0", "v29.0.10", "v29.1.0-beta.2", "v29.1.0-rc0", "v29.1.0"])
        self.assertEqual(rw.parse_ver("op-node/v1.21.0")[1], "1.21.0")
        self.assertIsNone(rw.parse_ver("nightly"))

    def test_latest_releases(self):
        rels = [
            {"tag": "v29.2.0-rc1", "pre": True, "date": "2026-11-20", "body": ""},
            {"tag": "v29.1.0", "pre": False, "date": "2026-11-09", "body": ""},
            {"tag": "v29.1.1-rc0", "pre": False, "date": "2026-11-15", "body": ""},  # rc not marked pre-release
            {"tag": "v28.9.9", "pre": False, "date": "2026-10-01", "body": ""},
        ]
        best = rw.latest_releases({}, rels)
        self.assertEqual(best["stable"][0], "v29.1.0")
        self.assertEqual(best["any"][0], "v29.2.0-rc1")

    def test_latest_releases_filters(self):
        rels = [
            {"tag": "op-reth/v2.10.0", "pre": False, "date": "2026-11-25", "body": ""},
            {"tag": "op-node/v1.21.0", "pre": False, "date": "2026-11-25", "body": ""},
            {"tag": "polkadot-stable2611", "pre": False, "date": "2026-11-20", "body": "node version 1.26.0 inside"},
        ]
        self.assertEqual(rw.latest_releases({"version_tag": "^op-node/"}, rels)["stable"][1], "1.21.0")
        best = rw.latest_releases({"version_body": r"version (\d+\.\d+\.\d+)"}, rels)
        self.assertEqual(best["stable"], ["polkadot-stable2611", "1.26.0", "2026-11-20"])


class Plans(unittest.TestCase):
    def test_pb_fields(self):
        f = rw.pb_fields(pb({1: "name", 3: 300, 4: b"\x00\x01"}))
        self.assertEqual(f, {1: b"name", 3: 300, 4: b"\x00\x01"})

    def test_read_plan(self):
        p = rw.read_plan(plan_output(height=1360))
        self.assertEqual((p["name"], p["height"], p["cur"]), ("v29.1.0", 1360, 1000))
        self.assertEqual(p["info"], '{"binaries":{}}')
        # 1000 blocks in 2 h -> 7.2 s per block, 360 blocks left -> 43.2 min
        self.assertAlmostEqual(p["eta"] - rw.parse_time("2026-11-26T12:00:00Z"), 360 * 7.2, places=0)

    def test_read_plan_none_and_passed(self):
        self.assertIsNone(rw.read_plan(plan_output(height=0, plan=False)))
        self.assertIsNone(rw.read_plan(plan_output(height=900))["eta"])

    def test_fmt_left(self):
        self.assertEqual(rw.fmt_left(30 * 60), "~30 min")
        self.assertEqual(rw.fmt_left(5 * 3600), "~5.0 h")
        self.assertEqual(rw.fmt_left(3 * 86400), "~3.0 days")


def example_watch(tmp):
    """Watch built from config.example.toml with env and state files moved into `tmp`."""
    with open(os.path.join(ROOT, "config.example.toml")) as f:
        text = f.read()
    text = text.replace('"/etc/release-watch.env"', json.dumps(os.path.join(tmp, "none.env")))
    text = text.replace('"/var/lib/release-watch/state.json"', json.dumps(os.path.join(tmp, "state.json")))
    path = os.path.join(tmp, "config.toml")
    with open(path, "w") as f:
        f.write(text)
    return rw.Watch(path, dry_run=True)


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_example_config_loads(self):
        w = example_watch(self.tmp.name)
        self.assertTrue(w.watch)
        for entry in w.watch:
            self.assertIn("label", entry)
            self.assertIn("repo", entry)

    def test_load_env_and_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write('# comment\nTG_TOKEN="from-file"\nTG_CHAT=123\n')
        try:
            os.environ["TG_CHAT"] = "456"
            env = rw.load_env(f.name)
        finally:
            os.environ.pop("TG_CHAT", None)
            os.unlink(f.name)
        self.assertEqual((env["TG_TOKEN"], env["TG_CHAT"]), ("from-file", "456"))

    def test_dry_run_prints(self):
        w = example_watch(self.tmp.name)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            w.send("<b>hello</b>")
        self.assertIn("hello", out.getvalue())
        self.assertEqual(rw.esc("<a&b>"), "&lt;a&amp;b&gt;")


if __name__ == "__main__":
    unittest.main()
