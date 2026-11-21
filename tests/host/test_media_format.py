import pytest

import ytmp3_host.media_format as mf


class TestMediaFormat:
    """Unit tests for the media_format module."""

    def test_option_valid(self):
        request = {"quality": "720"}
        assert mf._option(request, "quality", mf.MP4_QUALITIES) == "720"

    def test_option_default(self):
        request = {}
        assert mf._option(request, "quality", mf.MP4_QUALITIES) == "best"

    def test_option_invalid(self):
        request = {"quality": "1440"}
        with pytest.raises(ValueError):
            mf._option(request, "quality", mf.MP4_QUALITIES)

    # ----------------- Tests for media_format_from_request -----------------

    def test_media_format_from_request_mp3(self):
        request = {"format": "mp3"}
        assert isinstance(mf.media_format_from_request(request), mf.Mp3Format)

    def test_media_format_from_request_mp4(self):
        request = {"format": "mp4", "quality": "720", "codec": "compatible"}
        media_format = mf.media_format_from_request(request)
        assert isinstance(media_format, mf.Mp4Format)
        assert media_format.quality == "720"
        assert media_format.codec == "compatible"

    def test_media_format_from_request_default(self):
        request = {}
        assert isinstance(mf.media_format_from_request(request), mf.Mp3Format)

    def test_media_format_from_request_invalid_format(self):
        request = {"format": "avi"}
        with pytest.raises(ValueError):
            mf.media_format_from_request(request)

    # ----------------- Tests for get_suffix -----------------

    def test_get_suffix_mp3(self):
        assert mf.get_suffix(mf.Mp3Format()) == ".mp3"

    def test_get_suffix_mp4_for_every_preset(self):
        for quality in mf.MP4_QUALITIES:
            for codec in mf.MP4_CODECS:
                assert mf.get_suffix(mf.Mp4Format(quality=quality, codec=codec)) == ".mp4"