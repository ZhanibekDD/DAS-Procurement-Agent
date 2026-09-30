"""Real loopback IMAP protocol fixture. No external mailbox or credentials."""
import re
import socketserver
import threading


class CaptureIMAP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, reject_append=False, drop_append_reply=False, reject_header_search=False):
        self.messages = []
        self.append_calls = 0
        self.reject_append = reject_append
        self.drop_append_reply = drop_append_reply
        self.reject_header_search = reject_header_search
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
                if b'HEADER' in args[0] and self.server.reject_header_search:
                    reply(tag + b' NO [UNAVAILABLE] SEARCH Backend error');continue
                ids = []
                match = re.search(rb'<[^>]+>', args[0])
                if args[0] == b'SEARCH ALL':
                    ids = [str(i + 1).encode() for i in range(len(self.server.messages))]
                elif match:
                    ids = [str(i + 1).encode() for i, data in enumerate(self.server.messages) if match[0] in data]
                reply(b'* SEARCH ' + b' '.join(ids))
            elif command == b'UID' and args[0].startswith(b'FETCH'):
                _, ids, fields = args[0].split(b' ', 2)
                assert fields == b'(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.8193>)'
                for uid in ids.split(b','):
                    raw = self.server.messages[int(uid)-1].split(b'\r\n\r\n',1)[0]
                    header = b'\r\n'.join(row for row in raw.split(b'\r\n') if row.lower().startswith(b'message-id:'))
                    header = (header + b'\r\n\r\n')[:8193]
                    reply(b'* '+uid+b' FETCH (UID '+uid+b' BODY[HEADER.FIELDS (MESSAGE-ID)]<0> {'+str(len(header)).encode()+b'}')
                    self.wfile.write(header + b')\r\n');self.wfile.flush()
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
