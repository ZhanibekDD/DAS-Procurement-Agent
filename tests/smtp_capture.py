"""Real loopback SMTP fixture, intentionally never connects to external recipients."""
import socketserver
import threading
from email import policy
from email.parser import BytesParser


class CaptureSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True

    def __init__(self, reject=False):
        self.messages=[]
        self.reject=reject
        super().__init__(('127.0.0.1',0),SMTPHandler)

    def __enter__(self):
        self.thread=threading.Thread(target=self.serve_forever,daemon=True)
        self.thread.start()
        return self

    def __exit__(self,*args):
        self.shutdown();self.server_close();self.thread.join(timeout=5)


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        def reply(text):self.wfile.write(text+b'\r\n');self.wfile.flush()
        reply(b'220 localhost test SMTP')
        while True:
            line=self.rfile.readline(10000)
            if not line:return
            verb=line.split(b' ',1)[0].strip().upper()
            if verb==b'QUIT':reply(b'221 Bye');return
            if verb==b'EHLO':reply(b'250-localhost\r\n250 SIZE 35000000')
            elif verb in {b'HELO',b'MAIL',b'RCPT',b'RSET',b'NOOP'}:reply(b'250 OK')
            elif verb==b'DATA':
                reply(b'354 End with dot')
                lines=[]
                while True:
                    data=self.rfile.readline(1000000)
                    if data==b'.\r\n':break
                    if not data:return
                    lines.append(data[1:] if data.startswith(b'..') else data)
                if self.server.reject:reply(b'451 test rejection')
                else:
                    self.server.messages.append(BytesParser(policy=policy.default).parsebytes(b''.join(lines)))
                    reply(b'250 Accepted')
            else:reply(b'502 unsupported')
