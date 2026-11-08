"""The video info yt-dlp prints, and the file name and tags derived from it."""

import json
import logging

from ytmp3_host.naming import ARTIST_SEPARATOR_RE, UNKNOWN_ARTIST, artist_from_filename, safe_filename

logger = logging.getLogger(__name__)

# the video info yt-dlp prints as JSON before downloading: the title names the file, the rest
# become tags. artists/album/release_year/track_number exist only for YouTube Music uploads
# and music sites (SoundCloud, Bandcamp, ...); other sites have an uploader instead of a channel.
INFO_FIELDS = (
    "title", "artists", "album", "release_year", "track_number", "channel", "uploader", "webpage_url", "extractor_key"
)
# sites whose `artists` list the tracks of a mix, not who made it
TRACKLIST_EXTRACTORS = frozenset({"Mixcloud"})
TOPIC_SUFFIX = " - Topic"  # YouTube Music's auto-generated artist channels, e.g. "Queen - Topic"


def parse_info(text: str) -> dict[str, object]:
    """Parse the video info yt-dlp printed after INFO_MARKER.

    Args:
        text: The JSON object of INFO_FIELDS; yt-dlp leaves out the fields a video lacks.
    Returns:
        The info, or {} if it is not a JSON object (logged).
    """
    try:
        info = json.loads(text)
    except ValueError:
        logger.warning("Could not parse the video info: %r", text)
        return {}
    return info if isinstance(info, dict) else {}


def info_text(info: dict[str, object], key: str) -> str:
    """A video info field as stripped text, "" if it is missing or not a string or number.

    Args:
        info: The video info (see INFO_FIELDS).
        key: The field name.
    Returns:
        The text; a list, dict or null (yt-dlp's JSON for odd videos) gives "".
    """
    value = info.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def info_artists(info: dict[str, object]) -> str:
    """The artists the site names for the track e.g. "ACDC"; "" if none.
    Mixcloud's `artists` are the artists of the tracks in a mix, so they are left out.

    Args:
        info: The video info (see INFO_FIELDS).
    Returns:
        The artists joined with ", ".
    """
    artists = info.get("artists")
    if info_text(info, "extractor_key") in TRACKLIST_EXTRACTORS or not isinstance(artists, list):
        return ""
    return ", ".join(a.strip() for a in artists if isinstance(a, str) and a.strip())


def title_filename(info: dict[str, object]) -> str:
    """The filename of a download named after its title.
    A title that names no artist (no spaced dash, as on YouTube Music and SoundCloud)
    gets the site's artists in front, so the file is "Artist - Title" like most titles.

    Args:
        info: The video info (see INFO_FIELDS).
    Returns:
        The sanitized filename without extension; DEFAULT_FILENAME without a title.
    """
    title = info_text(info, "title")
    name = safe_filename(title)
    artists = info_artists(info)
    if title and artists and not ARTIST_SEPARATOR_RE.search(name):
        return safe_filename(f"{artists} - {title}")
    return name


def artist_for(filename: str, info: dict[str, object]) -> str:
    """The artist tag: from the filename, else from the video info.

    Args:
        filename: The final filename without extension.
        info: The video info (see INFO_FIELDS).
    Returns:
        artist_from_filename's artist if the filename has one, else the video's
        artists (info_artists), else its channel without " - Topic", else its uploader,
        else UNKNOWN_ARTIST.
    """
    artist = artist_from_filename(filename)
    if artist != UNKNOWN_ARTIST:
        return artist
    channel = info_text(info, "channel").removesuffix(TOPIC_SUFFIX).strip()
    return info_artists(info) or channel or info_text(info, "uploader") or UNKNOWN_ARTIST


def extra_tags(info: dict[str, object]) -> dict[str, str]:
    """The tags besides artist and title, as ffmpeg metadata keys (ID3v2.3 frame in the comment).
    The year is the release year only: the upload year of an official video is
    often decades after the song.

    Args:
        info: The video info (see INFO_FIELDS).
    Returns:
        The tags the video has values for.
    """
    tags = {
        "album": info_text(info, "album"),  # TALB
        "date": info_text(info, "release_year"),  # TYER
        "track": info_text(info, "track_number"),  # TRCK
        "comment": info_text(info, "webpage_url"),  # COMM: where the song came from
    }
    return {key: value for key, value in tags.items() if value}
