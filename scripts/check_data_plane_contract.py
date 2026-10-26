#!/usr/bin/env python3
"""Structural production boundary checks; complexity remains a PR review duty."""
from __future__ import annotations

import argparse
import ast
import configparser
import json
import re
import sys
from pathlib import Path, PurePosixPath


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError(f'invalid repository path: {value!r}')
    p = PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or str(p) != value or any(c in value for c in '*?['):
        raise ValueError(f'invalid repository path: {value!r}')
    return value


def check(root):
    errors = []
    manifest = json.loads((root / 'config/data_plane_contract.json').read_text())
    if manifest['version'] != 1:
        raise ValueError('unsupported data-plane registry version')
    exempt = set()
    for kind in ('native_exceptions', 'existing_python_debt'):
        for entry in manifest[kind]:
            for key in ('reason', 'owner', 'retirement'):
                if not isinstance(entry[key], str) or not entry[key].strip():
                    raise ValueError(f'{kind}: missing {key}')
            if not re.fullmatch(r'[0-9a-f]{40}', entry['source_revision']):
                raise ValueError(f'{kind}: source_revision must be an exact Git SHA')
            if not isinstance(entry['scope'], list) or not entry['scope']:
                raise ValueError(f'{kind}: scope must name concrete paths')
            for item in entry['scope']:
                relative(item)
                if kind == 'native_exceptions':
                    if item in exempt:
                        errors.append(f'duplicate native exception: {item}')
                    exempt.add(item)
                    if not (root / item).is_file():
                        errors.append(f'remove stale native exception: {item}')
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    project = (root / 'pyproject.toml').read_text()
    section = re.search(r'^\[project\.scripts\]\s*\n(.*?)(?=^\[|\Z)', project, re.M | re.S)
    if section is None:
        raise ValueError('missing project.scripts table')
    parser.read_string('[project.scripts]\n' + section.group(1))
    actual = {key: ast.literal_eval(value) for key, value in parser['project.scripts'].items()}
    if actual != manifest['entrypoints']:
        errors.append('project.scripts differs from reviewed entrypoint registry')
    for name, target in manifest['entrypoints'].items():
        module, symbol = target.split(':')
        path = root / 'src' / (module.replace('.', '/') + '.py')
        if not path.is_file():
            errors.append(f'entrypoint {name}: missing {module}')
        elif not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == symbol
                     for n in ast.parse(path.read_text()).body):
            errors.append(f'entrypoint {name}: missing function {symbol}')
    core = set()
    source_manifest = root / relative(manifest['native_manifest'])
    if source_manifest.exists():
        for line in source_manifest.read_text().splitlines():
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            path = 'src/native/' + relative(line)
            if path in core:
                errors.append(f'duplicate shared native source: {path}')
            core.add(path)
            if not (root / path).is_file():
                errors.append(f'missing shared native source: {path}')
        if not core:
            errors.append('shared native source manifest is empty')
    for path in sorted(core & exempt):
        errors.append(f'core-owned source still exempted as standalone: {path}')
    for path in (root / 'src/native').rglob('*.cpp'):
        name = path.relative_to(root).as_posix()
        if name not in core | exempt:
            errors.append(f'unowned native source: {name}; register in shared core or review a scoped tool exception')
    retired_modules = set()
    seen = set()
    for entry in manifest['retired_paths']:
        name = relative(entry['path'])
        replacement = relative(entry['replacement'])
        if not entry['reason'].strip() or not re.fullmatch(r'[0-9a-f]{40}', entry['source_revision']):
            raise ValueError(f'incomplete retirement record: {name}')
        if name in seen:
            errors.append(f'duplicate retirement: {name}')
        seen.add(name)
        if (root / name).exists():
            errors.append(f'retired path restored: {name}')
        if not (root / replacement).is_file():
            errors.append(f'retirement replacement missing: {replacement}')
        if name.startswith('src/') and name.endswith('.py'):
            retired_modules.add(name[4:-3].replace('/', '.'))
    forbidden = {'tests'} | retired_modules
    def inspect_import(path, node, module):
        if any(module == name or module.startswith(name + '.') for name in forbidden):
            errors.append(f'{path}:{node.lineno}: production dependency on {module}')
    for path in (root / 'src/emuflow').rglob('*.py'):
        name = path.relative_to(root).as_posix()
        package = path.relative_to(root / 'src').with_suffix('').parts
        package = package[:-1]
        for node in ast.walk(ast.parse(path.read_text(), filename=name)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    inspect_import(name, node, alias.name)
            elif isinstance(node, ast.ImportFrom):
                prefix = list(package[:len(package) - node.level + 1]) if node.level else []
                module = '.'.join(prefix + ([node.module] if node.module else []))
                inspect_import(name, node, module)
                for alias in node.names:
                    inspect_import(name, node, module + '.' + alias.name)
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                fn = node.func
                literal_import = (isinstance(fn, ast.Name) and fn.id == '__import__') or (
                    isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                    and fn.value.id == 'importlib' and fn.attr == 'import_module')
                if literal_import and isinstance(node.args[0].value, str):
                    inspect_import(name, node, node.args[0].value)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, nargs='?', default=Path('.'))
    args = parser.parse_args()
    try:
        errors = check(args.root.resolve())
    except (ValueError, KeyError, TypeError, OSError, SyntaxError, configparser.Error) as error:
        errors = [f'invalid production contract: {error}']
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print('Production structural gates passed; semantic data-plane and NVDLA review remain separate.')
    return bool(errors)


if __name__ == '__main__':
    sys.exit(main())
