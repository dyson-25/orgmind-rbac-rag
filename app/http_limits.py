from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse


class UploadBodyLimit:
    """Bound multipart consumption before Starlette spools uploaded files."""

    def __init__(self, app, max_bytes):
        self.app = app
        self.max_bytes = max_bytes + 65536

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        upload = path in {'/api/documents/upload', '/api/workspace/documents/upload'}
        workspace_body = path.startswith('/api/workspace/') and scope.get('method') in {'POST', 'PATCH'}
        if scope['type'] != 'http' or not (upload or workspace_body):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = -1
        limit = self.max_bytes if upload else 16384
        if length < 0 or length > limit:
            response = JSONResponse({'detail': 'Upload request exceeds the limit or has an invalid length'}, status_code=413)
            await response(scope, receive, send)
            return
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message['type'] == 'http.request':
                consumed += len(message.get('body', b''))
                if consumed > limit:
                    # Starlette closes any spooled files when MultiPartException is raised.
                    raise MultiPartException('Upload request exceeds the limit')
            return message

        await self.app(scope, bounded_receive, send)
