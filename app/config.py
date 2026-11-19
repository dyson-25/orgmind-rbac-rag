import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR', 'data')).resolve())
    environment: str = field(default_factory=lambda: os.getenv('APP_ENV', 'development'))
    session_ttl_days: int = field(default_factory=lambda: int(os.getenv('SESSION_TTL_DAYS', '30')))
    upload_requests_per_minute: int = field(default_factory=lambda: int(os.getenv('UPLOAD_REQUESTS_PER_MINUTE', '5')))
    ip_upload_requests_per_minute: int = field(default_factory=lambda: int(os.getenv('IP_UPLOAD_REQUESTS_PER_MINUTE', '10')))
    global_requests_per_minute: int = field(default_factory=lambda: int(os.getenv('GLOBAL_REQUESTS_PER_MINUTE', '30')))
    max_total_documents: int = field(default_factory=lambda: int(os.getenv('MAX_TOTAL_DOCUMENTS', '500')))
    generation_max_tokens: int = field(default_factory=lambda: int(os.getenv('GENERATION_MAX_TOKENS', '3072')))
    ephemeral_storage: bool = field(default_factory=lambda: os.getenv('EPHEMERAL_STORAGE', 'false').lower() == 'true')
    max_upload_bytes: int = field(default_factory=lambda: int(os.getenv('MAX_UPLOAD_BYTES', '5242880')))
    max_documents: int = field(default_factory=lambda: int(os.getenv('MAX_DOCUMENTS', '50')))
    chunk_size: int = field(default_factory=lambda: int(os.getenv('CHUNK_SIZE', '900')))
    chunk_overlap: int = field(default_factory=lambda: int(os.getenv('CHUNK_OVERLAP', '150')))
    max_query_chars: int = field(default_factory=lambda: int(os.getenv('MAX_QUERY_CHARS', '2000')))
    requests_per_minute: int = field(default_factory=lambda: int(os.getenv('REQUESTS_PER_MINUTE', '30')))
    generation_provider: str = field(default_factory=lambda: os.getenv('GENERATION_PROVIDER', 'openai'))
    openai_base_url: str = field(default_factory=lambda: os.getenv('OPENAI_BASE_URL', 'https://openrouter.ai/api/v1'))
    openai_model: str = field(default_factory=lambda: os.getenv('OPENAI_MODEL', 'baidu/ernie-5.5-flash-preview:free'))
    openai_api_key: str = field(default_factory=lambda: os.getenv('OPENAI_API_KEY', ''), repr=False)
    generation_timeout_seconds: float = field(default_factory=lambda: float(os.getenv('GENERATION_TIMEOUT_SECONDS', '30')))

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).resolve()
        if self.environment not in {'development', 'production', 'test'}:
            raise ValueError('APP_ENV must be development, production, or test')
        if not 1 <= self.session_ttl_days <= 90:
            raise ValueError('Session lifetime must be 1–90 days')
        if any(not 1 <= value <= 120 for value in (
            self.upload_requests_per_minute, self.ip_upload_requests_per_minute,
            self.global_requests_per_minute,
        )):
            raise ValueError('Request limits must be 1–120 per minute')
        if not 1 <= self.max_total_documents <= 5000:
            raise ValueError('Total document limit must be 1–5000')
        if not 128 <= self.generation_max_tokens <= 8192:
            raise ValueError('Model output limit must be 128–8192 tokens')
        if not 100 <= self.chunk_size <= 4000 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError('Chunk size must be 100–4000; overlap must be smaller than chunk size')
        if not 1 <= self.max_upload_bytes <= 20 * 1024 * 1024:
            raise ValueError('Upload limit must be 1 byte to 20 MiB')
        if not 1 <= self.max_documents <= 100:
            raise ValueError('Document limit must be 1–100')
        if not 1 <= self.max_query_chars <= 4000 or not 1 <= self.requests_per_minute <= 120:
            raise ValueError('Invalid query or request limit')
        if self.generation_provider not in {'openai', 'local'}:
            raise ValueError('Use GENERATION_PROVIDER=openai for OpenRouter, or local to disable generation')
        if self.openai_base_url.rstrip('/') != 'https://openrouter.ai/api/v1':
            raise ValueError('OPENAI_BASE_URL must be https://openrouter.ai/api/v1')
        if not self.openai_model.strip() or len(self.openai_model) > 200:
            raise ValueError('OPENAI_MODEL must name a model')
        if not math.isfinite(self.generation_timeout_seconds) or not 1 <= self.generation_timeout_seconds <= 120:
            raise ValueError('Generation timeout must be 1–120 seconds')

    @property
    def db_path(self):
        return self.data_dir / 'runtime' / 'orgmind.sqlite3'
