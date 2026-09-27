"""Real loopback IMAP protocol fixture. No external mailbox or credentials."""
import re
import socketserver
import threading


class CaptureIMAP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, reject_append=False, drop_append_reply=False):
        self.messages = []
        self.append_calls = 0
        self.reject_append = reject_append
        self.drop_append_reply = drop_append_reply
        super().__init__(('127.0.0.1', 0), IMAPHandler)

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.shutdown();self.server_close();self.thread.join(timeout=5)


class IMAPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        def reply(value):
            self.wfile.write(value + b'\r\n');self.wfile.flush()
        reply(b'* OK IMAP test')
        while True:
            line = self.rfile.readline(10000)
            if not line:return
            tag, command, *args = line.rstrip().split(b' ', 2)
            command = command.upper()
            if command == b'CAPABILITY':
                reply(b'* CAPABILITY IMAP4rev1')
            elif command == b'LOGIN':
                pass
            elif command == b'LIST':
                reply(b'* LIST (\\Sent) "/" "Sent"')
            elif command in {b'SELECT', b'EXAMINE'}:
                reply(b'* ' + str(len(self.server.messages)).encode() + b' EXISTS')
                reply(b'* FLAGS (\\Seen)')
            elif command == b'UID' and args[0].startswith(b'SEARCH'):
                ids = []
                match = re.search(rb'<[^>]+>', args[0])
                if match:
                    ids = [str(i + 1).encode() for i, data in enumerate(self.server.messages) if match[0] in data]
                reply(b'* SEARCH ' + b' '.join(ids))
            elif command == b'APPEND':
                self.server.append_calls += 1
                if self.server.reject_append:
                    reply(tag + b' NO test rejection');continue
                size = int(re.search(rb'\{(\d+)\}', args[0])[1])
                reply(b'+ Ready for literal')
                data = self.rfile.read(size)
                assert self.rfile.read(2) == b'\r\n'
                self.server.messages.append(data)
                if self.server.drop_append_reply:return
            elif command == b'LOGOUT':
                reply(b'* BYE');reply(tag + b' OK logout');return
            else:
                reply(tag + b' BAD unsupported');continue
            reply(tag + b' OK completed')
