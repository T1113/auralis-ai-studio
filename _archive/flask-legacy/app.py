import json
import base64
import hashlib
import hmac
import io
import shutil
import sqlite3
import threading
import uuid
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from flask import Flask, render_template, request, jsonify, make_response, send_file, send_from_directory, session, url_for
from authlib.integrations.flask_client import OAuth
import time
import os
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# --- OAuth Setup ---
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'impeccable.db'
PRIVATE_MEDIA_DIR = BASE_DIR / 'private_media'
UPLOAD_DIR = PRIVATE_MEDIA_DIR / 'uploads'
GENERATED_DIR = PRIVATE_MEDIA_DIR / 'generated'
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.heic'}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_GENERATED_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
RATE_LIMIT_BUCKETS = defaultdict(deque)
RATE_LIMIT_LOCK = threading.Lock()
DOUBAO_DEFAULT_API_URL = 'https://ark.cn-beijing.volces.com/api/v3/images/generations'
DOUBAO_MODEL_OPTIONS = {
    'doubao-seedream-5-0-260128': {
        'label': 'Seedream 5.0',
        'description': '最新图像创作模型，适合高一致性职业形象照。',
    },
    'doubao-seedream-4-5-251128': {
        'label': 'Seedream 4.5',
        'description': '稳定增强版，适合常规图生图与组图生成。',
    },
    'doubao-seedream-4-0-250828': {
        'label': 'Seedream 4.0',
        'description': '经典多模态生图模型，适合兼容性优先的生成。',
    },
}
DOUBAO_DEFAULT_MODEL = 'doubao-seedream-5-0-260128'
DEFAULT_RESULT_COUNT = 5
MAX_RESULT_COUNT = 12
FREE_TRIAL_MODE = os.getenv('FREE_TRIAL_MODE', 'true').lower() == 'true'
LOCAL_INITIAL_CREDITS = 0 if FREE_TRIAL_MODE else 1000
LOCAL_GENERATION_COST_CREDITS = 0 if FREE_TRIAL_MODE else 100
LOCAL_FREE_REGENERATIONS = 2

# 任务解锁套餐（¥39 / ¥99 / ¥249），与首页"选择适合你的套餐"对齐
UNLOCK_PACKAGES = {
    'experience': {'cents': 3900,  'label': '体验包', 'subtitle': '1 次生成 / 4 张职业成片'},
    'standard':   {'cents': 9900,  'label': '标准包', 'subtitle': '4 次生成 / 16 张多风格成片'},
    'flagship':   {'cents': 24900, 'label': '旗舰包', 'subtitle': '12 次生成 / 48 张全风格'},
}
PAYMENT_MODE = os.getenv('PAYMENT_MODE', 'mock').strip().lower() or 'mock'
PREVIEW_WATERMARK_TEXT = os.getenv('PREVIEW_WATERMARK_TEXT', 'Auralis Preview')
HALF_BODY_LABEL = '半身职业头像'
FULL_BODY_LABEL = '全身职业形象照'
BRAND_TEXT_LABEL = '带文字品牌形象照'
HALF_TEXT_LABEL = '半身带文字职业头像'
CELEBRITY_HEADSHOT_LABEL = '明星级职业头像（1:1）'
STUDIO_BUSINESS_PORTRAIT_LABEL = '专业工作室职业肖像（3:4）'
ACADEMIC_PROFILE_CARD_LABEL = '商务档案海报（1:1，本地排版）'
FONT_REGULAR = Path('/System/Library/Fonts/Helvetica.ttc')
FONT_BOLD = Path('/System/Library/Fonts/HelveticaNeue.ttc')
FONT_CJK = Path('/System/Library/Fonts/Hiragino Sans GB.ttc')
FONT_CJK_BOLD = Path('/System/Library/Fonts/STHeiti Medium.ttc')

CLOTHING_OPTIONS = {
    'jacket_colors': {
        'navy': '深蓝色',
        'black': '黑色',
        'gray': '灰色',
        'white': '白色',
        'brown': '棕色',
    },
    'inner_colors': {
        'white': '白色',
        'lightgray': '浅灰色',
        'black': '黑色',
        'lightblue': '浅蓝色',
        'navy': '深蓝色',
        'beige': '米色',
        'original': '__ORIGINAL__',
    },
    'jacket_styles': {
        'business_suit': '商务西装',
        'premium_formal': '高端正装',
        'tech_business': '科技商务风',
        'id_portrait': '职业证件照风格',
        'none': '__NO_JACKET__',
    },
    'inner_styles': {
        'shirt': '衬衫',
        'tshirt': 'T 恤',
        'turtleneck': '高领内搭',
        'knitwear': '针织衫',
    },
    'collar_styles': {
        'regular': '普通领',
        'high': '较高领',
        'stiff': '挺拔硬挺领',
        'structured': '有结构感领子',
    },
    'button_states': {
        'all_buttoned': '全扣',
        'one_open': '解开一个扣子',
        'two_open': '解开两个扣子',
        'hidden': '不显示扣子',
    },
    'backgrounds': {
        'gray_gradient': '干净极简的灰色渐变摄影棚背景',
        'white_studio': '白色摄影棚背景',
        'dark_business': '深色商务背景',
        'blue_gray_tech': '蓝灰科技背景',
        'solid_color': '纯色背景',
    },
    'visual_styles': {
        'premium_id': '高端证件照/商务人像风格',
        'american_id': '高端美式证件照人像风格',
        'tech_brand': '克制、高端、专业、科技品牌视觉系统感',
        'executive': '高管商务品牌人像风格',
    },
}

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

def load_env_file():
    env_path = BASE_DIR / '.env'
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env_file()

_configured_secret = os.getenv('SECRET_KEY') or os.getenv('FLASK_SECRET_KEY')
if not _configured_secret:
    import warnings
    warnings.warn('SECRET_KEY is not set — sessions will be invalidated on every restart.', stacklevel=1)
app.secret_key = _configured_secret or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true',
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES * 5,
)

def request_ip():
    forwarded_for = request.headers.get('X-Forwarded-For', '')
    if forwarded_for:
        hops = int(os.getenv('TRUSTED_PROXY_HOPS', '1'))
        ips = [ip.strip() for ip in forwarded_for.split(',')]
        idx = max(0, len(ips) - hops)
        return ips[idx]
    return request.remote_addr or 'unknown'

def rate_limit_key():
    user_id = session.get('user_id')
    return user_id or request_ip()

