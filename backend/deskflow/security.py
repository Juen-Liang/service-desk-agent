import hashlib
import json
import re
import secrets
from datetime import timedelta
import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from .config import settings
from .db import Session, now
from .models import User

passwords = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)


def digest(value):
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def encrypt(value):
    return Fernet(settings().encryption_key.encode()).encrypt(value.encode()).decode() if value else ''


def decrypt(value):
    return Fernet(settings().encryption_key.encode()).decrypt(value.encode()).decode() if value else ''


def access_token(user):
    return jwt.encode({'sub': user.id, 'type': 'access', 'exp': now() + timedelta(minutes=30)}, settings().jwt_secret, algorithm='HS256')


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    token = credentials.credentials if credentials else request.cookies.get('deskflow_access')
    if not token:
        raise HTTPException(401, '请先登录')
    try:
        claims = jwt.decode(token, settings().jwt_secret, algorithms=['HS256'])
        if claims.get('type') != 'access':
            raise ValueError('token type')
        with Session() as db:
            user = db.get(User, claims['sub'])
            if not user or not user.active:
                raise ValueError('inactive')
            return user
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, '登录已失效，请重新登录')


def require_role(user, *roles):
    if user.role not in roles:
        raise HTTPException(403, '当前角色无权进行此操作')


def check_origin(request: Request):
    """Reject browser cross-origin writes; bearer clients remain independently authenticated."""
    origin = request.headers.get('origin')
    allowed = {settings().frontend_origin, str(request.base_url).rstrip('/')}
    if origin and origin not in allowed:
        raise HTTPException(403, '不允许的请求来源')


def redact(value):
    text = str(value)
    text = re.sub(r'(?i)(bearer\s+)[\w.\-]+', r'\1[REDACTED]', text)
    text = re.sub(r'\bsk-[\w.\-]+', '[REDACTED]', text)
    text = re.sub(r'(?i)(password|api[_-]?key|authorization)([\s\"\x27:=]+)[^\s,}\"]+', r'\1\2[REDACTED]', text)
    return text[:1500]


def sign_permit(operation_id, action, params):
    return jwt.encode({'aud': 'deskflow-business', 'op': operation_id, 'action': action, 'hash': digest(params),
                       'exp': now() + timedelta(seconds=60)}, settings().demo_service_secret, algorithm='HS256')
