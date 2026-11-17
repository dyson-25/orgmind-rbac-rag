from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse


class UploadBodyLimit:
    """Bound multipart consumption before Starlette spools uploaded files."""

    def __init__(self, app, max_bytes):
        self.app = app
        self.max_bytes = max_bytes + 65536

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') != '/api/documents/upload':
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = -1
        if length < 0 or length > self.max_bytes:
            response = JSONResponse({'detail': 'Upload request exceeds the limit or has an invalid length'}, status_code=413)
            await response(scope, receive, send)
            return
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message['type'] == 'http.request':
                consumed += len(message.get('body', b''))
                if consumed > self.max_bytes:
                    # Starlette closes any spooled files when MultiPartException is raised.
                    raise MultiPartException('Upload request exceeds the limit')
            return message

        await self.app(scope, bounded_receive, send)
