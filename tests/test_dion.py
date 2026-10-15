from __future__ import annotations

import io
import threading
import unittest
from unittest.mock import patch

from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import ExtractorError

from bot import downloader
from bot.dion import DionIE
from bot.models import DownloadCancelled, DownloadError, FileTooLargeError


VIDEO_ID = "e874005e-6173-4c6d-b0c1-392e11d6e676"
PAGE = f"https://dion.vc/video/{VIDEO_ID}?ref_uid=example"
BASE = f"https://hls-delivery.dion.vc/v3/records/{VIDEO_ID}/hls/"
MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,NAME=360p
360p_record.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=854x480,NAME=480p
480p_record.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720,NAME=720p
720p_record.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,NAME=1080p
1080p_record.m3u8
"""
MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x1123ca75c70784200129a4ede609e8a7
#EXTINF:10.000000,
720p/000000.ts
#EXTINF:2.733333,
720p/000001.ts
#EXT-X-ENDLIST
"""


def webpage(url, *args, **kwargs):
    return MASTER if "master_playlist.m3u8" in url else MEDIA


class DionTests(unittest.TestCase):
    def test_page_uses_master_and_lists_all_qualities(self):
        with patch.object(DionIE, "_download_webpage", side_effect=webpage) as request:
            info = downloader.extract_info(PAGE)
        self.assertEqual(info.extractor, "DION")
        self.assertEqual(info.url, PAGE)
        self.assertEqual([q.height for q in info.qualities], [360, 480, 720, 1080])
        self.assertAlmostEqual(info.duration, 12.733333)
        self.assertTrue(info.has_audio)
        self.assertEqual(request.call_args_list[0].args[0], BASE + "master_playlist.m3u8")

    def test_direct_media_link_preserves_quality_and_query(self):
        url = BASE + "720p_record.m3u8?example=value"
        with patch.object(DionIE, "_download_webpage", return_value=MEDIA):
            info = downloader.extract_info(url)
        self.assertEqual(info.url, url)
        self.assertEqual([q.height for q in info.qualities], [720])
        self.assertTrue(info.has_audio)

    def test_master_link_is_supported(self):
        with patch.object(DionIE, "_download_webpage", side_effect=webpage):
            info = downloader.extract_info(BASE + "master_playlist.m3u8")
        self.assertEqual([q.height for q in info.qualities], [360, 480, 720, 1080])

    def test_720p_selects_720p_not_best_1080p(self):
        opts = {**downloader._base_opts(), "format": downloader._format_for_height(720)}
        with patch.object(DionIE, "_download_webpage", side_effect=webpage):
            with downloader._youtube_dl(opts) as ydl:
                info = ydl.extract_info(PAGE, download=False)
        self.assertEqual(info["height"], 720)
        self.assertEqual(info["url"], BASE + "720p_record.m3u8")

    def test_other_sites_do_not_match(self):
        for url in (
            "https://www.youtube.com/watch?v=example",
            "https://dion.vc.evil.example/video/" + VIDEO_ID,
            "https://example.com/video/" + VIDEO_ID,
            "https://dion.vc/video/not-a-record-id",
        ):
            with self.subTest(url=url):
                self.assertFalse(DionIE.suitable(url))

    def test_unfinished_record_is_rejected(self):
        with patch.object(DionIE, "_download_webpage", return_value=MEDIA.replace("#EXT-X-ENDLIST", "")):
            with self.assertRaisesRegex(DownloadError, "ещё идёт"):
                downloader.extract_info(BASE + "720p_record.m3u8")

    def test_drm_is_rejected(self):
        with patch.object(DionIE, "_download_webpage", return_value=MEDIA.replace("AES-128", "SAMPLE-AES")):
            with self.assertRaisesRegex(DownloadError, "защиты"):
                downloader.extract_info(BASE + "720p_record.m3u8")

    def test_html_instead_of_manifest_is_rejected(self):
        with patch.object(DionIE, "_download_webpage", return_value="<html>Sign in</html>"):
            with self.assertRaisesRegex(DownloadError, "неверный HLS"):
                downloader.extract_info(PAGE)

    def test_closed_or_missing_record_has_clear_error(self):
        for status, message in ((401, "требует входа"), (403, "требует входа"), (404, "не найдена")):
            with self.subTest(status=status):
                response = Response(io.BytesIO(), BASE + "master_playlist.m3u8", {}, status=status)
                error = ExtractorError("HTTP error", cause=HTTPError(response))
                with patch.object(DionIE, "_download_webpage", side_effect=error):
                    with self.assertRaisesRegex(DownloadError, message):
                        downloader.extract_info(PAGE)

    def test_hls_download_limit_does_not_require_known_total(self):
        with patch.object(downloader, "MAX_FILE_BYTES", 1000):
            with self.assertRaises(FileTooLargeError):
                downloader._progress_hook({"status": "downloading", "downloaded_bytes": 1001}, None, None)

    def test_hls_cancel_stops_on_next_progress_hook(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(DownloadCancelled):
            downloader._progress_hook({"status": "downloading", "downloaded_bytes": 100}, None, cancelled)


if __name__ == "__main__":
    unittest.main()
