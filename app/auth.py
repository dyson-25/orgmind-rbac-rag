from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException
from pwdlib import PasswordHash

from app.rbac_guard import PERSONAS, authorized_scopes

ISSUER = 'orgmind-auth'
AUDIENCE = 'orgmind-api'


class Auth:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.dummy_hash = PasswordHash.recommended().hash('not-a-login-password')

    def issue(self, user):
        now = datetime.now(timezone.utc)
        token = jwt.encode({'sub': user['id'], 'role': user['role'], 'ver': user['auth_version'],
                            'iat': now, 'exp': now + timedelta(minutes=self.settings.token_minutes),
                            'iss': ISSUER, 'aud': AUDIENCE}, self.settings.jwt_secret, algorithm='HS256')
        return {'access_token': token, 'token_type': 'bearer', 'user': self.public(user)}

    def public(self, user):
        return {'id': user['id'], 'role': user['role'], 'scopes': authorized_scopes(user), 'acl_epoch': self.db.epoch()}

    def verify(self, token):
        try:
            claims = jwt.decode(token, self.settings.jwt_secret, algorithms=['HS256'], issuer=ISSUER, audience=AUDIENCE,
                                options={'require': ['sub', 'role', 'ver', 'exp', 'iat', 'iss', 'aud']})
            user = self.db.user(claims['sub'])
            if not user or claims['ver'] != user['auth_version'] or claims['role'] != user['role']:
                raise ValueError('Identity changed')
            return user
        except (jwt.PyJWTError, ValueError, TypeError):
            raise HTTPException(401, 'Invalid or expired authentication') from None

    def login(self, uid, password):
        user = self.db.user(uid)
        valid = PasswordHash.recommended().verify(password, user['password_hash'] if user else self.dummy_hash)
        if not valid or not user:
            self.db.audit(None, 'login', 'deny', rule='credentials')
            raise HTTPException(401, 'Invalid credentials')
        self.db.audit(user, 'login', 'allow', rule='credentials')
        return self.issue(user)

    def demo(self, persona_id):
        if not self.settings.allow_demo:
            raise HTTPException(403, 'Demo access disabled')
        if persona_id not in PERSONAS:
            raise HTTPException(400, 'Unknown demo persona')
        user = self.db.user(persona_id)
        self.db.audit(user, 'demo-login', 'allow', rule='synthetic-demo')
        return self.issue(user)
