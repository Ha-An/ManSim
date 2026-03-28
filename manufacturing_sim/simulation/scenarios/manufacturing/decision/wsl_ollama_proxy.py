from __future__ import annotations

import argparse
import shlex
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_BY_HOP = {
    'connection', 'proxy-connection', 'keep-alive', 'transfer-encoding',
    'te', 'trailers', 'upgrade', 'proxy-authenticate', 'proxy-authorization',
}
STATUS_MARKER = '__STATUS__:'


def _build_wsl_curl_command(method: str, url: str, content_type: str) -> str:
    argv = ['curl', '-sS', '-o', '-', '-w', f'\\n{STATUS_MARKER}%{{http_code}}\\n']
    if method.upper() != 'GET':
        argv.extend(['-X', method.upper(), '-H', f'Content-Type: {content_type}', '--data-binary', '@-'])
    argv.append(url)
    return ' '.join(shlex.quote(part) for part in argv)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _forward(self) -> None:
        length = int(self.headers.get('Content-Length', '0') or '0')
        body = self.rfile.read(length) if length > 0 else b''
        url = f'http://localhost:{self.server.target_port}{self.path}'
        content_type = self.headers.get('Content-Type', 'application/json')
        shell_command = _build_wsl_curl_command(self.command, url, content_type)
        proc = subprocess.run(
            ['wsl', '-d', self.server.distro, '--', 'bash', '-lc', shell_command],
            input=body,
            capture_output=True,
        )
        if proc.returncode != 0:
            error_bytes = proc.stderr or b''
            self.send_response(502, 'Bad Gateway')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Connection', 'close')
            self.send_header('Content-Length', str(len(error_bytes)))
            self.end_headers()
            if error_bytes:
                self.wfile.write(error_bytes)
            return

        payload = proc.stdout or b''
        marker = ('\n' + STATUS_MARKER).encode('utf-8')
        status = 200
        body_bytes = payload
        idx = payload.rfind(marker)
        if idx >= 0:
            body_bytes = payload[:idx]
            try:
                status = int(payload[idx + len(marker):].strip().decode('utf-8'))
            except Exception:
                status = 200

        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Connection', 'close')
        self.send_header('Content-Length', str(len(body_bytes)))
        self.end_headers()
        if body_bytes:
            self.wfile.write(body_bytes)

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--listen-host', default='127.0.0.1')
    parser.add_argument('--listen-port', type=int, default=11434)
    parser.add_argument('--distro', default='Ubuntu-24.04')
    parser.add_argument('--target-port', type=int, default=11434)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ProxyHandler)
    server.distro = args.distro
    server.target_port = args.target_port
    server.serve_forever()


if __name__ == '__main__':
    main()
