"""Per-execution public HTTP/HTTPS egress proxy. No credentials or host mounts."""
import select
import socket
import socketserver
import threading
import time
import sys
from urllib.parse import urlsplit
sys.path.insert(0, '/')  # Trusted, read-only image module; Python -I excludes script paths.
from public_web import connect_public, web_url

MAX_BYTES=64_000_000
SLOTS=threading.BoundedSemaphore(16)

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not SLOTS.acquire(blocking=False):return
        upstream=None
        try:
            self.request.settimeout(10)
            header=bytearray()
            while b'\r\n\r\n' not in header:
                byte=self.request.recv(1)
                if not byte:return
                header.extend(byte)
                if len(header)>16384:raise ValueError('Header too large')
            lines=header.decode('iso-8859-1').split('\r\n')
            method,target,version=lines[0].split(' ',2)
            if method=='CONNECT':
                parsed=urlsplit('https://'+target)
                if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:raise ValueError('Invalid tunnel')
                port=parsed.port or 443
                if port!=443:raise ValueError('HTTPS tunnels use port 443')
                upstream=connect_public(parsed.hostname,port,10)
                self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            elif method in ('GET','HEAD'):
                parsed,port=web_url(target)
                if parsed.scheme!='http':raise ValueError('Use CONNECT for HTTPS')
                upstream=connect_public(parsed.hostname,port,10)
                path=parsed.path or '/'
                if parsed.query:path+='?'+parsed.query
                # Never forward proxy auth, hop-by-hop headers, alternate Host or request bodies.
                forwarded=[line for line in lines[1:] if ':' in line and line.split(':',1)[0].lower() not in
                    ('host','proxy-authorization','proxy-connection','connection','content-length','transfer-encoding','upgrade')]
                upstream.sendall((f'{method} {path} HTTP/1.1\r\nHost: {parsed.hostname}\r\nConnection: close\r\n'+'\r\n'.join(forwarded)+'\r\n\r\n').encode('iso-8859-1'))
            else:raise ValueError('Unsupported proxy method')
            deadline=time.monotonic()+30;counts={self.request:0,upstream:0}
            while time.monotonic()<deadline:
                ready,_,_=select.select([self.request,upstream],[],[],1)
                for source in ready:
                    chunk=source.recv(65536)
                    if not chunk:return
                    counts[source]+=len(chunk)
                    if counts[source]>MAX_BYTES:return
                    (upstream if source is self.request else self.request).sendall(chunk)
        except (ValueError,OSError,TypeError):
            if upstream is None:
                try:self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 37\r\nConnection: close\r\n\r\nPublic HTTP/HTTPS destinations only.\n')
                except OSError:pass
        finally:
            if upstream:upstream.close()
            SLOTS.release()

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True

if __name__=='__main__':
    with Server(('0.0.0.0',8080),Handler) as server:
        expiry=threading.Timer(660,server.shutdown);expiry.daemon=True;expiry.start()
        server.serve_forever()