def check_rate_limit(scope, key, limit, window_seconds):
    now = time.time()
    bucket_key = (scope, key)
    with RATE_LIMIT_LOCK:
        bucket = RATE_LIMIT_BUCKETS[bucket_key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
    return True

def same_origin_request():
    origin = request.headers.get('Origin')
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.netloc == request.host

@app.before_request
def enforce_basic_request_guards():
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and not same_origin_request():
        return jsonify({'success': False, 'error': '请求来源无效。'}), 403

    limits = {
        ('POST', '/api/auth/login'): ('auth_login', request_ip(), 10, 60),
        ('POST', '/api/auth/register'): ('auth_register', request_ip(), 5, 60),
        ('POST', '/api/upload'): ('upload', rate_limit_key(), 30, 60),
        ('POST', '/api/uploads'): ('upload', rate_limit_key(), 30, 60),
        ('POST', '/api/generate'): ('generate', rate_limit_key(), 5, 60),
        ('POST', '/api/jobs'): ('generate', rate_limit_key(), 5, 60),
        ('POST', '/api/chat'): ('chat', rate_limit_key(), 60, 60),
        ('POST', '/api/credits/topup'): ('credits_topup', rate_limit_key(), 10, 60),
        ('POST', '/api/account/delete'): ('account_delete', rate_limit_key(), 3, 60),
    }
    rule = limits.get((request.method, request.path))
    if rule:
        scope, key, limit, window_seconds = rule
        if not check_rate_limit(scope, key, limit, window_seconds):
            return jsonify({'success': False, 'error': '请求过于频繁，请稍后再试。'}), 429


@app.after_request
def disable_dev_cache(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(self), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "media-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )
    return response

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, 
                    email TEXT, 
                    name TEXT, 
                    password_hash TEXT,
                    register_time INTEGER)''')
    # Messages table
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    user_id TEXT,
                    is_admin INTEGER,
                    content TEXT,
                    timestamp INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS uploaded_photos (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    original_name TEXT,
                    file_path TEXT,
                    public_url TEXT,
                    created_at INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS generation_batches (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    source_photo_ids TEXT,
                    style_config TEXT,
                    status TEXT,
                    created_at INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS generated_images (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT,
                    label TEXT,
                    file_path TEXT,
                    public_url TEXT,
                    created_at INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_credits (
                    user_id TEXT PRIMARY KEY,
                    balance INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS credit_transactions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    batch_id TEXT,
                    created_at INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS payment_orders (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    credits INTEGER NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    paid_at INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS job_payments (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    out_trade_no TEXT UNIQUE NOT NULL,
                    package_id TEXT,
                    amount_cents INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    transaction_id TEXT,
                    created_at INTEGER NOT NULL,
                    paid_at INTEGER)''')
    existing_columns = {row[1] for row in c.execute('PRAGMA table_info(users)').fetchall()}
    if 'password_hash' not in existing_columns:
        c.execute('ALTER TABLE users ADD COLUMN password_hash TEXT')
    existing_columns = {row[1] for row in c.execute('PRAGMA table_info(generation_batches)').fetchall()}
    if 'client_request_id' not in existing_columns:
        c.execute('ALTER TABLE generation_batches ADD COLUMN client_request_id TEXT')
    if 'unlocked_at' not in existing_columns:
        c.execute('ALTER TABLE generation_batches ADD COLUMN unlocked_at INTEGER')
    if 'generation_cost_credits' not in existing_columns:
        c.execute('ALTER TABLE generation_batches ADD COLUMN generation_cost_credits INTEGER DEFAULT 100')
    if 'free_regenerations_remaining' not in existing_columns:
        c.execute('ALTER TABLE generation_batches ADD COLUMN free_regenerations_remaining INTEGER DEFAULT 2')
    if 'brand_profile_json' not in existing_columns:
        c.execute('ALTER TABLE generation_batches ADD COLUMN brand_profile_json TEXT')
    existing_columns = {row[1] for row in c.execute('PRAGMA table_info(generated_images)').fetchall()}
    if 'master_file_path' not in existing_columns:
        c.execute('ALTER TABLE generated_images ADD COLUMN master_file_path TEXT')
    existing_columns = {row[1] for row in c.execute('PRAGMA table_info(users)').fetchall()}
    if 'brand_company' not in existing_columns:
        c.execute('ALTER TABLE users ADD COLUMN brand_company TEXT')
    if 'brand_title' not in existing_columns:
        c.execute('ALTER TABLE users ADD COLUMN brand_title TEXT')
    if 'brand_phone' not in existing_columns:
        c.execute('ALTER TABLE users ADD COLUMN brand_phone TEXT')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_messages_user_timestamp ON messages(user_id, timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_uploaded_photos_user_id ON uploaded_photos(user_id, id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_generation_batches_user_created ON generation_batches(user_id, created_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_generation_batches_user_client_request ON generation_batches(user_id, client_request_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_generated_images_batch_created ON generated_images(batch_id, created_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_credit_transactions_user_created ON credit_transactions(user_id, created_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payment_orders_user_created ON payment_orders(user_id, created_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_job_payments_job ON job_payments(job_id, status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_job_payments_user_created ON job_payments(user_id, created_at)')
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_credit_balance(user_id):
    conn = get_db()
    balance = ensure_user_credits(conn, user_id)
    conn.commit()
    conn.close()
    return balance

def public_user_payload(user):
    return {
        'id': user['id'],
        'name': user['name'],
        'email': user['email'],
        'role': 'admin' if is_admin_user(user) else 'user',
        'credits': get_credit_balance(user['id']),
    }

def is_admin_user(user):
    return str(user['email']).lower().startswith('admin@')

def current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    if not user:
        session.clear()
        return None
    return user

def require_current_user():
    user = current_user()
    if not user:
        return None, (jsonify({'success': False, 'error': '请先登录。'}), 401)
    return user, None

def require_admin_user():
    user, error_response = require_current_user()
    if error_response:
        return None, error_response
    if not is_admin_user(user):
        return None, (jsonify({'success': False, 'error': '没有管理员权限。'}), 403)
    return user, None

def auth_response(user):
    payload = {'success': True, 'authenticated': True, 'user': public_user_payload(user)}
    response = make_response(jsonify(payload))
    session.clear()
    session.permanent = True
    session['user_id'] = user['id']
    response.delete_cookie('impeccable_user_id')
    return response

def hash_password(password):
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('ascii'), 120000).hex()
    return f'pbkdf2_sha256${salt}${digest}'

def verify_password(password, stored_hash):
    if not stored_hash:
        return False
    try:
        if stored_hash.startswith('pbkdf2_sha256$'):
            _, salt, digest = stored_hash.split('$', 2)
            candidate = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('ascii'), 120000).hex()
            return hmac.compare_digest(candidate, digest)
        legacy = hashlib.sha256(password.encode('utf-8')).hexdigest()
        return hmac.compare_digest(legacy, stored_hash)
    except (ValueError, TypeError):
        return False

def is_strong_password(password):
    return (
        len(password) >= 8
        and any(ch.isupper() for ch in password)
        and any(ch.islower() for ch in password)
        and any(ch.isdigit() for ch in password)
        and any(not ch.isalnum() for ch in password)
    )

def is_allowed_image(filename):
    return Path(filename or '').suffix.lower() in ALLOWED_EXTENSIONS

def image_quality_status(file_storage):
    size = request.content_length or 0
    if size > MAX_UPLOAD_BYTES * 5:
        return 'error', '本次上传总大小超过限制，请压缩后重新上传。'
    if not is_allowed_image(file_storage.filename):
        return 'error', '仅支持 JPG、PNG、WEBP 或 HEIC 图片。'
    return 'success', '照片已通过基础格式校验。'

def validate_image_bytes(data, filename='image'):
    if len(data) > MAX_UPLOAD_BYTES:
        return False, '单张照片超过 12MB，请压缩后重新上传。'
    ext = Path(filename or '').suffix.lower()
    if ext == '.heic':
        return True, ''
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                return False, '图片像素尺寸过大，请压缩后重新上传。'
            image.verify()
    except Exception:
        return False, '图片文件无法识别，请上传真实的 JPG、PNG 或 WEBP 图片。'
    return True, ''

def validate_generated_image_bytes(data, content_type):
    if len(data) > MAX_GENERATED_IMAGE_BYTES:
        raise RuntimeError('生成图片超过大小限制。')
    if content_type and not content_type.lower().startswith('image/'):
        raise RuntimeError('生成接口返回了非图片内容。')
    ok, message = validate_image_bytes(data, 'generated.jpg')
    if not ok:
        raise RuntimeError(message)

def photo_public_url(path):
    resolved = path.resolve()
    if UPLOAD_DIR.resolve() in resolved.parents:
        return f'/media/uploads/{resolved.name}'
    if GENERATED_DIR.resolve() in resolved.parents:
        return f'/media/generated/{resolved.name}'
    return '/' + path.relative_to(BASE_DIR).as_posix()

def path_is_owned_upload(conn, user_id, path):
    row = conn.execute(
        'SELECT id FROM uploaded_photos WHERE user_id = ? AND file_path = ?',
        (user_id, str(path)),
    ).fetchone()
    return row is not None

def path_is_owned_generated(conn, user_id, path):
    row = conn.execute(
        '''SELECT generated_images.id
           FROM generated_images
           JOIN generation_batches ON generation_batches.id = generated_images.batch_id
           WHERE generation_batches.user_id = ? AND generated_images.file_path = ?''',
        (user_id, str(path)),
    ).fetchone()
    return row is not None

GENERATION_STYLE_OPTIONS = [
    {'id': 'half_body_portrait', 'label': HALF_BODY_LABEL},
    {'id': 'full_body_portrait', 'label': FULL_BODY_LABEL},
    {'id': 'brand_text_portrait', 'label': BRAND_TEXT_LABEL},
    {'id': 'half_text_portrait', 'label': HALF_TEXT_LABEL},
    {'id': 'business_gray', 'label': '经典商务浅灰底'},
    {'id': 'linkedin_blue', 'label': '领英专属蓝底'},
    {'id': 'natural_gray', 'label': '自然光高级灰'},
    {'id': 'warm_white', 'label': '暖光极简白'},
    {'id': 'outdoor_bokeh', 'label': '户外虚化自然光'},
    {'id': 'executive_dark', 'label': '高管深色棚拍'},
    {'id': 'startup_clean', 'label': '科技创业极简'},
    {'id': 'finance_formal', 'label': '金融投行正装'},
    {'id': 'consulting_warm', 'label': '咨询顾问暖灰'},
    {'id': 'creative_editorial', 'label': '创意主理人肖像'},
    {'id': 'academic_soft', 'label': '学术研究柔光'},
    {'id': 'social_profile', 'label': '社交平台亲和头像'},
    {'id': 'portrait_id_brand', 'label': '高端美式证件照'},
    {'id': 'celebrity_headshot_square', 'label': CELEBRITY_HEADSHOT_LABEL},
    {'id': 'studio_business_portrait_3x4', 'label': STUDIO_BUSINESS_PORTRAIT_LABEL},
    {'id': 'academic_profile_card_1x1', 'label': ACADEMIC_PROFILE_CARD_LABEL},
]

def get_generation_labels():
    return [option['label'] for option in GENERATION_STYLE_OPTIONS]

def selected_generation_labels(style_config):
    valid_by_id = {option['id']: option['label'] for option in GENERATION_STYLE_OPTIONS}
    requested_ids = []
    if style_config.get('textRegionEnabled') is True:
        requested_ids.append('portrait_id_brand')
    single_style = str(style_config.get('singleStyle') or '').strip()
    if single_style:
        requested_ids.append(single_style)
    combination_styles = style_config.get('combinationStyles')
    if isinstance(combination_styles, list):
        requested_ids.extend(str(style_id).strip() for style_id in combination_styles)
    selected_ids = style_config.get('generationStyles')
    if isinstance(selected_ids, list):
        requested_ids.extend(str(style_id).strip() for style_id in selected_ids)
    if not requested_ids:
        crop = str(style_config.get('crop') or '').strip().lower()
        requested_ids.append('full_body_portrait' if crop in {'full', '全身'} else 'half_body_portrait')
    labels = []
    for style_id in requested_ids:
        label = valid_by_id.get(style_id)
        if label and label not in labels:
            labels.append(label)
    return labels or [HALF_BODY_LABEL]

def clamp_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))

def selected_doubao_model(style_config):
    requested = str(style_config.get('doubaoModel') or '').strip()
    env_model = os.getenv('DOUBAO_MODEL', DOUBAO_DEFAULT_MODEL).strip()
    for candidate in (requested, env_model, DOUBAO_DEFAULT_MODEL):
        if candidate in DOUBAO_MODEL_OPTIONS:
            return candidate
    return DOUBAO_DEFAULT_MODEL

def selected_result_count(style_config):
    if style_config.get('useCustomResultCount') is False:
        return DEFAULT_RESULT_COUNT
    env_default = clamp_int(os.getenv('DOUBAO_MAX_IMAGES'), DEFAULT_RESULT_COUNT, 1, MAX_RESULT_COUNT)
    return clamp_int(style_config.get('resultCount'), env_default, 1, MAX_RESULT_COUNT)

def clean_prompt_text(value, default, max_length=64):
    text = str(value or default).strip()
    text = ' '.join(text.split())
    return text[:max_length] if text else default

def brand_text_config(style_config):
    return {
        'title': clean_prompt_text(style_config.get('mainTitle') or style_config.get('brandTitle'), 'Portrait ID.', 60),
        'subtitle': clean_prompt_text(style_config.get('subtitle') or style_config.get('brandSubtitle'), 'Redefine Identity', 80),
        'meta': clean_prompt_text(style_config.get('infoLine') or style_config.get('brandMeta'), 'SERIES 01 // MINIMALIST', 120),
    }

def option_label(group, value, custom_value=''):
    if value == 'custom':
        return clean_prompt_text(custom_value, '自定义颜色', 40)
    return CLOTHING_OPTIONS[group].get(str(value or '').strip(), '')

def build_prompt_config(style_config):
    jacket_color = option_label('jacket_colors', style_config.get('jacketColorV2'), style_config.get('customJacketColor')) or color_name(style_config.get('jacketColor')) or '深蓝色'
    inner_color = option_label('inner_colors', style_config.get('innerColor'), style_config.get('customInnerColor')) or color_name(style_config.get('shirtColor')) or '白色'
    jacket_style = option_label('jacket_styles', style_config.get('jacketStyleV2')) or style_config.get('jacket') or '商务西装'
    inner_style = option_label('inner_styles', style_config.get('innerStyle')) or style_config.get('shirt') or '衬衫'
    collar_style = option_label('collar_styles', style_config.get('collarStyle')) or '普通领'
    button_state = option_label('button_states', style_config.get('buttonState')) or '解开一个扣子'
    background = option_label('backgrounds', style_config.get('backgroundType'), style_config.get('customBackgroundColor')) or '干净极简的灰色渐变摄影棚背景'
    visual_style = option_label('visual_styles', style_config.get('visualStyle')) or '高端证件照/商务人像风格'
    return {
        'jacket_color': jacket_color,
        'inner_color': inner_color,
        'jacket_style': jacket_style,
        'inner_style': inner_style,
        'collar_style': collar_style,
        'button_state': button_state,
        'background': background,
        'visual_style': visual_style,
    }

def build_style_summary(style_config):
    config = build_prompt_config(style_config)
    parts = [
        f'{config["jacket_color"]}{config["jacket_style"]}',
        f'内衬{config["inner_color"]}{config["inner_style"]}',
        config['background'],
        config['visual_style'],
    ]
    return ' / '.join(p for p in parts if p)

def has_explicit_clothing_config(style_config):
    explicit_keys = {
        'jacketColorV2',
        'customJacketColor',
        'jacketStyleV2',
        'innerColor',
        'customInnerColor',
        'innerStyle',
        'collarStyle',
        'buttonState',
    }
    return any(str(style_config.get(key) or '').strip() for key in explicit_keys)

def is_no_jacket_intent(style_config, config):
    if config.get('jacket_style') == '__NO_JACKET__':
        return True
    raw_jacket = str(style_config.get('jacket') or '').strip()
    return any(token in raw_jacket for token in ('仅衬衫', '无外套', '只穿', 'shirt only'))

def describe_inner_color(config):
    color = config.get('inner_color') or ''
    if color == '__ORIGINAL__':
        return ''
    return color

def describe_inner_clause(config):
    color = describe_inner_color(config)
    inner_style = config.get('inner_style') or '衬衫'
    if color:
        return f'{color}的{inner_style}'
    return f'{inner_style}（颜色保持与上传照片中的内搭一致）'

def build_clothing_phrase(config, style_config):
    generation_styles = style_config.get('generationStyles') if isinstance(style_config.get('generationStyles'), list) else []
    is_celebrity = (
        style_config.get('singleStyle') == 'celebrity_headshot_square'
        or 'celebrity_headshot_square' in generation_styles
    )
    if not has_explicit_clothing_config(style_config) and is_celebrity:
        return '服装为黑色西装或高级礼服，质感高级，剪裁利落，适合明星宣传照。'

    inner_clause = describe_inner_clause(config)
    inner_style = config.get('inner_style') or '衬衫'

    if is_no_jacket_intent(style_config, config):
        return (
            f'人物上身仅穿{inner_clause}，没有外套、没有西装、没有外搭，'
            f'画面中清晰可见{inner_style}本身的版型、领口与质感；'
            f'扣子状态为{config["button_state"]}。'
        )

    return (
        f'服装为{config["jacket_color"]}的{config["jacket_style"]}，'
        f'内衬为{inner_clause}，'
        f'领型为{config["collar_style"]}，扣子状态为{config["button_state"]}。'
    )

def ensure_user_credits(conn, user_id):
    row = conn.execute('SELECT balance FROM user_credits WHERE user_id = ?', (user_id,)).fetchone()
    if row:
        return int(row['balance'])
    now = int(time.time() * 1000)
    conn.execute(
        'INSERT INTO user_credits (user_id, balance, updated_at) VALUES (?, ?, ?)',
        (user_id, LOCAL_INITIAL_CREDITS, now),
    )
    conn.execute(
        '''INSERT INTO credit_transactions
           (id, user_id, amount, reason, batch_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        ('txn_' + uuid.uuid4().hex[:12], user_id, LOCAL_INITIAL_CREDITS, 'initial_grant', None, now),
    )
    return LOCAL_INITIAL_CREDITS

def mime_type_for(path):
    ext = path.suffix.lower()
    if ext in {'.jpg', '.jpeg'}:
        return 'image/jpeg'
    if ext == '.png':
        return 'image/png'
    if ext == '.webp':
        return 'image/webp'
    if ext == '.heic':
        return 'image/heic'
    return 'application/octet-stream'

def image_to_base64(path, input_format=None):
    encoded = base64.b64encode(path.read_bytes()).decode('ascii')
    selected_format = (input_format or os.getenv('DOUBAO_INPUT_IMAGE_FORMAT', 'data_url')).lower()
    if selected_format == 'data_url':
        return f'data:{mime_type_for(path)};base64,{encoded}'
    return encoded

def validate_public_image_url(image_url):
    parsed = urlparse(image_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise RuntimeError('豆包参考图地址无效：image 必须是可公开访问的 http/https URL。')
    if parsed.hostname in {'localhost', '127.0.0.1', '0.0.0.0'}:
        raise RuntimeError('豆包无法访问本机地址。请配置 DOUBAO_PUBLIC_BASE_URL 为公网 HTTPS 地址。')
    return image_url

def public_image_check_timeout():
    try:
        return max(1, int(os.getenv('DOUBAO_PUBLIC_URL_CHECK_TIMEOUT_SECONDS', '10')))
    except ValueError:
        return 10

def public_image_check_attempts():
    try:
        return max(1, int(os.getenv('DOUBAO_PUBLIC_URL_CHECK_ATTEMPTS', '6')))
    except ValueError:
        return 6

def ensure_public_image_accessible(image_url):
    if os.getenv('DOUBAO_SKIP_PUBLIC_IMAGE_CHECK', 'false').lower() == 'true':
        return

    last_error = None
    attempts = public_image_check_attempts()
    for attempt in range(attempts):
        for method, headers in (
            ('HEAD', {'User-Agent': 'AuralisHeadshot/1.0'}),
            ('GET', {'User-Agent': 'AuralisHeadshot/1.0', 'Range': 'bytes=0-0'}),
        ):
            req = Request(image_url, headers=headers, method=method)
            try:
                with urlopen(req, timeout=public_image_check_timeout()) as response:
                    content_type = response.headers.get('Content-Type', '')
                    if content_type and not content_type.startswith('image/'):
                        raise RuntimeError(
                            f'豆包参考图公网地址返回的不是图片（Content-Type: {content_type}）：{image_url}'
                        )
                    return
            except HTTPError as error:
                last_error = error
                if error.code not in {404, 429, 500, 502, 503, 504}:
                    break
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
                continue
        if isinstance(last_error, HTTPError) and last_error.code not in {404, 429, 500, 502, 503, 504}:
            break
        if attempt == attempts - 1:
            break
        time.sleep(0.8)

    if isinstance(last_error, HTTPError):
        raise RuntimeError(
            f'豆包参考图公网地址不可访问（HTTP {last_error.code}）：{image_url}。'
            '如果刚上传后立刻生成，请稍等几秒重试；如果仍失败，请确认 ngrok 正在运行且指向当前 Flask 端口。'
        ) from last_error
    if isinstance(last_error, URLError):
        raise RuntimeError(
            f'豆包参考图公网地址连接失败：{image_url}。'
            f'原因：{last_error.reason}。'
        ) from last_error
    if last_error:
        raise RuntimeError(
            f'豆包参考图公网地址连接超时或不可访问：{image_url}。'
            '如果刚上传后立刻生成，请稍等几秒重试；如果仍失败，请确认 ngrok 正在运行且指向当前 Flask 端口。'
        ) from last_error

def resolve_reference_image_url(source_path):
    override_url = os.getenv('DOUBAO_REFERENCE_IMAGE_URL')
    if override_url:
        image_url = validate_public_image_url(override_url)
        ensure_public_image_accessible(image_url)
        return image_url

    public_base_url = os.getenv('DOUBAO_PUBLIC_BASE_URL', '').strip().rstrip('/')
    if not public_base_url:
        raise RuntimeError(
            '豆包图生图接口要求 image 是公网图片 URL。请先用 ngrok/线上域名暴露本站，'
            '并在 .env 中设置 DOUBAO_PUBLIC_BASE_URL。'
        )

    image_url = validate_public_image_url(public_base_url + photo_public_url(source_path))
    ensure_public_image_accessible(image_url)
    return image_url

def resolve_reference_image_input(source_path):
    input_format = os.getenv('DOUBAO_INPUT_IMAGE_FORMAT', 'data_url').strip().lower()
    if input_format in {'base64', 'data_url'}:
        return image_to_base64(source_path)
    if input_format == 'url':
        return resolve_reference_image_url(source_path)
    raise RuntimeError('DOUBAO_INPUT_IMAGE_FORMAT 仅支持 base64、data_url 或 url。')

def build_headshot_prompt(label, style_config):
    config = build_prompt_config(style_config)
    custom_prompt = clean_prompt_text(style_config.get('customPrompt'), '', 1000)
    custom_prompt_text = f'用户选择的提示词模板要求：{custom_prompt}。' if custom_prompt else ''
    if label in {HALF_BODY_LABEL, FULL_BODY_LABEL, BRAND_TEXT_LABEL, HALF_TEXT_LABEL}:
        is_full = label in {FULL_BODY_LABEL, BRAND_TEXT_LABEL}
        needs_text_area = label in {BRAND_TEXT_LABEL, HALF_TEXT_LABEL}
        crop_text = '完整全身职业形象照' if is_full else '半身职业头像'
        text_area = (
            '构图需要在画面左下角或底部安全区域预留干净留白，供程序后期叠加品牌文字水印；'
            if needs_text_area else
            '构图不需要预留文字区域，画面保持干净完整；'
        )
        full_body_text = ''
        if is_full:
            pants = style_config.get('pants') or '标准微直筒西裤'
            pants_color = color_name(style_config.get('pantsColor')) or '深海军蓝'
            shoes = style_config.get('shoes') or '牛津皮鞋'
            shoes_color = color_name(style_config.get('shoesColor')) or '棕色'
            full_body_text = (
                f'下装为{pants_color}{pants}，鞋履为{shoes_color}{shoes}。'
                '必须生成从头顶到鞋底完整入镜的站立全身照，脚和鞋子必须完整可见，脚底下方保留少量地面留白；'
                '禁止裁切到膝盖、小腿、脚踝或鞋子，禁止半身、七分身、近景头像或腰部以上构图。'
                '人物完整体态比例自然修长，画幅优先使用竖版 2:3 或 3:4 全身摄影构图。'
            )
        return (
            f'将用户上传的人像照片生成{crop_text}。'
            '保持人物面部身份、五官特征、表情、头部角度、姿势、人物比例和整体气质一致。'
            f'将原始复杂背景替换为{config["background"]}，背景必须干净、极简、高级，突出人物主体，'
            '人物与背景之间有清晰分离感，背景不能抢视觉焦点。'
            f'{build_clothing_phrase(config, style_config)}'
            f'{full_body_text}'
            f'{custom_prompt_text}'
            '使用专业棚拍灯光，高对比度但不过曝；进行专业人像修复和色彩分级，'
            '改善肤色和皮肤纹理，使皮肤自然、干净、均匀、细腻、有轻微光泽，'
            '去除明显瑕疵和色差，增强五官立体感和脸部轮廓。'
            f'{text_area}'
            f'整体风格为{config["visual_style"]}，高级、克制、专业，适合商务展示和个人品牌。'
            '不要在图片中生成任何文字、字母、数字、logo、水印、签名或排版元素；最终水印会由程序后期叠加。'
        )
    if label == CELEBRITY_HEADSHOT_LABEL:
        return (
            '将用户上传的人像照片生成 1:1 明星经纪公司签约头像，风格为好莱坞经纪公司标准宣传照。'
            '保持人物面部身份、五官特征、表情、头部角度和整体气质一致，不改变人物身份。'
            '人物应具备高级审美、商业价值和强个人品牌吸引力，兼具商业价值与高级气质。'
            '服装为黑色西装或高级礼服，质感高级，剪裁利落。'
            '背景为高级灰影棚（深灰至中灰渐变），干净、极简、有层次，带轻微渐变和暗角，突出人物主体；'
            '不能出现原始杂乱环境，背景不能抢人物主体。'
            '使用专业棚拍灯光，强调脸部立体感和轮廓光，进行专业人像修复和色彩分级，'
            '让皮肤自然、干净、均匀、细腻、有轻微光泽，去除明显瑕疵和色差，但保持人物真实可识别。'
            '整体画面高级、克制、有明星感，适合个人品牌头像和签约宣传照。'
            '画幅严格为 1:1 正方形比例。'
            # —— 关键构图指令：人物靠右，左侧 35-40% 留空给本地水印 ——
            '人物必须明显偏向画面右侧呈现，人物（脸部、肩膀、躯干）的水平中心线大约位于画面横向 65-70% 的位置，'
            '人物不要居中，画面左侧约 35-40% 必须保留为干净的影棚灰色背景空白区域，无任何元素干扰。'
            '该左侧空白区域将由系统在本地后期叠加用户的姓名和署名英文文字排版，AI 模型只需输出人物本身。'
            '禁止在图片任何位置生成文字、字母、数字、logo、水印、签名或排版元素；所有文字都会由程序在本地后期叠加。'
        )
    if label == STUDIO_BUSINESS_PORTRAIT_LABEL:
        return (
            f'{custom_prompt_text}'
            '严格按照用户选择的男士版或女士版工作室职业肖像提示词生成。'
            '画面必须是经典头肩肖像构图，从胸口到头部入镜，人物居中，画幅比例 3:4。'
            '必须保留同一人物身份，不要改变脸型、五官比例、年龄感、发际线、发型特征和真实皮肤质感。'
            '背景为无文字、无 logo、无杂物的中灰色到暖灰褐色渐变工作室背景。'
            '不要在图片中生成任何文字、字母、数字、logo、水印、签名或排版元素；最终水印会由程序后期叠加。'
        )
    if label == ACADEMIC_PROFILE_CARD_LABEL:
        return (
            '将用户上传的人像照片生成正式商务档案头像海报，风格参考大学官网个人档案 / 学术会议人物介绍卡片 / 高端企业头像海报。'
            '保留人物真实身份特征、五官比例、脸型基础和整体气质，不要改变人物身份。'
            '可以适度优化面部轮廓，使脸部线条更清晰、自然、上镜；平滑皮肤质感，减少瑕疵、暗沉和肤色不均，'
            '但不要过度磨皮，保留真实皮肤细节和自然光影。'
            '请根据人物脸型设计一款匹配的商务发型，干净利落、成熟专业、适合正式场合，发丝自然、有层次，不夸张。'
            '请为人物搭配正式商务穿搭：深色西装外套、白色或浅色衬衫，可搭配领带；整体造型高级、简洁、专业、可信赖。'
            '画面为正面半身肖像，人物居中，直视镜头，表情自然自信。'
            '背景必须是纯白色 (#FFFFFF) 素色背景，无任何纹理、阴影、渐变或晕影。'
            '光线柔和均匀，类似专业证件照、企业头像摄影。整体风格干净、正式、商务、高清写实摄影质感。'
            '画幅严格为 1:1 正方形比例。'
            # —— 关键留白指令 ——
            '人物必须只占据画面上半部分，从画面顶部约 5% 处开始，人物（包括头发顶部）到肩膀/胸口结束于画面 70% 高度处。'
            '画面下方 30% 必须是绝对干净的纯白空白区域，禁止在该区域生成任何元素：'
            '不要文字、不要英文、不要中文、不要数字、不要 logo、不要装饰线条、不要色块、不要阴影、不要署名、不要二维码、不要任何排版痕迹。'
            '该底部空白区域将由系统在本地后期叠加用户的姓名、职位、机构等排版文字，AI 模型只需输出干净的人像照片即可。'
            '再次强调：禁止在图片任何位置生成文字、字母、数字、logo、水印、签名或排版元素；所有文字都会由程序在本地后期叠加。'
        )
    full_body_text = ''
    if str(style_config.get('crop') or '').strip().lower() in {'full', '全身'}:
        pants = style_config.get('pants') or '标准微直筒西裤'
        pants_color = color_name(style_config.get('pantsColor')) or '深海军蓝'
        shoes = style_config.get('shoes') or '牛津皮鞋'
        shoes_color = color_name(style_config.get('shoesColor')) or '棕色'
        full_body_text = (
            f' 必须生成从头顶到鞋底完整入镜的竖版全身站立照，下装为{pants_color}{pants}，鞋履为{shoes_color}{shoes}。'
            '脚、鞋子和脚底下方少量地面留白必须完整可见；禁止裁切到膝盖、小腿、脚踝或鞋子，禁止半身、七分身或腰部以上构图。'
        )
    return (
        '将用户上传的人像照片处理成高端商务证件照/品牌人像。'
        '保持人物面部身份、五官特征、表情、头部角度、姿势和人物比例一致。'
        f'{custom_prompt_text}'
        f'将原始复杂背景替换为{config["background"]}，背景风格为干净、极简、高级，突出人物主体；'
        '人物和背景之间要有清晰分离感，背景不要抢人物视觉焦点。'
        f'{build_clothing_phrase(config, style_config)}'
        f'{full_body_text}'
        '进行专业人像修复，改善肤色和皮肤纹理，使皮肤光滑、均匀、干净、有高级质感，'
        '但保持人物真实自然、可识别。去除明显瑕疵和色差，增强面部立体感，进行专业色彩分级，'
        f'使整体画面统一、柔和、高级。整体风格为{config["visual_style"]}，'
        f'本张变体风格为{label}。'
        '不要在图片中生成任何文字、字母、数字、logo、水印、签名或排版元素；最终水印会由程序后期叠加。'
    )

def color_name(value):
    if not value:
        return ''
    names = {
        'navy': '深海军蓝',
        'charcoal': '炭灰色',
        'ivory': '象牙白',
        'khaki': '卡其色',
        'white': '白色',
        'lightblue': '浅蓝色',
        'black': '黑色',
        'brown': '棕色',
        'gray': '灰色',
        'lightgray': '浅灰色',
        'blue': '蓝色',
        'deep_brown': '深棕色',
    }
    return names.get(str(value).strip().lower(), str(value).strip())

def save_remote_image(image_url, target_path):
    req = Request(image_url, headers={'User-Agent': 'AuralisHeadshot/1.0'})
    with urlopen(req, timeout=120) as response:
        target_path.write_bytes(response.read())

def save_b64_image(b64_json, target_path):
    if ',' in b64_json and b64_json.startswith('data:'):
        b64_json = b64_json.split(',', 1)[1]
    target_path.write_bytes(base64.b64decode(b64_json))

def response_image_items(payload):
    if isinstance(payload.get('data'), list):
        return payload['data']
    if isinstance(payload.get('images'), list):
        return payload['images']
    if isinstance(payload.get('result'), dict) and isinstance(payload['result'].get('data'), list):
        return payload['result']['data']
    return []

def build_headshot_set_prompt(labels, style_config):
    unique_labels = list(dict.fromkeys(labels))
    if len(unique_labels) == 1:
        prompt = build_headshot_prompt(unique_labels[0], style_config)
        framing = '画幅与构图：竖构图3:4比例，半身像取景（含肩部及胸口领口），禁止横构图，禁止只露脸特写。'
        if len(labels) > 1:
            return (
                f'{prompt}{framing}'
                f'请生成 {len(labels)} 张变体，每张背景、构图和光线需有可辨识差异，但人物身份必须保持一致。'
            )
        return f'{prompt}{framing}'
    style_summary = build_style_summary(style_config)
    celebrity_extra = ''
    if CELEBRITY_HEADSHOT_LABEL in unique_labels:
        celebrity_extra = (
            f'其中”{CELEBRITY_HEADSHOT_LABEL}”这一张：'
            f'{build_headshot_prompt(CELEBRITY_HEADSHOT_LABEL, style_config)}'
        )
    labels_str = '、'.join(unique_labels)
    return (
        f'基于用户上传的真实自拍，一次生成 {len(unique_labels)} 张职业形象照，分别对应：{labels_str}。'
        f'用户选择与场景摘要：{style_summary}。'
        '画幅与构图：竖构图3:4比例，标准职业形象照取景，半身像（含肩部及胸口领口），人物居中，头顶留出适当 headroom，禁止横构图，禁止只露脸特写。'
        '必须保持人物身份、五官比例、脸型、发际线、发型和年龄观感一致，不换脸，不改变性别，不生成多人。'
        '每张图需要有明显不同的背景、构图、光线和职场氛围，但人物本人必须一致。'
        '摄影标准：真实商业棚拍质感，85mm人像镜头效果，自然皮肤纹理，眼神清晰，发丝边缘干净，服装合身，背景简洁高级。'
        '输出质量：高清、无水印、无文字、无Logo、无畸变、无塑料皮肤、不过度磨皮、不过度锐化。'
        f'{celebrity_extra}'
    )

def generated_item_to_bytes(item):
    image_url = item.get('url') or item.get('image_url')
    b64_json = item.get('b64_json') or item.get('base64')
    if image_url:
        req = Request(image_url, headers={'User-Agent': 'AuralisHeadshot/1.0'})
        with urlopen(req, timeout=120) as response:
            content_type = response.headers.get('Content-Type', 'image/jpeg')
            content_length = response.headers.get('Content-Length')
            if content_length and int(content_length) > MAX_GENERATED_IMAGE_BYTES:
                raise RuntimeError('生成图片超过大小限制。')
            data = response.read(MAX_GENERATED_IMAGE_BYTES + 1)
        validate_generated_image_bytes(data, content_type)
        return data, content_type
    if b64_json:
        if ',' in b64_json and b64_json.startswith('data:'):
            b64_json = b64_json.split(',', 1)[1]
        data = base64.b64decode(b64_json)
        validate_generated_image_bytes(data, 'image/jpeg')
        return data, 'image/jpeg'
    raise RuntimeError('豆包接口返回中没有 url 或 b64_json。')

def extension_for_content_type(content_type, fallback_ext='.jpg'):
    content_type = (content_type or '').lower()
    if 'png' in content_type:
        return '.png'
    if 'webp' in content_type:
        return '.webp'
    if 'jpeg' in content_type or 'jpg' in content_type:
        return '.jpg'
    return fallback_ext if fallback_ext.startswith('.') else f'.{fallback_ext}'

def parse_hex_color(value, default):
    raw = str(value or '').strip()
    if raw.startswith('#') and len(raw) in {4, 7}:
        try:
            if len(raw) == 4:
                return tuple(int(raw[i] * 2, 16) for i in range(1, 4))
            return tuple(int(raw[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return default
    return default

def load_font(size, bold=False):
    candidates = [FONT_CJK_BOLD if bold else FONT_CJK, FONT_BOLD if bold else FONT_REGULAR, FONT_REGULAR]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()

def draw_letter_spaced(draw, position, text, font, fill, spacing):
    x, y = position
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        bbox = draw.textbbox((x, y), char, font=font)
        x += (bbox[2] - bbox[0]) + spacing

def text_width(draw, text, font, spacing=0):
    if spacing <= 0:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    total = 0
    for index, char in enumerate(text):
        bbox = draw.textbbox((0, 0), char, font=font)
        total += bbox[2] - bbox[0]
        if index < len(text) - 1:
            total += spacing
    return total

def fit_font(draw, text, preferred_size, max_width, bold=False, spacing=0, min_size=12):
    size = preferred_size
    while size > min_size:
        font = load_font(size, bold=bold)
        if text_width(draw, text, font, spacing) <= max_width:
            return font
        size -= 1
    return load_font(min_size, bold=bold)

def apply_celebrity_card_layout(image_path, style_config):
    """Preset 1 — 明星级 1:1：左侧垂直居中白色姓名 + 横线 + 副标题。"""
    name = clean_prompt_text(
        style_config.get('cardName') or style_config.get('mainTitle') or style_config.get('brandTitle'),
        '唐臻卓',
        40,
    )
    subtitle = clean_prompt_text(
        style_config.get('cardSubtitle') or style_config.get('subtitle') or style_config.get('brandSubtitle'),
        'DA / Amazon',
        80,
    )

    with Image.open(image_path) as image:
        image = image.convert('RGB')
        width, height = image.size

        # 文本块定位在画面左侧 12% 处（从左边距开始）
        text_x = int(width * 0.07)
        text_block_max_w = int(width * 0.30)
        block_center_y = int(height * 0.50)

        name_size = max(48, int(width * 0.075))
        subtitle_size = max(20, int(width * 0.028))

        draw = ImageDraw.Draw(image)
        name_font = fit_font(draw, name, name_size, text_block_max_w, bold=False, min_size=32)
        subtitle_font = fit_font(draw, subtitle, subtitle_size, text_block_max_w, bold=False, min_size=16)

        name_bbox = draw.textbbox((0, 0), name, font=name_font)
        sub_bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
        name_h = name_bbox[3] - name_bbox[1]
        sub_h = sub_bbox[3] - sub_bbox[1]
        line_gap = max(14, int(height * 0.018))
        sub_gap = max(8, int(height * 0.012))
        line_w = max(int(width * 0.16), name_bbox[2] - name_bbox[0])
        line_thickness = max(1, int(height * 0.0015))

        block_h = name_h + line_gap + line_thickness + sub_gap + sub_h
        y = block_center_y - block_h // 2 - name_bbox[1]

        white = (240, 240, 240)
        draw.text((text_x, y), name, font=name_font, fill=white)
        y += name_h + line_gap
        draw.line([(text_x, y), (text_x + line_w, y)], fill=white, width=line_thickness)
        y += line_thickness + sub_gap
        draw.text((text_x, y), subtitle, font=subtitle_font, fill=white)

        image.save(image_path, quality=96)


def apply_academic_card_layout(image_path, style_config):
    """Preset 3 — 商务档案海报 1:1：底部白条 + 左侧蓝色竖条 + 蓝色姓名 + 黑色职位/机构。"""
    name = clean_prompt_text(
        style_config.get('cardName') or style_config.get('mainTitle') or style_config.get('brandTitle'),
        'Chris Tang',
        60,
    )
    title = clean_prompt_text(
        style_config.get('cardTitle') or style_config.get('subtitle') or style_config.get('brandSubtitle'),
        'Senior Graphic Designer',
        80,
    )
    org = clean_prompt_text(
        style_config.get('cardOrg') or style_config.get('infoLine') or style_config.get('brandMeta'),
        'International Design Institute',
        100,
    )
    accent = parse_hex_color(style_config.get('cardAccentColor'), (24, 67, 195))
    name_color = accent
    body_color = (28, 32, 40)

    with Image.open(image_path) as image:
        image = image.convert('RGB')
        width, height = image.size

        # 底部白条 28%
        strip_h = int(height * 0.28)
        strip_y = height - strip_h

        draw = ImageDraw.Draw(image)
        # 强制底部白色（保险，假如 AI 残留少量阴影）
        draw.rectangle([(0, strip_y), (width, height)], fill=(255, 255, 255))

        margin_x = int(width * 0.06)
        bar_w = max(4, int(width * 0.005))
        bar_top = strip_y + int(strip_h * 0.18)
        bar_bottom = strip_y + int(strip_h * 0.82)
        draw.rectangle([(margin_x, bar_top), (margin_x + bar_w, bar_bottom)], fill=accent)

        text_x = margin_x + bar_w + int(width * 0.025)
        max_text_w = width - text_x - margin_x

        name_size = max(40, int(width * 0.075))
        body_size = max(18, int(width * 0.028))

        name_font = fit_font(draw, name, name_size, max_text_w, bold=True, min_size=28)
        title_font = fit_font(draw, title, body_size, max_text_w, bold=False, min_size=14)
        org_font = fit_font(draw, org, body_size, max_text_w, bold=False, min_size=14)

        name_bbox = draw.textbbox((0, 0), name, font=name_font)
        title_bbox = draw.textbbox((0, 0), title, font=title_font)
        org_bbox = draw.textbbox((0, 0), org, font=org_font)
        name_h = name_bbox[3] - name_bbox[1]
        title_h = title_bbox[3] - title_bbox[1]
        org_h = org_bbox[3] - org_bbox[1]
        gap_main = max(10, int(strip_h * 0.08))
        gap_minor = max(4, int(strip_h * 0.04))
        block_h = name_h + gap_main + title_h + gap_minor + org_h
        y = strip_y + (strip_h - block_h) // 2 - name_bbox[1]

        draw.text((text_x, y), name, font=name_font, fill=name_color)
        y += name_h + gap_main
        draw.text((text_x, y), title, font=title_font, fill=body_color)
        y += title_h + gap_minor
        draw.text((text_x, y), org, font=org_font, fill=body_color)

        image.save(image_path, quality=96)


def apply_brand_watermark(image_path, style_config, label=None, text_override=None):
    if label == ACADEMIC_PROFILE_CARD_LABEL:
        apply_academic_card_layout(image_path, style_config)
        return
    if label == CELEBRITY_HEADSHOT_LABEL:
        apply_celebrity_card_layout(image_path, style_config)
        return
    if style_config.get('watermarkEnabled') is False:
        return

    text = text_override if text_override is not None else brand_text_config(style_config)
    if not any(text.values()):
        return

    with Image.open(image_path) as image:
        image = image.convert('RGB')
        width, height = image.size
        margin_x = int(width * 0.05)
        margin_y = int(height * 0.05)
        max_text_width = width - margin_x * 2
        title_size = max(24, int(width * 0.038))
        subtitle_size = max(16, int(width * 0.022))
        info_size = max(11, int(width * 0.014))
        title_color = parse_hex_color(style_config.get('watermarkTitleColor'), (26, 44, 255))
        subtitle_color = parse_hex_color(style_config.get('watermarkSubtitleColor'), (238, 240, 244))
        info_color = parse_hex_color(style_config.get('watermarkInfoColor'), (196, 200, 208))
        draw = ImageDraw.Draw(image)
        title_font = fit_font(draw, text['title'], title_size, max_text_width, bold=True, min_size=18)
        subtitle_font = fit_font(draw, text['subtitle'], subtitle_size, max_text_width, min_size=14)
        info_spacing = max(1, int(width * 0.0025))
        info_font = fit_font(draw, text['meta'], info_size, max_text_width, spacing=info_spacing, min_size=10)

        title_bbox = draw.textbbox((0, 0), text['title'], font=title_font)
        subtitle_bbox = draw.textbbox((0, 0), text['subtitle'], font=subtitle_font)
        info_bbox = draw.textbbox((0, 0), text['meta'], font=info_font)
        title_h = title_bbox[3] - title_bbox[1]
        subtitle_h = subtitle_bbox[3] - subtitle_bbox[1]
        info_h = info_bbox[3] - info_bbox[1]
        gap = max(8, int(height * 0.01))
        block_h = title_h + subtitle_h + info_h + gap * 2
        position = str(style_config.get('watermarkPosition') or 'bottom_left')
        align = str(style_config.get('watermarkAlign') or 'left')

        x = margin_x
        y = height - margin_y - block_h
        if 'right' in position:
            max_w = max(title_bbox[2] - title_bbox[0], subtitle_bbox[2] - subtitle_bbox[0], info_bbox[2] - info_bbox[0])
            x = width - margin_x - max_w
        if 'top' in position:
            y = margin_y

        def aligned_x(bbox):
            if align != 'right':
                return x
            max_w = max(title_bbox[2] - title_bbox[0], subtitle_bbox[2] - subtitle_bbox[0], info_bbox[2] - info_bbox[0])
            return x + max_w - (bbox[2] - bbox[0])

        draw.text((aligned_x(title_bbox), y), text['title'], font=title_font, fill=title_color)
        y += title_h + gap
        draw.text((aligned_x(subtitle_bbox), y), text['subtitle'], font=subtitle_font, fill=subtitle_color)
        y += subtitle_h + gap
        draw_letter_spaced(draw, (aligned_x(info_bbox), y), text['meta'], info_font, info_color, info_spacing)
        image.save(image_path, quality=96)


def render_branded_image_bytes(master_path, style_config, label=None, text_override=None):
    """从干净母片合成一份带信息水印的图片，返回 (BytesIO, suffix)。"""
    master = Path(master_path)
    suffix = master.suffix.lower() or '.jpg'
    temp_path = master.with_name(master.stem + '__tmp_brand_' + uuid.uuid4().hex[:8] + suffix)
    shutil.copyfile(master, temp_path)
    try:
        apply_brand_watermark(temp_path, style_config, label=label, text_override=text_override)
        data = temp_path.read_bytes()
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    return io.BytesIO(data), suffix


def crop_to_square(image_path):
    with Image.open(image_path) as image:
        image = image.convert('RGB')
        width, height = image.size
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        image.crop((left, top, left + side, top + side)).save(image_path, quality=96)

def _classify_doubao_error(status_code, details):
    import re
    match = re.search(r'"code"\s*:\s*"([^"]+)"', str(details))
    code = match.group(1) if match else 'Unknown'
    messages = {
        'AccountOverdueError': ('图像生成服务暂时不可用（服务账户异常），我们已收到通知正在处理，预计稍后恢复。如急用请联系客服。', False),
        'InvalidApiKey': ('图像生成服务凭据失效，管理员正在更新，请稍后重试或联系客服。', False),
        'AuthenticationError': ('图像生成服务凭据失效，管理员正在更新，请稍后重试或联系客服。', False),
        'RateLimitExceeded': ('当前生成量已饱和，请 1–3 分钟后再来一次。', True),
        'QuotaExceeded': ('当前生成量已饱和，请 1–3 分钟后再来一次。', True),
        'ModelAccessDenied': ('所选风格暂未开放，请换一个风格再试。', True),
    }
    if code in messages:
        msg, retryable = messages[code]
    else:
        msg, retryable = '图像生成服务暂时不稳定，请稍后重试。', True
    return code, msg, retryable

class DoubaoGenerationHTTPError(RuntimeError):
    def __init__(self, status_code, details):
        self.status_code = status_code
        self.details = details
        self.error_code, self.friendly_message, self.retryable = _classify_doubao_error(status_code, details)
        super().__init__(f'豆包生成接口返回 {status_code}: {details[:300]}')

def is_reference_url_download_timeout(details):
    text = str(details or '').lower()
    return 'timeout while downloading url' in text or ('download' in text and 'timeout' in text)

def post_doubao_generation(api_url, api_key, body):
    req = Request(
        api_url,
        data=json.dumps(body).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )
    try:
        with urlopen(req, timeout=int(os.getenv('DOUBAO_TIMEOUT_SECONDS', '180'))) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        details = error.read().decode('utf-8', errors='replace')
        app.logger.error('Doubao image generation failed: status=%s body=%s', error.code, details[:1000])
        raise DoubaoGenerationHTTPError(error.code, details) from error
    except URLError as error:
        app.logger.error('Doubao image generation connection failed: %s', error.reason)
        raise RuntimeError(f'无法连接豆包生成接口: {error.reason}') from error

def call_doubao_seedream(source_path, labels, style_config):
    api_key = os.getenv('DOUBAO_API_KEY') or os.getenv('ARK_API_KEY')
    if not api_key:
        raise RuntimeError('缺少 DOUBAO_API_KEY，请先在 .env 中配置豆包 API Key。')

    api_url = os.getenv('DOUBAO_API_URL', DOUBAO_DEFAULT_API_URL)
    base_url = os.getenv('DOUBAO_BASE_URL')
    if base_url and not os.getenv('DOUBAO_API_URL'):
        api_url = base_url.rstrip('/') + '/images/generations'

    model = selected_doubao_model(style_config)
    input_format = os.getenv('DOUBAO_INPUT_IMAGE_FORMAT', 'data_url').strip().lower()
    image_value = resolve_reference_image_input(source_path)
    max_images = selected_result_count(style_config)
    body = {
        'model': model,
        'prompt': build_headshot_set_prompt(labels[:max_images], style_config),
        'image': image_value,
        'size': os.getenv('DOUBAO_FULL_BODY_IMAGE_SIZE' if str(style_config.get('crop') or '').strip().lower() in {'full', '全身'} else 'DOUBAO_IMAGE_SIZE', '2K'),
        'sequential_image_generation': 'auto',
        'sequential_image_generation_options': {
            'max_images': max_images,
        },
        'stream': False,
        'response_format': os.getenv('DOUBAO_RESPONSE_FORMAT', 'url'),
        'watermark': False,
    }

    try:
        payload = post_doubao_generation(api_url, api_key, body)
    except DoubaoGenerationHTTPError as error:
        if input_format != 'url' or error.status_code != 400 or not is_reference_url_download_timeout(error.details):
            raise
        app.logger.warning('Doubao could not download reference URL; retrying with data URL input.')
        body['image'] = image_to_base64(source_path, input_format='data_url')
        payload = post_doubao_generation(api_url, api_key, body)

    items = response_image_items(payload)
    if not items:
        app.logger.error('Doubao image generation returned no image items: %s', json.dumps(payload, ensure_ascii=False)[:1000])
        raise RuntimeError('豆包接口未返回图片数据。')

    assets = []
    for item in items[:max_images]:
        data, content_type = generated_item_to_bytes(item)
        assets.append({
            'data': data,
            'content_type': content_type,
            'model': model,
        })
    return assets

def copy_mock_image(source_path, target_path):
    shutil.copyfile(source_path, target_path)

def generate_image_assets(source_path, labels, style_config):
    provider = os.getenv('IMAGE_GENERATION_PROVIDER', 'mock').lower()
    if provider == 'doubao':
        try:
            return 'doubao', call_doubao_seedream(source_path, labels, style_config)
        except Exception:
            if os.getenv('DOUBAO_ALLOW_MOCK_FALLBACK', 'false').lower() != 'true':
                raise
    return 'mock', [{'source_path': source_path, 'content_type': mime_type_for(source_path), 'model': 'mock'} for _ in labels]

def generate_batch_assets(batch_id):
    conn = get_db()
    batch = conn.execute('SELECT * FROM generation_batches WHERE id = ?', (batch_id,)).fetchone()
    if not batch:
        conn.close()
        return
    try:
        user_id = batch['user_id']
        photo_ids = json.loads(batch['source_photo_ids'] or '[]')
        style_config = json.loads(batch['style_config'] or '{}')
        placeholders = ','.join('?' for _ in photo_ids)
        rows = conn.execute(
            f'SELECT * FROM uploaded_photos WHERE user_id = ? AND id IN ({placeholders})',
            [user_id] + photo_ids,
        ).fetchall() if photo_ids else []
        if not rows:
            raise RuntimeError('没有找到可用于生成的上传照片。')

        source_path = Path(rows[0]['file_path'])
        if not source_path.exists():
            raise RuntimeError('源照片文件已丢失，请重新上传。')

        requested_labels = selected_generation_labels(style_config)
        result_count = selected_result_count(style_config)
        labels = requested_labels * result_count if len(requested_labels) == 1 else requested_labels[:result_count]
        provider, generated_assets = generate_image_assets(source_path, labels, style_config)
        if len(generated_assets) < len(labels):
            raise RuntimeError('豆包接口返回图片数量不足，请重试。')

        now = int(time.time() * 1000)
        ext = source_path.suffix.lower() or '.jpg'
        for index, (label, asset) in enumerate(zip(labels, generated_assets), start=1):
            image_id = f'{batch_id}_{index}'
            asset_ext = extension_for_content_type(asset.get('content_type'), ext)
            target_path = GENERATED_DIR / f'{image_id}{asset_ext}'
            if provider == 'mock':
                copy_mock_image(asset['source_path'], target_path)
            else:
                target_path.write_bytes(asset['data'])
            if label == CELEBRITY_HEADSHOT_LABEL:
                crop_to_square(target_path)
            master_path = GENERATED_DIR / f'{image_id}__master{asset_ext}'
            shutil.copyfile(target_path, master_path)
            apply_brand_watermark(target_path, style_config, label=label)
            public_url = photo_public_url(target_path)
            conn.execute(
                '''INSERT INTO generated_images
                   (id, batch_id, label, file_path, public_url, created_at, master_file_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (image_id, batch_id, label, str(target_path), public_url, now, str(master_path)),
            )

        conn.execute('UPDATE generation_batches SET status = ? WHERE id = ?', ('preview', batch_id))
        conn.commit()
    except Exception as error:
        app.logger.exception('Background generation failed for %s', batch_id)
        friendly = getattr(error, 'friendly_message', None) or '图像生成服务暂时不可用，请稍后再试。'
        error_code = getattr(error, 'error_code', 'GenerationFailed')
        conn.execute(
            'UPDATE generation_batches SET status = ?, style_config = ? WHERE id = ?',
            ('failed', json.dumps({**json.loads(batch['style_config'] or '{}'), 'error': friendly, 'errorCode': error_code}, ensure_ascii=False), batch_id),
        )
        conn.commit()
    finally:
        conn.close()

def start_generation_thread(batch_id):
    thread = threading.Thread(target=generate_batch_assets, args=(batch_id,), daemon=True)
    thread.start()

def result_export_urls(job_id, image_id):
    base = f'/api/jobs/{job_id}/results/{image_id}/download'
    return {
        'branded': base + '?variant=branded',
        'clean': base + '?variant=clean',
        'jpg': base + '?variant=branded',  # 兼容旧前端
        'background': base + '?variant=background',
        'motion': base + '?variant=motion',
    }

def job_result_payload(batch, image_row):
    unlocked = bool(batch['unlocked_at'])
    public_url = image_row['public_url']
    return {
        'id': image_row['id'],
        'label': image_row['label'],
        'imageUrl': public_url,           # 始终下发：缩略图需要展示，靠 CSS 防盗水印盖住
        'previewUrl': public_url,
        'unlocked': unlocked,
        'provider': 'local',
        'referenceOnly': False,
        'exportUrls': result_export_urls(batch['id'], image_row['id']) if unlocked else None,
    }

def load_job_payload(conn, user_id, job_id):
    batch = conn.execute(
        'SELECT * FROM generation_batches WHERE id = ? AND user_id = ?',
        (job_id, user_id),
    ).fetchone()
    if not batch:
        return None
    images = conn.execute(
        'SELECT id, label, public_url FROM generated_images WHERE batch_id = ? ORDER BY id ASC',
        (job_id,),
    ).fetchall()
    photo_ids = json.loads(batch['source_photo_ids'] or '[]')
    source_photos = []
    if photo_ids:
        placeholders = ','.join('?' for _ in photo_ids)
        photo_rows = conn.execute(
            f'SELECT id, public_url FROM uploaded_photos WHERE user_id = ? AND id IN ({placeholders})',
            [user_id] + photo_ids,
        ).fetchall()
        source_photos = [{'id': row['id'], 'url': row['public_url']} for row in photo_rows]
    style_config = json.loads(batch['style_config'] or '{}')
    expected_count = max(1, int(style_config.get('resultCount') or len(images) or 1))
    created_at = int(batch['created_at'])
    results = [job_result_payload(batch, row) for row in images]
    brand_profile = brand_text_config(style_config)
    return {
        'id': batch['id'],
        'status': batch['status'],
        'createdAt': created_at,
        'updatedAt': created_at,
        'previewReadyAt': created_at,
        'secondsRemaining': 0,
        'unlockedAt': batch['unlocked_at'],
        'brandProfile': brand_profile,
        'generationCostCredits': int(batch['generation_cost_credits'] if batch['generation_cost_credits'] is not None else LOCAL_GENERATION_COST_CREDITS),
        'freeRegenerationsRemaining': int(batch['free_regenerations_remaining'] or 0),
        'resultCount': len(results),
        'expectedResultCount': expected_count,
        'sourcePhotos': source_photos,
        'images': [{'id': row['id'], 'label': row['label'], 'url': row['public_url']} for row in images],
        'results': results,
        'generation': {
            'provider': 'local',
            'ready': batch['status'] in {'preview', 'unlocked'},
            'referenceOnly': False,
            'requiredConfig': [],
            'error': style_config.get('error'),
        },
    }

# --- Auth API Routes ---
@app.route('/api/auth/register', methods=['POST'])
def register_user():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not name or '@' not in email or not is_strong_password(password):
        return jsonify({'success': False, 'error': '请填写用户名、有效邮箱；密码需至少 8 位，并包含大写字母、小写字母、数字和特殊符号。'}), 400
    if email.startswith('admin@'):
        return jsonify({'success': False, 'error': '该邮箱前缀保留给管理员账号。'}), 400

    conn = get_db()
    existing = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
    if existing:
        conn.close()
        return jsonify({'success': False, 'error': '该邮箱已经注册，请直接登录。'}), 409

    user_id = 'u_' + uuid.uuid4().hex[:12]
    conn.execute(
        '''INSERT INTO users (id, email, name, password_hash, register_time)
           VALUES (?, ?, ?, ?, ?)''',
        (user_id, email, name, hash_password(password), int(time.time() * 1000)),
    )
    ensure_user_credits(conn, user_id)
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.commit()
    conn.close()
    return auth_response(user)

@app.route('/api/auth/login', methods=['POST'])
def login_user():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    if not user or not verify_password(password, user['password_hash']):
        conn.close()
        return jsonify({'success': False, 'error': '邮箱或密码不正确。'}), 401
    stored = user['password_hash'] or ''
    if not stored.startswith('pbkdf2_sha256$'):
        conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (hash_password(password), user['id']))
    ensure_user_credits(conn, user['id'])
    conn.commit()
    conn.close()
    return auth_response(user)

@app.route('/api/auth/session', methods=['GET'])
def auth_session():
    user = current_user()
    if not user:
        return jsonify({'success': True, 'authenticated': False, 'user': None})
    return jsonify({'success': True, 'authenticated': True, 'user': public_user_payload(user)})

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    response = make_response(jsonify({'success': True, 'authenticated': False, 'user': None}))
    response.delete_cookie('impeccable_user_id')
    return response

@app.route('/api/auth/google/login')
def google_login():
    redirect_uri = url_for('google_auth_callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/api/auth/google/callback')
def google_auth_callback():
    token = google.authorize_access_token()
    user_info = token.get('userinfo')
    if not user_info:
        return jsonify({'success': False, 'error': '无法获取谷歌用户信息。'}), 400
    
    email = user_info.get('email', '').lower()
    name = user_info.get('name', 'Google User')
    
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    
    if not user:
        user_id = 'u_' + uuid.uuid4().hex[:12]
        conn.execute(
            'INSERT INTO users (id, email, name, register_time) VALUES (?, ?, ?, ?)',
            (user_id, email, name, int(time.time() * 1000))
        )
        ensure_user_credits(conn, user_id)
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        conn.commit()
    
    conn.close()
    return auth_response(user)

@app.route('/api/config', methods=['GET'])
def public_config():
    payment_mock = False if FREE_TRIAL_MODE else os.getenv('WECHATPAY_MOCK_ENABLED', 'false').lower() == 'true'
    generation_ready = os.getenv('IMAGE_GENERATION_PROVIDER', 'mock').lower() == 'mock' or bool(os.getenv('DOUBAO_API_KEY'))
    return jsonify({
        'success': True,
        'environment': {'production': os.getenv('FLASK_ENV', '').lower() == 'production', 'freeTrialMode': FREE_TRIAL_MODE},
        'billing': {
            'generationCostCredits': LOCAL_GENERATION_COST_CREDITS,
            'initialCredits': LOCAL_INITIAL_CREDITS,
            'topUpCredits': 1000,
            'topUpAmountCents': 9900,
            'enabled': not FREE_TRIAL_MODE,
        },
        'payment': {
            'provider': 'disabled' if FREE_TRIAL_MODE else 'wechatpay',
            'ready': False,
            'mockEnabled': payment_mock,
            'requiredConfig': [] if payment_mock else ['WECHATPAY_MCH_ID', 'WECHATPAY_PRIVATE_KEY', 'WECHATPAY_SERIAL_NO', 'WECHATPAY_API_V3_KEY'],
        },
        'unlock': {
            'mode': PAYMENT_MODE,
            'previewWatermarkText': PREVIEW_WATERMARK_TEXT,
            'packages': [
                {'id': pid, 'label': info['label'], 'subtitle': info['subtitle'], 'amountCents': info['cents']}
                for pid, info in UNLOCK_PACKAGES.items()
            ],
        },
        'generation': {
            'provider': os.getenv('IMAGE_GENERATION_PROVIDER', 'mock').lower(),
            'model': os.getenv('DOUBAO_MODEL') or DOUBAO_DEFAULT_MODEL,
            'ready': generation_ready,
            'requiredConfig': [] if generation_ready else ['DOUBAO_API_KEY'],
        },
    })

@app.route('/api/payments/wechat/prepay', methods=['POST'])
def create_mock_wechat_payment():
    if FREE_TRIAL_MODE:
        return jsonify({'success': False, 'error': '当前为免费试运行模式，暂不开放充值。'}), 403
    user, error_response = require_current_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    credits = clamp_int(data.get('credits'), 1000, 1, 100000)
    amount_cents = clamp_int(data.get('amountCents'), 9900, 1, 10000000)
    order_id = 'pay_' + uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    conn = get_db()
    conn.execute(
        '''INSERT INTO payment_orders
           (id, user_id, credits, amount_cents, status, created_at, paid_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (order_id, user['id'], credits, amount_cents, 'pending', now, None),
    )
    conn.commit()
    conn.close()
    return jsonify({
        'success': True,
        'mock': True,
        'orderId': order_id,
        'codeUrl': f'local-mock-wechatpay://{order_id}',
        'message': '本地模拟微信支付订单已创建。',
    })

@app.route('/api/payments/wechat/mock-confirm', methods=['POST'])
def confirm_mock_wechat_payment():
    if FREE_TRIAL_MODE:
        return jsonify({'success': False, 'error': '当前为免费试运行模式，模拟支付已关闭。'}), 403
    user, error_response = require_current_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    order_id = (data.get('orderId') or '').strip()
    conn = get_db()
    order = conn.execute(
        'SELECT * FROM payment_orders WHERE id = ? AND user_id = ?',
        (order_id, user['id']),
    ).fetchone()
    if not order:
        conn.close()
        return jsonify({'success': False, 'error': '支付订单不存在。'}), 404
    ensure_user_credits(conn, user['id'])
    now = int(time.time() * 1000)
    cursor = conn.execute(
        "UPDATE payment_orders SET status = 'paid', paid_at = ? WHERE id = ? AND status != 'paid'",
        (now, order_id),
    )
    if cursor.rowcount > 0:
        credits_to_add = int(order['credits'])
        conn.execute(
            'UPDATE user_credits SET balance = balance + ?, updated_at = ? WHERE user_id = ?',
            (credits_to_add, now, user['id']),
        )
        conn.execute(
            '''INSERT INTO credit_transactions
               (id, user_id, amount, reason, batch_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            ('txn_' + uuid.uuid4().hex[:12], user['id'], credits_to_add, 'wechatpay_mock_topup', None, now),
        )
    conn.commit()
    refreshed_user = conn.execute('SELECT * FROM users WHERE id = ?', (user['id'],)).fetchone()
    conn.close()
    return jsonify({'success': True, 'user': public_user_payload(refreshed_user)})

@app.route('/api/account/password', methods=['PATCH'])
def update_account_password():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    current_password = data.get('currentPassword') or ''
    new_password = data.get('newPassword') or ''
    if not is_strong_password(new_password):
        return jsonify({'success': False, 'error': '新密码需至少 8 位，并包含大写字母、小写字母、数字和特殊符号。'}), 400
    if not verify_password(current_password, user['password_hash']):
        return jsonify({'success': False, 'error': '当前密码不正确。'}), 401
    conn = get_db()
    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (hash_password(new_password), user['id']))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# --- Chat API Routes ---
@app.route('/api/chat', methods=['POST'])
def send_message():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    requested_user_id = data.get('userId')
    is_admin = 1 if is_admin_user(user) and requested_user_id else 0
    user_id = requested_user_id if is_admin else user['id']
    if is_admin and not requested_user_id:
        return jsonify({'success': False, 'error': '管理员回复缺少用户 ID。'}), 400
    content = str(data.get('content') or '').strip()
    if not content:
        return jsonify({'success': False, 'error': '消息内容不能为空。'}), 400
    
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO messages (user_id, is_admin, content, timestamp) VALUES (?, ?, ?, ?)",
              (user_id, is_admin, content, int(time.time() * 1000)))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/chat', methods=['GET'])
def get_messages():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    requested_user_id = request.args.get('userId')
    
    conn = get_db()
    c = conn.cursor()
    if requested_user_id == 'admin_all' and is_admin_user(user):
        c.execute("SELECT * FROM messages ORDER BY timestamp ASC")
    elif is_admin_user(user) and requested_user_id:
        c.execute("SELECT * FROM messages WHERE user_id = ? ORDER BY timestamp ASC", (requested_user_id,))
    else:
        c.execute("SELECT * FROM messages WHERE user_id = ? ORDER BY timestamp ASC", (user['id'],))
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(ix) for ix in rows])

@app.route('/api/chat/users', methods=['GET'])
def get_chat_users():
    user, error_response = require_admin_user()
    if error_response:
        return error_response
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT DISTINCT user_id FROM messages WHERE is_admin = 0")
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(ix)['user_id'] for ix in rows])

