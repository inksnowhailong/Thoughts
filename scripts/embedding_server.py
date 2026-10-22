"""Local embedding server: OpenAI-compatible /v1/embeddings on port 18794.

Uses fastembed (ONNX runtime) with BAAI/bge-small-zh-v1.5 for Chinese text.
Zero cloud dependency — everything runs locally on CPU.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from fastembed import TextEmbedding
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(level=logging.INFO, format='%(asctime)s [embedding] %(message)s')
log = logging.getLogger(__name__)

PORT = int(os.environ.get('EMBEDDING_PORT', 18794))
MODEL = 'BAAI/bge-small-zh-v1.5'

_model: TextEmbedding | None = None
_lock = threading.Lock()


def get_model() -> TextEmbedding:
    """Lazy-load the embedding model on first request."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                log.info('loading model %s ...', MODEL)
                _model = TextEmbedding(model_name=MODEL)
                log.info('model loaded')
    return _model


class EmbeddingHandler(BaseHTTPRequestHandler):
    """Handles POST /v1/embeddings in OpenAI-compatible format."""

    def do_POST(self):
        if self.path != '/v1/embeddings':
            self.send_error(404, 'Not Found')
            return

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, 'Bad Request')
            return

        texts = request.get('input')
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list) or not texts:
            self.send_error(400, 'input must be a string or list of strings')
            return

        model_name = request.get('model', MODEL)
        try:
            embedder = get_model()
            embeddings = list(embedder.embed(texts[:32]))  # cap at 32 texts per request
            response = {
                'object': 'list',
                'data': [
                    {'object': 'embedding', 'embedding': e.tolist(), 'index': i}
                    for i, e in enumerate(embeddings)
                ],
                'model': model_name,
                'usage': {'prompt_tokens': sum(len(t) // 4 for t in texts), 'total_tokens': sum(len(t) // 4 for t in texts)},
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            response_bytes = json.dumps(response).encode()
            self.send_header('Content-Length', str(len(response_bytes)))
            self.end_headers()
            self.wfile.write(response_bytes)
        except Exception as exc:
            log.exception('embedding failed')
            self.send_error(500, str(exc))

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True, 'model': MODEL}).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        log.info(format % args)


def main():
    log.info('embedding server starting on port %d (model: %s)', PORT, MODEL)
    server = HTTPServer(('127.0.0.1', PORT), EmbeddingHandler)
    log.info('listening on 127.0.0.1:%d', PORT)
    server.serve_forever()


if __name__ == '__main__':
    main()