"""Helpers to deal with Cast devices."""
from __future__ import annotations

import asyncio
import configparser
from dataclasses import dataclass
import logging
from typing import Optional

import aiohttp
import attr
from pychromecast import dial
from pychromecast.const import CAST_TYPE_GROUP
from pychromecast.models import CastInfo

from homeassistant.helpers import aiohttp_client

_LOGGER = logging.getLogger(__name__)

_PLS_SECTION_PLAYLIST = "playlist"


@attr.s(slots=True, frozen=True)
class ChromecastInfo:
    """Class to hold all data about a chromecast for creating connections.

    This also has the same attributes as the mDNS fields by zeroconf.
    """

    cast_info: CastInfo = attr.ib()
    is_dynamic_group = attr.ib(type=Optional[bool], default=None)

    @property
    def friendly_name(self) -> str:
        """Return the UUID."""
        return self.cast_info.friendly_name

    @property
    def is_audio_group(self) -> bool:
        """Return if the cast is an audio group."""
        return self.cast_info.cast_type == CAST_TYPE_GROUP

    @property
    def uuid(self) -> bool:
        """Return the UUID."""
        return self.cast_info.uuid

    def fill_out_missing_chromecast_info(self) -> ChromecastInfo:
        """Return a new ChromecastInfo object with missing attributes filled in.

        Uses blocking HTTP / HTTPS.
        """
        cast_info = self.cast_info
        if self.cast_info.cast_type is None or self.cast_info.manufacturer is None:
            # Manufacturer and cast type is not available in mDNS data, get it over http
            cast_info = dial.get_cast_type(
                cast_info,
                zconf=ChromeCastZeroconf.get_zeroconf(),
            )

        if not self.is_audio_group or self.is_dynamic_group is not None:
            # We have all information, no need to check HTTP API.
            return ChromecastInfo(cast_info=cast_info)

        # Fill out missing group information via HTTP API.
        is_dynamic_group = False
        http_group_status = None
        http_group_status = dial.get_multizone_status(
            None,
            services=self.cast_info.services,
            zconf=ChromeCastZeroconf.get_zeroconf(),
        )
        if http_group_status is not None:
            is_dynamic_group = any(
                g.uuid == self.cast_info.uuid for g in http_group_status.dynamic_groups
            )

        return ChromecastInfo(
            cast_info=cast_info,
            is_dynamic_group=is_dynamic_group,
        )


class ChromeCastZeroconf:
    """Class to hold a zeroconf instance."""

    __zconf = None

    @classmethod
    def set_zeroconf(cls, zconf):
        """Set zeroconf."""
        cls.__zconf = zconf

    @classmethod
    def get_zeroconf(cls):
        """Get zeroconf."""
        return cls.__zconf


class CastStatusListener:
    """Helper class to handle pychromecast status callbacks.

    Necessary because a CastDevice entity or dynamic group can create a new
    socket client and therefore callbacks from multiple chromecast connections can
    potentially arrive. This class allows invalidating past chromecast objects.
    """

    def __init__(self, cast_device, chromecast, mz_mgr, mz_only=False):
        """Initialize the status listener."""
        self._cast_device = cast_device
        self._uuid = chromecast.uuid
        self._valid = True
        self._mz_mgr = mz_mgr

        if cast_device._cast_info.is_audio_group:
            self._mz_mgr.add_multizone(chromecast)
        if mz_only:
            return

        chromecast.register_status_listener(self)
        chromecast.socket_client.media_controller.register_status_listener(self)
        chromecast.register_connection_listener(self)
        if not cast_device._cast_info.is_audio_group:
            self._mz_mgr.register_listener(chromecast.uuid, self)

    def new_cast_status(self, cast_status):
        """Handle reception of a new CastStatus."""
        if self._valid:
            self._cast_device.new_cast_status(cast_status)

    def new_media_status(self, media_status):
        """Handle reception of a new MediaStatus."""
        if self._valid:
            self._cast_device.new_media_status(media_status)

    def new_connection_status(self, connection_status):
        """Handle reception of a new ConnectionStatus."""
        if self._valid:
            self._cast_device.new_connection_status(connection_status)

    @staticmethod
    def added_to_multizone(group_uuid):
        """Handle the cast added to a group."""

    def removed_from_multizone(self, group_uuid):
        """Handle the cast removed from a group."""
        if self._valid:
            self._cast_device.multizone_new_media_status(group_uuid, None)

    def multizone_new_cast_status(self, group_uuid, cast_status):
        """Handle reception of a new CastStatus for a group."""

    def multizone_new_media_status(self, group_uuid, media_status):
        """Handle reception of a new MediaStatus for a group."""
        if self._valid:
            self._cast_device.multizone_new_media_status(group_uuid, media_status)

    def invalidate(self):
        """Invalidate this status listener.

        All following callbacks won't be forwarded.
        """
        # pylint: disable=protected-access
        if self._cast_device._cast_info.is_audio_group:
            self._mz_mgr.remove_multizone(self._uuid)
        else:
            self._mz_mgr.deregister_listener(self._uuid, self)
        self._valid = False