@app.route('/api/leads/contact', methods=['POST'])
def submit_sales_lead():
    data = request.get_json(silent=True) or {}
    source = data.get('source') if data.get('source') in {'index', 'enterprise'} else 'enterprise'
    name = str(data.get('name') or '').strip()[:80]
    company = str(data.get('company') or '').strip()[:120]
    contact = str(data.get('contact') or '').strip()[:120]
    note = str(data.get('note') or '').strip()[:300]
    if not any([name, company, contact, note]):
        return jsonify({'success': False, 'error': '请至少留下一个联系方式或备注。'}), 400
    conn = get_db()
    conn.execute(
        '''INSERT INTO messages (user_id, is_admin, content, timestamp)
           VALUES (?, ?, ?, ?)''',
        (
            'lead_' + uuid.uuid4().hex[:10],
            0,
            f'企业咨询来源：{source}\n姓名：{name}\n公司：{company}\n联系方式：{contact}\n备注：{note}',
            int(time.time() * 1000),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/admin/summary', methods=['GET'])
def admin_summary():
    user, error_response = require_admin_user()
    if error_response:
        return error_response
    conn = get_db()
    users = conn.execute(
        '''SELECT users.id, users.email, users.name, users.register_time, COALESCE(user_credits.balance, 0) AS credits
           FROM users
           LEFT JOIN user_credits ON user_credits.user_id = users.id
           ORDER BY users.register_time DESC
           LIMIT 100'''
    ).fetchall()
    jobs = conn.execute(
        '''SELECT generation_batches.id, generation_batches.user_id, generation_batches.status,
                  generation_batches.created_at, generation_batches.unlocked_at,
                  generation_batches.generation_cost_credits,
                  users.email, users.name
           FROM generation_batches
           LEFT JOIN users ON users.id = generation_batches.user_id
           ORDER BY generation_batches.created_at DESC
           LIMIT 100'''
    ).fetchall()
    totals = conn.execute(
        '''SELECT
              (SELECT COUNT(*) FROM users) AS user_count,
              (SELECT COUNT(*) FROM generation_batches) AS job_count,
              (SELECT COUNT(*) FROM uploaded_photos) AS upload_count,
              (SELECT COUNT(*) FROM generated_images) AS result_count'''
    ).fetchone()
    result_counts = {
        row['batch_id']: row['count']
        for row in conn.execute(
            'SELECT batch_id, COUNT(*) AS count FROM generated_images GROUP BY batch_id'
        ).fetchall()
    }
    conn.close()
    user_items = [
        {
            'id': row['id'],
            'email': row['email'],
            'name': row['name'],
            'createdAt': row['register_time'],
            'credits': row['credits'],
        }
        for row in users
    ]
    job_items = [
        {
            'id': row['id'],
            'userId': row['user_id'],
            'email': row['email'],
            'name': row['name'],
            'status': row['status'],
            'createdAt': row['created_at'],
            'unlocked': bool(row['unlocked_at']),
            'resultCount': int(result_counts.get(row['id'], 0)),
            'packageTier': '免费试运行' if int(row['generation_cost_credits'] or 0) == 0 else '基础版',
        }
        for row in jobs
    ]
    return jsonify({
        'success': True,
        'metrics': {
            'users': totals['user_count'],
            'images': totals['result_count'],
            'revenue': totals['job_count'],
        },
        'totals': dict(totals),
        'users': user_items,
        'jobs': job_items,
    })

# --- Image Pipeline API Routes ---
@app.route('/api/upload', methods=['POST'])
@app.route('/api/uploads', methods=['POST'])
def upload_photo():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    user_id = user['id']
    files = request.files.getlist('files') or request.files.getlist('photo')
    files = [file_storage for file_storage in files if file_storage and file_storage.filename]
    if not files:
        return jsonify({'success': False, 'error': '没有收到图片文件。'}), 400
    if len(files) > 5:
        return jsonify({'success': False, 'error': '一次最多上传 5 张照片。'}), 400

    conn = get_db()
    uploads = []
    for file_storage in files:
        status, message = image_quality_status(file_storage)
        if status == 'error':
            conn.close()
            return jsonify({'success': False, 'status': status, 'error': message}), 400
        upload_bytes = file_storage.read()
        ok, validation_message = validate_image_bytes(upload_bytes, file_storage.filename)
        if not ok:
            conn.close()
            return jsonify({'success': False, 'status': 'error', 'error': validation_message}), 400

        ext = Path(file_storage.filename).suffix.lower() or '.jpg'
        photo_id = 'photo_' + uuid.uuid4().hex[:12]
        save_path = UPLOAD_DIR / f'{photo_id}{ext}'
        save_path.write_bytes(upload_bytes)

        public_url = photo_public_url(save_path)
        conn.execute(
            '''INSERT INTO uploaded_photos
               (id, user_id, original_name, file_path, public_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (photo_id, user_id, file_storage.filename, str(save_path), public_url, int(time.time() * 1000)),
        )
        uploads.append({
            'id': photo_id,
            'name': file_storage.filename,
            'url': public_url,
            'previewUrl': public_url,
            'status': status,
            'message': message,
        })
    conn.commit()
    conn.close()

    return jsonify({
        'success': True,
        'photo': uploads[0],
        'uploads': uploads,
    })

@app.route('/api/generate', methods=['POST'])
def generate_images():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    user_id = user['id']
    photo_ids = data.get('photoIds') or []
    style_config = data.get('styleConfig') or {}
    client_request_id = (data.get('clientRequestId') or '').strip()

    if not photo_ids:
        return jsonify({'success': False, 'error': '请先上传至少 1 张照片再生成。'}), 400

    conn = get_db()
    if client_request_id:
        existing = conn.execute(
            'SELECT id, status FROM generation_batches WHERE user_id = ? AND client_request_id = ?',
            (user_id, client_request_id),
        ).fetchone()
        if existing:
            images = conn.execute(
                'SELECT id, label, public_url FROM generated_images WHERE batch_id = ? ORDER BY id ASC',
                (existing['id'],),
            ).fetchall()
            conn.close()
            return jsonify({
                'success': True,
                'batch': {
                    'id': existing['id'],
                    'status': existing['status'],
                    'count': len(images),
                    'images': [{'id': row['id'], 'label': row['label'], 'url': row['public_url']} for row in images],
                },
                'reused': True,
            })

    placeholders = ','.join('?' for _ in photo_ids)
    rows = conn.execute(
        f'SELECT * FROM uploaded_photos WHERE user_id = ? AND id IN ({placeholders})',
        [user_id] + photo_ids,
    ).fetchall()
    if not rows:
        conn.close()
        return jsonify({'success': False, 'error': '没有找到可用于生成的上传照片。'}), 400

    source = rows[0]
    source_path = Path(source['file_path'])
    if not source_path.exists():
        conn.close()
        return jsonify({'success': False, 'error': '源照片文件已丢失，请重新上传。'}), 400

    batch_id = 'gen_' + uuid.uuid4().hex[:12]
    requested_labels = selected_generation_labels(style_config)
    result_count = selected_result_count(style_config)
    if len(requested_labels) == 1:
        labels = requested_labels * result_count
    else:
        labels = requested_labels[:result_count]
    ext = source_path.suffix.lower() or '.jpg'
    now = int(time.time() * 1000)
    conn.execute(
        '''INSERT INTO generation_batches
           (id, user_id, source_photo_ids, style_config, status, created_at, client_request_id,
            unlocked_at, generation_cost_credits, free_regenerations_remaining)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            batch_id,
            user_id,
            json.dumps(photo_ids),
            json.dumps(style_config),
            'preview',
            now,
            client_request_id or None,
            None,
            LOCAL_GENERATION_COST_CREDITS,
            LOCAL_FREE_REGENERATIONS,
        ),
    )

    images = []
    try:
        provider, generated_assets = generate_image_assets(source_path, labels, style_config)
    except DoubaoGenerationHTTPError as error:
        conn.rollback()
        conn.close()
        app.logger.error('Doubao error in sync generate: %s %s', error.error_code, error.details[:500])
        status = 503 if error.error_code in {'AccountOverdueError', 'InvalidApiKey', 'AuthenticationError'} else 502
        return jsonify({'success': False, 'error': error.friendly_message, 'errorCode': error.error_code, 'retryable': error.retryable}), status
    except Exception as error:
        conn.rollback()
        conn.close()
        app.logger.exception('Generation failed in sync endpoint')
        return jsonify({'success': False, 'error': '图像生成服务暂时不可用，请稍后再试。'}), 502

    if len(generated_assets) < len(labels):
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': '豆包接口返回图片数量不足，请重试。'}), 502

    for index, (label, asset) in enumerate(zip(labels, generated_assets), start=1):
        image_id = f'{batch_id}_{index}'
        asset_ext = extension_for_content_type(asset.get('content_type'), ext)
        target_path = GENERATED_DIR / f'{image_id}{asset_ext}'
        if provider == 'mock':
            copy_mock_image(asset['source_path'], target_path)
        else:
            target_path.write_bytes(asset['data'])
        if label == CELEBRITY_HEADSHOT_LABEL:
            crop_to_square(target_path)
        master_path = GENERATED_DIR / f'{image_id}__master{asset_ext}'
        shutil.copyfile(target_path, master_path)
        apply_brand_watermark(target_path, style_config)
        public_url = photo_public_url(target_path)
        conn.execute(
            '''INSERT INTO generated_images
               (id, batch_id, label, file_path, public_url, created_at, master_file_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (image_id, batch_id, label, str(target_path), public_url, now, str(master_path)),
        )
        images.append({
            'id': image_id,
            'label': label,
            'url': public_url,
            'provider': provider,
            'model': asset.get('model') or selected_doubao_model(style_config),
        })

    conn.commit()
    conn.close()
    return jsonify({'success': True, 'batch': {'id': batch_id, 'status': 'preview', 'count': len(images), 'images': images}})

@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    conn = get_db()
    batches = conn.execute(
        '''SELECT * FROM generation_batches
           WHERE user_id = ?
           ORDER BY created_at DESC
           LIMIT 20''',
        (user['id'],),
    ).fetchall()
    jobs = []
    for batch in batches:
        cover = conn.execute(
            'SELECT public_url FROM generated_images WHERE batch_id = ? ORDER BY created_at ASC, id ASC LIMIT 1',
            (batch['id'],),
        ).fetchone()
        jobs.append({
            'id': batch['id'],
            'status': batch['status'],
            'createdAt': batch['created_at'],
            'previewReadyAt': batch['created_at'],
            'secondsRemaining': 0,
            'generationCostCredits': int(batch['generation_cost_credits'] if batch['generation_cost_credits'] is not None else LOCAL_GENERATION_COST_CREDITS),
            'freeRegenerationsRemaining': int(batch['free_regenerations_remaining'] or 0),
            'coverImageUrl': cover['public_url'] if cover else None,
        })
    conn.close()
    return jsonify({'success': True, 'jobs': jobs})

@app.route('/api/jobs', methods=['POST'])
def create_job():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    photo_ids = data.get('uploadIds') or data.get('photoIds') or []
    style_config = data.get('styleConfig') or {}
    if data.get('styleSummary') and not style_config.get('summary'):
        style_config['summary'] = data.get('styleSummary')
    if not style_config.get('resultCount'):
        style_config['resultCount'] = 1
    client_request_id = (data.get('clientRequestId') or 'job_' + uuid.uuid4().hex).strip()

    conn = get_db()
    if client_request_id:
        existing = conn.execute(
            'SELECT id FROM generation_batches WHERE user_id = ? AND client_request_id = ?',
            (user['id'], client_request_id),
        ).fetchone()
        if existing:
            job_payload = load_job_payload(conn, user['id'], existing['id'])
            conn.close()
            return jsonify({'success': True, 'jobId': existing['id'], 'job': job_payload, 'reused': True})

    if not photo_ids or len(photo_ids) > 5:
        conn.close()
        return jsonify({'success': False, 'error': '请先上传 1 到 5 张照片。'}), 400
    placeholders = ','.join('?' for _ in photo_ids)
    rows = conn.execute(
        f'SELECT id FROM uploaded_photos WHERE user_id = ? AND id IN ({placeholders})',
        [user['id']] + photo_ids,
    ).fetchall()
    if len(rows) != len(photo_ids):
        conn.close()
        return jsonify({'success': False, 'error': '上传文件无效，请重新上传后再试。'}), 400

    balance = ensure_user_credits(conn, user['id'])
    if LOCAL_GENERATION_COST_CREDITS > 0 and balance < LOCAL_GENERATION_COST_CREDITS:
        conn.close()
        return jsonify({
            'success': False,
            'error': f'积分不足。本次生成需要 {LOCAL_GENERATION_COST_CREDITS} 积分，当前剩余 {balance} 积分。',
        }), 402

    now = int(time.time() * 1000)
    batch_id = 'gen_' + uuid.uuid4().hex[:12]
    new_balance = max(0, balance - LOCAL_GENERATION_COST_CREDITS)
    conn.execute(
        '''INSERT INTO generation_batches
           (id, user_id, source_photo_ids, style_config, status, created_at, client_request_id,
            unlocked_at, generation_cost_credits, free_regenerations_remaining)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            batch_id,
            user['id'],
            json.dumps(photo_ids),
            json.dumps(style_config, ensure_ascii=False),
            'processing',
            now,
            client_request_id or None,
            None,
            LOCAL_GENERATION_COST_CREDITS,
            LOCAL_FREE_REGENERATIONS,
        ),
    )
    if LOCAL_GENERATION_COST_CREDITS > 0:
        conn.execute(
            'UPDATE user_credits SET balance = ?, updated_at = ? WHERE user_id = ?',
            (new_balance, now, user['id']),
        )
        conn.execute(
            '''INSERT INTO credit_transactions
               (id, user_id, amount, reason, batch_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            ('txn_' + uuid.uuid4().hex[:12], user['id'], -LOCAL_GENERATION_COST_CREDITS, 'generation_job', batch_id, now),
        )
    job_payload = load_job_payload(conn, user['id'], batch_id)
    conn.commit()
    conn.close()
    start_generation_thread(batch_id)
    return jsonify({
        'success': True,
        'jobId': batch_id,
        'chargedCredits': LOCAL_GENERATION_COST_CREDITS,
        'freeTrial': FREE_TRIAL_MODE,
        'job': job_payload,
        'batch': {'id': batch_id, 'status': 'processing', 'count': 0, 'images': []},
    })

@app.route('/api/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    user, error_response = require_current_user()
    if error_response:
        return error_response
    conn = get_db()
    job = load_job_payload(conn, user['id'], job_id)
    conn.close()
    if not job:
        return jsonify({'success': False, 'error': '任务不存在。'}), 404
    return jsonify({
        'success': True,
        'job': job,
    })

@app.route('/api/jobs/<job_id>/checkout', methods=['POST'])
def create_job_checkout(job_id):
    """为某次生成任务创建解锁订单。mock 模式下返回 mock 标记，真实模式返回二维码 URL。"""
    user, error_response = require_current_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    package_id = (data.get('packageId') or 'standard').strip().lower()
    if package_id not in UNLOCK_PACKAGES:
        return jsonify({'success': False, 'error': '套餐不存在。'}), 400
    package = UNLOCK_PACKAGES[package_id]

    conn = get_db()
    batch = conn.execute(
        'SELECT id, unlocked_at FROM generation_batches WHERE id = ? AND user_id = ?',
        (job_id, user['id']),
    ).fetchone()
    if not batch:
        conn.close()
        return jsonify({'success': False, 'error': '任务不存在。'}), 404
    if batch['unlocked_at']:
        conn.close()
        return jsonify({'success': True, 'alreadyUnlocked': True})

    out_trade_no = 'job_' + uuid.uuid4().hex[:16]
    now = int(time.time() * 1000)
    mode = PAYMENT_MODE
    code_url = None
    if mode == 'wechat_native':
        try:
            from wechatpay_native import WeChatPayClient
            if not WeChatPayClient.is_configured():
                mode = 'mock'
            else:
                client = WeChatPayClient.get_instance()
                order = client.create_native_order(out_trade_no, package['cents'], f'Auralis 解锁 · {package["label"]}')
                code_url = order.code_url
        except Exception as exc:
            app.logger.exception('wechatpay native order failed: %s', exc)
            mode = 'mock'

    conn.execute(
        '''INSERT INTO job_payments
           (id, job_id, user_id, out_trade_no, package_id, amount_cents, mode, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        ('jp_' + uuid.uuid4().hex[:12], job_id, user['id'], out_trade_no, package_id,
         package['cents'], mode, 'pending', now),
    )
    conn.commit()
    conn.close()
    return jsonify({
        'success': True,
        'outTradeNo': out_trade_no,
        'amountCents': package['cents'],
        'packageId': package_id,
        'packageLabel': package['label'],
        'mode': mode,
        'codeUrl': code_url,
    })


@app.route('/api/jobs/<job_id>/checkout/mock-confirm', methods=['POST'])
def mock_confirm_job_checkout(job_id):
    """开发期 / 演示用：把 pending 订单直接置为 paid 并解锁任务。"""
    user, error_response = require_current_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    out_trade_no = (data.get('outTradeNo') or '').strip()
    if not out_trade_no:
        return jsonify({'success': False, 'error': '缺少订单号。'}), 400

    conn = get_db()
    payment = conn.execute(
        'SELECT * FROM job_payments WHERE out_trade_no = ? AND user_id = ? AND job_id = ?',
        (out_trade_no, user['id'], job_id),
    ).fetchone()
    if not payment:
        conn.close()
        return jsonify({'success': False, 'error': '订单不存在。'}), 404
    if payment['mode'] != 'mock':
        conn.close()
        return jsonify({'success': False, 'error': '真实支付订单请通过微信回调确认。'}), 400

    now = int(time.time() * 1000)
    if payment['status'] != 'paid':
        conn.execute(
            "UPDATE job_payments SET status = 'paid', paid_at = ?, transaction_id = ? WHERE id = ?",
            (now, 'mock_' + uuid.uuid4().hex[:10], payment['id']),
        )
    batch = conn.execute(
        'SELECT unlocked_at FROM generation_batches WHERE id = ? AND user_id = ?',
        (job_id, user['id']),
    ).fetchone()
    if batch and not batch['unlocked_at']:
        conn.execute(
            'UPDATE generation_batches SET status = ?, unlocked_at = ? WHERE id = ?',
            ('unlocked', now, job_id),
        )
    job = load_job_payload(conn, user['id'], job_id)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'job': job, 'outTradeNo': out_trade_no})


