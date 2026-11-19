"""Keep the automated suite isolated from local provider credentials and data."""

import os

os.environ['APP_ENV'] = 'test'
os.environ['SESSION_TTL_DAYS'] = '30'
os.environ['UPLOAD_REQUESTS_PER_MINUTE'] = '5'
os.environ['IP_UPLOAD_REQUESTS_PER_MINUTE'] = '10'
os.environ['GLOBAL_REQUESTS_PER_MINUTE'] = '30'
os.environ['MAX_TOTAL_DOCUMENTS'] = '500'
os.environ['GENERATION_MAX_TOKENS'] = '3072'
os.environ['GENERATION_PROVIDER'] = 'local'
os.environ['OPENAI_API_KEY'] = ''
os.environ['OPENAI_BASE_URL'] = 'https://openrouter.ai/api/v1'
os.environ['OPENAI_MODEL'] = 'baidu/ernie-5.5-flash-preview:free'
os.environ['EPHEMERAL_STORAGE'] = 'false'
