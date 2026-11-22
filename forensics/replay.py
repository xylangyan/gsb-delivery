"""Replay a saved raw request against a fresh copy of the same service.

This performs no socket I/O: the exact request line and headers from an
artifact are fed to the WSGI application and the response is compared byte
for byte with the saved response.
"""

import argparse
import io
import os
import sys

from harness import MODEL_PATH, SERVICE_ROOT, build_service


def parse_request(path):
    with open(path, 'rb') as f:
        raw = f.read().decode('latin-1')
    head, _, body = raw.partition('\r\n\r\n')
    lines = head.split('\r\n')
    method, target, protocol = lines[0].split(' ', 2)
    path_part, _, query = target.partition('?')
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(':')
        headers[name.strip()] = value.strip()
    return {
        'method': method,
        'path': path_part,
        'query': query,
        'protocol': protocol,
        'headers': headers,
        'body': body.encode('latin-1'),
    }


def replay(request, topmax=1):
    svc, container, model = build_service(topmax)
    env = {
        'REQUEST_METHOD': request['method'],
        'SCRIPT_NAME': '',
        'PATH_INFO': request['path'],
        'QUERY_STRING': request['query'],
        'SERVER_NAME': 'host',
        'SERVER_PORT': '80',
        'SERVER_PROTOCOL': request['protocol'],
        'wsgi.version': (1, 0),
        'wsgi.input': io.BytesIO(request['body']),
        'wsgi.errors': sys.stderr,
        'wsgi.multithread': False,
        'wsgi.multiprocess': False,
        'wsgi.run_once': False,
        'wsgi.url_scheme': 'http',
    }
    for name, value in request['headers'].items():
        key = 'HTTP_' + name.upper().replace('-', '_')
        env[key] = value
    if 'Content-Type' in request['headers']:
        env['CONTENT_TYPE'] = request['headers']['Content-Type']
    if 'Content-Length' in request['headers']:
        env['CONTENT_LENGTH'] = request['headers']['Content-Length']
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured['status'] = status
        captured['headers'] = list(headers)
        return lambda data: None

    chunks = svc(env, start_response)
    body = b''.join(
        chunk if isinstance(chunk, bytes) else chunk.encode('utf-8')
        for chunk in chunks)
    return captured['status'], captured['headers'], body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request_file')
    parser.add_argument('--response', default=None,
                        help='saved response to compare against')
    parser.add_argument('--topmax', type=int, default=1)
    args = parser.parse_args()
    request = parse_request(args.request_file)
    status, headers, body = replay(request, args.topmax)
    print('%s %s%s' % (
        request['method'], request['path'],
        ('?' + request['query']) if request['query'] else ''))
    print(status)
    for name, value in headers:
        print('%s: %s' % (name, value))
    print()
    print(body.decode('utf-8'))
    if args.response:
        with open(args.response, 'rb') as f:
            expected = f.read()
        if expected == body:
            print('BYTE-IDENTICAL to %s' % args.response)
            return 0
        print('DIFFERS from %s' % args.response)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