@app.route('/api/payments/wechat/notify', methods=['POST'])
def wechat_pay_notify():
    """微信支付 Native 回调（真实模式）。由 wechatpay_native.parse_notification 解密验签。"""
    try:
        from wechatpay_native import WeChatPayClient
        if not WeChatPayClient.is_configured():
            return jsonify({'code': 'FAIL', 'message': 'wechatpay not configured'}), 500
        client = WeChatPayClient.get_instance()
        notification = client.parse_notification(dict(request.headers), request.get_data())
    except Exception:
        app.logger.exception('wechatpay notify parse failed')
        return jsonify({'code': 'FAIL', 'message': 'parse failed'}), 400

    out_trade_no = getattr(notification, 'out_trade_no', '') or ''
    if not out_trade_no:
        return jsonify({'code': 'FAIL', 'message': 'missing out_trade_no'}), 400

    conn = get_db()
    payment = conn.execute('SELECT * FROM job_payments WHERE out_trade_no = ?', (out_trade_no,)).fetchone()
    if not payment:
        conn.close()
        return jsonify({'code': 'FAIL', 'message': 'order not found'}), 404

    now = int(time.time() * 1000)
    if payment['status'] != 'paid':
        conn.execute(
            "UPDATE job_payments SET status = 'paid', paid_at = ?, transaction_id = ? WHERE id = ?",
            (now, getattr(notification, 'transaction_id', None), payment['id']),
        )
        conn.execute(
            'UPDATE generation_batches SET status = ?, unlocked_at = ? WHERE id = ? AND unlocked_at IS NULL',
            ('unlocked', now, payment['job_id']),
        )
        conn.commit()
    conn.close()
    return jsonify({'code': 'SUCCESS'})


