#!/usr/bin/env python3
"""Capture Neom build output privately and deliver sanitized ZIP logs to the owner."""
import argparse
import base64
import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile

MAX_DOCUMENT_BYTES = 40 * 1024 * 1024
STAGE_NAME = re.compile(r'^[a-z0-9_-]+$')


def now():
    return datetime.now(timezone.utc).isoformat()


def log_root():
    root = Path(os.environ['NEOM_LOG_DIR'])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def record(event):
    with (log_root() / 'events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'time': now(), **event}) + '\n')


def initialize():
    os.umask(0o077)
    record({'event': 'initialized', 'run_id': os.environ.get('GITHUB_RUN_ID'),
            'attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
            'repository': os.environ.get('GITHUB_REPOSITORY')})
    print('Private build logging initialized.')


def capture(stage, command):
    if not STAGE_NAME.fullmatch(stage) or not command:
        raise ValueError('Invalid private logging stage')
    root = log_root()
    started = time.monotonic()
    record({'event': 'start', 'stage': stage})
    code = 1
    with (root / (stage + '.log')).open('ab', buffering=0) as stage_log, \
            (root / 'full-build.log').open('ab', buffering=0) as full_log:
        header = f'\n===== {stage} started {now()} =====\n'.encode()
        stage_log.write(header)
        full_log.write(header)
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT,
                                       env={**os.environ, 'PYTHONUNBUFFERED': '1', 'NEOM_PRIVATE_CAPTURED': '1'})
            with process.stdout as incoming:
                while chunk := incoming.read(65536):
                    stage_log.write(chunk)
                    full_log.write(chunk)
            code = process.wait()
        except OSError as error:
            message = f'Unable to start command: {type(error).__name__}: {error}\n'.encode()
            stage_log.write(message)
            full_log.write(message)
        footer = f'\n===== {stage} exit={code} elapsed={time.monotonic()-started:.3f}s =====\n'.encode()
        stage_log.write(footer)
        full_log.write(footer)
    record({'event': 'end', 'stage': stage, 'exit_code': code,
            'duration_seconds': round(time.monotonic() - started, 3)})
    if code:
        print(f'::error::Stage {stage} failed. Detailed output is reserved for your Telegram log archive.')
    else:
        print(f'Stage {stage} completed. Detailed output captured privately.')
    return code if 0 <= code <= 255 else 1


