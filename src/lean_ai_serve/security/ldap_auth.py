"""LDAP/Active Directory authentication with connection pooling and caching."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time

from lean_ai_serve.config import LDAPConfig
from lean_ai_serve.models.schemas import AuthUser

logger = logging.getLogger(__name__)

try:
    import ldap3
    from ldap3 import ALL, SUBTREE, Connection, Server
    from ldap3.core.exceptions import LDAPBindError, LDAPException
except ImportError:
    ldap3 = None  # type: ignore[assignment]


class LDAPService:
    """LDAP authentication service with connection pooling and result caching.

    All ldap3 operations are wrapped in asyncio.to_thread() since ldap3 is synchronous.
    """

    def __init__(self, config: LDAPConfig):
        if ldap3 is None:
            raise ImportError(
                "ldap3 is required for LDAP authentication. "
                "Install it with: pip install lean-ai-serve[ldap]"
            )

        self._config = config
        self._server = Server(config.server_url, get_info=ALL)

        # Read bind password from environment
        self._bind_password = os.environ.get(config.bind_password_env, "")
        if not self._bind_password:
            raise ValueError(
                f"LDAP bind password not found in environment variable '{config.bind_password_env}'"
            )

        # Connection pool (asyncio.Queue for async borrow/return)
        self._pool: asyncio.Queue[Connection] = asyncio.Queue(
            maxsize=config.connection_pool_size
        )

        # Auth result cache: (username, password_hash) -> (AuthUser, timestamp)
        self._cache: dict[str, tuple[AuthUser, float]] = {}

    async def initialize(self) -> None:
        """Create pooled connections (service account bind)."""
        for _ in range(self._config.connection_pool_size):
            conn = await asyncio.to_thread(self._create_service_connection)
            await self._pool.put(conn)
        logger.info(
            "LDAP connection pool initialized (%d connections to %s)",
            self._config.connection_pool_size,
            self._config.server_url,
        )

    def _create_service_connection(self) -> Connection:
        """Create a new service account connection."""
        conn = Connection(
            self._server,
            user=self._config.bind_dn,
            password=self._bind_password,
            auto_bind=True,
            read_only=True,
        )
        return conn

    async def authenticate(self, username: str, password: str) -> AuthUser | None:
        """Authenticate a user via LDAP bind and resolve roles from group membership.

        Returns AuthUser on success, None on failure.
        """
        # Check cache
        cache_key = self._make_cache_key(username, password)
        cached = self._cache.get(cache_key)
        if cached is not None:
            user, ts = cached
            if time.monotonic() - ts < self._config.cache_ttl:
                logger.debug("LDAP cache hit for user '%s'", username)
                return user
            del self._cache[cache_key]

        # Search for user DN using service account
        conn = await self._borrow_connection()
        try:
            user_dn = await self._find_user_dn(conn, username)
            if user_dn is None:
                logger.info("LDAP user not found: %s", username)
                return None

            # Verify password with user bind
            if not await self._verify_password(user_dn, password):
                logger.info("LDAP bind failed for user: %s", username)
                return None

            # Resolve group memberships
            groups = await self._get_user_groups(conn, user_dn)
            roles = self._map_groups_to_roles(groups)
            display_name = await self._get_display_name(conn, user_dn)

            auth_user = AuthUser(
                user_id=username,
                display_name=display_name or username,
                roles=roles,
                allowed_models=["*"],
                auth_method="ldap",
            )

            # Cache the result
            self._cache[cache_key] = (auth_user, time.monotonic())
            logger.info("LDAP auth success: %s (roles=%s)", username, roles)
            return auth_user

        finally:
            await self._return_connection(conn)

    async def close(self) -> None:
        """Unbind all pooled connections."""
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                await asyncio.to_thread(conn.unbind)
            except Exception:
                pass
        self._cache.clear()
        logger.info("LDAP connection pool closed")

    # --- Internal helpers ---

    @staticmethod
    def _make_cache_key(username: str, password: str) -> str:
        """Create a cache key from username + password hash."""
        pw_hash = hashlib.sha256(password.encode()).hexdigest()
        return f"{username}:{pw_hash}"

    async def _borrow_connection(self) -> Connection:
        """Get a connection from the pool, rebinding if needed."""
        conn = await self._pool.get()
        # Check if connection is still bound
        if not conn.bound:
            try:
                await asyncio.to_thread(conn.rebind)
            except LDAPException:
                conn = await asyncio.to_thread(self._create_service_connection)
        return conn

    async def _return_connection(self, conn: Connection) -> None:
        """Return a connection to the pool."""
        await self._pool.put(conn)

    async def _find_user_dn(self, conn: Connection, username: str) -> str | None:
        """Search for a user's DN by username."""
        search_filter = self._config.user_search_filter.replace("{username}", username)

        def _search():
            conn.search(
                search_base=self._config.user_search_base,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=["distinguishedName", "cn"],
            )
            return conn.entries

        entries = await asyncio.to_thread(_search)
        if entries:
            return str(entries[0].entry_dn)
        return None

    async def _verify_password(self, user_dn: str, password: str) -> bool:
        """Attempt a direct bind with user credentials."""

        def _bind():
            try:
                test_conn = Connection(
                    self._server, user=user_dn, password=password, auto_bind=True
                )
                test_conn.unbind()
                return True
            except LDAPBindError:
                return False

        return await asyncio.to_thread(_bind)

    async def _get_user_groups(self, conn: Connection, user_dn: str) -> list[str]:
        """Get group DNs for a user."""
        search_filter = f"(member={user_dn})"

        def _search():
            conn.search(
                search_base=self._config.group_search_base,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=["distinguishedName"],
            )
            return [str(e.entry_dn) for e in conn.entries]

        return await asyncio.to_thread(_search)

    async def _get_display_name(self, conn: Connection, user_dn: str) -> str | None:
        """Get the display name (CN) for a user."""

        def _search():
            conn.search(
                search_base=user_dn,
                search_filter="(objectClass=*)",
                attributes=["cn", "displayName"],
            )
            if conn.entries:
                entry = conn.entries[0]
                if hasattr(entry, "displayName") and entry.displayName.value:
                    return str(entry.displayName.value)
                if hasattr(entry, "cn") and entry.cn.value:
                    return str(entry.cn.value)
            return None

        return await asyncio.to_thread(_search)

    def _map_groups_to_roles(self, group_dns: list[str]) -> list[str]:
        """Map LDAP group DNs to lean-ai-serve roles."""
        roles = set()
        mapping = self._config.group_role_mapping

        for dn in group_dns:
            # Case-insensitive comparison for AD DNs
            dn_lower = dn.lower()
            for group_dn, role in mapping.items():
                if group_dn.lower() == dn_lower:
                    roles.add(role)

        if not roles:
            roles.add(self._config.default_role)

        return sorted(roles)