@app.route('/api/jobs/<job_id>/payment-status', methods=['GET'])
def get_job_payment_status(job_id):
    """前端轮询用：知道 outTradeNo 后查支付是否完成。"""
    user, error_response = require_current_user()
    if error_response:
        return error_response
    out_trade_no = (request.args.get('outTradeNo') or '').strip()
    conn = get_db()
    if out_trade_no:
        payment = conn.execute(
            'SELECT status, paid_at FROM job_payments WHERE out_trade_no = ? AND user_id = ? AND job_id = ?',
            (out_trade_no, user['id'], job_id),
        ).fetchone()
    else:
        payment = conn.execute(
            '''SELECT status, paid_at FROM job_payments
               WHERE user_id = ? AND job_id = ?
               ORDER BY created_at DESC LIMIT 1''',
            (user['id'], job_id),
        ).fetchone()
    batch = conn.execute(
        'SELECT unlocked_at FROM generation_batches WHERE id = ? AND user_id = ?',
        (job_id, user['id']),
    ).fetchone()
    conn.close()
    return jsonify({
        'success': True,
        'status': payment['status'] if payment else None,
        'paidAt': payment['paid_at'] if payment else None,
        'unlockedAt': batch['unlocked_at'] if batch else None,
    })


@app.route('/api/jobs/<job_id>/unlock', methods=['POST'])
def unlock_job(job_id):
    user, error_response = require_current_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    out_trade_no = (data.get('outTradeNo') or '').strip()
    conn = get_db()
    batch = conn.execute(
        'SELECT * FROM generation_batches WHERE id = ? AND user_id = ?',
        (job_id, user['id']),
    ).fetchone()
    if not batch:
        conn.close()
        return jsonify({'success': False, 'error': '任务不存在。'}), 404

    if batch['unlocked_at']:
        job = load_job_payload(conn, user['id'], job_id)
        conn.close()
        return jsonify({'success': True, 'job': job, 'alreadyUnlocked': True})

    if not out_trade_no:
        conn.close()
        return jsonify({'success': False, 'error': '需要先完成支付才能解锁。'}), 402

    payment = conn.execute(
        'SELECT * FROM job_payments WHERE out_trade_no = ? AND job_id = ? AND user_id = ?',
        (out_trade_no, job_id, user['id']),
    ).fetchone()
    if not payment or payment['status'] != 'paid':
        conn.close()
        return jsonify({'success': False, 'error': '支付未完成，无法解锁。'}), 402

    now = int(time.time() * 1000)
    conn.execute(
        'UPDATE generation_batches SET status = ?, unlocked_at = ? WHERE id = ?',
        ('unlocked', now, job_id),
    )
    job = load_job_payload(conn, user['id'], job_id)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'job': job})

