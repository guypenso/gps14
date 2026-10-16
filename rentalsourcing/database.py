"""Explicit PostgreSQL configuration and bounded connection establishment."""
import logging
import time
from urllib.parse import urlsplit, parse_qsl


def validate_postgres_url(url, *, allow_local=False):
    message = ('DATABASE_URL must be a complete PostgreSQL URL with an explicit remote host, '
               'user and database; use the existing Render database connectionString')
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if (parts.scheme not in {'postgres', 'postgresql'} or not host or
                not parts.username or not parts.path.strip('/') or
                (not allow_local and host.lower().rstrip('.') in {'localhost', 'localhost.localdomain', '127.0.0.1', '::1'}) or
                host.startswith('/') or any(c.isspace() for c in host) or
                (port is not None and port == 0)):
            raise ValueError
        # libpq query parameters can override the authority or select a local socket.
        if any(k.lower() in {'host', 'hostaddr', 'port', 'user', 'dbname', 'service', 'servicefile'}
               for k, _ in parse_qsl(parts.query)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(message) from None
    return url


def connect_postgres(url):
    import psycopg
    validate_postgres_url(url, allow_local=True)
    for attempt in range(4):
        try:
            return psycopg.connect(url, autocommit=True, connect_timeout=10, client_encoding='utf8')
        except psycopg.OperationalError:
            if attempt == 3:
                raise RuntimeError('PostgreSQL unavailable after 4 connection attempts; check Render database readiness and DATABASE_URL') from None
            logging.getLogger(__name__).warning('PostgreSQL connection unavailable; retry %d/3', attempt + 1)
            time.sleep(2 ** attempt)
