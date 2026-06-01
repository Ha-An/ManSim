from __future__ import annotations

import argparse
import mimetypes
import shutil
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


def _is_inside(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


class ReplayStudio3DHandler(SimpleHTTPRequestHandler):
    repo_root: Path
    dist_root: Path

    def end_headers(self) -> None:  # noqa: N802 - http.server API
        # Replay Studio is rebuilt frequently during UI work; avoid serving stale bundles.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        request_path = unquote(parsed.path).lstrip("/")
        if not request_path:
            return str(self.dist_root / "index.html")
        candidate = (self.dist_root / request_path).resolve()
        if candidate.is_file() and _is_inside(self.dist_root, candidate):
            return str(candidate)
        # The 3D app is an SPA; unknown app routes should load index.html.
        return str(self.dist_root / "index.html")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        if parsed.path == "/__mansim_file":
            self._serve_repo_file(parsed.query)
            return
        super().do_GET()

    def _serve_repo_file(self, query: str) -> None:
        raw_path = parse_qs(query).get("path", [""])[0]
        if not raw_path:
            self.send_error(400, "Missing path")
            return
        candidate = Path(raw_path).resolve()
        if not _is_inside(self.repo_root, candidate) or not candidate.is_file():
            self.send_error(403, "Forbidden")
            return
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(candidate.stat().st_size))
        self.end_headers()
        with candidate.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the built 3D Replay Studio with ManSim local-file support.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5174)
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    dist_root = repo_root / "replay_studio_3d" / "dist"
    index_path = dist_root / "index.html"
    if not index_path.exists():
        raise SystemExit(f"Missing built 3D Replay Studio: {index_path}")

    ReplayStudio3DHandler.repo_root = repo_root
    ReplayStudio3DHandler.dist_root = dist_root
    server = ThreadingHTTPServer((args.host, args.port), ReplayStudio3DHandler)
    print(f"Serving 3D Replay Studio at http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
