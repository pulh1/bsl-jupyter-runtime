"""Strict configuration-only payloads for the Jupyter project bridge."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from uuid import UUID


MAX_BRIDGE_EPOCH = 2**53 - 1
PROJECT_REASONS = frozenset((
    'runtime-unavailable', 'project-config-invalid-or-limited',
))


@dataclass(frozen=True)
class ProjectConfig:
    installation_id: str
    source_root: str | None


@dataclass(frozen=True)
class ProjectLimits:
    max_message_bytes: int = 16384
    max_comms: int = 16
    max_path_chars: int = 8192

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in asdict(self).values()
        ):
            raise ValueError('invalid-project-limits')


def _validate(
    config: ProjectConfig,
    limits: ProjectLimits,
) -> dict[str, object]:
    if not isinstance(config, ProjectConfig):
        raise ValueError('invalid-project-config')
    installation_id = config.installation_id
    try:
        if (
            type(installation_id) is not str
            or len(installation_id) != 32
            or UUID(installation_id).hex != installation_id
        ):
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError('invalid-project-config') from None
    root = config.source_root
    if root is not None:
        if (
            type(root) is not str
            or not root
            or any(ord(character) < 32 or ord(character) == 127 for character in root)
            or not Path(root).is_absolute()
        ):
            raise ValueError('invalid-project-config')
        if len(root) > limits.max_path_chars:
            raise ValueError('project-path-limit')
    encoded = {'installation_id': installation_id, 'source_root': root}
    size = len(
        json.dumps(encoded, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    )
    if size > limits.max_message_bytes:
        raise ValueError('project-message-limit')
    return encoded


def encode_config(
    config: ProjectConfig,
    limits: ProjectLimits = ProjectLimits(),
) -> dict[str, object]:
    return _validate(config, limits)


def decode_config(
    data: object,
    limits: ProjectLimits = ProjectLimits(),
) -> ProjectConfig:
    if type(data) is not dict or set(data) != {'installation_id', 'source_root'}:
        raise ValueError('invalid-project-config')
    try:
        config = ProjectConfig(data['installation_id'], data['source_root'])
    except (KeyError, TypeError):
        raise ValueError('invalid-project-config') from None
    _validate(config, limits)
    return config


def _check_message_size(data: dict[str, object], limits: ProjectLimits) -> None:
    try:
        size = len(json.dumps(
            data, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8'))
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise ValueError('invalid-project-envelope') from None
    if size > limits.max_message_bytes:
        raise ValueError('project-message-limit')


def encode_envelope(
    bridge_epoch: int,
    config: ProjectConfig | None,
    reason: str | None,
    limits: ProjectLimits = ProjectLimits(),
) -> dict[str, object]:
    if (
        type(bridge_epoch) is not int
        or not 0 <= bridge_epoch <= MAX_BRIDGE_EPOCH
        or (
            config is None
            and (type(reason) is not str or reason not in PROJECT_REASONS)
        )
        or (config is not None and reason is not None)
    ):
        raise ValueError('invalid-project-envelope')
    encoded = None if config is None else encode_config(config, limits)
    envelope = {
        'version': 2,
        'bridge_epoch': bridge_epoch,
        'config': encoded,
        'reason': reason,
    }
    _check_message_size(envelope, limits)
    return envelope


def decode_envelope(
    data: object,
    limits: ProjectLimits = ProjectLimits(),
) -> tuple[int, ProjectConfig | None, str | None]:
    if (
        type(data) is not dict
        or set(data) != {'version', 'bridge_epoch', 'config', 'reason'}
        or type(data['version']) is not int
        or data['version'] != 2
        or type(data['bridge_epoch']) is not int
        or not 0 <= data['bridge_epoch'] <= MAX_BRIDGE_EPOCH
    ):
        raise ValueError('invalid-project-envelope')
    reason = data['reason']
    if data['config'] is None:
        if type(reason) is not str or reason not in PROJECT_REASONS:
            raise ValueError('invalid-project-envelope')
        config = None
    else:
        if reason is not None:
            raise ValueError('invalid-project-envelope')
        config = decode_config(data['config'], limits)
    _check_message_size(data, limits)
    return data['bridge_epoch'], config, reason
