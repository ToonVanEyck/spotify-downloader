from collections import deque
from threading import Lock

from spotdl.utils.web import Client, validate_search_term


def test_web_searchterms():
    url = "https://open.spotify.com/intl-pt/track/example"
    result = validate_search_term(url)
    assert result is True


def test_session_output_is_bounded():
    client = Client.__new__(Client)
    client.log_lines = deque(maxlen=1000)
    client.log_lock = Lock()

    client.append_log("\n".join(str(line) for line in range(1100)))

    assert client.get_log_output().splitlines() == [
        str(line) for line in range(100, 1100)
    ]

    client.append_log("x" * 9000)
    assert len(client.get_log_output().splitlines()[-1]) == 8000