@app.route('/api/jobs/<job_id>/regenerate', methods=['POST'])
def regenerate_job(job_id):
    user, error_response = require_current_user()
    if error_response:
        return error_response
    conn = get_db()
    batch = conn.execute(
        'SELECT * FROM generation_batches WHERE id = ? AND user_id = ?',
        (job_id, user['id']),
    ).fetchone()
    if not batch:
        conn.close()
        return jsonify({'success': False, 'error': '任务不存在。'}), 404
    remaining = int(batch['free_regenerations_remaining'] or 0)
    if remaining <= 0:
        conn.close()
        return jsonify({'success': False, 'error': '免费重绘次数已用完。'}), 402

    photo_ids = json.loads(batch['source_photo_ids'] or '[]')
    style_config = json.loads(batch['style_config'] or '{}')
    placeholders = ','.join('?' for _ in photo_ids)
    rows = conn.execute(
        f'SELECT * FROM uploaded_photos WHERE user_id = ? AND id IN ({placeholders})',
        [user['id']] + photo_ids,
    ).fetchall() if photo_ids else []
    if not rows:
        conn.close()
        return jsonify({'success': False, 'error': '源照片不存在，请重新上传。'}), 400

    source_path = Path(rows[0]['file_path'])
    if not source_path.exists():
        conn.close()
        return jsonify({'success': False, 'error': '源照片文件已丢失，请重新上传。'}), 400

    old_images = conn.execute('SELECT file_path, master_file_path FROM generated_images WHERE batch_id = ?', (job_id,)).fetchall()

    requested_labels = selected_generation_labels(style_config)
    result_count = selected_result_count(style_config)
    labels = requested_labels * result_count if len(requested_labels) == 1 else requested_labels[:result_count]
    provider, generated_assets = generate_image_assets(source_path, labels, style_config)
    if len(generated_assets) < len(labels):
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': '豆包接口返回图片数量不足，请重试。'}), 502

    for row in old_images:
        for col in ('file_path', 'master_file_path'):
            raw = row[col] if col in row.keys() else None
            if not raw:
                continue
            path = Path(raw)
            if path.exists() and GENERATED_DIR.resolve() in path.resolve().parents:
                path.unlink()
    conn.execute('DELETE FROM generated_images WHERE batch_id = ?', (job_id,))

    now = int(time.time() * 1000)
    ext = source_path.suffix.lower() or '.jpg'
    for index, (label, asset) in enumerate(zip(labels, generated_assets), start=1):
        image_id = f'{job_id}_{index}'
        asset_ext = extension_for_content_type(asset.get('content_type'), ext)
        target_path = GENERATED_DIR / f'{image_id}{asset_ext}'
        if provider == 'mock':
            copy_mock_image(asset['source_path'], target_path)
        else:
            target_path.write_bytes(asset['data'])
        if label == CELEBRITY_HEADSHOT_LABEL:
            crop_to_square(target_path)
        master_path = GENERATED_DIR / f'{image_id}__master{asset_ext}'
        shutil.copyfile(target_path, master_path)
        apply_brand_watermark(target_path, style_config)
        public_url = photo_public_url(target_path)
        conn.execute(
            '''INSERT INTO generated_images
               (id, batch_id, label, file_path, public_url, created_at, master_file_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (image_id, job_id, label, str(target_path), public_url, now, str(master_path)),
        )

    new_remaining = remaining - 1
    conn.execute(
        '''UPDATE generation_batches
           SET status = ?, created_at = ?, free_regenerations_remaining = ?
           WHERE id = ?''',
        ('preview', now, new_remaining, job_id),
    )
    job = load_job_payload(conn, user['id'], job_id)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'job': job, 'freeRegenerationsRemaining': new_remaining})

@app.route('/api/jobs/<job_id>/results/<result_id>/download', methods=['GET'])
def download_job_result(job_id, result_id):
    user, error_response = require_current_user()
    if error_response:
        return error_response
    variant = (request.args.get('variant') or 'branded').lower()
    if variant == 'jpg':
        variant = 'branded'
    if variant not in {'branded', 'clean'}:
        return jsonify({'success': False, 'error': '该导出格式本地版暂未接入。'}), 501

    conn = get_db()
    row = conn.execute(
        '''SELECT generated_images.file_path,
                  generated_images.master_file_path,
                  generated_images.label,
                  generation_batches.unlocked_at,
                  generation_batches.style_config,
                  generation_batches.brand_profile_json
           FROM generated_images
           JOIN generation_batches ON generation_batches.id = generated_images.batch_id
           WHERE generation_batches.user_id = ?
             AND generated_images.batch_id = ?
             AND generated_images.id = ?''',
        (user['id'], job_id, result_id),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'success': False, 'error': '结果图不存在。'}), 404
    if not row['unlocked_at']:
        return jsonify({'success': False, 'error': '请先完成支付以解锁下载。'}), 402

    branded_path = Path(row['file_path'])
    master_raw = row['master_file_path'] if 'master_file_path' in row.keys() else None
    master_path = Path(master_raw) if master_raw else None

    style_config = json.loads(row['style_config'] or '{}')
    custom_text = request.args.get('brand')  # 可选：?brand=<base64-json> 临时改信息水印
    text_override = None
    if custom_text:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(custom_text + '==').decode('utf-8'))
            text_override = {
                'title': clean_prompt_text(decoded.get('title'), brand_text_config(style_config)['title'], 60),
                'subtitle': clean_prompt_text(decoded.get('subtitle'), brand_text_config(style_config)['subtitle'], 80),
                'meta': clean_prompt_text(decoded.get('meta'), brand_text_config(style_config)['meta'], 120),
            }
        except (ValueError, json.JSONDecodeError):
            text_override = None

    if variant == 'clean':
        if master_path and master_path.exists():
            return send_file(master_path, as_attachment=True, download_name=f'{row["label"]}-clean{master_path.suffix}')
        # 兼容老 job：没有 master，只能返回当前文件
        if branded_path.exists():
            return send_file(branded_path, as_attachment=True, download_name=f'{row["label"]}{branded_path.suffix}')
        return jsonify({'success': False, 'error': '结果文件已丢失。'}), 404

    # variant == 'branded'
    if master_path and master_path.exists():
        buf, suffix = render_branded_image_bytes(master_path, style_config, label=row['label'], text_override=text_override)
        buf.seek(0)
        return send_file(buf, mimetype='image/jpeg', as_attachment=True, download_name=f'{row["label"]}-branded{suffix}')
    if branded_path.exists():
        return send_file(branded_path, as_attachment=True, download_name=f'{row["label"]}-branded{branded_path.suffix}')
    return jsonify({'success': False, 'error': '结果文件已丢失。'}), 404

@app.route('/api/jobs/<job_id>/download', methods=['GET'])
def download_job_archive(job_id):
    user, error_response = require_current_user()
    if error_response:
        return error_response
    export_format = request.args.get('format') or 'zip'
    if export_format != 'zip':
        return jsonify({'success': False, 'error': '动态特写合集本地版暂未接入，请先下载静态图全集。'}), 501
    conn = get_db()
    batch = conn.execute(
        'SELECT unlocked_at FROM generation_batches WHERE id = ? AND user_id = ?',
        (job_id, user['id']),
    ).fetchone()
    if not batch:
        conn.close()
        return jsonify({'success': False, 'error': '任务不存在。'}), 404
    if not batch['unlocked_at']:
        conn.close()
        return jsonify({'success': False, 'error': '请先完成支付以解锁下载。'}), 402
    rows = conn.execute(
        '''SELECT generated_images.file_path, generated_images.label
           FROM generated_images
           JOIN generation_batches ON generation_batches.id = generated_images.batch_id
           WHERE generation_batches.user_id = ?
             AND generated_images.batch_id = ?
           ORDER BY generated_images.id ASC''',
        (user['id'], job_id),
    ).fetchall()
    conn.close()
    if not rows:
        return jsonify({'success': False, 'error': '没有可下载的结果图。'}), 404
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
        for index, row in enumerate(rows, start=1):
            path = Path(row['file_path'])
            if path.exists():
                zf.write(path, arcname=f'{index:02d}-{row["label"]}{path.suffix}')
    archive.seek(0)
    return send_file(archive, mimetype='application/zip', as_attachment=True, download_name=f'{job_id}.zip')

@app.route('/api/assets', methods=['GET'])
def list_assets():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    user_id = user['id']
    conn = get_db()
    batches = conn.execute(
        '''SELECT * FROM generation_batches
           WHERE user_id = ?
           ORDER BY created_at DESC''',
        (user_id,),
    ).fetchall()

    assets = []
    for batch in batches:
        images = conn.execute(
            '''SELECT id, label, public_url
               FROM generated_images
               WHERE batch_id = ?
               ORDER BY created_at ASC, id ASC''',
            (batch['id'],),
        ).fetchall()
        image_items = [{'id': row['id'], 'label': row['label'], 'url': row['public_url']} for row in images]
        assets.append({
            'id': batch['id'],
            'timestamp': batch['created_at'],
            'status': batch['status'],
            'count': len(image_items),
            'coverUrl': image_items[0]['url'] if image_items else '',
            'images': image_items,
        })

    conn.close()
    return jsonify({'success': True, 'assets': assets})

@app.route('/api/credits', methods=['GET'])
def get_credits():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    user_id = user['id']
    conn = get_db()
    balance = ensure_user_credits(conn, user_id)
    transactions = conn.execute(
        '''SELECT amount, reason, batch_id, created_at
           FROM credit_transactions
           WHERE user_id = ?
           ORDER BY created_at DESC
           LIMIT 20''',
        (user_id,),
    ).fetchall()
    conn.commit()
    conn.close()
    return jsonify({
        'success': True,
        'balance': balance,
        'transactions': [dict(row) for row in transactions],
    })

@app.route('/api/credits/topup', methods=['POST'])
def topup_credits():
    user, error_response = require_admin_user()
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    user_id = data.get('userId')
    if not user_id:
        return jsonify({'success': False, 'error': '缺少充值用户 ID。'}), 400
    try:
        amount = int(data.get('amount') or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '充值点数格式不正确。'}), 400
    method = data.get('method') or 'manual'
    if amount <= 0:
        return jsonify({'success': False, 'error': '充值点数必须大于 0。'}), 400

    conn = get_db()
    balance = ensure_user_credits(conn, user_id) + amount
    now = int(time.time() * 1000)
    conn.execute(
        'UPDATE user_credits SET balance = ?, updated_at = ? WHERE user_id = ?',
        (balance, now, user_id),
    )
    conn.execute(
        '''INSERT INTO credit_transactions
           (id, user_id, amount, reason, batch_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        ('txn_' + uuid.uuid4().hex[:12], user_id, amount, f'topup_{method}', None, now),
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'balance': balance})

