from __future__ import annotations

import re
from urllib.parse import urlparse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import ExtractorError

_UUID = r"[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}"


class DionIE(InfoExtractor):
    """Public DION recordings, using the same HLS manifest as the web player."""

    IE_NAME = "dion"
    _VALID_URL = (
        rf"https?://(?:(?:www\.)?dion\.vc/video/(?P<page_id>{_UUID})/?"
        rf"|hls-delivery\.dion\.vc/v3/records/(?P<hls_id>{_UUID})/hls/"
        r"(?P<manifest>master_playlist|\d+p_record)\.m3u8)(?:[?#].*)?$"
    )

    def _playlist(self, url: str, video_id: str) -> str:
        try:
            text = self._download_webpage(url, video_id, note="Читаю HLS DION")
        except ExtractorError as exc:
            if isinstance(exc.cause, HTTPError):
                if exc.cause.status in {401, 403}:
                    raise ExtractorError(
                        "DION: запись требует входа или доступ по ссылке закрыт.",
                        expected=True,
                    ) from exc
                if exc.cause.status in {404, 410}:
                    raise ExtractorError(
                        "DION: запись не найдена или ещё не готова к скачиванию.",
                        expected=True,
                    ) from exc
            raise
        if not text.lstrip().startswith("#EXTM3U"):
            raise ExtractorError("DION: вместо видео получен неверный HLS-плейлист.", expected=True)
        return text

    def _real_extract(self, url: str) -> dict:
        match = self._match_valid_url(url)
        video_id = match.group("page_id") or match.group("hls_id")
        webpage_url = f"https://dion.vc/video/{video_id}"
        playlist_url = (
            url.split("#", 1)[0]
            if match.group("hls_id")
            else f"https://hls-delivery.dion.vc/v3/records/{video_id}/hls/master_playlist.m3u8"
        )
        playlist = self._playlist(playlist_url, video_id)
        formats, _subtitles = self._parse_m3u8_formats_and_subtitles(
            playlist, playlist_url, ext="mp4", entry_protocol="m3u8_native", m3u8_id="hls",
        )
        if not formats:
            raise ExtractorError("DION: в записи нет доступных видеопотоков.", expected=True)

        # Media playlists have no RESOLUTION/CODECS; the quality is in the filename.
        # Unknown codecs mean a muxed stream, rather than a stream without video/audio.
        for fmt in formats:
            height = re.search(r"/(\d+)p_record\.m3u8$", urlparse(fmt["url"]).path)
            if height and not fmt.get("height"):
                fmt["height"] = int(height[1])
            fmt["vcodec"] = fmt.get("vcodec") or "unknown"
            fmt["acodec"] = fmt.get("acodec") or "unknown"

        media_playlist = playlist
        if "#EXT-X-STREAM-INF:" in playlist:
            # Only fetch one rendition for duration; the master already lists qualities.
            media_playlist = self._playlist(formats[0]["url"], video_id)
        if "#EXT-X-ENDLIST" not in media_playlist:
            raise ExtractorError(
                "DION: запись ещё идёт. Пришлите ссылку после завершения записи.", expected=True,
            )
        if any(fmt.get("has_drm") for fmt in formats) or re.search(
            r"#EXT-X-KEY:METHOD=(?!NONE(?:,|$)|AES-128(?:,|$))", media_playlist,
        ):
            raise ExtractorError("DION: этот формат защиты записи не поддерживается.", expected=True)
        duration = sum(float(value) for value in re.findall(r"(?m)^#EXTINF:([\d.]+)", media_playlist))
        return {
            "id": video_id,
            "title": f"Запись DION {video_id}",
            "webpage_url": webpage_url,
            # Keep a direct rendition URL on subsequent downloads, so 720p stays 720p.
            "original_url": url,
            "duration": duration or None,
            "formats": formats,
            "is_live": False,
        }
