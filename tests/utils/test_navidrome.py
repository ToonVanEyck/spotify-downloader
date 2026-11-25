import hashlib

import pytest
import requests

from spotdl.utils.navidrome import Navidrome, NavidromeError


@pytest.fixture
def navidrome():
    return Navidrome("https://music.example/library", "test-user", "test-password")


def test_search_uses_salted_post_and_requires_artist_and_version(navidrome, mocker):
    post = mocker.patch("spotdl.utils.navidrome.requests.post")
    post.return_value.status_code = 200
    post.return_value.json.return_value = {
        "subsonic-response": {
            "status": "ok",
            "searchResult3": {
                "song": [
                    {"artist": "Another Artist", "title": "Get Lucky"},
                    {"artist": "Daft Punk", "title": "Get Lucky (Live)"},
                    {"id": "lucky", "artist": "DAFT PUNK", "title": "Get Lucky!"},
                ]
            },
        }
    }

    assert navidrome.contains("Daft Punk", "get lucky")
    url = post.call_args.args[0]
    options = post.call_args.kwargs
    assert url == "https://music.example/library/rest/search3.view"
    assert "test-password" not in repr(options)
    assert "params" not in options
    data = options["data"]
    assert (
        data["t"]
        == hashlib.md5(
            ("test-password" + data["s"]).encode(), usedforsecurity=False
        ).hexdigest()
    )
    assert not options["allow_redirects"]

    post.return_value.json.return_value["subsonic-response"]["searchResult3"][
        "song"
    ].pop()
    assert not navidrome.contains("Daft Punk", "get lucky")


def test_library_search_checks_later_pages(navidrome, mocker):
    request = mocker.patch.object(
        Navidrome,
        "request",
        side_effect=[
            {"searchResult3": {"song": [{"artist": "Other", "title": "Song"}] * 100}},
            {
                "searchResult3": {
                    "song": [{"id": "found", "artist": "Artist", "title": "Song"}]
                }
            },
        ],
    )

    assert navidrome.contains("Artist", "Song")
    assert [call.kwargs["songOffset"] for call in request.call_args_list] == [0, 100]


def test_song_lookup_returns_id_and_rejects_missing_id(navidrome, mocker):
    song = {"id": "song-123", "artist": "Artist", "title": "Song"}
    mocker.patch.object(
        Navidrome, "request", return_value={"searchResult3": {"song": [song]}}
    )
    assert navidrome.find_song_id("Artist", "Song") == "song-123"
    song.pop("id")
    with pytest.raises(NavidromeError, match="song's ID"):
        navidrome.find_song_id("Artist", "Song")


def test_search_songs_returns_display_metadata(navidrome, mocker):
    request = mocker.patch.object(
        Navidrome,
        "request",
        return_value={
            "searchResult3": {
                "song": [
                    {
                        "id": "song-123",
                        "title": "Song",
                        "artist": "Artist",
                        "album": "Album",
                        "duration": 210,
                        "path": "Artist/Album/song.mp3",
                    }
                ]
            }
        },
    )

    assert navidrome.search_songs("Artist Song") == [
        {
            "id": "song-123",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 210,
        }
    ]
    request.assert_called_once_with(
        "search3",
        query="Artist Song",
        artistCount=0,
        albumCount=0,
        songCount=20,
        songOffset=0,
    )


def test_get_song_requires_matching_id_and_path(navidrome, mocker):
    request = mocker.patch.object(
        Navidrome,
        "request",
        return_value={
            "song": {
                "id": "song-123",
                "title": "Song",
                "artist": "Artist",
                "path": "Artist/Album/song.mp3",
            }
        },
    )

    assert navidrome.get_song("song-123")["path"] == "Artist/Album/song.mp3"
    request.assert_called_once_with("getSong", id="song-123")

    request.return_value = {"song": {"id": "another-song", "path": "song.mp3"}}
    with pytest.raises(NavidromeError, match="incomplete song file metadata"):
        navidrome.get_song("song-123")


@pytest.mark.parametrize("songs", [["one", "two", "one"], []])
def test_playlist_replaces_contents_using_owned_id(navidrome, mocker, songs):
    request = mocker.patch.object(
        Navidrome,
        "request",
        side_effect=[
            {
                "playlists": {
                    "playlist": [
                        {"id": "other", "name": "Radio", "owner": "someone-else"},
                        {"id": "existing", "name": "Radio", "owner": "test-user"},
                    ]
                }
            },
            {"playlist": {"id": "existing"}},
        ],
    )

    assert navidrome.replace_playlist("Radio", songs) == "existing"
    assert request.call_args_list == [
        mocker.call("getPlaylists"),
        mocker.call(
            "createPlaylist", playlistId="existing", songId=list(dict.fromkeys(songs))
        ),
    ]


