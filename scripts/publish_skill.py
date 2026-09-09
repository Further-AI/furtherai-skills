"""Publish a validated CI artifact and conditionally promote it to stable."""

import argparse
import os
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Annotated, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SkillName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class CIContext(BaseModel):
    """GitHub's identity and OIDC credentials for a release from main."""

    model_config = ConfigDict(strict=True)

    repository: Literal["Further-AI/furtherai-skills"] = Field(alias="GITHUB_REPOSITORY")
    commit_sha: str = Field(alias="GITHUB_SHA", pattern=r"^[0-9a-f]{40}$")
    ref: Literal["refs/heads/main"] = Field(alias="GITHUB_REF")
    event: Literal["push", "workflow_dispatch"] = Field(alias="GITHUB_EVENT_NAME")
    token_url: str = Field(alias="ACTIONS_ID_TOKEN_REQUEST_URL", min_length=1)
    request_token: SecretStr = Field(alias="ACTIONS_ID_TOKEN_REQUEST_TOKEN", min_length=1)


class IdentityToken(BaseModel):
    """The short-lived token returned by GitHub's OIDC endpoint."""

    value: SecretStr = Field(min_length=1)


class PublishedVersion(BaseModel):
    """The immutable version and stable digest observed by the publishing API."""

    name: SkillName
    content_digest: Digest
    stable_digest: Digest | None


class PromotionRequest(BaseModel):
    """Promote only if stable still matches the version observed during upload."""

    content_digest: Digest
    expected_digest: Digest | None


class StableVersion(BaseModel):
    """The digest confirmed by a successful promotion."""

    content_digest: Digest


class PublishingError(Exception):
    """A release failed before it could confirm the requested stable version."""


class _NoRedirects(HTTPRedirectHandler):
    """Keep bearer credentials at their intended endpoint."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


def _request(request: Request, operation: str) -> bytes:
    """Send one request, reporting failures without tokens or response bodies."""
    try:
        with build_opener(_NoRedirects()).open(request, timeout=120) as response:
            return response.read()
    except HTTPError as exc:
        raise PublishingError(f"{operation} failed (HTTP {exc.code}).") from None
    except (URLError, TimeoutError) as exc:
        raise PublishingError(f"{operation} could not reach the server ({type(exc).__name__}).") from None


def _check_url(url: str) -> None:
    """Require HTTPS before sending credentials."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise PublishingError("Publishing and OIDC URLs must use HTTPS without credentials or fragments.")


def _multipart(bundle: Path, context: CIContext) -> tuple[bytes, str]:
    """Encode provenance and the exact ZIP bytes produced by validation."""
    boundary = uuid4().hex
    parts = []
    for name, value in {"repository": context.repository, "commit_sha": context.commit_sha}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="bundle"; filename="skill.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n".encode()
        + bundle.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _identity_token(context: CIContext, audience: str) -> str:
    """Request a GitHub token scoped to the publishing backend."""
    token_parts = urlsplit(context.token_url)
    query = [(key, value) for key, value in parse_qsl(token_parts.query) if key != "audience"]
    token_url = urlunsplit(token_parts._replace(query=urlencode([*query, ("audience", audience)])))
    token_response = _request(
        Request(
            token_url,
            headers={
                "Authorization": f"Bearer {context.request_token.get_secret_value()}",
            },
        ),
        "OIDC authentication",
    )
    return IdentityToken.model_validate_json(token_response).value.get_secret_value()


def publish_skill(bundle: Path, *, api_url: str, audience: str, context: CIContext) -> str:
    """Publish the validated artifact, then promote using the observed stable digest.

    A conflict stops the release. Retrying with a different expected digest here
    could overwrite another publisher's promotion.
    """
    _check_url(api_url)
    _check_url(context.token_url)
    if urlsplit(api_url).query or not audience:
        raise PublishingError("Set the API base URL without a query and a nonempty OIDC audience.")
    # Validate the filename before using it as an API path component.
    name = TypeAdapter(SkillName).validate_python(bundle.stem)
    body, content_type = _multipart(bundle, context)
    token = _identity_token(context, audience)
    headers = {"Authorization": f"Bearer {token}"}
    skill_url = f"{api_url.rstrip('/')}/internal/skills/{name}"
    response = _request(
        Request(
            f"{skill_url}/versions",
            data=body,
            headers={**headers, "Content-Type": content_type},
            method="POST",
        ),
        "Publishing",
    )
    published = PublishedVersion.model_validate_json(response)
    if published.name != name:
        raise PublishingError("Publishing returned a different skill name.")
    promotion = PromotionRequest(content_digest=published.content_digest, expected_digest=published.stable_digest)
    response = _request(
        Request(
            f"{skill_url}/channels/stable",
            data=promotion.model_dump_json().encode(),
            headers={**headers, "Content-Type": "application/json"},
            method="PUT",
        ),
        "Promotion",
    )
    promoted = StableVersion.model_validate_json(response)
    if promoted.content_digest != published.content_digest:
        raise PublishingError("Promotion returned a different content digest.")
    return promoted.content_digest


def main() -> None:
    """Publish from GitHub Actions using its short-lived OIDC identity."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--audience", required=True)
    args = parser.parse_args()
    try:
        context = CIContext.model_validate(dict(os.environ))
        digest = publish_skill(args.bundle, api_url=args.api_url, audience=args.audience, context=context)
    except ValidationError:
        parser.exit(1, "Invalid CI configuration or publishing API response.\n")
    except (PublishingError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Promoted {args.bundle.stem} to stable: {digest}")


if __name__ == "__main__":
    main()
