"""Owner-authenticated OAuth provider for the HTTP MCP server."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import sqlite3
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse, Response

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from starlette.requests import Request

logger = logging.getLogger(__name__)

_CODE_TTL_SECONDS = 300
_ACCESS_TOKEN_TTL_SECONDS = 3600
_REFRESH_TOKEN_TTL_SECONDS = 365 * 24 * 60 * 60
_MAX_LOGIN_ATTEMPTS = 5
_MAX_PENDING_AUTHORIZATIONS = 256
_MAX_PENDING_AUTHORIZATIONS_PER_CLIENT = 5


class OwnerOAuthProvider:
    """OAuth authorization-code provider requiring owner sign-in and consent."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        database_path: Path,
    ) -> None:
        if not username.strip():
            raise ValueError("MCP auth username must not be empty")
        if len(password) < 16:
            raise ValueError("MCP auth password must be at least 16 characters")

        self._username = username
        self._password = password
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        database_path.parent.chmod(0o700)
        self._database = sqlite3.connect(database_path, timeout=5, check_same_thread=False)
        self._database.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_codes (
                code_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                token_hash TEXT PRIMARY KEY,
                paired_token_hash TEXT NOT NULL,
                token_type TEXT NOT NULL,
                client_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                data_json TEXT NOT NULL
            );
            """
        )
        database_path.chmod(0o600)
        self._revoke_tokens_if_owner_credentials_changed()
        self._pending_authorizations: dict[str, _PendingAuthorization] = {}

    def close(self) -> None:
        """Close the persistent OAuth state database."""
        self._database.close()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._database.execute(
            "SELECT client_json FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row[0])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "OAuth client ID is missing")
        try:
            with self._database:
                self._database.execute(
                    "INSERT INTO oauth_clients (client_id, client_json) VALUES (?, ?)",
                    (client_info.client_id, client_info.model_dump_json()),
                )
        except sqlite3.IntegrityError as exc:
            raise RegistrationError(
                "invalid_client_metadata", "The OAuth client ID is already registered"
            ) from exc

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if not client.client_id:
            raise AuthorizeError("invalid_request", "OAuth client ID is missing")
        self._remove_expired_pending()
        client_requests = [
            request_id
            for request_id, pending in self._pending_authorizations.items()
            if pending.client_id == client.client_id
        ]
        if len(client_requests) >= _MAX_PENDING_AUTHORIZATIONS_PER_CLIENT:
            self._pending_authorizations.pop(client_requests[0])
        if len(self._pending_authorizations) >= _MAX_PENDING_AUTHORIZATIONS:
            oldest_request_id = next(iter(self._pending_authorizations))
            self._pending_authorizations.pop(oldest_request_id)

        request_id = secrets.token_urlsafe(32)
        self._pending_authorizations[request_id] = _PendingAuthorization(
            client_id=client.client_id,
            client_name=client.client_name or client.client_id,
            params=params,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=time.time() + _CODE_TTL_SECONDS,
        )
        return f"/auth/authorize?{urlencode({'request_id': request_id})}"

    async def handle_authorization_page(self, request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request_id", "")
            pending = self._get_pending(request_id)
            if pending is None:
                return self._page(
                    "Authorization request expired. Start again from your MCP client.",
                    400,
                )
            return self._login_page(request_id, pending)

        form = await request.form()
        request_id = _form_value(form, "request_id")
        csrf_token = _form_value(form, "csrf_token")
        action = _form_value(form, "action")
        pending = self._get_pending(request_id)
        if (
            pending is None
            or not csrf_token
            or not secrets.compare_digest(csrf_token, pending.csrf_token)
        ):
            return self._page("This authorization form is invalid or expired. Start again.", 400)

        if action == "deny":
            self._pending_authorizations.pop(request_id, None)
            return self._authorization_redirect(
                pending.params,
                error="access_denied",
                error_description="The owner denied access",
            )
        if action != "approve":
            return self._login_page(
                request_id, pending, "Choose whether to approve or deny access."
            )

        username = _form_value(form, "username")
        password = _form_value(form, "password")
        valid_user = secrets.compare_digest(username.encode(), self._username.encode())
        valid_password = secrets.compare_digest(password.encode(), self._password.encode())
        if not (valid_user and valid_password):
            logger.warning("MCP owner sign-in failed")
            pending.failed_attempts += 1
            if pending.failed_attempts >= _MAX_LOGIN_ATTEMPTS:
                self._pending_authorizations.pop(request_id, None)
                return self._page(
                    "Too many failed sign-in attempts. Start again from your MCP client.",
                    status_code=429,
                )
            return self._login_page(
                request_id, pending, "The username or password is incorrect.", 401
            )

        self._pending_authorizations.pop(request_id, None)
        self._store_authorization_code(pending)
        code = self._authorization_code_for_request(request_id, pending)
        return self._authorization_redirect(pending.params, code=code)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self._database.execute(
            "SELECT client_id, expires_at, data_json FROM oauth_codes WHERE code_hash = ?",
            (_token_hash(authorization_code),),
        ).fetchone()
        if row is None:
            return None
        client_id, expires_at, data_json = row
        if client_id != client.client_id or expires_at <= time.time():
            if expires_at <= time.time():
                self._database.execute(
                    "DELETE FROM oauth_codes WHERE code_hash = ?",
                    (_token_hash(authorization_code),),
                )
            return None
        data = json.loads(data_json)
        return AuthorizationCode(code=authorization_code, **data)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        code_hash = _token_hash(authorization_code.code)
        with self._database:
            row = self._database.execute(
                "SELECT client_id, expires_at, data_json FROM oauth_codes WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            if (
                row is None
                or row[0] != client.client_id
                or row[1] <= time.time()
                or authorization_code.client_id != client.client_id
            ):
                raise TokenError("invalid_grant", "Authorization code is invalid or expired")
            if not client.client_id:
                raise TokenError("invalid_client", "OAuth client ID is missing")
            stored_code = AuthorizationCode(
                code=authorization_code.code,
                **json.loads(row[2]),
            )
            self._database.execute("DELETE FROM oauth_codes WHERE code_hash = ?", (code_hash,))

        return self._issue_token_pair(
            client_id=client.client_id,
            scopes=stored_code.scopes,
            resource=stored_code.resource,
            subject=stored_code.subject,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        token_hash = _token_hash(token)
        row = self._database.execute(
            "SELECT expires_at, data_json FROM oauth_tokens "
            "WHERE token_hash = ? AND token_type = 'access'",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        expires_at, data_json = row
        if expires_at <= time.time():
            with self._database:
                self._database.execute(
                    "DELETE FROM oauth_tokens WHERE token_hash = ?", (token_hash,)
                )
            return None
        return AccessToken(token=token, **json.loads(data_json))

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token_hash = _token_hash(refresh_token)
        row = self._database.execute(
            "SELECT paired_token_hash, client_id, expires_at, data_json FROM oauth_tokens "
            "WHERE token_hash = ? AND token_type = 'refresh'",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        paired_hash, client_id, expires_at, data_json = row
        if expires_at <= time.time():
            self._delete_token_pair(token_hash, paired_hash)
            return None
        if client_id != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **json.loads(data_json))

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        token_hash = _token_hash(refresh_token.token)
        with self._database:
            row = self._database.execute(
                "SELECT paired_token_hash, client_id, expires_at FROM oauth_tokens "
                "WHERE token_hash = ? AND token_type = 'refresh'",
                (token_hash,),
            ).fetchone()
            if (
                row is None
                or row[1] != client.client_id
                or row[1] != refresh_token.client_id
                or row[2] <= time.time()
            ):
                raise TokenError("invalid_grant", "Refresh token is invalid or expired")
            if not client.client_id:
                raise TokenError("invalid_client", "OAuth client ID is missing")
            if not set(scopes).issubset(refresh_token.scopes):
                raise TokenError("invalid_scope", "Refresh token cannot grant additional scopes")
            self._delete_token_pair(token_hash, row[0])

        return self._issue_token_pair(
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            resource=refresh_token.resource,
            subject=refresh_token.subject,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        token_hash = _token_hash(token.token)
        row = self._database.execute(
            "SELECT paired_token_hash FROM oauth_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is not None:
            self._delete_token_pair(token_hash, row[0])

    async def exchange_identity_assertion(
        self,
        client: OAuthClientInformationFull,
        params: IdentityAssertionParams,
    ) -> OAuthToken:
        del client, params
        raise TokenError(
            "unsupported_grant_type",
            "The JWT bearer grant is not supported by this authorization server",
        )

    def _store_authorization_code(self, pending: _PendingAuthorization) -> None:
        code = secrets.token_urlsafe(32)
        data = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + _CODE_TTL_SECONDS,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=self._username,
        )
        with self._database:
            self._database.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, expires_at, data_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    _token_hash(code),
                    data.client_id,
                    data.expires_at,
                    json.dumps(data.model_dump(mode="json", exclude={"code"})),
                ),
            )
        pending.authorization_code = code

    def _revoke_tokens_if_owner_credentials_changed(self) -> None:
        row = self._database.execute(
            "SELECT value FROM oauth_metadata WHERE key = 'owner_credential_verifier'"
        ).fetchone()
        if row is None:
            salt = secrets.token_bytes(16)
            self._database.execute(
                "INSERT INTO oauth_metadata (key, value) VALUES ('owner_credential_salt', ?)",
                (base64.b64encode(salt).decode("ascii"),),
            )
        else:
            salt_row = self._database.execute(
                "SELECT value FROM oauth_metadata WHERE key = 'owner_credential_salt'"
            ).fetchone()
            if salt_row is None:
                raise RuntimeError("MCP OAuth database is missing its credential salt")
            salt = base64.b64decode(salt_row[0], validate=True)
        verifier = hashlib.scrypt(
            json.dumps(
                [self._username, self._password],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode(),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
        stored_verifier = base64.b64encode(verifier).decode("ascii")
        if row is not None and not secrets.compare_digest(row[0], stored_verifier):
            with self._database:
                self._database.execute("DELETE FROM oauth_tokens")
                self._database.execute("DELETE FROM oauth_codes")
            logger.info("MCP owner credentials changed; revoked existing OAuth tokens")
        with self._database:
            self._database.execute(
                "INSERT OR REPLACE INTO oauth_metadata (key, value) "
                "VALUES ('owner_credential_verifier', ?)",
                (stored_verifier,),
            )

    def _authorization_code_for_request(
        self, request_id: str, pending: _PendingAuthorization
    ) -> str:
        code = pending.authorization_code
        if not code:
            raise RuntimeError(f"No authorization code stored for request {request_id}")
        return code

    def _issue_token_pair(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        subject: str | None,
    ) -> OAuthToken:
        now = int(time.time())
        access_value = secrets.token_urlsafe(32)
        refresh_value = secrets.token_urlsafe(32)
        access = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + _ACCESS_TOKEN_TTL_SECONDS,
            resource=resource,
            subject=subject,
        )
        refresh = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + _REFRESH_TOKEN_TTL_SECONDS,
            resource=resource,
            subject=subject,
        )
        access_hash = _token_hash(access_value)
        refresh_hash = _token_hash(refresh_value)
        with self._database:
            self._database.executemany(
                "INSERT INTO oauth_tokens "
                "(token_hash, paired_token_hash, token_type, client_id, expires_at, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        access_hash,
                        refresh_hash,
                        "access",
                        client_id,
                        access.expires_at,
                        json.dumps(access.model_dump(mode="json", exclude={"token"})),
                    ),
                    (
                        refresh_hash,
                        access_hash,
                        "refresh",
                        client_id,
                        refresh.expires_at,
                        json.dumps(refresh.model_dump(mode="json", exclude={"token"})),
                    ),
                ],
            )
        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh_value,
        )

    def _delete_token_pair(self, token_hash: str, paired_hash: str) -> None:
        with self._database:
            self._database.execute(
                "DELETE FROM oauth_tokens WHERE token_hash IN (?, ?)",
                (token_hash, paired_hash),
            )

    def _get_pending(self, request_id: str) -> _PendingAuthorization | None:
        pending = self._pending_authorizations.get(request_id)
        if pending is not None and pending.expires_at <= time.time():
            self._pending_authorizations.pop(request_id, None)
            return None
        return pending

    def _remove_expired_pending(self) -> None:
        now = time.time()
        expired = [
            request_id
            for request_id, pending in self._pending_authorizations.items()
            if pending.expires_at <= now
        ]
        for request_id in expired:
            self._pending_authorizations.pop(request_id, None)

    def _login_page(
        self,
        request_id: str,
        pending: _PendingAuthorization,
        message: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        client_name = html.escape(pending.client_name)
        redirect_host = html.escape(
            urlparse(str(pending.params.redirect_uri)).hostname or "unknown"
        )
        escaped_message = f"<p role='alert'>{html.escape(message)}</p>" if message else ""
        body = f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize tescmd</title>
<body>
<main>
  <h1>Authorize MCP access</h1>
  <p><strong>{client_name}</strong> is requesting access to your Tesla account through tescmd.
     The client name is self-reported.</p>
  <p>After approval, access is sent to <strong>{redirect_host}</strong>.
     Approve only if you recognize this destination.</p>
  <p>Approving allows this client to use the vehicle tools available on this server.</p>
  {escaped_message}
  <form method="post" action="/auth/authorize" autocomplete="on">
    <input type="hidden" name="request_id" value="{html.escape(request_id, quote=True)}">
    <input type="hidden" name="csrf_token" value="{html.escape(pending.csrf_token, quote=True)}">
    <label>Owner username <input name="username" autocomplete="username"></label>
    <label>Owner password
      <input type="password" name="password" autocomplete="current-password">
    </label>
    <button type="submit" name="action" value="approve">Sign in and approve</button>
    <button type="submit" name="action" value="deny" formnovalidate>Deny</button>
  </form>
</main>
</body>
</html>"""
        return self._page(body, status_code)

    @staticmethod
    def _page(body: str, status_code: int = 200) -> HTMLResponse:
        response_body = body
        if not body.startswith("<!doctype"):
            response_body = (
                "<!doctype html><html lang='en'><meta charset='utf-8'><body>"
                f"<p>{html.escape(body)}</p></body></html>"
            )
        return HTMLResponse(
            response_body,
            status_code=status_code,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    @staticmethod
    def _authorization_redirect(
        params: AuthorizationParams,
        *,
        code: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> RedirectResponse:
        target = construct_redirect_uri(
            str(params.redirect_uri),
            code=code,
            error=error,
            error_description=error_description,
            state=params.state,
        )
        return RedirectResponse(
            target,
            status_code=302,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )


class _PendingAuthorization:
    def __init__(
        self,
        *,
        client_id: str,
        client_name: str,
        params: AuthorizationParams,
        csrf_token: str,
        expires_at: float,
    ) -> None:
        self.client_id = client_id
        self.client_name = client_name
        self.params = params
        self.csrf_token = csrf_token
        self.expires_at = expires_at
        self.failed_attempts = 0
        self.authorization_code: str | None = None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _form_value(form: Mapping[str, Any], key: str) -> str:
    value = form.get(key, "")
    return value if isinstance(value, str) else ""