@app.route('/api/batches/<batch_id>', methods=['GET'])
def get_batch(batch_id):
    user, error_response = require_current_user()
    if error_response:
        return error_response
    conn = get_db()
    batch = conn.execute(
        'SELECT * FROM generation_batches WHERE id = ? AND user_id = ?',
        (batch_id, user['id']),
    ).fetchone()
    if not batch:
        conn.close()
        return jsonify({'success': False, 'error': '生成批次不存在。'}), 404
    images = conn.execute(
        'SELECT id, label, public_url FROM generated_images WHERE batch_id = ? ORDER BY id ASC',
        (batch_id,),
    ).fetchall()
    conn.close()
    return jsonify({
        'success': True,
        'batch': {
            'id': batch['id'],
            'status': batch['status'],
            'images': [{'id': row['id'], 'label': row['label'], 'url': row['public_url']} for row in images],
        }
    })

@app.route('/api/batches/<batch_id>/unlock', methods=['POST'])
def unlock_batch(batch_id):
    data = request.get_json(silent=True) or {}
    user, error_response = require_current_user()
    if error_response:
        return error_response
    conn = get_db()
    batch = conn.execute(
        'SELECT * FROM generation_batches WHERE id = ? AND user_id = ?',
        (batch_id, user['id']),
    ).fetchone()
    if not batch:
        conn.close()
        return jsonify({'success': False, 'error': '生成批次不存在。'}), 404

    user_id = user['id']
    if batch['status'] == 'unlocked':
        balance = ensure_user_credits(conn, user_id)
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'balance': balance, 'alreadyUnlocked': True})

    balance = ensure_user_credits(conn, user_id)
    if balance < 1:
        conn.close()
        return jsonify({'success': False, 'error': '积分余额不足，请先补充算力。'}), 402

    now = int(time.time() * 1000)
    new_balance = balance - 1
    conn.execute('UPDATE generation_batches SET status = ? WHERE id = ?', ('unlocked', batch_id))
    conn.execute(
        'UPDATE user_credits SET balance = ?, updated_at = ? WHERE user_id = ?',
        (new_balance, now, user_id),
    )
    conn.execute(
        '''INSERT INTO credit_transactions
           (id, user_id, amount, reason, batch_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        ('txn_' + uuid.uuid4().hex[:12], user_id, -1, 'unlock_image', batch_id, now),
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'balance': new_balance})

def erase_user_media_data(user_id):
    conn = get_db()
    upload_rows = conn.execute('SELECT file_path FROM uploaded_photos WHERE user_id = ?', (user_id,)).fetchall()
    batch_rows = conn.execute('SELECT id FROM generation_batches WHERE user_id = ?', (user_id,)).fetchall()
    batch_ids = [row['id'] for row in batch_rows]

    generated_rows = []
    if batch_ids:
        placeholders = ','.join('?' for _ in batch_ids)
        generated_rows = conn.execute(
            f'SELECT file_path FROM generated_images WHERE batch_id IN ({placeholders})',
            batch_ids,
        ).fetchall()
        conn.execute(f'DELETE FROM generated_images WHERE batch_id IN ({placeholders})', batch_ids)

    for row in list(upload_rows) + list(generated_rows):
        path = Path(row['file_path'])
        if path.exists() and BASE_DIR in path.resolve().parents:
            path.unlink()

    conn.execute('DELETE FROM uploaded_photos WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM generation_batches WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM messages WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM credit_transactions WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM user_credits WHERE user_id = ?', (user_id,))
    deleted_objects = len(upload_rows) + len(generated_rows)
    conn.commit()
    conn.close()
    return deleted_objects

@app.route('/api/account/data', methods=['DELETE'])
def erase_account_data():
    user, error_response = require_current_user()
    if error_response:
        return error_response
    deleted_objects = erase_user_media_data(user['id'])
    return jsonify({'success': True, 'deletedObjects': deleted_objects})

@app.route('/api/account/delete', methods=['POST'])
def delete_account_data():
    data = request.get_json(silent=True) or {}
    user, error_response = require_current_user()
    if error_response:
        return error_response
    confirmation = data.get('confirmation')
    if confirmation != 'DELETE':
        return jsonify({'success': False, 'error': '删除确认失败。'}), 400
    deleted_objects = erase_user_media_data(user['id'])
    conn = get_db()
    conn.execute('DELETE FROM users WHERE id = ?', (user['id'],))
    conn.execute('DELETE FROM payment_orders WHERE user_id = ?', (user['id'],))
    conn.commit()
    conn.close()
    session.clear()
    return jsonify({'success': True, 'deletedObjects': deleted_objects})

# --- Frontend Template Engine Routes ---
@app.route('/')
def route_index(): return render_template('index.html')

@app.route('/dashboard.html')
def route_dash(): return render_template('dashboard.html')

@app.route('/admin.html')
def route_admin(): return render_template('admin.html')

@app.route('/media/uploads/<path:filename>')
def serve_private_upload(filename):
    user, error_response = require_current_user()
    if error_response:
        return error_response
    path = (UPLOAD_DIR / filename).resolve()
    if UPLOAD_DIR.resolve() not in path.parents or not path.exists():
        return jsonify({'success': False, 'error': '文件不存在。'}), 404
    conn = get_db()
    allowed = path_is_owned_upload(conn, user['id'], path)
    conn.close()
    if not allowed:
        return jsonify({'success': False, 'error': '文件不存在。'}), 404
    return send_from_directory(UPLOAD_DIR, path.name)

@app.route('/media/generated/<path:filename>')
def serve_private_generated(filename):
    user, error_response = require_current_user()
    if error_response:
        return error_response
    path = (GENERATED_DIR / filename).resolve()
    if GENERATED_DIR.resolve() not in path.parents or not path.exists():
        return jsonify({'success': False, 'error': '文件不存在。'}), 404
    conn = get_db()
    allowed = path_is_owned_generated(conn, user['id'], path)
    conn.close()
    if not allowed:
        return jsonify({'success': False, 'error': '文件不存在。'}), 404
    return send_from_directory(GENERATED_DIR, path.name)

@app.route('/assets/<path:filename>')
def route_legacy_assets(filename):
    return send_from_directory(BASE_DIR / 'static' / 'assets', filename)

@app.route('/<path:filename>')
def serve_html(filename):
    if filename.endswith('.html'):
        if (BASE_DIR / 'templates' / filename).exists():
            return render_template(filename)
    return "Not Found", 404

if __name__ == '__main__':
    port = int(os.getenv('PORT', '5001'))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.logger.info(f"Fullstack server starting at http://127.0.0.1:{port} ...")
    app.run(debug=debug, port=port)