def test_playlist_creation_does_not_replace_another_users_playlist(navidrome, mocker):
    request = mocker.patch.object(
        Navidrome,
        "request",
        side_effect=[
            {
                "playlists": {
                    "playlist": [{"id": "other", "name": "Radio", "owner": "other"}]
                }
            },
            {"playlist": {"id": "new"}},
        ],
    )
    assert navidrome.replace_playlist("Radio", ["one", "one"]) == "new"
    request.assert_called_with("createPlaylist", name="Radio", songId=["one"])


@pytest.mark.parametrize("existing_name", ["Radio - Station", "spotDL Radio - Station"])
def test_playlist_reuses_owned_id_and_migrates_old_name(
    navidrome, mocker, existing_name
):
    request = mocker.patch.object(
        Navidrome,
        "request",
        side_effect=[
            {
                "playlists": {
                    "playlist": [
                        {
                            "id": "owned",
                            "name": existing_name,
                            "owner": navidrome.username,
                        },
                        {
                            "id": "other",
                            "name": "spotDL Radio - Station",
                            "owner": "other",
                        },
                    ]
                }
            },
            {"playlist": {"id": "owned"}},
            {},
        ],
    )

    assert (
        navidrome.replace_playlist(
            "Radio - Station", ["one"], previous_name="spotDL Radio - Station"
        )
        == "owned"
    )
    assert request.call_args_list[:2] == [
        mocker.call("getPlaylists"),
        mocker.call("createPlaylist", playlistId="owned", songId=["one"]),
    ]
    if existing_name.startswith("spotDL"):
        request.assert_called_with(
            "updatePlaylist", playlistId="owned", name="Radio - Station"
        )
    else:
        assert request.call_count == 2


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"playlists": {"playlist": None}},
        {"playlists": {"playlist": [None]}},
        {"playlists": {"playlist": [{"name": "Radio", "owner": "test-user"}]}},
        {"playlists": {"playlist": [{"id": "one", "name": "Radio"}]}},
        {
            "playlists": {
                "playlist": [
                    {"id": "one", "name": "Radio", "owner": "test-user"},
                    {"id": "two", "name": "Radio", "owner": "test-user"},
                ]
            }
        },
    ],
)
def test_invalid_or_ambiguous_playlists_block_writes(navidrome, mocker, response):
    request = mocker.patch.object(Navidrome, "request", return_value=response)
    with pytest.raises(NavidromeError):
        navidrome.replace_playlist("Radio", ["one"])
    request.assert_called_once_with("getPlaylists")


def test_playlist_failure_is_reported(navidrome, mocker):
    mocker.patch.object(
        Navidrome,
        "request",
        side_effect=[{"playlists": {}}, {}],
    )
    with pytest.raises(NavidromeError, match="playlist ID"):
        navidrome.replace_playlist("Radio", [])


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"subsonic-response": {"status": "failed", "error": {"code": 40}}},
        {"subsonic-response": {"status": "ok", "searchResult3": None}},
        {"subsonic-response": {"status": "ok", "searchResult3": {"song": None}}},
    ],
)
def test_invalid_library_responses_are_not_treated_as_missing(
    navidrome, mocker, payload
):
    post = mocker.patch("spotdl.utils.navidrome.requests.post")
    post.return_value.status_code = 200
    post.return_value.json.return_value = payload

    with pytest.raises(NavidromeError):
        navidrome.contains("Artist", "Song")


def test_failed_connection_does_not_expose_credentials(navidrome, mocker):
    mocker.patch(
        "spotdl.utils.navidrome.requests.post",
        side_effect=requests.ConnectionError("possibly sensitive transport details"),
    )
    with pytest.raises(NavidromeError, match="connection failed") as error:
        navidrome.ping()
    assert "sensitive" not in str(error.value)
    assert "test-password" not in repr(navidrome)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@music.example",
        "https://music.example?q=x",
    ],
)
def test_invalid_server_url_is_rejected(url):
    with pytest.raises(ValueError):
        Navidrome(url, "user", "test-password")
