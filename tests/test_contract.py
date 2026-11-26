import os, json, queue, unittest
from unittest import mock

os.environ["PROXY_TEST"] = "1"
import proxy


class Contract(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(proxy, "log")
        p.start()
        self.addCleanup(p.stop)

    def test_offline_json_is_valid_and_nonempty(self):
        r = proxy._post("http://upstream.invalid", {"messages": []},
                        "", "application/json", 5)
        body = json.loads(r.read().decode("utf-8"))
        self.assertEqual(r.status, 200)
        self.assertTrue(body["choices"][0]["message"]["content"].strip())

    def test_offline_stream_ends_with_done(self):
        r = proxy._post("http://upstream.invalid", {"messages": []},
                        "", "text/event-stream", 5)
        raw = b"".join(r.readlines()).decode("utf-8")
        self.assertIn("data: [DONE]", raw)
        self.assertIn("finish_reason", raw)

    def test_upstream_oserror_falls_back_not_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("net down")):
            r = proxy._post("http://upstream.invalid", {"messages": []},
                            "fake-key", "application/json", 5)
        self.assertIsInstance(r, proxy._OfflineResp)

    def test_missing_secret_never_trusted(self):
        with mock.patch.object(proxy, "marker_secret", return_value=""):
            self.assertFalse(proxy.parse_marker("[[dfy:uid=123;nick=x]]")[2])

    def test_sticker_full_stays_retryable(self):
        full = queue.Queue(maxsize=1)
        full.put_nowait("occupied")
        with mock.patch.object(proxy, "STICKER_Q", full), \
             mock.patch.object(proxy, "STICKER_SEEN", set()), \
             mock.patch.object(proxy, "STICKER_WORKER", [True]):
            proxy._sticker_enqueue("http://x/one.jpg")
            self.assertNotIn("http://x/one.jpg", proxy.STICKER_SEEN)


if __name__ == "__main__":
    unittest.main()
