from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit
import json
import hmac
from tariffs.primitives import canonical
from publication.store import Archive, Conflict
from publication.service import Service


def handler(database, token):
    if not isinstance(token, str) or len(token) < 24:
        raise ValueError('local API requires a token of at least 24 characters')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, format, *args):
            pass

        def reply(self, status, value):
            body = canonical(value).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_GET(self):
            self.reply(405, {'error': 'use_authenticated_POST'})

        def do_POST(self):
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied, 'Bearer ' + token):
                self.reply(401, {'error': 'unauthorized'})
                return
            if self.headers.get('Transfer-Encoding') is not None:
                self.reply(400, {'error': 'transfer_encoding_not_supported'})
                return
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                self.reply(400, {'error': 'single_content_length_required'})
                return
            length = int(lengths[0])
            if length > 10_000_000:
                self.reply(413, {'error': 'request_too_large'})
                return
            if self.headers.get_content_type() != 'application/json':
                self.reply(415, {'error': 'application_json_required'})
                return
            path = urlsplit(self.path)
            if path.query or path.fragment or not path.path.startswith('/v1/'):
                self.reply(404, {'error': 'unknown_route'})
                return
            command = path.path[len('/v1/'):]
            if not command or '/' in command:
                self.reply(404, {'error': 'unknown_route'})
                return
            self.connection.settimeout(10)
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('incomplete body')
                body = json.loads(raw)
                with Archive(database) as archive:
                    result = Service(archive).dispatch(command, body)
            except Conflict as exc:
                self.reply(409, {'error': str(exc)})
            except KeyError:
                self.reply(404, {'error': 'not_found'})
            except (ValueError, TypeError, UnicodeError) as exc:
                self.reply(400, {'error': str(exc)[:1000]})
            except TimeoutError:
                self.reply(408, {'error': 'request_timeout'})
            else:
                self.reply(200, {'result': result})

    return Handler


def serve(database, token, host='127.0.0.1', port=8080):
    if host not in ('127.0.0.1', '::1', 'localhost'):
        raise ValueError('service only binds loopback')
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('invalid listening port')
    with HTTPServer((host, port), handler(database, token)) as server:
        server.serve_forever(poll_interval=.2)
