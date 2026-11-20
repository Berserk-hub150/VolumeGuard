"""Read only Compose storage declarations. No Docker daemon or environment access."""
from dataclasses import dataclass
from pathlib import Path
import json
import posixpath
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
import yaml
from yaml.nodes import MappingNode, SequenceNode

LIMIT = 1024 * 1024
STANDARD_TAGS = {'tag:yaml.org,2002:' + name for name in
                 ('str', 'int', 'float', 'bool', 'null', 'seq', 'map')}
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")


class InputError(ValueError):
    pass


def read_yaml(data):
    if len(data) > LIMIT:
        raise InputError("Compose input exceeds 1 MiB.")
    loader = None
    try:
        loader = yaml.SafeLoader(data)
        root = loader.get_single_node()
        budget = 0

        def visit(node, depth):
            nonlocal budget
            budget += 1
            if depth > 64 or budget > 40000:
                raise InputError("YAML nesting or expanded alias limit exceeded.")
            if node.tag not in STANDARD_TAGS:
                raise InputError("Unsupported YAML tag. Supply canonical Compose JSON.")
            if isinstance(node, MappingNode):
                seen = set()
                for key, value in node.value:
                    if key.tag != 'tag:yaml.org,2002:str' or key.value in seen:
                        raise InputError("Duplicate, non-string or merge key. Supply canonical Compose JSON.")
                    seen.add(key.value)
                    visit(value, depth + 1)
            elif isinstance(node, SequenceNode):
                for value in node.value:
                    visit(value, depth + 1)

        if root is None:
            raise InputError("Empty Compose file.")
        visit(root, 0)
        return loader.construct_document(root)
    except (yaml.YAMLError, RecursionError, UnicodeError, OverflowError) as exc:
        raise InputError("Invalid or unsupported YAML. Supply canonical Compose JSON.") from exc
    finally:
        if loader is not None:
            loader.dispose()


def mapping(value):
    if not isinstance(value, dict):
        raise InputError("Expected a Compose mapping.")
    return value


def literal(value):
    if not isinstance(value, str) or not value or '$' in value or any(not c.isprintable() for c in value):
        raise InputError("Storage contains an unresolved variable or unsupported value. Supply canonical JSON.")
    return value


def identifier(value):
    if not IDENTIFIER.fullmatch(literal(value)):
        raise InputError("Unsupported resource name. Supply canonical Compose JSON.")
    return value


@dataclass(frozen=True)
class Mount:
    kind: str
    source: str
    subpath: str = ""
    options: str = ""

    def describe(self):
        return f"{self.kind} {self.source}" + (f" (subpath {self.subpath})" if self.subpath else "")


def named_volumes(config, project):
    result = {}
    for key, spec in mapping(config.get('volumes', {})).items():
        identifier(key)
        spec = {} if spec is None else mapping(spec)
        external = spec.get('external', False)
        if not isinstance(external, bool):
            raise InputError("Legacy external volume declaration. Supply canonical Compose JSON.")
        name = identifier(spec.get('name', key if external else f'{project}_{key}'))
        opts = mapping(spec.get('driver_opts', {}))
        # Driver options can contain credentials. Compare internally, never print them.
        if any(not isinstance(v, (str, int, float, bool)) or '$' in str(v) for v in opts.values()):
            raise InputError("Unresolved volume driver options.")
        driver = literal(spec.get('driver', 'local'))
        signature = json.dumps([driver, opts], sort_keys=True)
        result[key] = (name, signature)
    return result


def parse_mount(item, volumes, directory):
    if isinstance(item, str):
        parts = literal(item).split(':')
        if len(parts) > 3 or any(not p for p in parts):
            raise InputError("Ambiguous short mount. Use Compose long syntax.")
        source, target = (None, parts[0]) if len(parts) == 1 else parts[:2]
        kind = 'bind' if source and source.startswith(('.', '/', '~')) else 'volume'
        spec = {}
    else:
        spec = mapping(item)
        source = spec.get('source')
        target = spec.get('target')
        kind = spec.get('type')
    target = literal(target)
    if not target.startswith('/'):
        raise InputError("Only absolute Linux container targets are supported.")
    target = posixpath.normpath(target)
    if kind == 'bind':
        source = literal(source)
        if source.startswith('~') or '\\' in source or re.match(r'^[A-Za-z]:', source):
            raise InputError("Home-relative and Windows host binds require deployment-specific review.")
        source = posixpath.normpath(posixpath.join(directory, source))
        return target, Mount(kind, source)
    if kind == 'volume':
        if source is None:
            return target, Mount('anonymous volume', '(runtime assigned)')
        source = identifier(source)
        if source not in volumes:
            raise InputError("Service references an undeclared named volume.")
        options = mapping(spec.get('volume', {}))
        subpath = literal(options['subpath']) if 'subpath' in options else ''
        name, signature = volumes[source]
        return target, Mount(kind, name, subpath, signature)
    if kind == 'tmpfs':
        return target, Mount(kind, '(memory)')
    raise InputError("Unsupported mount type. Inspect the deployment manually.")


def snapshot(data, directory, fallback_project, override=None):
    if data is None:  # A file absent from one revision, unlike an empty/broken file.
        return {}
    config = mapping(read_yaml(data))
    if 'include' in config or 'models' in config:
        raise InputError("Includes or models require a resolved deployment configuration.")
    project = literal(override or config.get('name', fallback_project))
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', project):
        raise InputError("Invalid or unknown project name. Pass --project-name explicitly.")
    volumes = named_volumes(config, project)
    result = {}
    for svc, spec in mapping(config.get('services')).items():
        identifier(svc)
        spec = mapping(spec)
        if 'extends' in spec or 'volumes_from' in spec:
            raise InputError("Inherited mounts require review. Resolve extends before comparison.")
        mounts = {}
        items = spec.get('volumes', [])
        if not isinstance(items, list):
            raise InputError("Expected a service volumes list.")
        for item in items:
            target, mount = parse_mount(item, volumes, directory)
            if target in mounts:
                raise InputError("Duplicate container mount target.")
            mounts[target] = mount
        result[svc] = mounts
    return result


def compare(before, after):
    findings = []
    for service, mounts in sorted(before.items()):
        for target, old in sorted(mounts.items()):
            if old.kind == 'tmpfs':
                continue
            new = after.get(service, {}).get(target)
            if new == old:
                continue
            if new is None:
                reason = 'service removed or renamed' if service not in after else 'mount removed or moved'
            elif (old.kind, old.source, old.subpath) == (new.kind, new.source, new.subpath):
                reason = 'volume driver or options changed'
            else:
                reason = 'storage source changed'
            findings.append(dict(service=service, target=target, reason=reason,
                                 before=old.describe(), after=new.describe() if new else None))
    return findings