def checkout(destination):
    repository = os.environ.get('ENGINE_REPO', '')
    token = os.environ.get('ENGINE_READ_TOKEN', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not token:
        raise ValueError('ENGINE_REPO must be owner/repo and ENGINE_READ_TOKEN must be set')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', str(destination)], check=True)
    subprocess.run(['git', '-C', str(destination), 'remote', 'add', 'origin',
                    'https://github.com/' + repository + '.git'], check=True)
    credentials = base64.b64encode(('x-access-token:' + token).encode()).decode()
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_TRACE': '0',
           'GIT_TRACE_CURL': '0', 'GIT_CURL_VERBOSE': '0',
           'GIT_CONFIG_COUNT': '1',
           'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
           'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + credentials}
    for attempt in range(3):
        result = subprocess.run(['git', '-C', str(destination), '-c', 'protocol.version=2',
                                 'fetch', '--no-tags', '--depth=1', 'origin', 'refs/heads/main'],
                                env=env, timeout=600)
        if result.returncode == 0:
            break
        if attempt == 2:
            return result.returncode
        time.sleep(5)
    subprocess.run(['git', '-C', str(destination), 'checkout', '-B', 'main', 'FETCH_HEAD'], check=True)
    subprocess.run(['git', '-C', str(destination), 'rev-parse', 'HEAD'], check=True)
    return 0


def check_drive(config):
    config = Path(config)
    if not config.is_file():
        raise ValueError('rclone.conf is missing')
    requested = os.environ.get('RCLONE_REMOTE_NAME', '')
    print('Requested remote name:', repr(requested))
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(config.read_text(encoding='utf-8-sig'))
    except configparser.Error as error:
        # Exception strings can include a whole token line; report safe location only.
        print('Invalid rclone configuration:', type(error).__name__,
              'line', getattr(error, 'lineno', 'unknown'))
        return 1
    print('Configuration sections:', json.dumps(parser.sections()))
    if requested != requested.strip() or requested.endswith(':'):
        print('RCLONE_REMOTE_NAME contains whitespace or a trailing colon; use the plain section name.')
        return 1
    if requested not in parser:
        print('RCLONE_REMOTE_NAME does not match any section of the decoded RCLONE_CONFIG_B64.')
        return 1
    selected = parser[requested]
    print('Remote storage type:', selected.get('type', '(missing)'))
    if selected.get('type') == 'drive':
        try:
            token = json.loads(selected.get('token', ''))
        except (ValueError, TypeError):
            print('The drive token field is not valid JSON.')
            return 1
        if not isinstance(token, dict) or not token.get('refresh_token'):
            print('The drive token is missing refresh_token.')
            return 1
        print('OAuth access_token present:', bool(token.get('access_token')))
        print('OAuth refresh_token present:', bool(token.get('refresh_token')))
    command = ['rclone', '--config=' + str(config)]
    result = subprocess.run(command + ['listremotes'], capture_output=True, text=True, timeout=60)
    print('rclone listremotes exit:', result.returncode)
    print(result.stdout, end='')
    print(result.stderr, file=sys.stderr, end='')
    if result.returncode:
        return result.returncode
    if requested + ':' not in result.stdout.splitlines():
        print('rclone did not list the configured remote.')
        return 1
    print('Checking Google Drive authentication and storage quota...')
    # Preserve actual rclone stderr in the private stage log instead of discarding it.
    result = subprocess.run(command + ['about', requested + ':', '--json',
                                     '--contimeout', '15s', '--timeout', '45s', '--retries', '1'],
                            timeout=90)
    return result.returncode


def secret_values(engine):
    values = set()
    for key, value in os.environ.items():
        if value and (key.endswith(('_TOKEN', '_PASSWORD', '_SECRET', '_CONFIG_B64'))
                      or key == 'GITHUB_TOKEN'):
            values.add(value)
            if key in ('ENGINE_READ_TOKEN', 'GITHUB_TOKEN'):
                values.add(base64.b64encode(('x-access-token:' + value).encode()).decode())
            if key.endswith('_CONFIG_B64'):
                try:
                    decoded = base64.b64decode(value).decode()
                    values.add(decoded)
                    for line in decoded.splitlines():
                        key_part, separator, candidate = line.partition('=')
                        if separator and key_part.strip() == 'token':
                            token = json.loads(candidate.strip())
                            if isinstance(token, dict):
                                values.update(str(v) for k, v in token.items()
                                              if k.lower() in ('access_token', 'refresh_token', 'client_secret') and v)
                except (ValueError, UnicodeError, TypeError):
                    pass
    config = Path(engine) / 'rclone.conf'
    if config.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(config.read_text())
            for section in parser.sections():
                for key, value in parser[section].items():
                    if key in ('client_secret', 'password') and value:
                        values.add(value)
                    if key == 'token':
                        values.add(value)
                        token = json.loads(value)
                        if isinstance(token, dict):
                            values.update(str(v) for k, v in token.items()
                                          if k.lower() in ('access_token', 'refresh_token', 'client_secret') and v)
        except (OSError, ValueError, configparser.Error):
            pass
    return sorted((x for x in values if len(x) > 3), key=len, reverse=True)


def redact(text, values):
    for value in values:
        text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)("(?:access_token|refresh_token|client_secret)"\s*:\s*")[^"]*(")',
                  r'\1[REDACTED]\2', text)
    text = re.sub(r'(?im)(authorization\s*[:=]\s*(?:basic|bearer)\s+)\S+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'(?im)^((?:client_secret|password|token)\s*=\s*).+$', r'\1[REDACTED]', text)
    text = re.sub(r'ya29\.[A-Za-z0-9_.-]+|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+',
                  '[REDACTED]', text)
    return text


def archive_logs(engine, status):
    root = log_root()
    engine = Path(engine)
    values = secret_values(engine)
    sanitized = root / 'sanitized'
    sanitized.mkdir(exist_ok=True, mode=0o700)
    for source in sorted(root.glob('*.log')) + sorted(root.glob('*.jsonl')) + sorted(root.glob('*.json')):
        if source.is_file():
            with source.open(encoding='utf-8', errors='replace') as incoming, \
                    (sanitized / source.name).open('w', encoding='utf-8') as outgoing:
                for line in incoming:
                    outgoing.write(redact(line, values))
    steps = json.loads(os.environ.get('NEOM_JOB_STEPS', '{}'))
    summary = {'status': status, 'repository': os.environ.get('GITHUB_REPOSITORY'),
               'run_id': os.environ.get('GITHUB_RUN_ID'),
               'attempt': os.environ.get('GITHUB_RUN_ATTEMPT'), 'created': now(),
               'steps': {key: {field: data.get(field) for field in ('outcome', 'conclusion')}
                         for key, data in steps.items() if isinstance(data, dict)}}
    (sanitized / 'summary.json').write_text(redact(json.dumps(summary, indent=2), values))
    state = engine / 'bin/ddevice'
    allowed = {'output_zip.txt', 'device_code.txt', 'device_f.txt', 'name_devices.txt',
               'base_rom_code.txt', 'rom_os.txt', 'androidver.txt', 'sdkLevel.txt',
               'device_type.txt', 'fstype.txt', 'superSize.txt', 'chip.txt', 'structure.txt'}
    state_lines = []
    if state.is_dir():
        for path in sorted(state.glob('*.txt')):
            if path.name in allowed or path.name.startswith('fstype_'):
                state_lines.append(path.name + ': ' + path.read_text(errors='replace').strip())
    (sanitized / 'build-state.txt').write_text(redact('\n'.join(state_lines) + '\n', values))
    (sanitized / 'README.txt').write_text(
        'Private Neom build logs. full-build.log contains stage output in order.\n'
        'Individual stage logs and exit codes are included. Known credentials are redacted.\n'
        'If delivered as .zip.001 etc, concatenate parts in numeric order to restore the ZIP.\n')
    run = re.sub(r'[^A-Za-z0-9_-]', '', os.environ.get('GITHUB_RUN_ID', 'local'))
    attempt = re.sub(r'[^A-Za-z0-9_-]', '', os.environ.get('GITHUB_RUN_ATTEMPT', '1'))
    archive = root / f'Neom_Build_Logs_{run}_{attempt}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for path in sorted(sanitized.iterdir()):
            package.write(path, path.name)
    if archive.stat().st_size <= MAX_DOCUMENT_BYTES:
        return [archive]
    parts = []
    with archive.open('rb') as incoming:
        while chunk := incoming.read(MAX_DOCUMENT_BYTES):
            path = root / f'{archive.name}.{len(parts)+1:03d}'
            path.write_bytes(chunk)
            parts.append(path)
    return parts


def telegram_request(token, method, fields, document=None, attempts=3):
    for attempt in range(attempts):
        if document is None:
            body = json.dumps(fields).encode()
            content_type = 'application/json'
        else:
            boundary = 'neom_' + uuid.uuid4().hex
            chunks = []
            for key, value in fields.items():
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                               f'{value}\r\n').encode())
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                           f'filename="{document.name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
            chunks.extend([document.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
            body = b''.join(chunks)
            content_type = 'multipart/form-data; boundary=' + boundary
        request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
                                         data=body, headers={'Content-Type': content_type})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                result = json.loads(error.read())
            except ValueError:
                result = {'ok': False, 'error_code': error.code}
        except (OSError, ValueError):
            result = {'ok': False, 'error_code': 0}
        if result.get('ok'):
            return result['result']
        code = result.get('error_code', 0)
        record({'event': 'telegram_retry', 'method': method, 'http_status': code,
                'attempt': attempt + 1})
        if code not in (0, 429) and code < 500:
            break
        if attempt + 1 < attempts:
            delay = result.get('parameters', {}).get('retry_after', 2)
            time.sleep(min(max(int(delay), 1), 30))
    raise RuntimeError('Telegram request failed; details of credentials are not printed')


def deliver(engine, status):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    owner = os.environ.get('TELEGRAM_OWNER_ID', '')
    if not token or not re.fullmatch(r'[1-9][0-9]*', owner):
        raise ValueError('TELEGRAM_BOT_TOKEN and a positive personal TELEGRAM_OWNER_ID are required')
    chat = telegram_request(token, 'getChat', {'chat_id': owner})
    if chat.get('type') != 'private' or str(chat.get('id')) != owner:
        raise ValueError('Log recipient must be the configured private owner chat')
    archives = archive_logs(engine, status)
    run = os.environ.get('GITHUB_RUN_ID', 'local')
    for index, archive in enumerate(archives, 1):
        caption = f'Neom build {status.upper()}\nRun: {run}\nComplete private build/extract/pack/upload logs.'
        if len(archives) > 1:
            caption += f'\nArchive part {index}/{len(archives)}; join numbered parts to restore ZIP.'
        telegram_request(token, 'sendDocument', {'chat_id': owner, 'caption': caption}, archive)
    print('Private compressed build logs delivered to your Telegram account.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('init')
    run = commands.add_parser('run')
    run.add_argument('stage')
    run.add_argument('command', nargs=argparse.REMAINDER)
    clone = commands.add_parser('checkout')
    clone.add_argument('destination')
    drive = commands.add_parser('check-drive')
    drive.add_argument('config')
    send = commands.add_parser('deliver')
    send.add_argument('engine')
    send.add_argument('status')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'init':
            initialize()
            return 0
        if args.action == 'run':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            return capture(args.stage, command)
        if args.action == 'checkout':
            return checkout(args.destination)
        if args.action == 'check-drive':
            return check_drive(args.config)
        return deliver(args.engine, args.status)
    except Exception as error:
        # CLI exceptions never reveal token-bearing URLs in the Actions console.
        record({'event': 'helper_error', 'action': args.action, 'type': type(error).__name__,
                'message': str(error)})
        print('Private CI helper failed:', type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
