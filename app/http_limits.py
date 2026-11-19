from starlette.responses import JSONResponse


class RequestBodyLimit:
    """Bound even chunked bodies before JSON or multipart parsing."""

    def __init__(self, app, max_bytes):
        self.app = app
        self.upload_limit = max_bytes + 65536

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            await self.app(scope, receive, send)
            return
        limit = self.upload_limit if scope.get('path') == '/api/documents/upload' else 16384
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = -1
        rejection = JSONResponse({'detail': 'Request exceeds the size limit or has an invalid length.'}, status_code=413)
        if length < 0 or length > limit:
            await rejection(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > limit:
                await rejection(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        sent = False

        async def buffered_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, buffered_receive, send)
