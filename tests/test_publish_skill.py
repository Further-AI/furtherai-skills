"""Check authenticated publishing, promotion conflicts, and transport failures."""

import json
import sys
from collections.abc import Iterator
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest
from pydantic import ValidationError

from scripts import publish_skill
from scripts.skill_bundle import build_bundle

DIGEST = "a" * 64
PREVIOUS = "b" * 64
API_URL = "https://backend.example"
AUDIENCE = "furtherai-skills-us-staging"


@pytest.fixture
def context() -> publish_skill.CIContext:
    """Provide GitHub's environment shape without real credentials."""
    return publish_skill.CIContext.model_validate(
        {
            "GITHUB_REPOSITORY": "Further-AI/furtherai-skills",
            "GITHUB_SHA": "c" * 40,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "push",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.example/token?request=1&audience=old",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
        }
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """Build an actual validated ZIP for the publishing flow."""
    skill = tmp_path / "document-extraction"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: document-extraction\ndescription: Extract fields.\n---\nExtract fields.\n"
    )
    output = tmp_path / "document-extraction.zip"
    build_bundle(skill, output)
    return output


@pytest.fixture
def request_mock() -> Iterator[Mock]:
    """Keep publishing requests isolated from external services."""
    with patch.object(publish_skill, "_request") as mocked:
        mocked.side_effect = [
            b'{"value":"identity-token"}',
            json.dumps({"name": "document-extraction", "content_digest": DIGEST, "stable_digest": None}).encode(),
            json.dumps({"content_digest": DIGEST}).encode(),
        ]
        yield mocked


@pytest.mark.parametrize("stable_digest", [None, PREVIOUS, DIGEST])
def test_publish_skill_uploads_exact_artifact_then_promotes_observed_version(
    context: publish_skill.CIContext,
    bundle: Path,
    request_mock: Mock,
    stable_digest: str | None,
) -> None:
    request_mock.side_effect = [
        b'{"value":"identity-token"}',
        json.dumps({"name": "document-extraction", "content_digest": DIGEST, "stable_digest": stable_digest}).encode(),
        json.dumps({"content_digest": DIGEST}).encode(),
    ]
    assert publish_skill.publish_skill(bundle, api_url=API_URL, audience=AUDIENCE, context=context) == DIGEST
    token_request, upload, promote = [call.args[0] for call in request_mock.call_args_list]
    assert parse_qs(urlsplit(token_request.full_url).query) == {"request": ["1"], "audience": [AUDIENCE]}
    assert token_request.get_header("Authorization") == "Bearer request-token"
    assert upload.method == "POST"
    assert upload.full_url == f"{API_URL}/internal/skills/document-extraction/versions"
    assert upload.get_header("Authorization") == "Bearer identity-token"
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {upload.get_header('Content-type')}\r\n\r\n".encode() + upload.data
    )
    parts = {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
        for part in message.iter_parts()
    }
    assert parts == {
        "repository": context.repository.encode(),
        "commit_sha": context.commit_sha.encode(),
        "bundle": bundle.read_bytes(),
    }
    assert promote.method == "PUT"
    assert promote.full_url == f"{API_URL}/internal/skills/document-extraction/channels/stable"
    assert promote.get_header("Authorization") == "Bearer identity-token"
    assert json.loads(promote.data) == {"content_digest": DIGEST, "expected_digest": stable_digest}


@pytest.mark.parametrize("failure_step", [0, 1, 2])
def test_publish_skill_stops_on_failure_without_retrying(
    context: publish_skill.CIContext,
    bundle: Path,
    request_mock: Mock,
    failure_step: int,
) -> None:
    responses = [
        b'{"value":"identity-token"}',
        json.dumps({"name": "document-extraction", "content_digest": DIGEST, "stable_digest": None}).encode(),
    ]
    request_mock.side_effect = [*responses[:failure_step], publish_skill.PublishingError("HTTP 409")]
    with pytest.raises(publish_skill.PublishingError, match="HTTP 409"):
        publish_skill.publish_skill(bundle, api_url=API_URL, audience=AUDIENCE, context=context)
    assert request_mock.call_count == failure_step + 1


@pytest.mark.parametrize(
    "response",
    [
        b"not JSON",
        b"{}",
        json.dumps({"name": "document-extraction", "content_digest": "invalid", "stable_digest": None}).encode(),
        json.dumps({"name": "different-skill", "content_digest": DIGEST, "stable_digest": None}).encode(),
    ],
)
def test_publish_skill_invalid_upload_response_blocks_promotion(
    context: publish_skill.CIContext,
    bundle: Path,
    request_mock: Mock,
    response: bytes,
) -> None:
    request_mock.side_effect = [b'{"value":"identity-token"}', response]
    with pytest.raises((ValidationError, publish_skill.PublishingError)):
        publish_skill.publish_skill(bundle, api_url=API_URL, audience=AUDIENCE, context=context)
    assert request_mock.call_count == 2


