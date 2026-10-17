"""Persistent, revocable browser sessions. Only cookie hashes enter the DB."""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets

SESSION_SECONDS = 30 * 24 * 3600


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def create_session(pool, token: str) -> tuple[str, dict]:
    secret = secrets.token_urlsafe(32)
    row = {'csrf_token': secrets.token_urlsafe(32),
           'expires_at': datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)}
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute('DELETE FROM web_sessions WHERE expires_at <= NOW()')
            await conn.execute('''INSERT INTO web_sessions
                (session_hash, token_fingerprint, csrf_token, expires_at)
                VALUES ($1,$2,$3,$4)''', digest(secret), digest(token), row['csrf_token'], row['expires_at'])
    return secret, row


async def load_session(pool, secret: str, token: str) -> dict | None:
    if not secret or len(secret) > 128 or not token:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''SELECT csrf_token, expires_at FROM web_sessions
            WHERE session_hash=$1 AND token_fingerprint=$2 AND expires_at > NOW()''',
            digest(secret), digest(token))
    return dict(row) if row else None


async def delete_session(pool, secret: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM web_sessions WHERE session_hash=$1', digest(secret))
