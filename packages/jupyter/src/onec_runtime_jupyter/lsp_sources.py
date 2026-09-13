"""Bounded current-file reads under authenticated project context authority."""
from datetime import datetime, timezone
import os
from pathlib import Path
from re import fullmatch
import stat
from urllib.parse import unquote

from .lsp_child import context_token
from .lsp_workspace import linked, root_identity

DEFAULT_MAX_SOURCE_BYTES = 4 * 1024 * 1024


class SourceStore:
    def __init__(self, registry, *, max_bytes=DEFAULT_MAX_SOURCE_BYTES):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('source-size-limit-invalid')
        self.registry, self.max_bytes = registry, max_bytes

    def _resolve(self, path, owner):
        if type(path) is not str or len(path) > 8192:
            raise ValueError('source-unavailable')
        parts = unquote(path, errors='strict').split('/')
        if (len(parts) < 3 or not fullmatch('[a-f0-9]{32}', parts[0])
                or not fullmatch('[a-f0-9]{64}', parts[1])
                or any(p in ('', '.', '..') or p.endswith(('.', ' '))
                       or any(c in p for c in '\\:%?#')
                       or any(ord(c) < 32 for c in p) for p in parts[2:])):
            raise ValueError('source-unavailable')
        context = self.registry.context(parts[0], owner=owner)
        if context_token(context) != parts[1] or not context.get('source_root'):
            raise ValueError('source-unavailable')
        root = Path(context['source_root'])
        source = root.joinpath(*parts[2:])
        if source.suffix.lower() not in ('.bsl', '.os'):
            raise ValueError('source-unavailable')
        root_identity(root)
        identities = []
        for candidate in (source, *source.parents):
            info = candidate.lstat()
            if linked(info):
                raise ValueError('source-unavailable')
            identities.append((info.st_dev, info.st_ino))
        source.relative_to(root)
        if not stat.S_ISREG(source.lstat().st_mode):
            raise ValueError('source-unavailable')
        return source, tuple(identities)

    def read(self, path, *, owner, content=True):
        try:
            source, identities = self._resolve(path, owner)
            # Check the opened descriptor against the admitted lexical path
            # before reading any bytes; repeat admission before returning.
            with source.open('rb') as stream:
                info = os.fstat(stream.fileno())
                if (info.st_dev, info.st_ino) != identities[0] or info.st_size > self.max_bytes:
                    raise ValueError('source-unavailable')
                if self._resolve(path, owner)[1] != identities:
                    raise ValueError('source-unavailable')
                raw = stream.read(self.max_bytes + 1) if content else None
                if raw is not None and len(raw) > self.max_bytes:
                    raise ValueError('source-unavailable')
            if self._resolve(path, owner)[1] != identities:
                raise ValueError('source-unavailable')
            return {'name': source.name, 'path': path, 'type': 'file', 'writable': False,
                    'size': info.st_size,
                    'created': datetime.fromtimestamp(info.st_ctime, timezone.utc).isoformat(),
                    'last_modified': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                    'mimetype': 'text/x-bsl', 'format': 'text' if content else None,
                    'content': raw.decode('utf-8-sig') if raw is not None else None}
        except (OSError, UnicodeError):
            raise ValueError('source-unavailable') from None

    def close(self):
        pass  # No historical bytes, pins or open file handles are retained.
