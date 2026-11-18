import os
import secrets
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR', 'data')).resolve())
    environment: str = field(default_factory=lambda: os.getenv('APP_ENV', 'development'))
    jwt_secret: str = field(default_factory=lambda: os.getenv('JWT_SECRET', ''))
    allow_demo: bool = field(default_factory=lambda: os.getenv(
        'ALLOW_DEMO_PERSONA', 'false' if os.getenv('APP_ENV') == 'production' else 'true'
    ).lower() == 'true')
    max_upload_bytes: int = field(default_factory=lambda: int(os.getenv('MAX_UPLOAD_BYTES', '5242880')))
    chunk_size: int = field(default_factory=lambda: int(os.getenv('CHUNK_SIZE', '900')))
    chunk_overlap: int = field(default_factory=lambda: int(os.getenv('CHUNK_OVERLAP', '150')))
    token_minutes: int = 60
    demo_admin_username: str = field(default_factory=lambda: os.getenv('DEMO_ADMIN_USERNAME', ''))
    demo_admin_password: str = field(default_factory=lambda: os.getenv('DEMO_ADMIN_PASSWORD', ''), repr=False)
    demo_employee_username: str = field(default_factory=lambda: os.getenv('DEMO_EMPLOYEE_USERNAME', ''))
    demo_employee_password: str = field(default_factory=lambda: os.getenv('DEMO_EMPLOYEE_PASSWORD', ''), repr=False)

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).resolve()
        if len(self.jwt_secret) < 32:
            if self.environment == 'production':
                raise ValueError('Production requires JWT_SECRET of at least 32 random characters')
            self.jwt_secret = secrets.token_urlsafe(48)
            warnings.warn('Using ephemeral development-only JWT secret; tokens expire on restart.', stacklevel=2)
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError('Chunk overlap must be smaller than chunk size')
        if self.max_upload_bytes < 1:
            raise ValueError('Upload limit must be positive')
        for kind in ('admin', 'employee'):
            username = getattr(self, f'demo_{kind}_username').strip().casefold()
            password = getattr(self, f'demo_{kind}_password')
            setattr(self, f'demo_{kind}_username', username)
            if bool(username) != bool(password):
                raise ValueError(f'DEMO_{kind.upper()}_USERNAME and PASSWORD must be configured together')
            if password and not 12 <= len(password) <= 256:
                raise ValueError('Demo passwords must contain 12 to 256 characters')
        if self.demo_admin_username and self.demo_admin_username == self.demo_employee_username:
            raise ValueError('Demo admin and employee usernames must differ')

    @property
    def db_path(self):
        return self.data_dir / 'runtime' / 'orgmind.sqlite3'