def test_publish_skill_rejects_unconfirmed_promotion(
    context: publish_skill.CIContext,
    bundle: Path,
    request_mock: Mock,
) -> None:
    request_mock.side_effect = [
        b'{"value":"identity-token"}',
        json.dumps({"name": "document-extraction", "content_digest": DIGEST, "stable_digest": None}).encode(),
        json.dumps({"content_digest": PREVIOUS}).encode(),
    ]
    with pytest.raises(publish_skill.PublishingError, match="different content digest"):
        publish_skill.publish_skill(bundle, api_url=API_URL, audience=AUDIENCE, context=context)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("GITHUB_REPOSITORY", "someone/furtherai-skills"),
        ("GITHUB_SHA", "main"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ""),
    ],
)
def test_context_rejects_untrusted_or_missing_release_identity(
    context: publish_skill.CIContext,
    field: str,
    value: str,
) -> None:
    values = context.model_dump(by_alias=True)
    values[field] = value
    with pytest.raises(ValidationError):
        publish_skill.CIContext.model_validate(values)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://backend.example",
        "https://user:pass@backend.example",
        "https://backend.example/#fragment",
        "https://backend.example/?query=1",
    ],
)
def test_publish_skill_rejects_invalid_api_url_before_sending_credentials(
    context: publish_skill.CIContext,
    bundle: Path,
    request_mock: Mock,
    url: str,
) -> None:
    with pytest.raises(publish_skill.PublishingError):
        publish_skill.publish_skill(bundle, api_url=url, audience=AUDIENCE, context=context)
    request_mock.assert_not_called()


@pytest.fixture
def http_server() -> Iterator[tuple[str, list[str]]]:
    """Serve controlled responses to exercise urllib's actual redirect handling."""
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/secret")
            else:
                self.send_response(int(self.path.removeprefix("/")))
            self.end_headers()
            self.wfile.write(b"secret-response-token")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", paths
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("status", [200, 201])
def test_request_returns_successful_response(http_server: tuple[str, list[str]], status: int) -> None:
    url, paths = http_server
    assert publish_skill._request(Request(f"{url}/{status}"), "Publishing") == b"secret-response-token"
    assert paths == [f"/{status}"]


@pytest.mark.parametrize("path", ["/redirect", "/403", "/409", "/500"])
def test_request_rejects_redirects_and_hides_error_body(http_server: tuple[str, list[str]], path: str) -> None:
    url, paths = http_server
    with pytest.raises(publish_skill.PublishingError, match="Publishing failed \\(HTTP") as error:
        publish_skill._request(Request(f"{url}{path}", headers={"Authorization": "Bearer secret"}), "Publishing")
    assert "secret" not in str(error.value)
    assert paths == [path]


@pytest.mark.parametrize("failure", [URLError("secret-host"), TimeoutError("secret-host")])
def test_request_network_failure_hides_connection_details(failure: Exception) -> None:
    with patch.object(publish_skill, "build_opener") as opener:
        opener.return_value.open.side_effect = failure
        with pytest.raises(publish_skill.PublishingError, match="could not reach") as error:
            publish_skill._request(Request(API_URL), "Publishing")
    assert "secret-host" not in str(error.value)


@pytest.mark.parametrize("token_url", ["http://github.example/token", "https://token:secret@github.example/token"])
def test_publish_skill_invalid_oidc_url_blocks_token_request(
    context: publish_skill.CIContext,
    bundle: Path,
    request_mock: Mock,
    token_url: str,
) -> None:
    context.token_url = token_url
    with pytest.raises(publish_skill.PublishingError):
        publish_skill.publish_skill(bundle, api_url=API_URL, audience=AUDIENCE, context=context)
    request_mock.assert_not_called()


@pytest.mark.parametrize("filename", ["Bad_Name.zip", "skill.name.zip"])
def test_publish_skill_invalid_name_blocks_token_request(
    context: publish_skill.CIContext,
    bundle: Path,
    request_mock: Mock,
    filename: str,
) -> None:
    with pytest.raises(ValidationError):
        publish_skill.publish_skill(bundle.with_name(filename), api_url=API_URL, audience=AUDIENCE, context=context)
    request_mock.assert_not_called()


@pytest.mark.parametrize("failure", ["identity", "api_response", "publishing"])
def test_main_reports_failure_without_exposing_tokens(
    context: publish_skill.CIContext,
    bundle: Path,
    request_mock: Mock,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    environment = context.model_dump(by_alias=True)
    environment["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] = "secret-token"
    if failure == "identity":
        environment.pop("GITHUB_REF")
    elif failure == "api_response":
        request_mock.side_effect = [b'{"value":"secret-token"}', b"secret-token"]
    else:
        request_mock.side_effect = publish_skill.PublishingError("Publishing failed (HTTP 403).")
    with (
        patch.dict(publish_skill.os.environ, environment, clear=True),
        patch.object(
            sys,
            "argv",
            ["publish_skill.py", str(bundle), "--api-url", API_URL, "--audience", AUDIENCE],
        ),
        pytest.raises(SystemExit) as error,
    ):
        publish_skill.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "secret-token" not in output.err + output.out
    expected = "HTTP 403" if failure == "publishing" else "Invalid CI configuration"
    assert expected in output.err


def test_main_reports_confirmed_stable_digest(
    context: publish_skill.CIContext,
    bundle: Path,
    request_mock: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = context.model_dump(by_alias=True)
    environment["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] = "secret-token"
    with (
        patch.dict(publish_skill.os.environ, environment, clear=True),
        patch.object(
            sys,
            "argv",
            ["publish_skill.py", str(bundle), "--api-url", API_URL, "--audience", AUDIENCE],
        ),
    ):
        publish_skill.main()
    assert request_mock.call_count == 3
    assert capsys.readouterr().out == f"Promoted document-extraction to stable: {DIGEST}\n"
