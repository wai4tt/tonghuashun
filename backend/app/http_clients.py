"""Reuse verified trust stores across HTTP pools without disabling TLS checks."""
from functools import lru_cache
import os
from pathlib import Path
import ssl
from threading import Lock

import certifi

_context_lock = Lock()


def _version(path):
    try:
        stat = Path(path).stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


@lru_cache(maxsize=4)
def _context(cafile, capath, version):
    # Build from the captured configuration; never mutate a cached context.
    return ssl.create_default_context(cafile=cafile, capath=capath)


def verified_ssl_context():
    """Keep HTTPX's SSL_CERT_FILE/SSL_CERT_DIR precedence and certificate validation."""
    cafile = os.getenv("SSL_CERT_FILE")
    capath = None
    if not cafile:
        capath = os.getenv("SSL_CERT_DIR")
        if not capath:
            cafile = certifi.where()
    path = cafile or capath
    # Certificate rotation or an environment change gets a new verified context.
    with _context_lock:
        return _context(cafile, capath, _version(path))