class PlaylistError(Exception):
    """Exception wrapper for pls and m3u helpers."""


@dataclass
class PlaylistItem:
    """Playlist item."""

    length: str | None
    title: str | None
    url: str


async def _fetch_playlist(hass, url):
    """Fetch a playlist from the given url."""
    try:
        session = aiohttp_client.async_get_clientsession(hass, verify_ssl=False)
        async with session.get(url, timeout=5) as resp:
            playlist_data = (await resp.content.read(64 * 1024)).decode()
    except asyncio.TimeoutError as err:
        raise PlaylistError(f"Timeout while fetching pls {url}") from err
    except aiohttp.client_exceptions.ClientError as err:
        raise PlaylistError(f"Error while fetching pls {url}") from err

    return playlist_data


async def parse_m3u(hass, url):
    """Very simple m3u parser.

    Based on https://github.com/dvndrsn/M3uParser/blob/master/m3uparser.py
    """
    m3u_data = await _fetch_playlist(hass, url)

    m3u_lines = m3u_data.splitlines()
    if not m3u_lines:
        raise PlaylistError(f"Empty playlist {url}")

    playlist = []

    length = None
    title = None

    for line in m3u_lines:
        line = line.strip()
        if line.startswith("#EXTINF:"):
            # Get length and title from #EXTINF line
            info = line.split("#EXTINF:")[1].split(",", 1)
            if len(info) != 2:
                _LOGGER.debug("Ignoring invalid extinf %s in playlist %s", line, url)
                continue
            length = info[0].split(" ", 1)
            title = info[1].strip()
        elif line.startswith("#"):
            # Ignore other extensions
            continue
        elif len(line) != 0:
            # Get song path from all other, non-blank lines
            playlist.append(PlaylistItem(length=length, title=title, url=line))
            # reset the song variables so it doesn't use the same EXTINF more than once
            length = None
            title = None

    return playlist


async def parse_pls(hass, url):
    """Very simple pls parser.

    Based on https://github.com/mariob/plsparser/blob/master/src/plsparser.py
    """
    pls_data = await _fetch_playlist(hass, url)

    pls_parser = configparser.ConfigParser()
    try:
        pls_parser.read_string(pls_data, url)
    except configparser.Error as err:
        raise PlaylistError(f"Can't parse playlist {url}") from err

    if (
        _PLS_SECTION_PLAYLIST not in pls_parser
        or pls_parser[_PLS_SECTION_PLAYLIST].getint("Version") != 2
    ):
        raise PlaylistError(f"Invalid playlist {url}")

    try:
        num_entries = pls_parser.getint(_PLS_SECTION_PLAYLIST, "NumberOfEntries")
    except (configparser.NoOptionError, ValueError) as err:
        raise PlaylistError(f"Invalid NumberOfEntries in playlist {url}") from err

    playlist_section = pls_parser[_PLS_SECTION_PLAYLIST]

    playlist = []
    for entry in range(1, num_entries + 1):
        file_option = f"File{entry}"
        if file_option not in playlist_section:
            _LOGGER.warning("Missing %s in pls from %s", file_option, url)
            continue
        playlist.append(
            PlaylistItem(
                length=playlist_section.get(f"Length{entry}"),
                title=playlist_section.get(f"Title{entry}"),
                url=playlist_section[file_option],
            )
        )
    return playlist
