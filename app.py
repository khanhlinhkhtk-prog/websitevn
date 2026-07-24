from flask import Flask, render_template, request, redirect, url_for, Response, g, has_request_context
import json, os, re, unicodedata, math, time, shutil, subprocess, tempfile
import html as html_lib
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from PIL import Image
from google import genai
import uuid
from datetime import datetime
from flask import session
import random
from flask import jsonify
import fitz  # PyMuPDF
from flask import flash
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from threading import Lock

load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")


@app.teardown_appcontext
def close_exam_db_connection(error=None):
    conn = getattr(g, "_exam_db_conn", None)
    if conn is not None:
        conn.close()

# Cấu hình thư mục upload
UPLOAD_FOLDER = os.path.join('static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.environ.get("GOOGLE_API_KEY") and os.environ.get("GOOGLE_API_KEYS"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GOOGLE_API_KEYS"].split(",")[0].strip()

GLOBAL_BACK_BUTTON_HTML = """
<style>
    .global-back-button {
        position: fixed !important;
        top: 16px !important;
        left: 16px !important;
        z-index: 2147483000;
        display: inline-flex !important;
        align-items: center;
        justify-content: center;
        width: auto !important;
        max-width: max-content !important;
        min-height: 40px !important;
        margin: 0 !important;
        padding: 0 14px !important;
        color: #1d4ed8 !important;
        background: rgba(255, 255, 255, 0.92) !important;
        border: 1px solid rgba(37, 99, 235, 0.35) !important;
        border-radius: 8px !important;
        box-shadow: 0 10px 24px rgba(37, 99, 235, 0.18) !important;
        font: 600 14px/1.2 Arial, sans-serif !important;
        text-decoration: none !important;
        cursor: pointer !important;
        backdrop-filter: blur(8px);
    }

    .global-back-button:hover {
        background: #ffffff !important;
        box-shadow: 0 14px 30px rgba(37, 99, 235, 0.24) !important;
    }

    @media (max-width: 560px) {
        .global-back-button {
            top: 10px !important;
            left: 10px !important;
            min-height: 36px !important;
            padding: 0 11px !important;
            font-size: 13px !important;
        }
    }
</style>
<button type="button" class="global-back-button" onclick="if (window.history.length > 1) { window.history.back(); } else { window.location.href = '/'; }">← Quay lại</button>
"""


@app.after_request
def add_global_back_button(response):
    if response.status_code != 200 or response.direct_passthrough:
        return response

    if request.path == "/":
        return response

    if request.path.startswith("/exam_system/"):
        return response

    content_type = response.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        return response

    html = response.get_data(as_text=True)
    existing_navigation_markers = (
        "global-back-button",
        "back-link",
        "back-button",
        "back-btn",
        "btn-back",
        "btn-home",
        "header-btn",
        "Quay lại",
        "Quay về",
        "Trang chủ",
    )

    if "</body>" not in html or any(marker in html for marker in existing_navigation_markers):
        return response

    html = html.replace("</body>", f"{GLOBAL_BACK_BUTTON_HTML}\n</body>", 1)
    response.set_data(html)
    response.headers["Content-Length"] = str(len(response.get_data()))
    return response

api_key = os.environ.get("GOOGLE_API_KEY")  # ← SỬA DÒNG NÀY
if not api_key:  
    raise ValueError(" Thiếu GOOGLE_API_KEY trong file .env")


def get_google_api_keys():
    keys = []
    multi_key_value = os.environ.get("GOOGLE_API_KEYS", "")
    single_key_value = os.environ.get("GOOGLE_API_KEY", "")

    for raw_key in multi_key_value.split(","):
        key = raw_key.strip()
        if key:
            keys.append(key)

    single_key = single_key_value.strip()
    if single_key and single_key not in keys:
        keys.append(single_key)

    return keys


def sanitize_gemini_error(error):
    message = str(error).replace('\n', ' ')
    for key in GOOGLE_API_KEYS if 'GOOGLE_API_KEYS' in globals() else []:
        if key:
            message = message.replace(key, '[REDACTED_API_KEY]')
    message = re.sub(r"api_key:[^'\"\s]+", "api_key:[REDACTED_API_KEY]", message)
    message = re.sub(r"AIza[0-9A-Za-z_-]+", "[REDACTED_API_KEY]", message)
    return message[:500]


class GeminiKeyRotationError(Exception):
    pass


class RotatingGeminiModel:
    def __init__(self, model_name, api_keys):
        self.model_name = model_name
        self.api_keys = api_keys
        self.current_key_index = 0
        self.key_blocked_until = [0] * len(api_keys)
        self.lock = Lock()

    def _normalized_model_name(self):
        if self.model_name.startswith("models/"):
            return self.model_name.split("/", 1)[1]
        return self.model_name

    def _is_limit_error(self, error):
        status_code = getattr(error, "code", None)
        status_name = getattr(error, "status", "")
        message = str(error).lower()

        return (
            status_code in (403, 429, 503)
            or str(status_code) in ("403", "429", "503")
            or "resource_exhausted" in status_name.lower()
            or "permission_denied" in status_name.lower()
            or "quota" in message
            or "rate limit" in message
            or "429" in message
            or "403" in message
            or "permission denied" in message
            or "permission_denied" in message
            or "suspended" in message
            or "consumer_suspended" in message
            or "api key not valid" in message
            or "api_key_invalid" in message
            or "invalid api key" in message
            or "invalid_argument" in message
        )

    def _set_current_key(self, key_index):
        with self.lock:
            self.current_key_index = key_index % len(self.api_keys)

    def _block_key_after_error(self, key_index, error):
        message = str(error).lower()
        status_name = str(getattr(error, "status", "")).lower()
        now = time.time()

        if (
            "consumer_suspended" in message
            or "suspended" in message
            or "api key not valid" in message
            or "api_key_invalid" in message
            or "invalid api key" in message
        ):
            block_seconds = 24 * 60 * 60
        elif "quota" in message or "rate limit" in message or "resource_exhausted" in status_name or "429" in message:
            block_seconds = 90
        else:
            block_seconds = 30

        with self.lock:
            self.key_blocked_until[key_index] = now + block_seconds

    def _available_key_indices(self, start_key_index):
        now = time.time()
        indices = [(start_key_index + attempt) % len(self.api_keys) for attempt in range(len(self.api_keys))]
        available = [index for index in indices if self.key_blocked_until[index] <= now]
        return available or indices

    def generate_content(self, *args, **kwargs):
        last_error = None
        total_keys = len(self.api_keys)

        with self.lock:
            start_key_index = self.current_key_index

        if args:
            contents = args[0]
            if len(args) > 1:
                raise TypeError("generate_content accepts one positional contents argument")
        else:
            contents = kwargs.pop("contents")

        for key_index in self._available_key_indices(start_key_index):
            api_key = self.api_keys[key_index]

            client = genai.Client(api_key=api_key)

            try:
                response = client.models.generate_content(
                    model=self._normalized_model_name(),
                    contents=contents,
                    **kwargs
                )
                self._set_current_key(key_index + 1)
                return response
            except Exception as error:
                last_error = error
                if total_keys == 1 or not self._is_limit_error(error):
                    raise

                self._block_key_after_error(key_index, error)
                self._set_current_key(key_index + 1)

        raise GeminiKeyRotationError(
            "Tri-hand chua goi duoc Gemini vi tat ca API key hien co dang het quota, "
            "bi khoa/suspended hoac khong hop le. Hay doi quota reset hoac them API key moi vao GOOGLE_API_KEYS trong .env."
        )


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "models/gemini-2.5-flash")
GOOGLE_API_KEYS = get_google_api_keys()
model = RotatingGeminiModel(GEMINI_MODEL, GOOGLE_API_KEYS)
analysis_model = model




CLASS_ACTIVITY_FILE = os.path.join('data', 'class_activities.json')
CLASS_ACTIVITY_IMAGES = os.path.join('static', 'class_activity_uploads')

# Tạo thư mục nếu chưa có
os.makedirs(os.path.dirname(CLASS_ACTIVITY_FILE), exist_ok=True)
os.makedirs(CLASS_ACTIVITY_IMAGES, exist_ok=True)
# Định nghĩa các extension được phép
#############

# ==========================================
# HỆ THỐNG KIỂM TRA CÓ GÌ PHẢI LO
# ==========================================

import hashlib
from werkzeug.security import generate_password_hash, check_password_hash
import mammoth  # Để đọc file .docx

# File paths
EXAM_USERS_FILE = os.path.join('data', 'exam_system_users.json')
EXAM_LESSONS_FILE = os.path.join('data', 'exam_system_lessons.json')
EXAM_EXAMS_FILE = os.path.join('data', 'exam_system_exams.json')
EXAM_SUBMISSIONS_FILE = os.path.join('data', 'exam_system_submissions.json')
EXAM_MATERIALS_FILE = os.path.join('data', 'exam_system_materials.json')
EXAM_CLASSES_FILE = os.path.join('data', 'exam_system_classes.json')
EXAM_COLLECTION_CONFIG = {
    'users': (EXAM_USERS_FILE, {"students": [], "teachers": [], "parents": []}, dict),
    'classes': (EXAM_CLASSES_FILE, [], list),
    'lessons': (EXAM_LESSONS_FILE, [], list),
    'exams': (EXAM_EXAMS_FILE, [], list),
    'submissions': (EXAM_SUBMISSIONS_FILE, [], list),
    'materials': (EXAM_MATERIALS_FILE, [], list),
}
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL")
DATABASE_SSLMODE = os.environ.get("DATABASE_SSLMODE", "require")
ADMIN_USERNAME = os.environ.get("EXAM_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = generate_password_hash(
    os.environ.get("EXAM_ADMIN_PASSWORD", "admin2026")
)

_exam_store_initialized = False


# Helper functions
def exam_db_enabled():
    return bool(DATABASE_URL)


def normalize_database_url(url):
    if url and url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def create_exam_db_connection():
    try:
        import psycopg2
    except ImportError as exc:
        raise RuntimeError(
            "DATABASE_URL is configured but psycopg2-binary is not installed."
        ) from exc

    dsn = normalize_database_url(DATABASE_URL)
    kwargs = {"connect_timeout": 10}
    if "sslmode=" not in dsn:
        kwargs["sslmode"] = DATABASE_SSLMODE
    return psycopg2.connect(dsn, **kwargs)


def get_exam_db_connection():
    if has_request_context():
        conn = getattr(g, "_exam_db_conn", None)
        if conn is None or conn.closed:
            conn = create_exam_db_connection()
            g._exam_db_conn = conn
        return conn
    return create_exam_db_connection()


def get_exam_collection_cache():
    if not has_request_context():
        return None
    cache = getattr(g, "_exam_collection_cache", None)
    if cache is None:
        cache = {}
        g._exam_collection_cache = cache
    return cache


def ensure_exam_store_table():
    global _exam_store_initialized
    if _exam_store_initialized or not exam_db_enabled():
        return

    with get_exam_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS exam_system_store (
                    collection TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
    _exam_store_initialized = True


def read_json_file(path, fallback):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if data is not None else fallback
    except FileNotFoundError:
        return fallback
    except json.JSONDecodeError:
        return fallback


def write_json_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_collection_payload(data, fallback, expected_type=None):
    if expected_type and not isinstance(data, expected_type):
        data = fallback
    return data


def preload_exam_collections(cache):
    if cache is None or cache.get("__bulk_loaded"):
        return

    ensure_exam_store_table()
    collection_names = list(EXAM_COLLECTION_CONFIG.keys())
    with get_exam_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT collection, payload
                FROM exam_system_store
                WHERE collection = ANY(%s)
                """,
                (collection_names,)
            )
            rows = {collection: payload for collection, payload in cur.fetchall()}

            missing_rows = []
            for collection, (path, fallback, expected_type) in EXAM_COLLECTION_CONFIG.items():
                if collection in rows:
                    payload = normalize_collection_payload(rows[collection], fallback, expected_type)
                else:
                    payload = read_json_file(path, fallback)
                    payload = normalize_collection_payload(payload, fallback, expected_type)
                    missing_rows.append((collection, payload))

                if collection not in cache:
                    cache[collection] = payload

            if missing_rows:
                from psycopg2.extras import Json
                cur.executemany(
                    """
                    INSERT INTO exam_system_store (collection, payload, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (collection)
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                    """,
                    [(collection, Json(payload)) for collection, payload in missing_rows]
                )

    cache["__bulk_loaded"] = True


def load_exam_collection(collection, path, fallback, expected_type=None):
    cache = get_exam_collection_cache()
    if cache is not None and collection in cache:
        return cache[collection]

    if not exam_db_enabled():
        data = read_json_file(path, fallback)
        data = normalize_collection_payload(data, fallback, expected_type)
        if cache is not None:
            cache[collection] = data
        return data

    if cache is not None:
        preload_exam_collections(cache)
        if collection in cache:
            return cache[collection]

    ensure_exam_store_table()
    with get_exam_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM exam_system_store WHERE collection = %s",
                (collection,)
            )
            row = cur.fetchone()
            if row:
                data = normalize_collection_payload(row[0], fallback, expected_type)
                if cache is not None:
                    cache[collection] = data
                return data

            from psycopg2.extras import Json
            fallback_data = read_json_file(path, fallback)
            fallback_data = normalize_collection_payload(fallback_data, fallback, expected_type)
            cur.execute(
                """
                INSERT INTO exam_system_store (collection, payload, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (collection)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                """,
                (collection, Json(fallback_data))
            )
            if cache is not None:
                cache[collection] = fallback_data
            return fallback_data


def save_exam_collection(collection, path, data):
    cache = get_exam_collection_cache()
    if cache is not None:
        cache[collection] = data

    if not exam_db_enabled():
        write_json_file(path, data)
        return

    ensure_exam_store_table()
    with get_exam_db_connection() as conn:
        with conn.cursor() as cur:
            from psycopg2.extras import Json
            cur.execute(
                """
                INSERT INTO exam_system_store (collection, payload, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (collection)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                """,
                (collection, Json(data))
            )


def load_exam_users():
    data = load_exam_collection('users', EXAM_USERS_FILE, {}, dict)

    if not isinstance(data, dict):
        data = {}
    data.setdefault("students", [])
    data.setdefault("teachers", [])
    data.setdefault("parents", [])
    return data


def save_exam_users(data):
    save_exam_collection('users', EXAM_USERS_FILE, data)


def load_exam_lessons():
    return load_exam_collection('lessons', EXAM_LESSONS_FILE, [], list)


def save_exam_lessons(data):
    save_exam_collection('lessons', EXAM_LESSONS_FILE, data)


def load_exam_exams():
    return load_exam_collection('exams', EXAM_EXAMS_FILE, [], list)


def save_exam_exams(data):
    save_exam_collection('exams', EXAM_EXAMS_FILE, data)


def load_exam_submissions():
    return load_exam_collection('submissions', EXAM_SUBMISSIONS_FILE, [], list)


def save_exam_submissions(data):
    save_exam_collection('submissions', EXAM_SUBMISSIONS_FILE, data)


def load_exam_materials():
    return load_exam_collection('materials', EXAM_MATERIALS_FILE, [], list)


def save_exam_materials(data):
    save_exam_collection('materials', EXAM_MATERIALS_FILE, data)


def load_exam_classes():
    return load_exam_collection('classes', EXAM_CLASSES_FILE, [], list)


def save_exam_classes(data):
    save_exam_collection('classes', EXAM_CLASSES_FILE, data)


def generate_class_code(classes):
    existing_codes = {c.get('class_code') for c in classes}
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    while True:
        code = ''.join(random.choice(alphabet) for _ in range(6))
        if code not in existing_codes:
            return code


def generate_join_password():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(random.choice(alphabet) for _ in range(6))


def is_admin_logged_in():
    return session.get('admin_logged_in') is True


def require_admin():
    if not is_admin_logged_in():
        flash('Vui lòng đăng nhập admin!', 'error')
        return redirect(url_for('admin_login'))
    return None


def require_teacher():
    if session.get('exam_user_type') != 'teacher':
        flash('Vui lòng đăng nhập với tư cách giáo viên!', 'error')
        return redirect(url_for('exam_teacher_login'))
    return None


def require_parent():
    if session.get('exam_user_type') != 'parent':
        flash('Vui lòng đăng nhập với tư cách phụ huynh!', 'error')
        return redirect(url_for('exam_parent_login'))
    return None


def get_teacher_class(class_id):
    teacher_id = session.get('exam_user_id')
    return next(
        (
            c for c in load_exam_classes()
            if c.get('id') == class_id and c.get('teacher_id') == teacher_id
        ),
        None
    )


def student_in_class(class_obj, student_id):
    return student_id in class_obj.get('student_ids', [])


def get_student_classes(student_id):
    return [
        c for c in load_exam_classes()
        if student_in_class(c, student_id)
    ]


def get_student_by_id(student_id):
    users = load_exam_users()
    return next((s for s in users.get('students', []) if s.get('id') == student_id), None)


def get_parent_context(parent_id=None):
    users = load_exam_users()
    parent_id = parent_id or session.get('exam_user_id')
    parent = next((p for p in users.get('parents', []) if p.get('id') == parent_id), None)
    if not parent or parent.get('active', True) is False:
        return None

    class_obj = next(
        (c for c in load_exam_classes() if c.get('id') == parent.get('class_id')),
        None
    )
    student = get_student_by_id(parent.get('student_id'))
    if not class_obj or not student or not student_in_class(class_obj, student.get('id')):
        return None

    return {
        'parent': parent,
        'student': student,
        'class_obj': class_obj
    }


def build_class_stats(class_obj):
    class_id = class_obj.get('id')
    lessons = [l for l in load_exam_lessons() if l.get('class_id') == class_id]
    exams = [e for e in load_exam_exams() if e.get('class_id') == class_id]
    materials = [m for m in load_exam_materials() if m.get('class_id') == class_id]
    submissions = [
        s for s in load_exam_submissions()
        if any(e.get('id') == s.get('exam_id') for e in exams)
    ]
    avg_score = None
    if submissions:
        avg_score = round(sum(float(s.get('score', 0)) for s in submissions) / len(submissions), 2)
    return {
        'student_count': len(class_obj.get('student_ids', [])),
        'lesson_count': len(lessons),
        'exam_count': len(exams),
        'material_count': len(materials),
        'submission_count': len(submissions),
        'avg_score': avg_score
    }


def parse_exam_datetime(value):
    try:
        return datetime.strptime(value or '', "%d/%m/%Y %H:%M")
    except ValueError:
        return datetime.min


def remove_vietnamese_accents(text):
    text = unicodedata.normalize('NFD', str(text or ''))
    return ''.join(char for char in text if unicodedata.category(char) != 'Mn')


def classify_error_topic(text):
    normalized = remove_vietnamese_accents(text or '').lower()
    topic_rules = [
        ('Điều kiện xác định', ['dieu kien', 'xac dinh']),
        ('Căn bậc hai', ['can bac hai', 'sqrt', 'can thuc']),
        ('Rút gọn biểu thức', ['rut gon', 'bien doi', 'hang dang thuc']),
        ('Phương trình', ['phuong trinh', 'nghiem']),
        ('Tính toán', ['tinh', 'gia tri', 'bang bao nhieu']),
        ('Hình học', ['hinh hoc', 'tam giac', 'goc', 'duong tron'])
    ]
    for topic, keywords in topic_rules:
        if any(keyword in normalized for keyword in keywords):
            return topic
    return 'Kiến thức khác'


def build_teacher_class_analysis(class_obj, students=None, exams=None, submissions=None):
    class_id = class_obj.get('id')
    users = load_exam_users()
    students = students if students is not None else [
        s for s in users.get('students', [])
        if s.get('id') in class_obj.get('student_ids', [])
    ]
    exams = exams if exams is not None else [
        e for e in load_exam_exams()
        if e.get('class_id') == class_id
    ]
    exam_lookup = {exam.get('id'): exam for exam in exams}
    submissions = submissions if submissions is not None else [
        sub for sub in load_exam_submissions()
        if sub.get('exam_id') in exam_lookup
    ]

    submissions_by_student = {}
    for sub in submissions:
        submissions_by_student.setdefault(sub.get('student_id'), []).append(sub)

    ranking_rows = []
    for student in students:
        student_submissions = sorted(
            submissions_by_student.get(student.get('id'), []),
            key=lambda sub: parse_exam_datetime(sub.get('submitted_at'))
        )
        scores = [float(sub.get('score', 0)) for sub in student_submissions]
        avg_score = round(sum(scores) / len(scores), 2) if scores else None
        improvement = round(scores[-1] - scores[0], 2) if len(scores) >= 2 else None
        ranking_rows.append({
            'student': student,
            'submission_count': len(student_submissions),
            'avg_score': avg_score,
            'best_score': max(scores) if scores else None,
            'latest_score': scores[-1] if scores else None,
            'improvement': improvement
        })

    ranking_rows.sort(
        key=lambda row: (
            row['avg_score'] is not None,
            row['avg_score'] if row['avg_score'] is not None else -1,
            row['submission_count']
        ),
        reverse=True
    )
    improvement_rows = sorted(
        [row for row in ranking_rows if row['improvement'] is not None],
        key=lambda row: row['improvement'],
        reverse=True
    )

    error_topics = {}
    question_errors = {}
    total_checked_answers = 0
    total_wrong_answers = 0
    for sub in submissions:
        for result in sub.get('detailed_results', []):
            total_checked_answers += 1
            is_wrong = result.get('is_correct') is False
            if 'is_correct' not in result:
                score = float(result.get('score', 0) or 0)
                points = float(result.get('points', 0) or 0)
                is_wrong = points > 0 and score < points * 0.6
            if not is_wrong:
                continue

            total_wrong_answers += 1
            question_text = result.get('question', '')
            topic = classify_error_topic(
                f"{question_text} {result.get('explanation', '')} {result.get('feedback', '')}"
            )
            error_topics[topic] = error_topics.get(topic, 0) + 1
            key = question_text[:140] or f"Câu {result.get('question_id', '')}"
            question_errors[key] = question_errors.get(key, 0) + 1

    error_rows = [
        {
            'topic': topic,
            'wrong_count': count,
            'percent': round((count / total_wrong_answers) * 100, 1) if total_wrong_answers else 0
        }
        for topic, count in error_topics.items()
    ]
    error_rows.sort(key=lambda row: row['wrong_count'], reverse=True)

    question_error_rows = [
        {'question': question, 'wrong_count': count}
        for question, count in question_errors.items()
    ]
    question_error_rows.sort(key=lambda row: row['wrong_count'], reverse=True)

    weak_students = [
        row for row in ranking_rows
        if row['avg_score'] is not None and row['avg_score'] < 5
    ]
    strong_students = [
        row for row in ranking_rows
        if row['avg_score'] is not None and row['avg_score'] >= 8
    ]
    students_without_submissions = [
        row for row in ranking_rows
        if row['submission_count'] == 0
    ]

    recommendations = []
    for row in error_rows[:3]:
        recommendations.append(
            f"Ôn lại nhóm '{row['topic']}' vì đang chiếm {row['percent']}% lỗi sai đã ghi nhận."
        )
    if weak_students:
        recommendations.append(
            f"Tổ chức nhóm hỗ trợ cho {len(weak_students)} học sinh có điểm trung bình dưới 5."
        )
    if students_without_submissions:
        recommendations.append(
            f"Nhắc {len(students_without_submissions)} học sinh chưa có bài nộp tham gia làm bài."
        )
    if not recommendations:
        recommendations.append('Chưa đủ dữ liệu bài nộp để đề xuất trọng tâm luyện tập.')

    saved_reviews = class_obj.get('student_reviews', {})
    review_rows = []
    for row in ranking_rows:
        student = row['student']
        student_id = student.get('id')
        saved = saved_reviews.get(student_id, {})
        if row['avg_score'] is None:
            draft = 'Em chưa có dữ liệu bài làm trong lớp. Giáo viên cần nhắc em tham gia các bài kiểm tra để theo dõi tiến bộ.'
        elif row['avg_score'] >= 8:
            draft = f"Em đang có kết quả tốt với điểm trung bình {row['avg_score']}/10. Cần duy trì nhịp học và thử thêm bài vận dụng."
        elif row['avg_score'] >= 5:
            draft = f"Em đã nắm được kiến thức cơ bản với điểm trung bình {row['avg_score']}/10. Cần luyện thêm các dạng còn sai để ổn định kết quả."
        else:
            draft = f"Em cần được hỗ trợ thêm vì điểm trung bình hiện là {row['avg_score']}/10. Nên ôn lại kiến thức nền và làm bài bổ trợ ngắn."
        review_rows.append({
            'student': student,
            'avg_score': row['avg_score'],
            'submission_count': row['submission_count'],
            'comment': saved.get('comment') or draft,
            'published': saved.get('published', False)
        })

    class_avg = None
    scored_rows = [row for row in ranking_rows if row['avg_score'] is not None]
    if scored_rows:
        class_avg = round(sum(row['avg_score'] for row in scored_rows) / len(scored_rows), 2)

    return {
        'ranking_rows': ranking_rows,
        'improvement_rows': improvement_rows,
        'error_rows': error_rows,
        'question_error_rows': question_error_rows,
        'recommendations': recommendations,
        'review_rows': review_rows,
        'class_avg': class_avg,
        'strong_count': len(strong_students),
        'weak_count': len(weak_students),
        'not_started_count': len(students_without_submissions),
        'total_checked_answers': total_checked_answers,
        'total_wrong_answers': total_wrong_answers
    }


def build_teacher_overview_report(class_rows):
    class_count = len(class_rows)
    classes_with_scores = [
        row for row in class_rows
        if row['stats'].get('avg_score') is not None
    ]
    total_submissions = sum(row['stats'].get('submission_count', 0) for row in class_rows)
    total_students = sum(row['stats'].get('student_count', 0) for row in class_rows)
    avg_score = None
    if classes_with_scores:
        avg_score = round(
            sum(row['stats']['avg_score'] for row in classes_with_scores) / len(classes_with_scores),
            2
        )

    strengths = []
    weaknesses = []
    actions = []

    strong_classes = [
        row for row in classes_with_scores
        if row['stats']['avg_score'] >= 8
    ]
    weak_classes = [
        row for row in classes_with_scores
        if row['stats']['avg_score'] < 5
    ]
    inactive_classes = [
        row for row in class_rows
        if row['stats'].get('submission_count', 0) == 0
    ]

    if strong_classes:
        names = ', '.join(row['class'].get('name', 'Lớp học') for row in strong_classes[:3])
        strengths.append(f"Các lớp có mặt bằng điểm tốt: {names}.")
    if total_submissions:
        strengths.append(f"Đã ghi nhận {total_submissions} bài nộp để theo dõi chất lượng học tập.")
    if not strengths:
        strengths.append('Đã có cấu trúc lớp học, đề kiểm tra và học sinh để bắt đầu thu dữ liệu.')

    if weak_classes:
        names = ', '.join(row['class'].get('name', 'Lớp học') for row in weak_classes[:3])
        weaknesses.append(f"Các lớp cần hỗ trợ thêm: {names}.")
    if inactive_classes:
        weaknesses.append(f"{len(inactive_classes)} lớp chưa có bài nộp nên chưa đủ dữ liệu phân tích.")
    if not weaknesses:
        weaknesses.append('Chưa phát hiện lớp có điểm trung bình dưới 5 từ dữ liệu hiện tại.')

    if inactive_classes:
        actions.append('Ưu tiên giao một bài kiểm tra ngắn cho các lớp chưa có dữ liệu.')
    if weak_classes:
        actions.append('Mở tiết luyện tập bổ trợ cho lớp có điểm trung bình thấp.')
    actions.append('Theo dõi dashboard từng lớp sau mỗi bài kiểm tra để cập nhật lỗ hổng kiến thức.')

    return {
        'class_count': class_count,
        'total_students': total_students,
        'total_submissions': total_submissions,
        'avg_score': avg_score,
        'strengths': strengths,
        'weaknesses': weaknesses,
        'actions': actions
    }


def classify_learning_axis(text):
    normalized = remove_vietnamese_accents(text or '').lower()
    if any(keyword in normalized for keyword in ['hinh hoc', 'tam giac', 'goc', 'duong tron', 'dien tich']):
        return 'Hình học'
    if any(keyword in normalized for keyword in ['thuc te', 'van dung', 'bai toan', 'ti le', 'phan tram']):
        return 'Toán thực tế'
    if any(keyword in normalized for keyword in ['phuong trinh', 'bieu thuc', 'hang dang thuc', 'da thuc', 'an so']):
        return 'Đại số'
    if any(keyword in normalized for keyword in ['suy luan', 'chung minh', 'logic', 'lap luan']):
        return 'Tư duy logic'
    return 'Kỹ năng tính toán'


def build_student_learning_profile(student_id, class_id=None):
    users = load_exam_users()
    student = next((s for s in users.get('students', []) if s.get('id') == student_id), None)
    classes = get_student_classes(student_id)
    if class_id:
        classes = [c for c in classes if c.get('id') == class_id]

    class_ids = {c.get('id') for c in classes}
    lessons = [l for l in load_exam_lessons() if l.get('class_id') in class_ids]
    materials = [m for m in load_exam_materials() if m.get('class_id') in class_ids]
    exams = [e for e in load_exam_exams() if e.get('class_id') in class_ids]
    exam_lookup = {e.get('id'): e for e in exams}
    submissions = [
        s for s in load_exam_submissions()
        if s.get('student_id') == student_id and s.get('exam_id') in exam_lookup
    ]
    submissions.sort(key=lambda sub: parse_exam_datetime(sub.get('submitted_at')))

    scores = [float(s.get('score', 0) or 0) for s in submissions]
    avg_score = round(sum(scores) / len(scores), 2) if scores else None
    latest_score = scores[-1] if scores else None
    best_score = max(scores) if scores else None
    improvement = round(scores[-1] - scores[0], 2) if len(scores) >= 2 else None

    axis_order = ['Đại số', 'Hình học', 'Toán thực tế', 'Kỹ năng tính toán', 'Tư duy logic']
    axis_data = {axis: {'correct': 0, 'total': 0} for axis in axis_order}
    weak_topics = {}
    for sub in submissions:
        for result in sub.get('detailed_results', []):
            text = ' '.join([
                str(result.get('question', '')),
                str(result.get('explanation', '')),
                str(result.get('feedback', ''))
            ])
            axis = classify_learning_axis(text)
            axis_data.setdefault(axis, {'correct': 0, 'total': 0})
            axis_data[axis]['total'] += 1

            if 'is_correct' in result:
                is_correct = result.get('is_correct') is True
            else:
                points = float(result.get('points', 0) or 0)
                score = float(result.get('score', 0) or 0)
                is_correct = points > 0 and score >= points * 0.6

            if is_correct:
                axis_data[axis]['correct'] += 1
            else:
                weak_topics[axis] = weak_topics.get(axis, 0) + 1

    radar_rows = []
    for axis in axis_order:
        total = axis_data.get(axis, {}).get('total', 0)
        correct = axis_data.get(axis, {}).get('correct', 0)
        percent = round((correct / total) * 100, 1) if total else None
        radar_rows.append({
            'axis': axis,
            'correct': correct,
            'total': total,
            'percent': percent
        })

    if avg_score is None:
        ai_comment = 'Chưa có dữ liệu bài làm. Học sinh nên hoàn thành ít nhất một đề kiểm tra để hệ thống bắt đầu phân tích tiến bộ.'
    elif avg_score >= 8:
        ai_comment = f'Học sinh đang có nền tảng tốt với điểm trung bình {avg_score}/10. Nên tiếp tục luyện bài vận dụng để giữ nhịp tiến bộ.'
    elif avg_score >= 5:
        ai_comment = f'Học sinh đã nắm được phần cơ bản với điểm trung bình {avg_score}/10. Cần tập trung vào nhóm kỹ năng còn sai để ổn định kết quả.'
    else:
        ai_comment = f'Học sinh cần được hỗ trợ thêm vì điểm trung bình hiện là {avg_score}/10. Nên ôn lại kiến thức nền và làm các bài ngắn theo từng dạng.'

    weak_axes = sorted(
        [row for row in radar_rows if row['percent'] is not None],
        key=lambda row: row['percent']
    )
    recommendations = []
    for row in weak_axes[:3]:
        if row['percent'] < 75:
            recommendations.append(f"Ôn lại nhóm {row['axis']} vì tỉ lệ đúng hiện khoảng {row['percent']}%.")
    if lessons:
        recommendations.append(f"Xem lại bài giảng gần nhất: {lessons[-1].get('title', 'Bài giảng của lớp')}.")
    if materials:
        recommendations.append(f"Mở học liệu bổ trợ: {materials[-1].get('title', 'Kho học liệu của lớp')}.")
    if exams:
        recommendations.append(f"Làm hoặc làm lại đề: {exams[-1].get('title', 'Đề kiểm tra của lớp')}.")
    if not recommendations:
        recommendations.append('Chưa có đủ học liệu hoặc bài kiểm tra trong lớp. Hãy theo dõi khi giáo viên cập nhật nội dung mới.')

    published_reviews = []
    for class_obj in classes:
        saved = class_obj.get('student_reviews', {}).get(student_id)
        if saved and saved.get('published'):
            published_reviews.append({
                'class': class_obj,
                'comment': saved.get('comment', ''),
                'updated_at': saved.get('updated_at', '')
            })

    progress_points = []
    for sub in submissions:
        exam = exam_lookup.get(sub.get('exam_id'), {})
        progress_points.append({
            'label': f"{exam.get('title', 'Bài kiểm tra')} - {sub.get('submitted_at', '')}",
            'score': float(sub.get('score', 0) or 0),
            'exam_title': exam.get('title', 'Bài kiểm tra'),
            'submitted_at': sub.get('submitted_at', ''),
            'submission_id': sub.get('id')
        })

    return {
        'student': student,
        'classes': classes,
        'class_ids': list(class_ids),
        'lessons': lessons,
        'materials': materials,
        'exams': exams,
        'submissions': submissions,
        'avg_score': avg_score,
        'latest_score': latest_score,
        'best_score': best_score,
        'improvement': improvement,
        'radar_rows': radar_rows,
        'radar_labels': [row['axis'] for row in radar_rows],
        'radar_values': [row['percent'] if row['percent'] is not None else 0 for row in radar_rows],
        'progress_points': progress_points,
        'progress_labels': [point['label'] for point in progress_points],
        'progress_scores': [point['score'] for point in progress_points],
        'ai_comment': ai_comment,
        'recommendations': recommendations,
        'published_reviews': published_reviews
    }


def build_admin_report_data():
    users = load_exam_users()
    teachers = users.get('teachers', [])
    students = users.get('students', [])
    parents = users.get('parents', [])
    classes = load_exam_classes()

    teacher_lookup = {teacher.get('id'): teacher for teacher in teachers}
    student_lookup = {student.get('id'): student for student in students}
    class_lookup = {class_obj.get('id'): class_obj for class_obj in classes}
    class_rows = []
    for class_obj in classes:
        teacher = teacher_lookup.get(class_obj.get('teacher_id'), {})
        stats = build_class_stats(class_obj)
        class_rows.append({
            'class': class_obj,
            'teacher': teacher,
            'stats': stats
        })

    teacher_rows = []
    for teacher in teachers:
        teacher_classes = [
            row for row in class_rows
            if row['class'].get('teacher_id') == teacher.get('id')
        ]
        teacher_rows.append({
            'teacher': teacher,
            'class_count': len(teacher_classes),
            'student_count': sum(row['stats']['student_count'] for row in teacher_classes),
            'lesson_count': sum(row['stats']['lesson_count'] for row in teacher_classes),
            'exam_count': sum(row['stats']['exam_count'] for row in teacher_classes),
            'submission_count': sum(row['stats']['submission_count'] for row in teacher_classes)
        })

    parent_rows = []
    parent_counts_by_class = {}
    for parent in parents:
        class_id = parent.get('class_id')
        parent_counts_by_class[class_id] = parent_counts_by_class.get(class_id, 0) + 1
        parent_rows.append({
            'parent': parent,
            'class': class_lookup.get(parent.get('class_id'), {}),
            'student': student_lookup.get(parent.get('student_id'), {})
        })

    for row in class_rows:
        row['parent_count'] = parent_counts_by_class.get(row['class'].get('id'), 0)

    student_options = []
    for class_obj in classes:
        for student_id in class_obj.get('student_ids', []):
            student = student_lookup.get(student_id)
            if student:
                student_options.append({
                    'class_id': class_obj.get('id'),
                    'class_name': class_obj.get('name'),
                    'student_id': student.get('id'),
                    'student_name': student.get('full_name'),
                    'student_username': student.get('username')
                })

    subject_counts = {}
    for row in class_rows:
        subject = row['class'].get('subject') or 'Chưa phân môn'
        subject_counts[subject] = subject_counts.get(subject, 0) + 1

    summary = {
        'teacher_count': len(teachers),
        'active_teacher_count': len([t for t in teachers if t.get('active', True) is not False]),
        'student_count': len(students),
        'parent_count': len(parents),
        'active_parent_count': len([p for p in parents if p.get('active', True) is not False]),
        'class_count': len(classes),
        'lesson_count': sum(row['stats']['lesson_count'] for row in class_rows),
        'exam_count': sum(row['stats']['exam_count'] for row in class_rows),
        'submission_count': sum(row['stats']['submission_count'] for row in class_rows),
        'storage_backend': 'PostgreSQL/Supabase' if exam_db_enabled() else 'JSON local'
    }

    return {
        'summary': summary,
        'teacher_rows': teacher_rows,
        'parent_rows': parent_rows,
        'student_options': student_options,
        'class_rows': class_rows,
        'class_chart_labels': [row['class'].get('name', 'Lớp học') for row in class_rows],
        'class_student_counts': [row['stats']['student_count'] for row in class_rows],
        'subject_chart_labels': list(subject_counts.keys()),
        'subject_chart_counts': list(subject_counts.values())
    }


def is_google_drive_url(url):
    url = (url or '').strip()
    return (
        url.startswith(('https://drive.google.com/', 'http://drive.google.com/'))
        or url.startswith(('https://docs.google.com/', 'http://docs.google.com/'))
    )


def get_google_embed_url(url):
    url = (url or '').strip()

    folder_match = re.search(r'drive\.google\.com/drive/folders/([^/?#]+)', url)
    if folder_match:
        return f"https://drive.google.com/embeddedfolderview?id={folder_match.group(1)}#list"

    file_match = re.search(r'drive\.google\.com/file/d/([^/?#]+)', url)
    if file_match:
        return f"https://drive.google.com/file/d/{file_match.group(1)}/preview"

    id_match = re.search(r'[?&]id=([^&#]+)', url)
    if 'drive.google.com' in url and id_match:
        return f"https://drive.google.com/file/d/{id_match.group(1)}/preview"

    docs_match = re.search(
        r'docs\.google\.com/(document|spreadsheets|presentation|forms)/d/([^/?#]+)',
        url
    )
    if docs_match:
        doc_type, doc_id = docs_match.groups()
        return f"https://docs.google.com/{doc_type}/d/{doc_id}/preview"

    return url


# ---------------- AUTHENTICATION ----------------
@app.route('/exam_system/student_register', methods=['GET', 'POST'])
def exam_student_register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        full_name = request.form.get('full_name', '').strip()
        class_name = request.form.get('class_name', '').strip()
        email = request.form.get('email', '').strip()

        if not all([username, password, full_name, class_name]):
            flash('Vui lòng nhập đầy đủ thông tin!', 'error')
            return redirect(url_for('exam_student_register'))

        users = load_exam_users()
        if any(s['username'] == username for s in users['students']):
            flash('Tên đăng nhập đã tồn tại!', 'error')
            return redirect(url_for('exam_student_register'))

        new_student = {
            'id': str(uuid.uuid4()),
            'username': username,
            'password': generate_password_hash(password),
            'full_name': full_name,
            'class': class_name,
            'email': email,
            'created_at': datetime.now().strftime("%d/%m/%Y %H:%M")
        }
        users['students'].append(new_student)
        save_exam_users(users)

        flash('Đăng ký thành công! Hãy đăng nhập.', 'success')
        return redirect(url_for('exam_student_login'))

    return render_template('exam_system/auth/student_register.html')


@app.route('/exam_system/student_login', methods=['GET', 'POST'])
def exam_student_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        users = load_exam_users()
        student = next(
            (s for s in users['students'] if s['username'] == username), None)

        if student and check_password_hash(student['password'], password):
            session['exam_user_type'] = 'student'
            session['exam_user_id'] = student['id']
            session['exam_user_name'] = student['full_name']
            flash('Đăng nhập thành công!', 'success')
            return redirect(url_for('student_dashboard'))
        else:
            flash('Sai tên đăng nhập hoặc mật khẩu!', 'error')

    return render_template('exam_system/auth/student_login.html')


@app.route('/exam_system/teacher_login', methods=['GET', 'POST'])
def exam_teacher_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        users = load_exam_users()
        teacher = next(
            (t for t in users['teachers'] if t['username'] == username), None)

        if teacher:
            if teacher.get('active', True) is False:
                flash('Tài khoản giáo viên đã bị khóa!', 'error')
                return render_template('exam_system/auth/teacher_login.html')

            # Kiểm tra xem password có phải hash không
            teacher_password = teacher['password']

            # Nếu password bắt đầu bằng 'pbkdf2:', 'scrypt:', 'bcrypt:' thì là hash
            if teacher_password.startswith(('pbkdf2:', 'scrypt:', 'bcrypt:')):
                # So sánh dạng hash
                if check_password_hash(teacher_password, password):
                    session['exam_user_type'] = 'teacher'
                    session['exam_user_id'] = teacher['id']
                    session['exam_user_name'] = teacher['full_name']
                    session['exam_subject'] = teacher.get('subject', 'Chung')
                    flash('Đăng nhập thành công!', 'success')
                    return redirect(url_for('teacher_dashboard'))
            else:
                # So sánh plain text
                if teacher_password == password:
                    session['exam_user_type'] = 'teacher'
                    session['exam_user_id'] = teacher['id']
                    session['exam_user_name'] = teacher['full_name']
                    session['exam_subject'] = teacher.get('subject', 'Chung')
                    flash('Đăng nhập thành công!', 'success')
                    return redirect(url_for('teacher_dashboard'))

        flash('Sai tên đăng nhập hoặc mật khẩu!', 'error')

    return render_template('exam_system/auth/teacher_login.html')


@app.route('/exam_system/parent_login', methods=['GET', 'POST'])
def exam_parent_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        users = load_exam_users()
        parent = next(
            (p for p in users.get('parents', []) if p.get('username') == username),
            None
        )

        if parent:
            if parent.get('active', True) is False:
                flash('Tài khoản phụ huynh đã bị khóa!', 'error')
                return render_template('exam_system/auth/parent_login.html')

            parent_password = parent.get('password', '')
            password_ok = (
                check_password_hash(parent_password, password)
                if parent_password.startswith(('pbkdf2:', 'scrypt:', 'bcrypt:'))
                else parent_password == password
            )
            if password_ok:
                session['exam_user_type'] = 'parent'
                session['exam_user_id'] = parent.get('id')
                session['exam_user_name'] = parent.get('full_name')
                session['exam_parent_student_id'] = parent.get('student_id')
                session['exam_parent_class_id'] = parent.get('class_id')
                flash('Đăng nhập phụ huynh thành công!', 'success')
                return redirect(url_for('parent_dashboard'))

        flash('Sai tên đăng nhập hoặc mật khẩu phụ huynh!', 'error')

    return render_template('exam_system/auth/parent_login.html')


@app.route('/exam_system/logout')
def exam_logout():
    session.pop('exam_user_type', None)
    session.pop('exam_user_id', None)
    session.pop('exam_user_name', None)
    session.pop('exam_subject', None)
    session.pop('exam_parent_student_id', None)
    session.pop('exam_parent_class_id', None)
    flash('Đã đăng xuất!', 'info')
    return redirect(url_for('exam_student_login'))


# ---------------- ADMIN ROUTES ----------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session.clear()
            session['admin_logged_in'] = True
            session['admin_username'] = username
            flash('Đăng nhập admin thành công!', 'success')
            return redirect(url_for('admin_dashboard'))
        flash('Sai tài khoản hoặc mật khẩu admin!', 'error')
    return render_template('admin/login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_username', None)
    flash('Đã đăng xuất admin!', 'info')
    return redirect(url_for('admin_login'))


@app.route('/admin/dashboard', methods=['GET', 'POST'])
def admin_dashboard():
    blocked = require_admin()
    if blocked:
        return blocked

    users = load_exam_users()
    users.setdefault('teachers', [])
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        full_name = request.form.get('full_name', '').strip()
        subject = request.form.get('subject', '').strip()
        email = request.form.get('email', '').strip()

        if not all([username, password, full_name, subject]):
            flash('Vui lòng nhập đủ tên đăng nhập, mật khẩu, họ tên và môn dạy.', 'error')
            return redirect(url_for('admin_dashboard'))

        if any(t.get('username') == username for t in users['teachers']):
            flash('Tên đăng nhập giáo viên đã tồn tại.', 'error')
            return redirect(url_for('admin_dashboard'))

        users['teachers'].append({
            'id': str(uuid.uuid4()),
            'username': username,
            'password': generate_password_hash(password),
            'full_name': full_name,
            'subject': subject,
            'email': email,
            'active': True,
            'created_at': datetime.now().strftime("%d/%m/%Y %H:%M")
        })
        save_exam_users(users)
        flash('Đã tạo tài khoản giáo viên.', 'success')
        return redirect(url_for('admin_dashboard'))

    report_data = build_admin_report_data()

    return render_template('admin/dashboard.html', **report_data)


@app.route('/admin/classes/<class_id>/parents')
def admin_class_parents(class_id):
    blocked = require_admin()
    if blocked:
        return blocked

    users = load_exam_users()
    classes = load_exam_classes()
    class_obj = next((c for c in classes if c.get('id') == class_id), None)
    if not class_obj:
        flash('Không tìm thấy lớp học.', 'error')
        return redirect(url_for('admin_dashboard'))

    student_lookup = {
        student.get('id'): student
        for student in users.get('students', [])
    }
    parent_rows = []
    for parent in users.get('parents', []):
        if parent.get('class_id') != class_id:
            continue
        parent_rows.append({
            'parent': parent,
            'student': student_lookup.get(parent.get('student_id'), {})
        })

    parent_rows.sort(
        key=lambda row: (
            (row['student'].get('full_name') or '').lower(),
            (row['parent'].get('full_name') or '').lower()
        )
    )
    return render_template(
        'admin/class_parents.html',
        class_obj=class_obj,
        parent_rows=parent_rows
    )


@app.route('/admin/export_report')
def admin_export_report():
    blocked = require_admin()
    if blocked:
        return blocked

    report_data = build_admin_report_data()
    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M")
    summary = report_data['summary']
    teacher_rows = report_data['teacher_rows']
    parent_rows = report_data['parent_rows']
    class_rows = report_data['class_rows']

    def cell(value):
        return html_lib.escape(str(value if value is not None else ''))

    teacher_html = ''.join(
        f"""
        <tr>
            <td>{cell(row['teacher'].get('full_name'))}</td>
            <td>{cell(row['teacher'].get('username'))}</td>
            <td>{cell(row['teacher'].get('subject'))}</td>
            <td>{cell(row['teacher'].get('email'))}</td>
            <td>{cell(row['class_count'])}</td>
            <td>{cell(row['student_count'])}</td>
            <td>{cell(row['lesson_count'])}</td>
            <td>{cell(row['exam_count'])}</td>
            <td>{cell(row['submission_count'])}</td>
            <td>{'Đã khóa' if row['teacher'].get('active', True) is False else 'Đang hoạt động'}</td>
        </tr>
        """
        for row in teacher_rows
    )

    class_html = ''.join(
        f"""
        <tr>
            <td>{cell(row['class'].get('name'))}</td>
            <td>{cell(row['class'].get('class_code'))}</td>
            <td>{cell(row['class'].get('join_password_plain') or 'Cần đặt lại')}</td>
            <td>{cell(row['class'].get('grade'))}</td>
            <td>{cell(row['class'].get('subject'))}</td>
            <td>{cell(row['teacher'].get('full_name', ''))}</td>
            <td>{cell(row['stats']['student_count'])}</td>
            <td>{cell(row['stats']['lesson_count'])}</td>
            <td>{cell(row['stats']['exam_count'])}</td>
            <td>{cell(row['stats']['material_count'])}</td>
            <td>{cell(row['stats']['submission_count'])}</td>
            <td>{cell(row['stats']['avg_score'] if row['stats']['avg_score'] is not None else 'Chưa có')}</td>
        </tr>
        """
        for row in class_rows
    )

    parent_html = ''.join(
        f"""
        <tr>
            <td>{cell(row['parent'].get('full_name'))}</td>
            <td>{cell(row['parent'].get('username'))}</td>
            <td>{cell(row['parent'].get('phone'))}</td>
            <td>{cell(row['parent'].get('email'))}</td>
            <td>{cell(row['student'].get('full_name', ''))}</td>
            <td>{cell(row['student'].get('username', ''))}</td>
            <td>{cell(row['class'].get('name', ''))}</td>
            <td>{cell(row['class'].get('class_code', ''))}</td>
            <td>{'Đã khóa' if row['parent'].get('active', True) is False else 'Đang hoạt động'}</td>
        </tr>
        """
        for row in parent_rows
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #999; padding: 6px; }}
        th {{ background: #dbeafe; font-weight: bold; }}
        h1, h2 {{ color: #123a7a; }}
    </style>
</head>
<body>
    <h1>Báo cáo admin hệ thống học tập</h1>
    <p>Thời điểm xuất: {cell(generated_at)}</p>
    <h2>Tổng quan</h2>
    <table>
        <tr><th>Giáo viên</th><th>Giáo viên hoạt động</th><th>Học sinh</th><th>Phụ huynh</th><th>Lớp</th><th>Bài giảng</th><th>Đề kiểm tra</th><th>Bài nộp</th></tr>
        <tr>
            <td>{summary['teacher_count']}</td>
            <td>{summary['active_teacher_count']}</td>
            <td>{summary['student_count']}</td>
            <td>{summary['parent_count']}</td>
            <td>{summary['class_count']}</td>
            <td>{summary['lesson_count']}</td>
            <td>{summary['exam_count']}</td>
            <td>{summary['submission_count']}</td>
        </tr>
    </table>
    <h2>Danh sách giáo viên</h2>
    <table>
        <tr><th>Giáo viên</th><th>Tài khoản</th><th>Môn</th><th>Email</th><th>Số lớp</th><th>Học sinh</th><th>Bài giảng</th><th>Đề kiểm tra</th><th>Bài nộp</th><th>Trạng thái</th></tr>
        {teacher_html}
    </table>
    <h2>Danh sách phụ huynh</h2>
    <table>
        <tr><th>Phụ huynh</th><th>Tài khoản</th><th>Điện thoại</th><th>Email</th><th>Học sinh</th><th>Tài khoản học sinh</th><th>Lớp</th><th>Mã lớp</th><th>Trạng thái</th></tr>
        {parent_html}
    </table>
    <h2>Danh sách lớp học</h2>
    <table>
        <tr><th>Lớp</th><th>Mã lớp</th><th>Mật khẩu lớp</th><th>Khối/Lớp</th><th>Môn</th><th>Giáo viên</th><th>Học sinh</th><th>Bài giảng</th><th>Đề kiểm tra</th><th>Học liệu</th><th>Bài nộp</th><th>Điểm trung bình</th></tr>
        {class_html}
    </table>
</body>
</html>"""

    filename = f"bao-cao-admin-{datetime.now().strftime('%Y%m%d-%H%M')}.xls"
    return Response(
        html,
        mimetype='application/vnd.ms-excel; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/admin/teachers/<teacher_id>/toggle', methods=['POST'])
def admin_toggle_teacher(teacher_id):
    blocked = require_admin()
    if blocked:
        return blocked

    users = load_exam_users()
    teacher = next((t for t in users.get('teachers', []) if t.get('id') == teacher_id), None)
    if not teacher:
        flash('Không tìm thấy giáo viên.', 'error')
        return redirect(url_for('admin_dashboard'))

    teacher['active'] = not teacher.get('active', True)
    save_exam_users(users)
    flash('Đã cập nhật trạng thái giáo viên.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/teachers/<teacher_id>/reset_password', methods=['POST'])
def admin_reset_teacher_password(teacher_id):
    blocked = require_admin()
    if blocked:
        return blocked

    new_password = request.form.get('new_password', '').strip()
    if not new_password:
        flash('Vui lòng nhập mật khẩu mới.', 'error')
        return redirect(url_for('admin_dashboard'))

    users = load_exam_users()
    teacher = next((t for t in users.get('teachers', []) if t.get('id') == teacher_id), None)
    if not teacher:
        flash('Không tìm thấy giáo viên.', 'error')
        return redirect(url_for('admin_dashboard'))

    teacher['password'] = generate_password_hash(new_password)
    teacher['updated_at'] = datetime.now().strftime("%d/%m/%Y %H:%M")
    save_exam_users(users)
    flash('Đã reset mật khẩu giáo viên.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/teachers/<teacher_id>/edit', methods=['POST'])
def admin_edit_teacher(teacher_id):
    blocked = require_admin()
    if blocked:
        return blocked

    users = load_exam_users()
    teacher = next((t for t in users.get('teachers', []) if t.get('id') == teacher_id), None)
    if not teacher:
        flash('Không tìm thấy giáo viên.', 'error')
        return redirect(url_for('admin_dashboard'))

    teacher['full_name'] = request.form.get('full_name', '').strip() or teacher.get('full_name', '')
    teacher['subject'] = request.form.get('subject', '').strip() or teacher.get('subject', '')
    teacher['email'] = request.form.get('email', '').strip()
    teacher['updated_at'] = datetime.now().strftime("%d/%m/%Y %H:%M")
    save_exam_users(users)
    flash('Đã cập nhật thông tin giáo viên.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/parents/create', methods=['POST'])
def admin_create_parent():
    blocked = require_admin()
    if blocked:
        return blocked

    username = request.form.get('parent_username', '').strip()
    password = request.form.get('parent_password', '').strip()
    full_name = request.form.get('parent_full_name', '').strip()
    email = request.form.get('parent_email', '').strip()
    phone = request.form.get('parent_phone', '').strip()
    class_id = request.form.get('parent_class_id', '').strip()
    student_id = request.form.get('parent_student_id', '').strip()

    if not all([username, password, full_name, class_id, student_id]):
        flash('Vui lòng nhập đủ tài khoản, mật khẩu, họ tên, lớp và học sinh cho phụ huynh.', 'error')
        return redirect(url_for('admin_dashboard'))

    users = load_exam_users()
    username_exists = (
        any(u.get('username') == username for u in users.get('teachers', [])) or
        any(u.get('username') == username for u in users.get('students', [])) or
        any(u.get('username') == username for u in users.get('parents', []))
    )
    if username_exists:
        flash('Tên đăng nhập đã tồn tại trong hệ thống.', 'error')
        return redirect(url_for('admin_dashboard'))

    class_obj = next((c for c in load_exam_classes() if c.get('id') == class_id), None)
    if not class_obj or not student_in_class(class_obj, student_id):
        flash('Học sinh không thuộc lớp đã chọn.', 'error')
        return redirect(url_for('admin_dashboard'))

    users.setdefault('parents', []).append({
        'id': str(uuid.uuid4()),
        'username': username,
        'password': generate_password_hash(password),
        'full_name': full_name,
        'email': email,
        'phone': phone,
        'class_id': class_id,
        'student_id': student_id,
        'active': True,
        'created_at': datetime.now().strftime("%d/%m/%Y %H:%M")
    })
    save_exam_users(users)
    flash('Đã tạo tài khoản phụ huynh.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/parents/<parent_id>/toggle', methods=['POST'])
def admin_toggle_parent(parent_id):
    blocked = require_admin()
    if blocked:
        return blocked
    next_url = request.form.get('next') or url_for('admin_dashboard')
    if not next_url.startswith('/admin/'):
        next_url = url_for('admin_dashboard')

    users = load_exam_users()
    parent = next((p for p in users.get('parents', []) if p.get('id') == parent_id), None)
    if not parent:
        flash('Không tìm thấy tài khoản phụ huynh.', 'error')
        return redirect(next_url)

    parent['active'] = not parent.get('active', True)
    parent['updated_at'] = datetime.now().strftime("%d/%m/%Y %H:%M")
    save_exam_users(users)
    flash('Đã cập nhật trạng thái phụ huynh.', 'success')
    return redirect(next_url)


@app.route('/admin/parents/<parent_id>/reset_password', methods=['POST'])
def admin_reset_parent_password(parent_id):
    blocked = require_admin()
    if blocked:
        return blocked
    next_url = request.form.get('next') or url_for('admin_dashboard')
    if not next_url.startswith('/admin/'):
        next_url = url_for('admin_dashboard')

    new_password = request.form.get('new_password', '').strip()
    if not new_password:
        flash('Vui lòng nhập mật khẩu mới cho phụ huynh.', 'error')
        return redirect(next_url)

    users = load_exam_users()
    parent = next((p for p in users.get('parents', []) if p.get('id') == parent_id), None)
    if not parent:
        flash('Không tìm thấy tài khoản phụ huynh.', 'error')
        return redirect(next_url)

    parent['password'] = generate_password_hash(new_password)
    parent['updated_at'] = datetime.now().strftime("%d/%m/%Y %H:%M")
    save_exam_users(users)
    flash('Đã reset mật khẩu phụ huynh.', 'success')
    return redirect(next_url)


# ---------------- TEACHER ROUTES ----------------
@app.route('/exam_system/teacher/dashboard')
def teacher_dashboard():
    blocked = require_teacher()
    if blocked:
        return blocked

    teacher_id = session.get('exam_user_id')
    classes = [
        c for c in load_exam_classes()
        if c.get('teacher_id') == teacher_id
    ]
    class_rows = [
        {'class': class_obj, 'stats': build_class_stats(class_obj)}
        for class_obj in classes
    ]
    overview_report = build_teacher_overview_report(class_rows)

    return render_template('exam_system/teacher/dashboard.html',
                           class_rows=class_rows,
                           overview_report=overview_report)


@app.route('/exam_system/teacher/classes/create', methods=['POST'])
def teacher_create_class():
    blocked = require_teacher()
    if blocked:
        return blocked

    name = request.form.get('name', '').strip()
    grade = request.form.get('grade', '').strip()
    subject = request.form.get('subject', '').strip() or session.get('exam_subject', 'Chung')
    join_password = request.form.get('join_password', '').strip()

    if not all([name, grade]):
        flash('Vui lòng nhập tên lớp và khối/lớp.', 'error')
        return redirect(url_for('teacher_dashboard'))

    classes = load_exam_classes()
    join_password = join_password or generate_join_password()
    class_obj = {
        'id': str(uuid.uuid4()),
        'class_code': generate_class_code(classes),
        'join_password': generate_password_hash(join_password),
        'join_password_plain': join_password,
        'name': name,
        'grade': grade,
        'subject': subject,
        'teacher_id': session.get('exam_user_id'),
        'teacher_name': session.get('exam_user_name'),
        'student_ids': [],
        'created_at': datetime.now().strftime("%d/%m/%Y %H:%M"),
        'updated_at': None,
        'active': True
    }
    classes.insert(0, class_obj)
    save_exam_classes(classes)
    flash(f'Đã tạo lớp. Mã lớp: {class_obj["class_code"]} · Mật khẩu lớp: {join_password}', 'success')
    return redirect(url_for('teacher_class_detail', class_id=class_obj['id']))


@app.route('/exam_system/teacher/classes/<class_id>/reset_password', methods=['POST'])
def teacher_reset_class_password(class_id):
    blocked = require_teacher()
    if blocked:
        return blocked

    classes = load_exam_classes()
    class_obj = next(
        (
            c for c in classes
            if c.get('id') == class_id and c.get('teacher_id') == session.get('exam_user_id')
        ),
        None
    )
    if not class_obj:
        flash('Không tìm thấy lớp học hoặc bạn không có quyền truy cập.', 'error')
        return redirect(url_for('teacher_dashboard'))

    new_password = request.form.get('join_password', '').strip() or generate_join_password()
    class_obj['join_password'] = generate_password_hash(new_password)
    class_obj['join_password_plain'] = new_password
    class_obj['updated_at'] = datetime.now().strftime("%d/%m/%Y %H:%M")
    save_exam_classes(classes)
    flash(f'Đã cập nhật mật khẩu lớp: {new_password}', 'success')
    return redirect(url_for('teacher_class_detail', class_id=class_id))


@app.route('/exam_system/teacher/classes/<class_id>')
def teacher_class_detail(class_id):
    blocked = require_teacher()
    if blocked:
        return blocked

    class_obj = get_teacher_class(class_id)
    if not class_obj:
        flash('Không tìm thấy lớp học hoặc bạn không có quyền truy cập.', 'error')
        return redirect(url_for('teacher_dashboard'))

    lessons = [l for l in load_exam_lessons() if l.get('class_id') == class_id]
    exams = [e for e in load_exam_exams() if e.get('class_id') == class_id]
    materials = [m for m in load_exam_materials() if m.get('class_id') == class_id]
    users = load_exam_users()
    students = [
        s for s in users.get('students', [])
        if s.get('id') in class_obj.get('student_ids', [])
    ]
    submissions = [
        sub for sub in load_exam_submissions()
        if any(exam.get('id') == sub.get('exam_id') for exam in exams)
    ]
    class_analysis = build_teacher_class_analysis(
        class_obj,
        students=students,
        exams=exams,
        submissions=submissions
    )

    return render_template('exam_system/teacher/class_detail.html',
                           class_obj=class_obj,
                           lessons=lessons,
                           exams=exams,
                           materials=materials,
                           students=students,
                           submissions=submissions,
                           stats=build_class_stats(class_obj),
                           class_analysis=class_analysis)


@app.route('/exam_system/teacher/classes/<class_id>/reviews/save', methods=['POST'])
def teacher_save_class_reviews(class_id):
    blocked = require_teacher()
    if blocked:
        return blocked

    classes = load_exam_classes()
    class_obj = next(
        (
            c for c in classes
            if c.get('id') == class_id and c.get('teacher_id') == session.get('exam_user_id')
        ),
        None
    )
    if not class_obj:
        flash('Không tìm thấy lớp học hoặc bạn không có quyền truy cập.', 'error')
        return redirect(url_for('teacher_dashboard'))

    student_ids = request.form.getlist('student_id[]')
    comments = request.form.getlist('comment[]')
    published_ids = set(request.form.getlist('published_student_id[]'))
    reviews = class_obj.setdefault('student_reviews', {})

    for index, student_id in enumerate(student_ids):
        comment = comments[index].strip() if index < len(comments) else ''
        if not comment:
            continue
        reviews[student_id] = {
            'comment': comment,
            'published': student_id in published_ids,
            'updated_at': datetime.now().strftime("%d/%m/%Y %H:%M")
        }

    class_obj['updated_at'] = datetime.now().strftime("%d/%m/%Y %H:%M")
    save_exam_classes(classes)
    flash('Đã lưu nhận xét cá nhân cho học sinh trong lớp.', 'success')
    return redirect(url_for('teacher_class_detail', class_id=class_id))


@app.route('/exam_system/teacher/materials', methods=['GET', 'POST'])
@app.route('/exam_system/teacher/classes/<class_id>/materials', methods=['GET', 'POST'])
def teacher_material_library(class_id=None):
    blocked = require_teacher()
    if blocked:
        return blocked
    class_obj = None
    if class_id:
        class_obj = get_teacher_class(class_id)
        if not class_obj:
            flash('Không tìm thấy lớp học hoặc bạn không có quyền truy cập.', 'error')
            return redirect(url_for('teacher_dashboard'))
    redirect_target = (
        url_for('teacher_material_library', class_id=class_id)
        if class_id else url_for('teacher_material_library')
    )

    allowed_grades = {'6', '7', '8', '9'}

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        grade = request.form.get('grade', '').strip()
        drive_url = request.form.get('drive_url', '').strip()
        description = request.form.get('description', '').strip()

        if not title or not grade or not drive_url:
            flash('Vui lòng nhập tên sách, lớp và link Google Drive.', 'error')
            return redirect(redirect_target)

        if grade not in allowed_grades:
            flash('Phân loại lớp chỉ nhận lớp 6, 7, 8 hoặc 9.', 'error')
            return redirect(redirect_target)

        if not is_google_drive_url(drive_url):
            flash('Link học liệu phải là link Google Drive hoặc Google Docs.', 'error')
            return redirect(redirect_target)

        materials = load_exam_materials()
        materials.insert(0, {
            'id': str(uuid.uuid4()),
            'title': title,
            'grade': grade,
            'drive_url': drive_url,
            'description': description,
            'class_id': class_id,
            'teacher_id': session.get('exam_user_id'),
            'teacher_name': session.get('exam_user_name'),
            'created_at': datetime.now().strftime("%d/%m/%Y %H:%M"),
            'updated_at': None
        })
        save_exam_materials(materials)
        flash('Đã thêm sách vào kho học liệu.', 'success')
        return redirect(redirect_target)

    teacher_id = session.get('exam_user_id')
    materials = [
        m for m in load_exam_materials()
        if m.get('teacher_id') == teacher_id and (not class_id or m.get('class_id') == class_id)
    ]
    materials = sorted(
        materials,
        key=lambda m: (m.get('grade', ''), m.get('title', '').lower())
    )
    return render_template('exam_system/teacher/material_library.html',
                           materials=materials,
                           grades=['6', '7', '8', '9'],
                           class_obj=class_obj)


@app.route('/exam_system/teacher/materials/<material_id>/edit',
           methods=['GET', 'POST'])
def teacher_edit_material(material_id):
    blocked = require_teacher()
    if blocked:
        return blocked

    materials = load_exam_materials()
    material = next((m for m in materials if m.get('id') == material_id), None)
    if not material or material.get('teacher_id') != session.get('exam_user_id'):
        flash('Không tìm thấy sách hoặc bạn không có quyền chỉnh sửa.', 'error')
        return redirect(url_for('teacher_material_library'))
    class_obj = get_teacher_class(material.get('class_id')) if material.get('class_id') else None

    allowed_grades = {'6', '7', '8', '9'}

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        grade = request.form.get('grade', '').strip()
        drive_url = request.form.get('drive_url', '').strip()
        description = request.form.get('description', '').strip()

        if not title or not grade or not drive_url:
            flash('Vui lòng nhập tên sách, lớp và link Google Drive.', 'error')
            return redirect(url_for('teacher_edit_material',
                                    material_id=material_id))

        if grade not in allowed_grades:
            flash('Phân loại lớp chỉ nhận lớp 6, 7, 8 hoặc 9.', 'error')
            return redirect(url_for('teacher_edit_material',
                                    material_id=material_id))

        if not is_google_drive_url(drive_url):
            flash('Link học liệu phải là link Google Drive hoặc Google Docs.', 'error')
            return redirect(url_for('teacher_edit_material',
                                    material_id=material_id))

        material.update({
            'title': title,
            'grade': grade,
            'drive_url': drive_url,
            'description': description,
            'updated_at': datetime.now().strftime("%d/%m/%Y %H:%M")
        })
        save_exam_materials(materials)
        flash('Đã cập nhật sách.', 'success')
        if material.get('class_id'):
            return redirect(url_for('teacher_material_library', class_id=material.get('class_id')))
        return redirect(url_for('teacher_material_library'))

    teacher_id = session.get('exam_user_id')
    visible_materials = [
        m for m in materials
        if m.get('teacher_id') == teacher_id
        and (not material.get('class_id') or m.get('class_id') == material.get('class_id'))
    ]
    visible_materials = sorted(
        visible_materials,
        key=lambda m: (m.get('grade', ''), m.get('title', '').lower())
    )
    return render_template('exam_system/teacher/material_library.html',
                           materials=visible_materials,
                           grades=['6', '7', '8', '9'],
                           editing_material=material,
                           class_obj=class_obj)


@app.route('/exam_system/teacher/materials/<material_id>/delete',
           methods=['POST'])
def teacher_delete_material(material_id):
    blocked = require_teacher()
    if blocked:
        return blocked

    materials = load_exam_materials()
    material = next((m for m in materials if m.get('id') == material_id), None)
    if not material or material.get('teacher_id') != session.get('exam_user_id'):
        flash('Không tìm thấy sách hoặc bạn không có quyền xóa.', 'error')
        return redirect(url_for('teacher_material_library'))

    materials = [m for m in materials if m.get('id') != material_id]
    save_exam_materials(materials)
    flash('Đã xóa sách khỏi kho học liệu.', 'success')
    if material.get('class_id'):
        return redirect(url_for('teacher_material_library', class_id=material.get('class_id')))
    return redirect(url_for('teacher_material_library'))


@app.route('/exam_system/materials/<material_id>/view')
def exam_material_view(material_id):
    user_type = session.get('exam_user_type')
    if user_type not in {'teacher', 'student'}:
        return redirect(url_for('exam_student_login'))

    material = next(
        (m for m in load_exam_materials() if m.get('id') == material_id),
        None
    )
    if not material:
        flash('Không tìm thấy học liệu.', 'error')
        if user_type == 'teacher':
            return redirect(url_for('teacher_material_library'))
        return redirect(url_for('student_material_library'))

    if user_type == 'teacher' and material.get('teacher_id') != session.get('exam_user_id'):
        flash('Bạn không có quyền xem học liệu này trong trang quản lý giáo viên.', 'error')
        return redirect(url_for('teacher_material_library'))
    if user_type == 'student' and material.get('class_id'):
        class_obj = next(
            (c for c in load_exam_classes() if c.get('id') == material.get('class_id')),
            None
        )
        if not class_obj or not student_in_class(class_obj, session.get('exam_user_id')):
            flash('Bạn cần tham gia lớp để xem học liệu này.', 'error')
            return redirect(url_for('student_dashboard'))

    return render_template('exam_system/material_viewer.html',
                           material=material,
                           class_obj=next((c for c in load_exam_classes() if c.get('id') == material.get('class_id')), None),
                           embed_url=get_google_embed_url(material.get('drive_url')))


@app.route('/exam_system/teacher/create_lesson', methods=['GET', 'POST'])
@app.route('/exam_system/teacher/classes/<class_id>/create_lesson', methods=['GET', 'POST'])
def teacher_create_lesson(class_id=None):
    blocked = require_teacher()
    if blocked:
        return blocked
    if not class_id:
        flash('Vui lòng chọn lớp trước khi tạo bài giảng.', 'info')
        return redirect(url_for('teacher_dashboard'))
    class_obj = get_teacher_class(class_id)
    if not class_obj:
        flash('Không tìm thấy lớp học hoặc bạn không có quyền truy cập.', 'error')
        return redirect(url_for('teacher_dashboard'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        content = request.form.get('content', '').strip()
        subject = request.form.get('subject', '').strip() or class_obj.get('subject', '')
        grade = request.form.get('grade', '').strip() or class_obj.get('grade', '')

        attachments = []
        files = request.files.getlist('attachments')
        for f in files:
            if f and f.filename:
                filename = f"{uuid.uuid4()}_{secure_filename(f.filename)}"
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                attachments.append(filename)

        new_lesson = {
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'content': content,
            'attachments': attachments,
            'teacher_id': session.get('exam_user_id'),
            'class_id': class_id,
            'created_at': datetime.now().strftime("%d/%m/%Y %H:%M"),
            'subject': subject,
            'grade': grade
        }

        lessons = load_exam_lessons()
        lessons.insert(0, new_lesson)
        save_exam_lessons(lessons)

        flash('Đã tạo bài giảng!', 'success')
        return redirect(url_for('teacher_class_detail', class_id=class_id))

    return render_template('exam_system/teacher/create_lesson.html',
                           class_obj=class_obj)


@app.route('/exam_system/teacher/create_exam', methods=['GET', 'POST'])
@app.route('/exam_system/teacher/classes/<class_id>/create_exam', methods=['GET', 'POST'])
def teacher_create_exam(class_id=None):
    blocked = require_teacher()
    if blocked:
        return blocked
    if not class_id:
        flash('Vui lòng chọn lớp trước khi tạo đề kiểm tra.', 'info')
        return redirect(url_for('teacher_dashboard'))
    class_obj = get_teacher_class(class_id)
    if not class_obj:
        flash('Không tìm thấy lớp học hoặc bạn không có quyền truy cập.', 'error')
        return redirect(url_for('teacher_dashboard'))

    if request.method == 'POST':
        exam_type = request.form.get('exam_type')
        if exam_type == 'multiple_choice':
            return redirect(url_for('teacher_create_multiple_choice', class_id=class_id))
        elif exam_type == 'essay':
            return redirect(url_for('teacher_create_essay', class_id=class_id))

    return render_template('exam_system/teacher/create_exam.html',
                           class_obj=class_obj)


@app.route('/exam_system/teacher/create_multiple_choice',
           methods=['GET', 'POST'])
@app.route('/exam_system/teacher/classes/<class_id>/create_multiple_choice',
           methods=['GET', 'POST'])
def teacher_create_multiple_choice(class_id=None):
    blocked = require_teacher()
    if blocked:
        return blocked
    if not class_id:
        flash('Vui lòng chọn lớp trước khi tạo đề kiểm tra.', 'info')
        return redirect(url_for('teacher_dashboard'))
    class_obj = get_teacher_class(class_id)
    if not class_obj:
        flash('Không tìm thấy lớp học hoặc bạn không có quyền truy cập.', 'error')
        return redirect(url_for('teacher_dashboard'))

    if request.method == 'POST':
        if request.form.get('manual_create') == 'yes':
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            time_limit_raw = request.form.get('time_limit', '0').strip()
            subject = request.form.get('subject', '').strip() or class_obj.get('subject', '')
            grade = request.form.get('grade', '').strip() or class_obj.get('grade', '')

            question_texts = request.form.getlist('question[]')
            option_a_list = request.form.getlist('option_a[]')
            option_b_list = request.form.getlist('option_b[]')
            option_c_list = request.form.getlist('option_c[]')
            option_d_list = request.form.getlist('option_d[]')
            correct_answers = request.form.getlist('correct_answer[]')
            explanations = request.form.getlist('explanation[]')

            questions = []
            for index, question_text in enumerate(question_texts):
                question_text = question_text.strip()
                option_values = [
                    option_a_list[index].strip() if index < len(option_a_list) else '',
                    option_b_list[index].strip() if index < len(option_b_list) else '',
                    option_c_list[index].strip() if index < len(option_c_list) else '',
                    option_d_list[index].strip() if index < len(option_d_list) else ''
                ]
                correct_answer = (
                    correct_answers[index].strip().upper()
                    if index < len(correct_answers) else ''
                )
                explanation = (
                    explanations[index].strip()
                    if index < len(explanations) else ''
                )

                if not question_text and not any(option_values):
                    continue

                if not question_text or not all(option_values) or correct_answer not in {'A', 'B', 'C', 'D'}:
                    flash('Vui lòng nhập đủ nội dung câu hỏi, 4 đáp án và chọn đáp án đúng cho mỗi câu.', 'error')
                    return redirect(url_for('teacher_create_multiple_choice', class_id=class_id))

                questions.append({
                    'id': len(questions) + 1,
                    'question': question_text,
                    'options': [
                        f'A. {option_values[0]}',
                        f'B. {option_values[1]}',
                        f'C. {option_values[2]}',
                        f'D. {option_values[3]}'
                    ],
                    'correct_answer': correct_answer,
                    'explanation': explanation
                })

            if not title:
                flash('Vui lòng nhập tiêu đề đề kiểm tra.', 'error')
                return redirect(url_for('teacher_create_multiple_choice', class_id=class_id))

            if not questions:
                flash('Vui lòng tạo ít nhất 1 câu hỏi trắc nghiệm.', 'error')
                return redirect(url_for('teacher_create_multiple_choice', class_id=class_id))

            try:
                time_limit = max(0, int(time_limit_raw or 0))
            except ValueError:
                flash('Thời gian làm bài phải là số phút hợp lệ.', 'error')
                return redirect(url_for('teacher_create_multiple_choice', class_id=class_id))

            new_exam = {
                'id': str(uuid.uuid4()),
                'title': title,
                'description': description,
                'type': 'multiple_choice',
                'teacher_id': session.get('exam_user_id'),
                'class_id': class_id,
                'created_at': datetime.now().strftime("%d/%m/%Y %H:%M"),
                'time_limit': time_limit,
                'subject': subject,
                'grade': grade,
                'status': 'active',
                'questions': questions
            }

            exams = load_exam_exams()
            exams.insert(0, new_exam)
            save_exam_exams(exams)

            flash(f'Đã tạo đề trắc nghiệm thủ công với {len(questions)} câu.', 'success')
            return redirect(url_for('teacher_class_detail', class_id=class_id))

        if 'word_file' in request.files:
            word_file = request.files['word_file']
            if word_file and word_file.filename.endswith('.docx'):
                # Đọc nội dung Word
                word_content = mammoth.extract_raw_text(word_file).value

                # Dùng AI parse thành JSON
                prompt = f"""Đây là nội dung đề trắc nghiệm từ file Word:

{word_content}

Hãy chuyển đổi thành JSON với format:
{{
  "questions": [
    {{
      "id": 1,
      "question": "Câu hỏi",
      "options": ["A. Đáp án 1", "B. Đáp án 2", "C. Đáp án 3", "D. Đáp án 4"],
      "correct_answer": "A",
      "explanation": "Giải thích"
    }}
  ]
}}

CHỈ TRẢ VỀ JSON, KHÔNG THÊM TEXT KHÁC."""

                try:
                    response = model.generate_content([prompt])
                    ai_json = response.text.replace('```json',
                                                    '').replace('```',
                                                                '').strip()
                    questions_data = json.loads(ai_json)

                    # Lưu vào session để preview
                    session['preview_questions'] = questions_data

                    return render_template(
                        'exam_system/teacher/preview_questions.html',
                        questions=questions_data['questions'],
                        class_obj=class_obj)
                except Exception as e:
                    flash(f'Lỗi khi parse file: {str(e)}', 'error')

        # Nếu confirm từ preview
        if request.form.get('confirm') == 'yes':
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            time_limit = request.form.get('time_limit', '0')
            subject = request.form.get('subject', '').strip() or class_obj.get('subject', '')
            grade = request.form.get('grade', '').strip() or class_obj.get('grade', '')

            questions_json = request.form.get('questions_json')
            questions = json.loads(questions_json)

            new_exam = {
                'id': str(uuid.uuid4()),
                'title': title,
                'description': description,
                'type': 'multiple_choice',
                'teacher_id': session.get('exam_user_id'),
                'class_id': class_id,
                'created_at': datetime.now().strftime("%d/%m/%Y %H:%M"),
                'time_limit': int(time_limit),
                'subject': subject,
                'grade': grade,
                'status': 'active',
                'questions': questions
            }

            exams = load_exam_exams()
            exams.insert(0, new_exam)
            save_exam_exams(exams)

            flash('Đã tạo đề trắc nghiệm!', 'success')
            return redirect(url_for('teacher_class_detail', class_id=class_id))

    return render_template('exam_system/teacher/create_multiple_choice.html',
                           class_obj=class_obj)


@app.route('/exam_system/teacher/create_essay', methods=['GET', 'POST'])
@app.route('/exam_system/teacher/classes/<class_id>/create_essay', methods=['GET', 'POST'])
def teacher_create_essay(class_id=None):
    blocked = require_teacher()
    if blocked:
        return blocked
    if not class_id:
        flash('Vui lòng chọn lớp trước khi tạo đề kiểm tra.', 'info')
        return redirect(url_for('teacher_dashboard'))
    class_obj = get_teacher_class(class_id)
    if not class_obj:
        flash('Không tìm thấy lớp học hoặc bạn không có quyền truy cập.', 'error')
        return redirect(url_for('teacher_dashboard'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        time_limit = request.form.get('time_limit', '0')
        subject = request.form.get('subject', '').strip() or class_obj.get('subject', '')
        grade = request.form.get('grade', '').strip() or class_obj.get('grade', '')

        # Lấy các câu hỏi tự luận
        questions = []
        i = 0
        while True:
            q_text = request.form.get(f'question_{i}')
            if not q_text:
                break
            points = request.form.get(f'points_{i}', '10')
            suggested = request.form.get(f'suggested_{i}', '')

            questions.append({
                'id': i + 1,
                'question': q_text,
                'points': int(points),
                'suggested_answer': suggested
            })
            i += 1

        new_exam = {
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'type': 'essay',
            'teacher_id': session.get('exam_user_id'),
            'class_id': class_id,
            'created_at': datetime.now().strftime("%d/%m/%Y %H:%M"),
            'time_limit': int(time_limit),
            'subject': subject,
            'grade': grade,
            'status': 'active',
            'essay_questions': questions
        }

        exams = load_exam_exams()
        exams.insert(0, new_exam)
        save_exam_exams(exams)

        flash('Đã tạo đề tự luận!', 'success')
        return redirect(url_for('teacher_class_detail', class_id=class_id))

    return render_template('exam_system/teacher/create_essay.html',
                           class_obj=class_obj)


@app.route('/exam_system/teacher/view_submissions/<exam_id>')
def teacher_view_submissions(exam_id):
    blocked = require_teacher()
    if blocked:
        return blocked

    exam = next((e for e in load_exam_exams() if e['id'] == exam_id), None)
    if not exam:
        flash('Không tìm thấy đề!', 'error')
        return redirect(url_for('teacher_dashboard'))
    class_obj = get_teacher_class(exam.get('class_id')) if exam.get('class_id') else None
    if exam.get('teacher_id') != session.get('exam_user_id') or (exam.get('class_id') and not class_obj):
        flash('Bạn không có quyền xem bài nộp của đề này.', 'error')
        return redirect(url_for('teacher_dashboard'))

    submissions = [
        s for s in load_exam_submissions() if s['exam_id'] == exam_id
    ]
    users = load_exam_users()

    # Ghép thông tin học sinh
    for sub in submissions:
        student = next(
            (s for s in users['students'] if s['id'] == sub['student_id']),
            None)
        sub['student_name'] = student['full_name'] if student else 'Unknown'
        sub['student_class'] = student.get('class', '') if student else ''

    return render_template('exam_system/teacher/view_submissions.html',
                           exam=exam,
                           submissions=submissions,
                           class_obj=class_obj)


@app.route('/exam_system/teacher/view_submission/<submission_id>')
def teacher_view_submission(submission_id):
    blocked = require_teacher()
    if blocked:
        return blocked

    submission = next(
        (s for s in load_exam_submissions() if s['id'] == submission_id), None)
    if not submission:
        flash('Không tìm thấy bài làm!', 'error')
        return redirect(url_for('teacher_dashboard'))

    exam = next(
        (e for e in load_exam_exams() if e['id'] == submission['exam_id']),
        None)
    class_obj = get_teacher_class(exam.get('class_id')) if exam and exam.get('class_id') else None
    if not exam or exam.get('teacher_id') != session.get('exam_user_id') or (exam.get('class_id') and not class_obj):
        flash('Bạn không có quyền xem bài làm này.', 'error')
        return redirect(url_for('teacher_dashboard'))
    users = load_exam_users()
    student = next(
        (s for s in users['students'] if s['id'] == submission['student_id']),
        None)

    return render_template('exam_system/teacher/view_submission_detail.html',
                           submission=submission,
                           exam=exam,
                           student=student,
                           class_obj=class_obj)


@app.route('/exam_system/teacher/delete_exam/<exam_id>', methods=['POST'])
def teacher_delete_exam(exam_id):
    blocked = require_teacher()
    if blocked:
        return blocked

    exams = load_exam_exams()
    exam = next((e for e in exams if e.get('id') == exam_id), None)
    if not exam or exam.get('teacher_id') != session.get('exam_user_id'):
        flash('Không tìm thấy đề hoặc bạn không có quyền xóa.', 'error')
        return redirect(url_for('teacher_dashboard'))

    class_id = exam.get('class_id')
    exams = [e for e in exams if e['id'] != exam_id]
    save_exam_exams(exams)

    flash('Đã xóa đề kiểm tra!', 'success')
    if class_id:
        return redirect(url_for('teacher_class_detail', class_id=class_id))
    return redirect(url_for('teacher_dashboard'))


# ---------------- PARENT ROUTES ----------------
@app.route('/exam_system/parent/dashboard')
def parent_dashboard():
    blocked = require_parent()
    if blocked:
        return blocked

    context = get_parent_context()
    if not context:
        flash('Tài khoản phụ huynh chưa được gán đúng lớp và học sinh.', 'error')
        return redirect(url_for('exam_parent_login'))

    profile = build_student_learning_profile(
        context['student'].get('id'),
        context['class_obj'].get('id')
    )
    return render_template(
        'exam_system/learning_portal.html',
        profile=profile,
        viewer_type='parent',
        parent=context['parent'],
        student=context['student'],
        selected_class=context['class_obj']
    )


# ---------------- STUDENT ROUTES ----------------
@app.route('/exam_system/student/materials')
def student_material_library():
    if session.get('exam_user_type') != 'student':
        return redirect(url_for('exam_student_login'))

    joined_class_ids = {c.get('id') for c in get_student_classes(session.get('exam_user_id'))}
    materials = sorted(
        [
            m for m in load_exam_materials()
            if not m.get('class_id') or m.get('class_id') in joined_class_ids
        ],
        key=lambda m: (m.get('grade', ''), m.get('title', '').lower())
    )
    grouped_materials = {
        grade: [m for m in materials if m.get('grade') == grade]
        for grade in ['6', '7', '8', '9']
    }
    return render_template('exam_system/student/material_library.html',
                           grouped_materials=grouped_materials,
                           grades=['6', '7', '8', '9'])


@app.route('/exam_system/student/dashboard')
def student_dashboard():
    if session.get('exam_user_type') != 'student':
        flash('Vui lòng đăng nhập với tư cách học sinh!', 'error')
        return redirect(url_for('exam_student_login'))

    student_id = session.get('exam_user_id')
    classes = get_student_classes(student_id)

    return render_template('exam_system/student/dashboard.html',
                           classes=classes)


@app.route('/exam_system/student/learning_portal')
def student_learning_portal():
    if session.get('exam_user_type') != 'student':
        flash('Vui lòng đăng nhập với tư cách học sinh!', 'error')
        return redirect(url_for('exam_student_login'))

    profile = build_student_learning_profile(session.get('exam_user_id'))
    return render_template(
        'exam_system/learning_portal.html',
        profile=profile,
        viewer_type='student',
        parent=None,
        student=profile.get('student'),
        selected_class=None
    )


@app.route('/exam_system/student/classes/<class_id>')
def student_class_detail(class_id):
    if session.get('exam_user_type') != 'student':
        flash('Vui lòng đăng nhập với tư cách học sinh!', 'error')
        return redirect(url_for('exam_student_login'))

    student_id = session.get('exam_user_id')
    class_obj = next(
        (
            c for c in load_exam_classes()
            if c.get('id') == class_id and student_in_class(c, student_id)
        ),
        None
    )
    if not class_obj:
        flash('Bạn cần tham gia lớp trước khi xem nội dung lớp này.', 'error')
        return redirect(url_for('student_dashboard'))

    lessons = [l for l in load_exam_lessons() if l.get('class_id') == class_id]
    materials = [m for m in load_exam_materials() if m.get('class_id') == class_id]
    exams = [
        e for e in load_exam_exams()
        if e.get('status') == 'active' and e.get('class_id') == class_id
    ]

    return render_template('exam_system/student/class_detail.html',
                           class_obj=class_obj,
                           lessons=lessons,
                           materials=materials,
                           exams=exams)


@app.route('/exam_system/student/classes/<class_id>/learning_portal')
def student_class_learning_portal(class_id):
    if session.get('exam_user_type') != 'student':
        flash('Vui lòng đăng nhập với tư cách học sinh!', 'error')
        return redirect(url_for('exam_student_login'))

    student_id = session.get('exam_user_id')
    class_obj = next(
        (
            c for c in load_exam_classes()
            if c.get('id') == class_id and student_in_class(c, student_id)
        ),
        None
    )
    if not class_obj:
        flash('Bạn cần tham gia lớp trước khi xem báo cáo học tập.', 'error')
        return redirect(url_for('student_dashboard'))

    profile = build_student_learning_profile(student_id, class_id)
    return render_template(
        'exam_system/learning_portal.html',
        profile=profile,
        viewer_type='student',
        parent=None,
        student=profile.get('student'),
        selected_class=class_obj
    )


@app.route('/exam_system/student/join_class', methods=['POST'])
def student_join_class():
    if session.get('exam_user_type') != 'student':
        return redirect(url_for('exam_student_login'))

    class_code = request.form.get('class_code', '').strip().upper()
    join_password = request.form.get('join_password', '').strip()
    classes = load_exam_classes()
    class_obj = next((c for c in classes if c.get('class_code') == class_code), None)
    if not class_obj:
        flash('Không tìm thấy lớp học với mã này.', 'error')
        return redirect(url_for('student_dashboard'))

    stored_password = class_obj.get('join_password', '')
    password_ok = (
        check_password_hash(stored_password, join_password)
        if stored_password.startswith(('pbkdf2:', 'scrypt:', 'bcrypt:'))
        else stored_password == join_password
    )
    if not password_ok:
        flash('Mật khẩu lớp không đúng.', 'error')
        return redirect(url_for('student_dashboard'))

    student_id = session.get('exam_user_id')
    class_obj.setdefault('student_ids', [])
    if student_id not in class_obj['student_ids']:
        class_obj['student_ids'].append(student_id)
        class_obj['updated_at'] = datetime.now().strftime("%d/%m/%Y %H:%M")
        save_exam_classes(classes)
        flash('Đã tham gia lớp học.', 'success')
    else:
        flash('Bạn đã ở trong lớp này rồi.', 'info')
    return redirect(url_for('student_class_detail', class_id=class_obj.get('id')))


@app.route('/exam_system/student/view_lesson/<lesson_id>')
def student_view_lesson(lesson_id):
    if session.get('exam_user_type') != 'student':
        return redirect(url_for('exam_student_login'))

    lesson = next((l for l in load_exam_lessons() if l['id'] == lesson_id),
                  None)
    if not lesson:
        flash('Không tìm thấy bài giảng!', 'error')
        return redirect(url_for('student_dashboard'))
    class_obj = None
    if lesson.get('class_id'):
        class_obj = next((c for c in load_exam_classes() if c.get('id') == lesson.get('class_id')), None)
        if not class_obj or not student_in_class(class_obj, session.get('exam_user_id')):
            flash('Bạn cần tham gia lớp để xem bài giảng này.', 'error')
            return redirect(url_for('student_dashboard'))

    return render_template('exam_system/student/view_lesson.html',
                           lesson=lesson,
                           class_obj=class_obj)


@app.route('/exam_system/student/take_exam/<exam_id>', methods=['GET', 'POST'])
def student_take_exam(exam_id):
    if session.get('exam_user_type') != 'student':
        return redirect(url_for('exam_student_login'))

    exam = next((e for e in load_exam_exams() if e['id'] == exam_id), None)
    if not exam:
        flash('Không tìm thấy đề!', 'error')
        return redirect(url_for('student_dashboard'))
    if exam.get('class_id'):
        class_obj = next((c for c in load_exam_classes() if c.get('id') == exam.get('class_id')), None)
        if not class_obj or not student_in_class(class_obj, session.get('exam_user_id')):
            flash('Bạn cần tham gia lớp để làm đề này.', 'error')
            return redirect(url_for('student_dashboard'))

    if request.method == 'POST':
        student_id = session.get('exam_user_id')
        time_taken = request.form.get('time_taken', '0')

        submission_id = str(uuid.uuid4())

        if exam['type'] == 'multiple_choice':
            answers = {}
            for q in exam['questions']:
                ans = request.form.get(f"q_{q['id']}")
                answers[str(q['id'])] = ans

            # Chấm điểm trắc nghiệm
            correct_count = 0
            detailed_results = []
            for q in exam['questions']:
                student_ans = answers.get(str(q['id']))
                is_correct = (student_ans == q['correct_answer'])
                if is_correct:
                    correct_count += 1

                detailed_results.append({
                    'question_id': q['id'],
                    'question': q['question'],
                    'is_correct': is_correct,
                    'student_answer': student_ans,
                    'correct_answer': q['correct_answer'],
                    'explanation': q.get('explanation', '')
                })

            score = round((correct_count / len(exam['questions'])) * 10, 2)

            # AI feedback
            prompt = f"""Học sinh làm đúng {correct_count}/{len(exam['questions'])} câu trắc nghiệm.

Hãy đưa ra:
1. Nhận xét chung về kết quả
2. Phân tích điểm mạnh/yếu
3. Lời khuyên cải thiện

Trả lời ngắn gọn, khuyến khích."""

            try:
                response = model.generate_content([prompt])
                ai_feedback = clean_ai_output(response.text)
            except:
                ai_feedback = "Không có nhận xét từ AI."

            submission = {
                'id': submission_id,
                'exam_id': exam_id,
                'class_id': exam.get('class_id'),
                'student_id': student_id,
                'submitted_at': datetime.now().strftime("%d/%m/%Y %H:%M"),
                'time_taken': int(time_taken),
                'answers': answers,
                'score': score,
                'ai_feedback': ai_feedback,
                'detailed_results': detailed_results
            }

        elif exam['type'] == 'essay':
            essay_answers = {}
            for q in exam['essay_questions']:
                ans = request.form.get(f"essay_{q['id']}", '').strip()
                essay_answers[str(q['id'])] = ans

            # Chấm điểm tự luận bằng AI
            total_points = 0
            detailed_results = []

            for q in exam['essay_questions']:
                student_ans = essay_answers.get(str(q['id']), '')

                prompt = f"""Đây là câu hỏi tự luận:

Câu hỏi: {q['question']}
Điểm tối đa: {q['points']}
Đáp án gợi ý: {q.get('suggested_answer', 'Không có')}

Câu trả lời của học sinh:
{student_ans}

Hãy chấm điểm (0-{q['points']}) và nhận xét ngắn gọn.
Format: ĐIỂM: X/{q['points']}
NHẬN XÉT: ..."""

                try:
                    response = model.generate_content([prompt])
                    feedback = clean_ai_output(response.text)

                    # Trích xuất điểm
                    import re
                    match = re.search(r'ĐIỂM:\s*(\d+\.?\d*)', feedback)
                    q_score = float(match.group(1)) if match else 0
                except:
                    feedback = "Không chấm được."
                    q_score = 0

                total_points += q_score
                detailed_results.append({
                    'question_id': q['id'],
                    'question': q['question'],
                    'student_answer': student_ans,
                    'points': q['points'],
                    'score': q_score,
                    'feedback': feedback
                })

            max_points = sum(q['points'] for q in exam['essay_questions'])
            score = round((total_points / max_points) * 10, 2)

            submission = {
                'id': submission_id,
                'exam_id': exam_id,
                'class_id': exam.get('class_id'),
                'student_id': student_id,
                'submitted_at': datetime.now().strftime("%d/%m/%Y %H:%M"),
                'time_taken': int(time_taken),
                'essay_answers': essay_answers,
                'score': score,
                'ai_feedback': f"Tổng điểm: {total_points}/{max_points}",
                'detailed_results': detailed_results
            }

        submissions = load_exam_submissions()
        submissions.insert(0, submission)
        save_exam_submissions(submissions)

        flash('Đã nộp bài!', 'success')
        return redirect(
            url_for('student_view_result', submission_id=submission_id))

    return render_template('exam_system/student/take_exam.html', exam=exam)


@app.route('/exam_system/student/view_result/<submission_id>')
def student_view_result(submission_id):
    if session.get('exam_user_type') != 'student':
        return redirect(url_for('exam_student_login'))

    submission = next(
        (s for s in load_exam_submissions() if s['id'] == submission_id), None)
    if not submission:
        flash('Không tìm thấy bài làm!', 'error')
        return redirect(url_for('student_dashboard'))
    if submission.get('student_id') != session.get('exam_user_id'):
        flash('Bạn không có quyền xem bài làm này.', 'error')
        return redirect(url_for('student_dashboard'))

    exam = next(
        (e for e in load_exam_exams() if e['id'] == submission['exam_id']),
        None)

    return render_template('exam_system/student/view_result.html',
                           submission=submission,
                           exam=exam)


@app.route('/exam_system/student/my_submissions')
def student_my_submissions():
    if session.get('exam_user_type') != 'student':
        return redirect(url_for('exam_student_login'))

    student_id = session.get('exam_user_id')
    submissions = [
        s for s in load_exam_submissions() if s['student_id'] == student_id
    ]

    exams = load_exam_exams()
    for sub in submissions:
        exam = next((e for e in exams if e['id'] == sub['exam_id']), None)
        sub['exam_title'] = exam['title'] if exam else 'Unknown'

    return render_template('exam_system/student/my_submissions.html',
                           submissions=submissions)


#################
ALLOWED_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'pdf', 'docx', 'txt'
}


def allowed_file(filename):
    """Kiểm tra file có extension hợp lệ không"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_pdf(pdf_path):
    """Trích xuất text từ file PDF"""
    try:
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception as e:
        return f"Lỗi khi đọc PDF: {str(e)}"


def xml_local_name(tag):
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def omml_child(element, child_name):
    for child in list(element):
        if xml_local_name(child.tag) == child_name:
            return child
    return None


def omml_attr(element, attr_name, default=''):
    if element is None:
        return default

    for key, value in element.attrib.items():
        if xml_local_name(key) == attr_name:
            return value

    return default


def omml_text(element):
    if element is None:
        return ''

    tag_name = xml_local_name(element.tag)

    if tag_name == 't':
        return element.text or ''

    if tag_name == 'r':
        return ''.join(
            child.text or '' for child in element.iter()
            if xml_local_name(child.tag) == 't'
        )

    if tag_name == 'f':
        numerator = omml_text(omml_child(element, 'num'))
        denominator = omml_text(omml_child(element, 'den'))
        return f"\\frac{{{numerator}}}{{{denominator}}}"

    if tag_name == 'sSup':
        base = omml_text(omml_child(element, 'e'))
        sup = omml_text(omml_child(element, 'sup'))
        return f"{base}^{{{sup}}}"

    if tag_name == 'sSub':
        base = omml_text(omml_child(element, 'e'))
        sub = omml_text(omml_child(element, 'sub'))
        return f"{base}_{{{sub}}}"

    if tag_name == 'sSubSup':
        base = omml_text(omml_child(element, 'e'))
        sub = omml_text(omml_child(element, 'sub'))
        sup = omml_text(omml_child(element, 'sup'))
        return f"{base}_{{{sub}}}^{{{sup}}}"

    if tag_name == 'rad':
        base = omml_text(omml_child(element, 'e'))
        degree = omml_text(omml_child(element, 'deg'))
        return f"\\sqrt[{degree}]{{{base}}}" if degree else f"\\sqrt{{{base}}}"

    if tag_name == 'd':
        dpr = omml_child(element, 'dPr')
        begin = omml_attr(omml_child(dpr, 'begChr'), 'val', '(')
        end = omml_attr(omml_child(dpr, 'endChr'), 'val', ')')
        return f"{begin}{omml_text(omml_child(element, 'e'))}{end}"

    if tag_name == 'func':
        fname = omml_text(omml_child(element, 'fName'))
        expr = omml_text(omml_child(element, 'e'))
        return f"\\{fname}({expr})" if fname in {'sin', 'cos', 'tan', 'cot', 'log', 'ln'} else f"{fname}({expr})"

    if tag_name == 'nary':
        nary_pr = omml_child(element, 'naryPr')
        symbol = omml_attr(omml_child(nary_pr, 'chr'), 'val', '∑')
        symbol_map = {'∑': '\\sum', '∫': '\\int', '∏': '\\prod', '⋂': '\\cap', '⋃': '\\cup'}
        latex_symbol = symbol_map.get(symbol, symbol)
        sub = omml_text(omml_child(element, 'sub'))
        sup = omml_text(omml_child(element, 'sup'))
        body = omml_text(omml_child(element, 'e'))
        limits = ''
        if sub:
            limits += f"_{{{sub}}}"
        if sup:
            limits += f"^{{{sup}}}"
        return f"{latex_symbol}{limits} {body}".strip()

    if tag_name == 'limLow':
        return f"\\lim_{{{omml_text(omml_child(element, 'lim'))}}} {omml_text(omml_child(element, 'e'))}"

    if tag_name == 'limUpp':
        return f"{omml_text(omml_child(element, 'e'))}^{{{omml_text(omml_child(element, 'lim'))}}}"

    if tag_name == 'bar':
        return f"\\overline{{{omml_text(omml_child(element, 'e'))}}}"

    if tag_name == 'acc':
        acc_pr = omml_child(element, 'accPr')
        char = omml_attr(omml_child(acc_pr, 'chr'), 'val', '^')
        expr = omml_text(omml_child(element, 'e'))
        if char == '¯':
            return f"\\overline{{{expr}}}"
        if char == '→':
            return f"\\vec{{{expr}}}"
        return f"\\hat{{{expr}}}"

    return ''.join(omml_text(child) for child in list(element))


def extract_omml_equations_from_docx(docx_path):
    """Trích công thức Word OMML thành LaTeX tuyến tính."""
    equations = []
    try:
        import xml.etree.ElementTree as ET

        with ZipFile(docx_path) as docx_zip:
            xml_parts = [
                name for name in docx_zip.namelist()
                if name.startswith('word/') and name.endswith('.xml')
            ]
            for part_name in xml_parts:
                root = ET.fromstring(docx_zip.read(part_name))
                for element in root.iter():
                    if xml_local_name(element.tag) in {'oMath', 'oMathPara'}:
                        equation = re.sub(r'\s+', ' ', omml_text(element)).strip()
                        if equation and equation not in equations:
                            equations.append(equation)
    except (BadZipFile, KeyError, ET.ParseError):
        return []
    except Exception:
        return []

    return equations


def inspect_docx_embedded_assets(docx_path):
    summary = {
        'media_count': 0,
        'ole_count': 0,
        'mathtype_count': 0,
        'media_extensions': {}
    }

    try:
        with ZipFile(docx_path) as docx_zip:
            for name in docx_zip.namelist():
                if name.startswith('word/media/'):
                    summary['media_count'] += 1
                    ext = os.path.splitext(name)[1].lower().lstrip('.') or 'unknown'
                    summary['media_extensions'][ext] = summary['media_extensions'].get(ext, 0) + 1
                elif name.startswith('word/embeddings/oleObject'):
                    summary['ole_count'] += 1
                    data = docx_zip.read(name)
                    if b'MathType' in data or b'Equation Native' in data:
                        summary['mathtype_count'] += 1
    except Exception:
        pass

    return summary


def extract_docx_images_for_ai(docx_path, max_images=6):
    """Lấy một số ảnh PNG/JPG/WebP trong DOCX để gửi kèm Gemini vision."""
    images = []
    try:
        with ZipFile(docx_path) as docx_zip:
            image_names = [
                name for name in docx_zip.namelist()
                if name.startswith('word/media/')
                and os.path.splitext(name)[1].lower() in {'.png', '.jpg', '.jpeg', '.webp'}
            ]

            for name in image_names:
                if len(images) >= max_images:
                    break
                try:
                    img = Image.open(BytesIO(docx_zip.read(name)))
                    img.load()
                    if img.width < 80 or img.height < 40:
                        continue
                    images.append(img.convert('RGB'))
                except Exception:
                    continue
    except Exception:
        return []

    return images


def find_libreoffice_executable():
    configured_path = os.environ.get('LIBREOFFICE_PATH', '').strip()
    if configured_path and os.path.exists(configured_path):
        return configured_path

    for command_name in ('soffice', 'libreoffice'):
        command_path = shutil.which(command_name)
        if command_path:
            return command_path

    windows_candidates = [
        r'C:\Program Files\LibreOffice\program\soffice.exe',
        r'C:\Program Files (x86)\LibreOffice\program\soffice.exe',
    ]
    for candidate in windows_candidates:
        if os.path.exists(candidate):
            return candidate

    return None


def render_pdf_pages_for_ai(pdf_path, max_pages=4, zoom=1.25):
    images = []
    try:
        document = fitz.open(pdf_path)
        matrix = fitz.Matrix(zoom, zoom)
        for page_index in range(min(max_pages, document.page_count)):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            img = Image.open(BytesIO(pixmap.tobytes('png')))
            img.load()
            images.append(img.convert('RGB'))
        document.close()
    except Exception:
        return []

    return images


def render_docx_pages_for_ai(docx_path, max_pages=None):
    """Render vài trang DOCX thành ảnh nếu server có LibreOffice."""
    libreoffice_path = find_libreoffice_executable()
    if not libreoffice_path:
        return []

    if max_pages is None:
        try:
            max_pages = int(os.environ.get('DOCX_RENDER_MAX_PAGES', '4'))
        except ValueError:
            max_pages = 4

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [
                    libreoffice_path,
                    '--headless',
                    '--convert-to',
                    'pdf',
                    '--outdir',
                    temp_dir,
                    docx_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                return []

            pdf_files = [
                os.path.join(temp_dir, name)
                for name in os.listdir(temp_dir)
                if name.lower().endswith('.pdf')
            ]
            if not pdf_files:
                return []

            return render_pdf_pages_for_ai(pdf_files[0], max_pages=max_pages)
    except Exception:
        return []


def extract_text_from_docx(docx_path):
    """Trích xuất text từ file DOCX, kèm công thức OMML và ghi chú MathType/OLE."""
    try:
        with open(docx_path, 'rb') as docx_file:
            result = mammoth.extract_raw_text(docx_file)
        text = (result.value or '').strip()
        docx_notes = []

        omml_equations = extract_omml_equations_from_docx(docx_path)
        if omml_equations:
            equation_lines = '\n'.join(
                f"{index}. \\({equation}\\)"
                for index, equation in enumerate(omml_equations[:200], 1)
            )
            docx_notes.append(f"CÔNG THỨC TRÍCH XUẤT TỪ DOCX:\n{equation_lines}")

        asset_summary = inspect_docx_embedded_assets(docx_path)
        if asset_summary.get('ole_count') or asset_summary.get('media_count'):
            ext_summary = ', '.join(
                f"{ext}: {count}"
                for ext, count in sorted(asset_summary.get('media_extensions', {}).items())
            )
            docx_notes.append(f"""GHI CHÚ HỆ THỐNG VỀ FILE DOCX:
- File có {asset_summary.get('media_count', 0)} ảnh/đối tượng media ({ext_summary or 'không rõ định dạng'}).
- File có {asset_summary.get('ole_count', 0)} OLE object, trong đó phát hiện {asset_summary.get('mathtype_count', 0)} đối tượng MathType/Equation cũ.
- Một số công thức kiểu MathType/OLE/WMF có thể không trích được thành chữ. Nếu câu hỏi/phương án bị thiếu biểu thức, hãy nói rõ phần công thức trong file bị nhúng dạng ảnh/cũ và chỉ phân tích dựa trên phần đọc được, không tự bịa công thức.
""".strip())

        if docx_notes:
            text = '\n\n'.join(docx_notes + [text]).strip()

        return text
    except Exception as e:
        return f"Lỗi khi đọc DOCX: {str(e)}"


def extract_text_from_txt(txt_path):
    """Đọc file TXT với một vài encoding phổ biến để tránh lỗi tiếng Việt."""
    for encoding in ('utf-8-sig', 'utf-8', 'cp1258', 'cp1252'):
        try:
            with open(txt_path, 'r', encoding=encoding) as text_file:
                return text_file.read().strip()
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return f"Lỗi khi đọc TXT: {str(e)}"

    try:
        with open(txt_path, 'r', encoding='utf-8', errors='replace') as text_file:
            return text_file.read().strip()
    except Exception as e:
        return f"Lỗi khi đọc TXT: {str(e)}"


def extract_text_from_chatbot_file(file_path, file_ext):
    """Trả về nội dung text từ các file học liệu chatbot hỗ trợ."""
    if file_ext == 'pdf':
        return extract_text_from_pdf(file_path)
    if file_ext == 'docx':
        return extract_text_from_docx(file_path)
    if file_ext == 'txt':
        return extract_text_from_txt(file_path)
    return ''


def build_chatbot_file_analysis_prompt(system_prompt, file_text, user_message,
                                       filename):
    trimmed_text = (file_text or '').strip()
    question = user_message or (
        'Hãy tóm tắt, phân tích kiến thức liên quan và gợi ý sơ đồ tư duy từ file này.'
    )

    if not trimmed_text:
        return f"""{system_prompt}

Học sinh gửi file "{filename}" nhưng hệ thống chưa trích xuất được nội dung chữ.
Câu hỏi của học sinh: {question}

Hãy trả lời thân thiện rằng em cần gửi file rõ hơn hoặc ảnh/PDF có chữ đọc được, không bịa nội dung file.
"""

    return f"""{system_prompt}

Học sinh gửi file "{filename}" với nội dung trích xuất bên dưới.

NHIỆM VỤ:
- Nếu file là đề kiểm tra/bài tập: không chỉ đưa đáp án, hãy nhận diện các dạng bài, kiến thức liên quan, công thức cần nhớ và phương pháp giải.
- Nếu file là bài học/ghi chú: tóm tắt ý chính, rút ra khái niệm trọng tâm, công thức và ví dụ ôn tập.
- Nếu học sinh hỏi thêm, hãy ưu tiên trả lời đúng theo câu hỏi đó dựa trên nội dung file.
- Trình bày đủ các mục sau bằng tiếng Việt có dấu:
  1. Tóm tắt nội dung
  2. Các mạch kiến thức liên quan
  3. Công thức cần nhớ
  4. Phương pháp giải theo dạng bài
  5. Lỗi sai thường gặp
  6. Lộ trình ôn tập ngắn
  7. Gợi ý nhánh sơ đồ tư duy
- Công thức Toán phải viết LaTeX rõ ràng để MathJax hiển thị, ví dụ \\(a^2 + b^2 = c^2\\). Không viết sai ký hiệu toán học.
- Với đề kiểm tra, chỉ giải thích hướng làm và kiến thức nền; không biến toàn bộ phản hồi thành đáp án hoàn chỉnh nếu học sinh chưa làm.
- Nếu nội dung file có ghi chú hệ thống về MathType/OLE/WMF hoặc công thức bị nhúng dạng ảnh, hãy nói rõ hạn chế đọc công thức, dùng ảnh gửi kèm nếu có, và không tự bịa phần biểu thức bị thiếu.

Câu hỏi của học sinh: {question}

NỘI DUNG FILE:
{trimmed_text[:12000]}
"""


####
### shl
#################
def load_class_activities():
    """Load danh sách các phiên sinh hoạt lớp"""
    try:
        with open(CLASS_ACTIVITY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_class_activities(data):
    """Lưu danh sách sinh hoạt lớp"""
    with open(CLASS_ACTIVITY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.route('/class_activity', methods=['GET'])
def class_activity():
    """Trang chính - Danh sách các phiên sinh hoạt"""
    activities = load_class_activities()
    return render_template('class_activity.html', activities=activities)


@app.route('/class_activity/new', methods=['GET', 'POST'])
def new_class_activity():
    """Tạo phiên sinh hoạt mới"""
    if request.method == 'POST':
        week_name = request.form.get('week_name', '').strip()
        description = request.form.get('description', '').strip()

        if not week_name:
            flash('Vui lòng nhập tên tuần sinh hoạt!', 'error')
            return redirect(url_for('new_class_activity'))

        activity_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")

        new_activity = {
            'id': activity_id,
            'week_name': week_name,
            'description': description,
            'created_at': timestamp,
            'status': 'collecting',  # collecting, analyzed
            'groups': {
                'to_1': [],
                'to_2': [],
                'to_3': [],
                'to_4': [],
                'giao_vien': []
            },
            'ai_analysis': None
        }

        activities = load_class_activities()
        activities.insert(0, new_activity)
        save_class_activities(activities)

        flash('Đã tạo phiên sinh hoạt mới!', 'success')
        return redirect(
            url_for('class_activity_detail', activity_id=activity_id))

    return render_template('new_class_activity.html')


###
def load_chat_messages(activity_id):
    """Load tin nhắn chat của một phiên sinh hoạt"""
    try:
        with open(CLASS_CHAT_FILE, 'r', encoding='utf-8') as f:
            all_chats = json.load(f)
            return all_chats.get(activity_id, [])
    except FileNotFoundError:
        return []


def save_chat_message(activity_id, message_data):
    """Lưu tin nhắn chat mới"""
    try:
        with open(CLASS_CHAT_FILE, 'r', encoding='utf-8') as f:
            all_chats = json.load(f)
    except FileNotFoundError:
        all_chats = {}

    if activity_id not in all_chats:
        all_chats[activity_id] = []

    all_chats[activity_id].append(message_data)

    with open(CLASS_CHAT_FILE, 'w', encoding='utf-8') as f:
        json.dump(all_chats, f, ensure_ascii=False, indent=2)


@app.route('/class_activity/<activity_id>/chat', methods=['GET'])
def class_activity_chat(activity_id):
    """Trang chat ẩn danh của lớp"""
    activities = load_class_activities()
    activity = next((a for a in activities if a['id'] == activity_id), None)

    if not activity:
        flash('Không tìm thấy phiên sinh hoạt!', 'error')
        return redirect(url_for('class_activity'))

    messages = load_chat_messages(activity_id)

    return render_template('class_activity_chat.html',
                           activity=activity,
                           messages=messages)


@app.route('/class_activity/<activity_id>/chat/send', methods=['POST'])
def send_chat_message(activity_id):
    """Gửi tin nhắn chat"""
    activities = load_class_activities()
    activity = next((a for a in activities if a['id'] == activity_id), None)

    if not activity:
        return jsonify({'success': False, 'error': 'Activity not found'}), 404

    data = request.get_json()
    message_text = data.get('message', '').strip()
    nickname = data.get('nickname', '').strip()

    if not message_text:
        return jsonify({'success': False, 'error': 'Message is empty'}), 400

    if not nickname:
        nickname = 'Ẩn danh'

    # Tạo message data
    message_data = {
        'id': str(uuid.uuid4()),
        'nickname': nickname,
        'message': message_text,
        'timestamp': datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        'avatar_color': generate_avatar_color(nickname)
    }

    save_chat_message(activity_id, message_data)

    return jsonify({'success': True, 'message': message_data})


@app.route('/class_activity/<activity_id>/chat/messages', methods=['GET'])
def get_chat_messages(activity_id):
    """Lấy danh sách tin nhắn (API cho auto-refresh)"""
    messages = load_chat_messages(activity_id)
    return jsonify({'success': True, 'messages': messages})


def generate_avatar_color(nickname):
    """Tạo màu avatar dựa trên nickname"""
    colors = [
        '#FF6B6B', '#4ECDC4', '#45B7D1', '#FFA07A', '#98D8C8', '#F7DC6F',
        '#BB8FCE', '#85C1E2', '#F8B500', '#52B788', '#E63946', '#457B9D'
    ]
    # Hash nickname để lấy màu cố định cho mỗi nickname
    hash_value = sum(ord(c) for c in nickname)
    return colors[hash_value % len(colors)]


#####
@app.route('/class_activity/<activity_id>', methods=['GET', 'POST'])
def class_activity_detail(activity_id):
    """Chi tiết phiên sinh hoạt - Upload ảnh cho từng tổ"""
    activities = load_class_activities()
    activity = next((a for a in activities if a['id'] == activity_id), None)

    if not activity:
        flash('Không tìm thấy phiên sinh hoạt!', 'error')
        return redirect(url_for('class_activity'))

    if request.method == 'POST':
        group_name = request.form.get('group_name')
        uploaded_files = request.files.getlist('images')

        if not group_name or group_name not in activity['groups']:
            flash('Tổ không hợp lệ!', 'error')
            return redirect(
                url_for('class_activity_detail', activity_id=activity_id))

        if not uploaded_files or all(f.filename == '' for f in uploaded_files):
            flash('Vui lòng chọn ít nhất 1 ảnh!', 'error')
            return redirect(
                url_for('class_activity_detail', activity_id=activity_id))

        # Xử lý từng file
        for uploaded_file in uploaded_files:
            if uploaded_file and uploaded_file.filename != '':
                if not allowed_file(uploaded_file.filename):
                    continue

                # Lưu file
                file_id = str(uuid.uuid4())
                filename = f"{file_id}_{secure_filename(uploaded_file.filename)}"
                file_path = os.path.join(CLASS_ACTIVITY_IMAGES, filename)
                uploaded_file.save(file_path)

                # Thêm vào group
                activity['groups'][group_name].append({
                    'id':
                    file_id,
                    'filename':
                    filename,
                    'uploaded_at':
                    datetime.now().strftime("%d/%m/%Y %H:%M")
                })

        # Cập nhật activity
        for i, a in enumerate(activities):
            if a['id'] == activity_id:
                activities[i] = activity
                break

        save_class_activities(activities)

        flash(f'Đã upload ảnh cho {group_name}!', 'success')
        return redirect(
            url_for('class_activity_detail', activity_id=activity_id))

    return render_template('class_activity_detail.html', activity=activity)


#####
#####
@app.route('/class_activity/<activity_id>/analyze', methods=['POST'])
def analyze_class_activity(activity_id):
    """AI phân tích tất cả báo cáo của các tổ VÀ tạo HTML infographic"""
    activities = load_class_activities()
    activity = next((a for a in activities if a['id'] == activity_id), None)

    if not activity:
        flash('Không tìm thấy phiên sinh hoạt!', 'error')
        return redirect(url_for('class_activity'))

    # Kiểm tra xem có đủ dữ liệu không
    total_images = sum(len(images) for images in activity['groups'].values())
    if total_images == 0:
        flash(
            'Chưa có ảnh nào được upload. Vui lòng upload ảnh trước khi phân tích!',
            'error')
        return redirect(
            url_for('class_activity_detail', activity_id=activity_id))

    try:
        # ========================================
        # BƯỚC 1: PHÂN TÍCH TEXT TỪ ẢNH CÁC TỔ
        # ========================================
        analysis_prompt = [
            f"""Bạn là giáo viên chủ nhiệm đang đánh giá sinh hoạt lớp tuần này.

THÔNG TIN TUẦN SINH HOẠT:
- Tên: {activity['week_name']}
- Mô tả: {activity.get('description', 'Không có')}

NHIỆM VỤ:
1. Phân tích báo cáo của 4 tổ (Tổ 1, 2, 3, 4)
2. Đánh giá từng tổ: điểm mạnh, điểm yếu, cho điểm (0-10)
3. So sánh các tổ và xếp hạng
4. Đối chiếu với báo cáo giáo viên (nếu có)
5. Trích xuất THỜI KHÓA BIỂU từ ảnh (nếu có)
6. Đánh giá các tiêu chí: Ký luật, Nội quy, Chuẩn bị bài, Vệ sinh
7. Đề xuất phương hướng tuần mới CỤ THỂ (4-5 mục tiêu)
8. Khen cá nhân tập thể có điểm số thi đua cao tặng huy hiệu thi đua tuần cho cá nhân và tập thể tổ đó
**LƯU Ý VỀ ĐỒNG PHỤC:**
- CHỈ sử dụng các lựa chọn SAU ĐÂY:
  + "Đồng phục áo trắng, quần tối màu"
  + "Đồng phục thể dục"
  + "Áo khoác mùa đông" (có thể kết hợp với các loại trên)
- KHÔNG được viết "váy",hay bất kỳ từ ngữ nào khác
- VÍ DỤ ĐÚNG: "Đồng phục áo trắng, quần tối màu, áo khoác mùa đông"
- VÍ DỤ SAI: "Áo trắng, váy tối màu"

ĐỊNH DẠNG PHẢN HỒI (JSON) - BẮT BUỘC:
{{
  "tong_quan": "Tổng quan về tuần học...",
  "thoi_khoa_bieu": [
    {{"thu": "Thứ 2", "tiet_1": "Toán", "tiet_2": "Văn", "tiet_3": "Anh", "tiet_4": "Hóa", "tiet_5": "Thể dục", "do_dong_phuc": "Đồng phục áo trắng, quần tối màu"}},
    {{"thu": "Thứ 3", "tiet_1": "Lý", "tiet_2": "Sinh", "tiet_3": "Sử", "tiet_4": "Địa", "tiet_5": "GDCD", "do_dong_phuc": "Đồng phục thể dục"}},
    {{"thu": "Thứ 4", "tiet_1": "Toán", "tiet_2": "Văn", "tiet_3": "Anh", "tiet_4": "Vật lý", "tiet_5": "TD", "do_dong_phuc": "Đồng phục áo trắng, quần tối màu, áo khoác mùa đông"}},
    {{"thu": "Thứ 5", "tiet_1": "Toán", "tiet_2": "Văn", "tiet_3": "Anh", "tiet_4": "Hóa", "tiet_5": "Sinh", "do_dong_phuc": "Đồng phục thể dục, áo khoác mùa đông"}},
    {{"thu": "Thứ 6", "tiet_1": "Toán", "tiet_2": "Văn", "tiet_3": "Anh", "tiet_4": "Sử", "tiet_5": "TD", "do_dong_phuc": "Đồng phục áo trắng, quần tối màu"}}
  ],
  "danh_gia_chi_tiet": {{
    "to_1": {{"diem_manh": "Học tập tốt", "diem_yeu": "Đi trễ", "xep_loai": "Tốt", "diem": 9}},
    "to_2": {{"diem_manh": "Đoàn kết", "diem_yeu": "Chưa tích cực", "xep_loai": "Khá", "diem": 8}},
    "to_3": {{"diem_manh": "Sáng tạo", "diem_yeu": "Vệ sinh chưa tốt", "xep_loai": "Khá", "diem": 7.5}},
    "to_4": {{"diem_manh": "Năng động", "diem_yeu": "Chú ý giờ giấc", "xep_loai": "TB", "diem": 7}}
  }},
  "nhan_xet_tuan_qua": [
    {{"tieu_chi": "Ký luật giờ học", "danh_gia": "Vẫn còn chuyện riêng", "xep_loai": "Khá", "icon": "📚"}},
    {{"tieu_chi": "Nội quy lớp", "danh_gia": "Sai trang phục", "xep_loai": "Trung bình", "icon": "👔"}},
    {{"tieu_chi": "Chuẩn bị bài vở", "danh_gia": "Chưa đầy đủ", "xep_loai": "Cần cải thiện", "icon": "📖"}},
    {{"tieu_chi": "Vệ sinh lớp học", "danh_gia": "Đã cải thiện", "xep_loai": "Tốt", "icon": "🧹"}}
  ],
  "phuong_huong_tuan_moi": [
    "Ôn tập chủ động, chuẩn bị bài trước khi đến lớp",
    "Nghiêm túc tập trung, tham gia phát biểu tích cực",
    "Hoàn thành bài tập đầy đủ, nộp đúng hạn",
    "Khen những học sinh đạt điểm cao tặng huy hiệu cho học sinh đó",
    "Giữ gìn vệ sinh, không xả rác bừa bãi"
  ]
}}

CHỈ TRẢ VỀ JSON, KHÔNG THÊM TEXT KHÁC.

Dưới đây là báo cáo các tổ:
"""
        ]

        # Thêm ảnh của từng tổ
        for group_name, images in activity['groups'].items():
            if images:
                group_display = {
                    'to_1': 'TỔ 1',
                    'to_2': 'TỔ 2',
                    'to_3': 'TỔ 3',
                    'to_4': 'TỔ 4',
                    'giao_vien': 'GIÁO VIÊN'
                }
                analysis_prompt.append(
                    f"\n--- BÁO CÁO {group_display[group_name]} ---")

                for img_data in images:
                    img_path = os.path.join(CLASS_ACTIVITY_IMAGES,
                                            img_data['filename'])
                    if os.path.exists(img_path):
                        img = Image.open(img_path)
                        analysis_prompt.append(img)

        # Gọi Gemini phân tích
        analysis_response = model.generate_content(analysis_prompt)
        ai_analysis = clean_ai_output(analysis_response.text)

        # Parse JSON
        try:
            # Loại bỏ markdown code blocks
            ai_analysis_clean = ai_analysis.replace('```json',
                                                    '').replace('```',
                                                                '').strip()
            analysis_data = json.loads(ai_analysis_clean)
        except Exception as parse_error:
            print(f"JSON Parse Error: {parse_error}")
            print(f"AI Response: {ai_analysis}")
            # Tạo data mẫu nếu parse thất bại
            analysis_data = {
                "tong_quan":
                "Không thể phân tích được dữ liệu từ ảnh.",
                "thoi_khoa_bieu": [{
                    "thu": "Thứ 2",
                    "tiet_1": "Toán",
                    "tiet_2": "Văn",
                    "tiet_3": "Anh",
                    "tiet_4": "Hóa",
                    "tiet_5": "TD"
                }, {
                    "thu": "Thứ 3",
                    "tiet_1": "Lý",
                    "tiet_2": "Sinh",
                    "tiet_3": "Sử",
                    "tiet_4": "Địa",
                    "tiet_5": "GDCD"
                }],
                "nhan_xet_tuan_qua": [{
                    "tieu_chi": "Học tập",
                    "danh_gia": "Tốt",
                    "xep_loai": "Khá",
                    "icon": "✅"
                }],
                "phuong_huong_tuan_moi":
                ["Ôn tập chủ động", "Tham gia phát biểu"]
            }

        # ========================================
        # BƯỚC 2: TẠO HTML INFOGRAPHIC ĐẦY ĐỦ
        # ========================================

        # Build thời khóa biểu HTML
        tkb_html = ""
        for day_info in analysis_data.get('thoi_khoa_bieu', [])[:5]:
            thu = day_info.get('thu', 'Thứ 2')
            tkb_html += f"<tr><td colspan='3' style='background: #2196F3; color: white; font-weight: bold; text-align: center;'>{thu}</td></tr>"
            for i in range(1, 6):
                mon = day_info.get(f'tiet_{i}', '-')
                tkb_html += f"<tr><td style='text-align:center; font-weight:bold;'>{i}</td><td>{mon}</td><td style='text-align:center;'>📚</td></tr>"
            # Thêm info đồng phục nếu có
            do_dp = day_info.get('do_dong_phuc', '')
            if do_dp:
                tkb_html += f"<tr><td colspan='3' style='background:#e3f2fd; text-align:center; padding:8px;'>👔 {do_dp}</td></tr>"

        # Build nhận xét tuần qua
        nhan_xet_html = ""
        for item in analysis_data.get('nhan_xet_tuan_qua', [])[:6]:
            icon = item.get('icon', '✅')
            tieu_chi = item.get('tieu_chi', '')
            danh_gia = item.get('danh_gia', '')
            xep_loai = item.get('xep_loai', '')

            nhan_xet_html += f"""
            <div class="eval-row">
                <div class="eval-icon">{icon}</div>
                <div class="eval-label">{tieu_chi}</div>
                <div class="eval-content">
                    <div>{danh_gia}</div>
                    <span class="eval-badge">{xep_loai}</span>
                </div>
            </div>
            """

        # Build phương hướng tuần mới
        phuong_huong_html = ""
        for item in analysis_data.get('phuong_huong_tuan_moi', [])[:5]:
            phuong_huong_html += f"""
            <div class="goal-item">
                <div class="goal-icon">✅</div>
                <div class="goal-text">{item}</div>
            </div>
            """

        # HTML PROMPT ĐẦY ĐỦ
        html_prompt = f"""Tạo file HTML HOÀN CHỈNH cho infographic kế hoạch tuần học lớp 8A9 - THCS Cẩm Phả.

YÊU CẦU BẮT BUỘC:
- File HTML hoàn chỉnh: <!DOCTYPE html>, <html lang="vi">, <head> với <meta charset="UTF-8">
- Kích thước: 1200px width, chiều cao tự động
- Design 2.5D hiện đại, giống hình mẫu đã gửi
- Background: gradient pastel giống lớp học (#e8d5c4 → #d4b5a0)
- Header: gradient xanh dương (#4facfe → #00f2fe), logo trường, mặt trời icon
- Layout: Grid 2 cột cho phần chính
- Font: 'Segoe UI', sans-serif - hỗ trợ tiếng Việt có dấu
- Thêm CDN: html2canvas từ https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js
- Nút "TẢI XUỐNG ẢNH PNG" với function downloadImage()
- Box có shadow, border-radius, viền màu gradient

CẤU TRÚC CHÍNH:

=== HEADER ===
<div id="infographic" style="width:1200px; background: linear-gradient(135deg, #e8d5c4 0%, #d4b5a0 100%);">
  <div class="header" style="background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); padding:30px; position:relative;">
    <div class="logo" style="position:absolute; top:20px; left:30px; background:white; width:80px; height:80px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-weight:bold; color:#3a8fd9;">THCS<br>CẨM PHẢ</div>
    <span style="position:absolute; top:20px; left:120px; font-size:60px;">☀️</span>
    <h1 style="text-align:center; color:white; font-size:48px; text-shadow: 3px 3px 6px rgba(0,0,0,0.3); margin-bottom:10px;">KẾ HOẠCH TUẦN HỌC LỚP 8A9</h1>
    <div style="text-align:center; color:white; font-size:32px;">THCS CẨM PHẢ - TUẤN HẠC</div>
    <div style="text-align:center; color:white; font-size:24px; margin-top:10px;">{activity['week_name']}</div>
  </div>

  <div class="content" style="display:grid; grid-template-columns:1fr 1fr; gap:30px; padding:30px;">

    <!-- CỘT TRÁI: THỜI KHÓA BIỂU -->
    <div class="schedule-box" style="background:white; border-radius:15px; padding:20px; box-shadow:0 8px 20px rgba(0,0,0,0.15); border:4px solid #4facfe;">
      <div class="title" style="background:linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); color:white; padding:15px; border-radius:10px; text-align:center; font-size:20px; font-weight:bold; margin-bottom:20px;">📅 THỜI KHÓA BIỂU</div>
      <table style="width:100%; border-collapse:collapse;">
        <tr style="background:#ffd89b; color:white;">
          <th style="border:2px solid #ddd; padding:10px;">Tiết</th>
          <th style="border:2px solid #ddd; padding:10px;">Môn học</th>
          <th style="border:2px solid #ddd; padding:10px;">Icon</th>
        </tr>
        {tkb_html}
      </table>
    </div>

    <!-- CỘT PHẢI: NHẬN XÉT -->
    <div class="eval-box" style="background:white; border-radius:15px; padding:20px; box-shadow:0 8px 20px rgba(0,0,0,0.15); border:4px solid #5ec793;">
      <div class="title" style="background:linear-gradient(135deg, #5ec793 0%, #3da66d 100%); color:white; padding:15px; border-radius:10px; text-align:center; font-size:20px; font-weight:bold; margin-bottom:20px;">📊 NHẬN XÉT SINH HOẠT LỚP TUẦN QUA</div>
      {nhan_xet_html}
    </div>
  </div>

  <!-- PHƯƠNG HƯỚNG TUẦN MỚI (Full width) -->
  <div style="padding:0 30px 30px 30px;">
    <div class="goals-box" style="background:white; border-radius:15px; padding:20px; box-shadow:0 8px 20px rgba(0,0,0,0.15); border:4px solid #f093fb;">
      <div class="title" style="background:linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color:white; padding:15px; border-radius:10px; text-align:center; font-size:24px; font-weight:bold; margin-bottom:20px;">🎯 PHƯƠNG HƯỚNG TUẦN MỚI</div>
      {phuong_huong_html}
    </div>
  </div>
</div>

<button onclick="downloadImage()" style="margin:20px auto; display:block; padding:15px 40px; font-size:18px; background:linear-gradient(135deg, #667eea 0%, #764ba2 100%); color:white; border:none; border-radius:50px; cursor:pointer; font-weight:bold; box-shadow:0 4px 15px rgba(0,0,0,0.2);">⬇️ TẢI XUỐNG ẢNH PNG</button>

<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script>
async function downloadImage() {{
    const element = document.getElementById('infographic');
    const canvas = await html2canvas(element, {{
        scale: 2,
        backgroundColor: '#e8d5c4',
        logging: false,
        useCORS: true
    }});
    const link = document.createElement('a');
    link.download = 'ke-hoach-tuan-hoc.png';
    link.href = canvas.toDataURL('image/png');
    link.click();
}}
</script>

STYLING CSS:
- .eval-row: display:flex; gap:15px; align-items:center; padding:12px; background:#f8f9fa; border-radius:10px; margin-bottom:10px;
- .eval-icon: font-size:32px;
- .eval-label: flex:1; font-weight:600; color:#333;
- .eval-content: display:flex; flex-direction:column; gap:5px;
- .eval-badge: background:linear-gradient(135deg, #ffd89b 0%, #ff9a56 100%); padding:5px 15px; border-radius:20px; color:white; font-weight:bold; align-self:flex-start;
- .goal-item: display:flex; gap:15px; align-items:center; padding:15px; background:#f8f9fa; border-radius:10px; margin-bottom:15px; box-shadow:0 2px 5px rgba(0,0,0,0.1);
- .goal-icon: font-size:32px;
- .goal-text: font-size:18px; font-weight:500;

CHỈ TRẢ VỀ CODE HTML HOÀN CHỈNH, KHÔNG GIẢI THÍCH."""

        # Gọi Gemini tạo HTML
        html_response = model.generate_content([html_prompt])
        html_content = clean_ai_output(html_response.text)

        # Loại bỏ markdown code blocks
        html_content = html_content.replace('```html', '').replace('```',
                                                                   '').strip()

        # Lưu file HTML
        infographic_dir = "static/class_activity_infographics"
        os.makedirs(infographic_dir, exist_ok=True)

        infographic_filename = f"{activity_id}_infographic.html"
        infographic_path = os.path.join(infographic_dir, infographic_filename)

        with open(infographic_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        activity[
            'infographic_html'] = f"/static/class_activity_infographics/{infographic_filename}"

        # ========================================
        # LƯU KẾT QUẢ
        # ========================================
        activity['ai_analysis'] = ai_analysis
        activity['analysis_data'] = analysis_data
        activity['status'] = 'analyzed'
        activity['analyzed_at'] = datetime.now().strftime("%d/%m/%Y %H:%M")

        for i, a in enumerate(activities):
            if a['id'] == activity_id:
                activities[i] = activity
                break

        save_class_activities(activities)

        flash('Đã phân tích và tạo infographic thành công!', 'success')

    except Exception as e:
        flash(f'Lỗi khi phân tích: {str(e)}', 'error')
        import traceback
        print(traceback.format_exc())

    return redirect(url_for('class_activity_result', activity_id=activity_id))


    #################
@app.route('/class_activity/<activity_id>/result')
def class_activity_result(activity_id):
    """Xem kết quả phân tích"""
    activities = load_class_activities()
    activity = next((a for a in activities if a['id'] == activity_id), None)

    if not activity:
        flash('Không tìm thấy phiên sinh hoạt!', 'error')
        return redirect(url_for('class_activity'))

    if activity['status'] != 'analyzed' or not activity.get('ai_analysis'):
        flash('Phiên này chưa được phân tích!', 'error')
        return redirect(
            url_for('class_activity_detail', activity_id=activity_id))

    return render_template('class_activity_result.html', activity=activity)


@app.route('/class_activity/<activity_id>/delete', methods=['POST'])
def delete_class_activity(activity_id):
    """Xóa phiên sinh hoạt"""
    activities = load_class_activities()
    activity = next((a for a in activities if a['id'] == activity_id), None)

    if activity:
        # Xóa các file ảnh
        for group_name, images in activity['groups'].items():
            for img_data in images:
                img_path = os.path.join(CLASS_ACTIVITY_IMAGES,
                                        img_data['filename'])
                try:
                    if os.path.exists(img_path):
                        os.remove(img_path)
                except:
                    pass

        # Xóa activity
        activities = [a for a in activities if a['id'] != activity_id]
        save_class_activities(activities)

        flash('Đã xóa phiên sinh hoạt!', 'success')

    return redirect(url_for('class_activity'))


###############
###
#
MINDMAP_DIR = os.path.join('static', 'chatbot_mindmaps')

TUTOR_PERSONA_PROMPT = """

Bạn là Tri-hand, một gia sư Toán thân thiện cho học sinh THCS/THPT.
- Bạn không phải công cụ đưa đáp án. Bạn là gia sư giúp học sinh tự suy nghĩ.
- Nói tiếng Việt có dấu tự nhiên, gần gũi, gọi học sinh là "em".
- Giải thích chậm rãi, rõ từng ý, không viết dài quá mức cần thiết.
- Ưu tiên Toán học. Nếu học sinh hỏi môn khác, vẫn giữ phong cách gia sư gợi mở.
- Nếu học sinh gửi đề nhưng chưa có bài làm: KHÔNG đưa lời giải hoàn chỉnh.
- Chỉ nêu dạng bài, kiến thức cần dùng, công thức, định lý, hướng tiếp cận và câu hỏi gợi mở.
- Nếu học sinh đã thử làm: kiểm tra đúng/sai, chỉ lỗi, giải thích ngắn, gợi ý cách sửa.
- Hình học/chứng minh: dùng phân tích ngược: kết luận -> điều cần có -> định lý/căn cứ -> giả thiết; sau đó yêu cầu em viết lời giải thuận.
- Cấu trúc mặc định: nhận diện dạng bài -> công thức/kiến thức -> gợi ý bước đầu -> đặt 1 câu hỏi cho em làm tiếp.
"""

MATH_FORMAT_RULES = """

QUY TẮC HIỂN THỊ CÔNG THỨC TOÁN:
- Tri-hand ưu tiên phục vụ môn Toán, nên công thức phải hiển thị đúng và đẹp.
- Viết công thức bằng LaTeX để MathJax render trên giao diện.
- Công thức ngắn đặt trong \\( ... \\), ví dụ: \\(x^2 + 2x + 1\\).
- Công thức quan trọng hoặc cần canh giữa đặt trong \\[ ... \\].
- Dùng \\frac{}{}, ^{}, _{}, \\sqrt{}, \\lim, \\sin, \\cos, \\tan, \\ln, \\to cho phân số, lũy thừa, căn, giới hạn, lượng giác, logarit.
- Không viết công thức dạng text thô nếu có thể viết LaTeX.
"""


def strip_json_fences(text):
    text = (text or '').strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)
    match = re.search(r'\{.*\}', text, re.DOTALL)
    return match.group(0) if match else text


def load_mindmap_json(raw_json):
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        escaped_latex = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw_json)
        return json.loads(escaped_latex)


def safe_text(value, fallback=''):
    value = str(value or fallback).strip()
    return value[:180]


def normalize_latex_slashes(value):
    formula = str(value or '').strip()
    if not formula:
        return ''
    formula = formula.replace('`', '')
    while '\\\\' in formula:
        formula = formula.replace('\\\\', '\\')
    formula = re.sub(r'\s+', ' ', formula)
    return formula


def extract_formula_from_text(value):
    text = normalize_latex_slashes(value)
    if not text:
        return '', ''

    patterns = [
        r'\\\((.*?)\\\)',
        r'\\\[(.*?)\\\]',
        r'\$\$(.*?)\$\$',
        r'\$(.*?)\$'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue

        formula = match.group(0)
        cleaned = re.sub(pattern, '', text, count=1).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)
        cleaned = re.sub(r'\s*[:：,;|-]\s*$', '', cleaned).strip()
        return cleaned, safe_formula(formula)

    bare_latex = re.search(
        r'((?:[A-Za-z0-9_{}^+\-*/=<>|,. ]|\\[A-Za-z]+)+'
        r'\\(?:sqrt|frac|pm|ge|le|cdot|Rightarrow|Leftrightarrow)'
        r'(?:[A-Za-z0-9_{}^+\-*/=<>|,. ]|\\[A-Za-z]+)*)',
        text
    )
    if bare_latex:
        formula = bare_latex.group(1).strip()
        cleaned = text.replace(bare_latex.group(1), '', 1).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)
        cleaned = re.sub(r'\s*[:：,;|-]\s*$', '', cleaned).strip()
        return cleaned, safe_formula(formula)

    return text, ''


def clean_mindmap_text(value, fallback=''):
    text, _ = extract_formula_from_text(value)
    text = safe_text(text, fallback)
    text = re.sub(r'\s+', ' ', text).strip()
    return text or fallback


def has_vietnamese_diacritics(text):
    text = str(text or '')
    return any(
        ch in text
        for ch in 'ăâêôơưđĂÂÊÔƠƯĐáàảãạắằẳẵặấầẩẫậéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ'
    )


def is_sqrt_topic(source_text, title):
    folded = fold_search_text(f'{title} {source_text}')
    return any(keyword in folded for keyword in ['can bac 2', 'can bac hai', 'can thuc', 'sqrt'])


def is_short_topic_text(source_text):
    text = str(source_text or '').strip()
    return bool(text) and len(text) <= 120 and '\n' not in text


def get_curated_mindmap_title(source_text, title):
    if is_sqrt_topic(source_text, title):
        return 'Căn bậc hai'
    return title


def is_likely_math_formula(formula):
    formula = normalize_latex_slashes(formula)
    if not formula:
        return False
    if re.search(r'\\[A-Za-z]+', formula):
        return True
    if re.search(r'[=<>^_{}+\-*/|±√×÷≤≥]', formula):
        return True
    if re.fullmatch(r'\d+(?:[.,]\d+)?', formula):
        return True
    if re.fullmatch(r'[A-Za-z]', formula):
        return True
    return False


def safe_formula(value):
    formula = normalize_latex_slashes(value)
    if not formula:
        return ''
    formula = (
        formula
        .replace('≥', '\\ge ')
        .replace('≤', '\\le ')
        .replace('×', '\\times ')
        .replace('÷', '\\div ')
    )
    folded_formula = fold_search_text(formula)
    if 'lon hon hoac bang 0' in folded_formula or 'khong am' in folded_formula:
        return '\\(A \\ge 0\\)'

    delimited = (
        re.search(r'\\\((.*?)\\\)', formula)
        or re.search(r'\\\[(.*?)\\\]', formula)
        or re.search(r'\$\$(.*?)\$\$', formula)
        or re.search(r'\$(.*?)\$', formula)
    )
    if delimited:
        formula = delimited.group(1).strip()

    if not is_likely_math_formula(formula):
        return ''

    if formula.startswith('\\[') and formula.endswith('\\]'):
        formula = f"\\({formula[2:-2].strip()}\\)"
    elif formula.startswith('$$') and formula.endswith('$$'):
        formula = f"\\({formula[2:-2].strip()}\\)"
    elif formula.startswith('$') and formula.endswith('$'):
        formula = f"\\({formula[1:-1].strip()}\\)"
    formula = formula[:260]
    if formula.startswith('\\('):
        return formula
    return f'\\({formula}\\)'


def normalize_mindmap_child(child):
    if isinstance(child, dict):
        raw_title = child.get('title') or child.get('text') or child.get('label')
        title, title_formula = extract_formula_from_text(raw_title)
        formula = safe_formula(child.get('formula') or child.get('math')) or title_formula
        title = safe_text(title, '')
    else:
        title, title_formula = extract_formula_from_text(child)
        title = safe_text(title, '')
        formula = title_formula

    if not title and formula:
        title = 'Công thức'

    return {'title': title, 'formula': formula}


def fold_search_text(text):
    text = unicodedata.normalize('NFKD', str(text or '').lower())
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace('đ', 'd')


def build_fallback_mindmap_branches(source_text, title):
    topic = safe_text(title or source_text, 'Nội dung học tập')
    folded = fold_search_text(f'{topic} {source_text}')

    if any(keyword in folded for keyword in ['can bac 2', 'can thuc', 'sqrt']):
        return [
            {
                'title': 'Khái niệm',
                'note': 'Căn bậc hai của a là số x có bình phương bằng a',
                'formula': '\\(x^2=a\\)',
                'children': [
                    {'title': 'Với a dương có hai căn đối nhau', 'formula': '\\(x=\\pm\\sqrt a\\)'},
                    {'title': 'Số 0 có đúng một căn bậc hai', 'formula': '\\(\\sqrt0=0\\)'},
                    {'title': 'Số âm không có căn bậc hai trong tập số thực', 'formula': '\\(a<0\\)'}
                ]
            },
            {
                'title': 'Căn bậc hai số học',
                'note': 'Là căn không âm của một số không âm',
                'formula': '\\(\\sqrt a\\ge0\\)',
                'children': [
                    {'title': 'Ký hiệu căn số học của a', 'formula': '\\(\\sqrt a\\)'},
                    {'title': 'Bình phương của căn số học', 'formula': '\\((\\sqrt a)^2=a\\)'},
                    {'title': 'Điều kiện của số dưới căn', 'formula': '\\(a\\ge0\\)'}
                ]
            },
            {
                'title': 'Điều kiện xác định',
                'note': 'Biểu thức dưới dấu căn phải không âm',
                'formula': '\\(A\\ge0\\)',
                'children': [
                    {'title': 'Ví dụ căn của x trừ 3', 'formula': '\\(x-3\\ge0\\)'},
                    {'title': 'Suy ra miền giá trị phù hợp', 'formula': '\\(x\\ge3\\)'},
                    {'title': 'Luôn đặt điều kiện trước khi biến đổi', 'formula': '\\(A\\ge0\\)'}
                ]
            },
            {
                'title': 'Tính chất cơ bản',
                'note': 'Dùng để rút gọn và biến đổi căn thức',
                'formula': '\\(\\sqrt{ab}=\\sqrt a\\sqrt b\\)',
                'children': [
                    {'title': 'Căn của một thương', 'formula': '\\(\\sqrt{\\frac a b}=\\frac{\\sqrt a}{\\sqrt b}\\)'},
                    {'title': 'Căn của bình phương', 'formula': '\\(\\sqrt{A^2}=|A|\\)'},
                    {'title': 'Bình phương căn bậc hai số học', 'formula': '\\((\\sqrt A)^2=A\\)'}
                ]
            },
            {
                'title': 'Phép tính với căn',
                'note': 'Rút gọn, khai phương, trục căn thức ở mẫu',
                'formula': '\\(\\sqrt{k^2A}=|k|\\sqrt A\\)',
                'children': [
                    {'title': 'Đưa thừa số ra ngoài dấu căn', 'formula': '\\(\\sqrt{12}=2\\sqrt3\\)'},
                    {'title': 'Nhân chia hai căn bậc hai', 'formula': '\\(\\sqrt a\\sqrt b=\\sqrt{ab}\\)'},
                    {'title': 'Trục căn thức đơn giản', 'formula': '\\(\\frac A{\\sqrt B}=\\frac{A\\sqrt B}{B}\\)'}
                ]
            },
            {
                'title': 'Lỗi cần tránh',
                'note': 'Không tách căn qua phép cộng và không bỏ giá trị tuyệt đối',
                'formula': '\\(\\sqrt{a+b}\\ne\\sqrt a+\\sqrt b\\)',
                'children': [
                    {'title': 'Không bỏ dấu giá trị tuyệt đối', 'formula': '\\(\\sqrt{A^2}=|A|\\)'},
                    {'title': 'Không quên điều kiện dưới căn', 'formula': '\\(A\\ge0\\)'},
                    {'title': 'Phân biệt căn số học và hai nghiệm', 'formula': '\\(x^2=a\\Rightarrow x=\\pm\\sqrt a\\)'}
                ]
            }
        ]

    sentences = [s.strip() for s in re.split(r'[.\n;:]+', source_text) if s.strip()]
    if len(sentences) >= 3:
        return [
            {
                'title': safe_text(sentence, f'Ý {index + 1}')[:42],
                'note': 'Ý chính cần ghi nhớ',
                'formula': '',
                'children': [
                    {'title': 'Từ khóa quan trọng', 'formula': ''},
                    {'title': 'Liên hệ với bài học', 'formula': ''}
                ]
            }
            for index, sentence in enumerate(sentences[:5])
        ]

    return [
        {
            'title': 'Khái niệm',
            'note': f'Hiểu đúng định nghĩa của {topic}',
            'formula': '',
            'children': [
                {'title': 'Từ khóa chính', 'formula': ''},
                {'title': 'Ý nghĩa trong bài học', 'formula': ''}
            ]
        },
        {
            'title': 'Công thức',
            'note': 'Ghi lại công thức hoặc quy tắc cần dùng',
            'formula': '',
            'children': [
                {'title': 'Điều kiện áp dụng', 'formula': ''},
                {'title': 'Kí hiệu quan trọng', 'formula': ''}
            ]
        },
        {
            'title': 'Cách làm',
            'note': 'Chia bài thành các bước nhỏ để xử lý',
            'formula': '',
            'children': [
                {'title': 'Bước 1: nhận dạng dạng bài', 'formula': ''},
                {'title': 'Bước 2: áp dụng quy tắc', 'formula': ''}
            ]
        },
        {
            'title': 'Ví dụ',
            'note': 'Tự chọn một bài ngắn để luyện',
            'formula': '',
            'children': [
                {'title': 'Làm mẫu một ý đơn giản', 'formula': ''},
                {'title': 'So sánh với bài tương tự', 'formula': ''}
            ]
        },
        {
            'title': 'Lưu ý',
            'note': 'Ghi lại lỗi dễ nhầm khi học chủ đề này',
            'formula': '',
            'children': [
                {'title': 'Kiểm tra điều kiện', 'formula': ''},
                {'title': 'Không bỏ qua bước giải thích', 'formula': ''}
            ]
        }
    ]


def normalize_mindmap_data(raw_data, source_text):
    raw_data = raw_data if isinstance(raw_data, dict) else {}
    title = safe_text(raw_data.get('title') or str(source_text).splitlines()[0][:60], 'Sơ đồ tư duy')
    summary = safe_text(raw_data.get('summary'), 'Tóm tắt kiến thức chính')
    source_head = safe_text(str(source_text).splitlines()[0] if source_text else '', '')
    if (
        source_head
        and len(source_head) <= 80
        and has_vietnamese_diacritics(source_head)
        and not has_vietnamese_diacritics(title)
    ):
        title = source_head
    title = get_curated_mindmap_title(source_text, title)
    if is_sqrt_topic(source_text, title) and is_short_topic_text(source_text):
        return {
            'title': title,
            'summary': f'Tổng quan về {title}: khái niệm, căn số học, điều kiện, tính chất và lỗi cần tránh.',
            'branches': build_fallback_mindmap_branches(source_text, title)
        }
    branches = raw_data.get('branches') if isinstance(raw_data.get('branches'), list) else []

    normalized = []
    for index, branch in enumerate(branches[:7]):
        if not isinstance(branch, dict):
            continue
        branch_title, title_formula = extract_formula_from_text(branch.get('title'))
        branch_note, note_formula = extract_formula_from_text(branch.get('note'))
        children = branch.get('children') if isinstance(branch.get('children'), list) else []
        normalized_children = []
        for child in children[:3]:
            normalized_child = normalize_mindmap_child(child)
            if normalized_child['title']:
                normalized_children.append(normalized_child)

        normalized.append({
            'title': safe_text(branch_title, f'Y {index + 1}'),
            'note': safe_text(branch_note, ''),
            'formula': safe_formula(branch.get('formula') or branch.get('math')) or title_formula or note_formula,
            'children': normalized_children
        })

    ai_visible_text = ' '.join(
        [summary]
        + [
            f"{branch.get('title', '')} {branch.get('note', '')} "
            + ' '.join(child.get('title', '') for child in branch.get('children', []))
            for branch in normalized
        ]
    )
    if is_sqrt_topic(source_text, title) and not has_vietnamese_diacritics(ai_visible_text):
        normalized = build_fallback_mindmap_branches(source_text, title)
        summary = f'Tổng quan về {title}: định nghĩa, tính chất, điều kiện và ứng dụng.'

    if len(normalized) < 3:
        sentences = [
            s.strip()
            for s in re.split(r'[.\n]+', source_text)
            if s.strip() and fold_search_text(s.strip()) != fold_search_text(title)
        ]
        for index, sentence in enumerate(sentences[:5 - len(normalized)]):
            normalized.append({
                'title': sentence[:42],
                'note': 'Ý chính cần ghi nhớ',
                'formula': '',
                'children': [
                    {'title': 'Từ khóa quan trọng', 'formula': ''},
                    {'title': 'Liên hệ với bài học', 'formula': ''}
                ]
            })

    if len(normalized) < 3:
        fallback_branches = build_fallback_mindmap_branches(source_text, title)
        existing_titles = {fold_search_text(branch.get('title')) for branch in normalized}
        for branch in fallback_branches:
            if fold_search_text(branch.get('title')) in existing_titles:
                continue
            normalized.append(branch)
            if len(normalized) >= 5:
                break

    return {
        'title': title,
        'summary': summary,
        'branches': normalized
    }


def get_mindmap_variant(data):
    seed_text = data['title'] + '|' + '|'.join(branch['title'] for branch in data['branches'])
    seed = int(hashlib.sha256(seed_text.encode('utf-8')).hexdigest()[:8], 16)
    variants = ['orbit', 'split', 'cascade', 'constellation', 'ribbon', 'radial']
    palettes = [
        ['#2563eb', '#f97316', '#16a34a', '#e11d48', '#7c3aed', '#0891b2', '#ca8a04'],
        ['#0f766e', '#db2777', '#ea580c', '#4f46e5', '#65a30d', '#0284c7', '#be123c'],
        ['#1d4ed8', '#9333ea', '#dc2626', '#059669', '#d97706', '#0e7490', '#be185d'],
        ['#0369a1', '#b45309', '#15803d', '#c026d3', '#b91c1c', '#047857', '#4338ca']
    ]
    return variants[seed % len(variants)], palettes[(seed // 7) % len(palettes)], seed


def wrap_svg_text(text, max_chars):
    words = html_lib.escape(str(text)).split()
    lines = []
    current = ''
    for word in words:
        candidate = f'{current} {word}'.strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines[:4]


def render_node(x, y, w, h, text, fill, stroke, text_color='#0f172a', shape='rect', subtitle='', formula=''):
    if shape == 'pill':
        body = f'<rect x="{x - w / 2:.1f}" y="{y - h / 2:.1f}" width="{w}" height="{h}" rx="{h / 2:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="3"/>'
    elif shape == 'circle':
        body = f'<ellipse cx="{x:.1f}" cy="{y:.1f}" rx="{w / 2:.1f}" ry="{h / 2:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="3"/>'
    else:
        body = f'<rect x="{x - w / 2:.1f}" y="{y - h / 2:.1f}" width="{w}" height="{h}" rx="18" fill="{fill}" stroke="{stroke}" stroke-width="3"/>'

    subtitle_html = f'<div class="node-subtitle">{html_lib.escape(subtitle)}</div>' if subtitle else ''
    display_formula = safe_formula(formula).replace('\\\\', '\\') if formula else ''
    formula_html = f'<div class="node-formula">{html_lib.escape(display_formula)}</div>' if display_formula else ''

    return f'''
    <g class="node">
        {body}
        <foreignObject x="{x - w / 2 + 12:.1f}" y="{y - h / 2 + 10:.1f}" width="{w - 24}" height="{h - 20}">
            <div xmlns="http://www.w3.org/1999/xhtml" class="node-content" style="color:{text_color};">
                <div class="node-title">{html_lib.escape(text)}</div>
                {subtitle_html}
                {formula_html}
            </div>
        </foreignObject>
    </g>
    '''


def get_branch_positions(count, variant):
    if count <= 0:
        return []

    if count >= 5:
        radius_x = 590
        radius_y = 520
        start_offsets = {
            'orbit': -math.pi / 2,
            'radial': -math.pi / 2 + 0.16,
            'constellation': -math.pi / 2 - 0.16,
            'ribbon': -math.pi / 2,
            'cascade': -math.pi / 2 + 0.10,
            'split': -math.pi / 2 - 0.10,
        }
        offset = start_offsets.get(variant, -math.pi / 2)
        return [
            (radius_x * math.cos(offset + 2 * math.pi * i / count),
             radius_y * math.sin(offset + 2 * math.pi * i / count))
            for i in range(count)
        ]

    if variant == 'split':
        left = [(-560, -300), (-610, -20), (-520, 300)]
        right = [(560, -300), (610, -20), (520, 300), (120, 390)]
        return (left + right)[:count]

    if variant == 'cascade':
        return [(-620 + i * (1240 / max(count - 1, 1)), -320 + (i % 3) * 300) for i in range(count)]

    if variant == 'ribbon':
        return [(-640 + i * (1280 / max(count - 1, 1)), 310 * math.sin(i * 1.2)) for i in range(count)]

    if variant == 'constellation':
        base = [(-570, -300), (-180, -360), (310, -330), (620, -40), (390, 370), (-230, 390), (-640, 40)]
        return base[:count]

    radius_x = 620 if variant == 'orbit' else 570
    radius_y = 350 if variant == 'orbit' else 330
    offset = -math.pi / 2
    return [
        (radius_x * math.cos(offset + 2 * math.pi * i / count),
         radius_y * math.sin(offset + 2 * math.pi * i / count))
        for i in range(count)
    ]


def clamp(value, low, high):
    return max(low, min(high, value))


def get_child_position(parent_x, parent_y, center_x, center_y, child_index, total_children, width, height):
    dx = parent_x - center_x
    dy = parent_y - center_y
    distance = math.hypot(dx, dy) or 1
    ux, uy = dx / distance, dy / distance
    px, py = -uy, ux
    if abs(ux) < 0.42:
        spread_step = 330
    elif abs(uy) < 0.42:
        spread_step = 155
    else:
        spread_step = 260
    spread = (child_index - (total_children - 1) / 2) * spread_step
    outward = 270 + min(total_children, 4) * 20
    child_x = parent_x + ux * outward + px * spread
    child_y = parent_y + uy * outward + py * spread
    return clamp(child_x, 155, width - 155), clamp(child_y, 130, height - 130)


def render_mindmap_html(data):
    variant, palette, seed = get_mindmap_variant(data)
    branches = data['branches']
    width, height = 2200, 1900
    cx, cy = width / 2, height / 2
    positions = get_branch_positions(len(branches), variant)
    shapes = ['rect', 'pill', 'rect', 'pill', 'circle']

    links = []
    nodes = []
    child_nodes = []

    center_fill = '#eff6ff'
    nodes.append(render_node(cx, cy, 360, 136, data['title'], center_fill, '#1d4ed8', '#123a7a', 'pill', data.get('summary', '')))

    for index, branch in enumerate(branches):
        dx, dy = positions[index]
        x, y = cx + dx, cy + dy
        color = palette[index % len(palette)]
        branch_fill = '#ffffff'
        curve = 80 if dx >= 0 else -80
        links.append(f'<path d="M {cx:.1f} {cy:.1f} C {cx + curve:.1f} {cy:.1f}, {x - curve:.1f} {y:.1f}, {x:.1f} {y:.1f}" fill="none" stroke="{color}" stroke-width="6" stroke-linecap="round" opacity="0.82"/>')
        branch_height = 166 if branch.get('formula') else 104
        nodes.append(render_node(x, y, 330, branch_height, branch['title'], branch_fill, color, '#0f172a', shapes[(seed + index) % len(shapes)], branch.get('note', ''), branch.get('formula', '')))

        children = branch.get('children', [])
        for child_index, child in enumerate(children):
            child_x, child_y = get_child_position(x, y, cx, cy, child_index, len(children), width, height)
            control_y = (y + child_y) / 2 - (35 if child_y >= y else -35)
            links.append(f'<path d="M {x:.1f} {y:.1f} Q {(x + child_x) / 2:.1f} {control_y:.1f}, {child_x:.1f} {child_y:.1f}" fill="none" stroke="{color}" stroke-width="3.5" stroke-linecap="round" opacity="0.58"/>')
            child_height = 132 if child.get('formula') else 78
            child_nodes.append(render_node(child_x, child_y, 300, child_height, child['title'], '#f8fbff', color, '#13233a', 'pill', '', child.get('formula', '')))

    decorative = []
    for i, color in enumerate(palette[:5]):
        decorative.append(f'<circle cx="{115 + i * 75}" cy="{105 + (i % 2) * 34}" r="{14 + (i % 3) * 4}" fill="{color}" opacity="0.16"/>')
        decorative.append(f'<path d="M {1390 - i * 62} {840 + (i % 2) * 34} l 18 -18 m -18 18 l -18 -18" stroke="{color}" stroke-width="5" stroke-linecap="round" opacity="0.18"/>')

    svg = f'''
    <svg id="mindmap-svg" xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
        <defs>
            <style>
                <![CDATA[
                .node-content {{
                    width: 100%;
                    height: 100%;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    gap: 4px;
                    padding: 2px 4px;
                    text-align: center;
                    font-family: "Segoe UI", Arial, sans-serif;
                    line-height: 1.18;
                    overflow: hidden;
                }}
                .node-title {{
                    max-width: 100%;
                    font-size: 16px;
                    font-weight: 800;
                    overflow-wrap: anywhere;
                }}
                .node-subtitle {{
                    max-width: 100%;
                    font-size: 13px;
                    font-weight: 700;
                    opacity: 0.72;
                    overflow-wrap: anywhere;
                }}
                .node-formula {{
                    max-width: 100%;
                    font-size: 14px;
                    font-weight: 700;
                    color: #0f172a;
                    overflow: hidden;
                }}
                .node-formula mjx-container {{
                    margin: 0 !important;
                    max-width: 100%;
                }}
                ]]>
            </style>
            <pattern id="notebook" width="68" height="40" patternUnits="userSpaceOnUse">
                <rect width="68" height="40" fill="#ffffff"/>
                <path d="M 0 0 H 68 M 0 0 V 40" stroke="rgba(37,99,235,0.32)" stroke-width="2"/>
            </pattern>
            <filter id="softShadow" x="-20%" y="-20%" width="140%" height="140%">
                <feDropShadow dx="0" dy="10" stdDeviation="10" flood-color="#1d4ed8" flood-opacity="0.16"/>
            </filter>
        </defs>
        <rect width="100%" height="100%" fill="url(#notebook)"/>
        <g filter="url(#softShadow)">
            {''.join(decorative)}
            {''.join(links)}
            {''.join(child_nodes)}
            {''.join(nodes)}
        </g>
        <text x="80" y="930" font-family="Segoe UI, Arial, sans-serif" font-size="18" fill="#3a5f93" font-weight="700">Tri-hand Mindmap</text>
    </svg>
    '''

    return f'''<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sơ đồ tư duy - {html_lib.escape(data['title'])}</title>
    <script>
        if (window.self !== window.top) {{
            document.documentElement.classList.add('embedded');
        }}
    </script>
    <script>
        window.MathJax = {{
            tex: {{
                inlineMath: [['\\\\(', '\\\\)'], ['$', '$']],
                displayMath: [['\\\\[', '\\\\]']]
            }},
            svg: {{ fontCache: 'none' }},
            startup: {{ typeset: false }}
        }};
    </script>
    <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
    <script defer src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: "Segoe UI", Arial, sans-serif;
            background:
                linear-gradient(90deg, rgba(37, 99, 235, 0.42) 2px, transparent 2px) 0 0 / 68px 68px,
                linear-gradient(rgba(37, 99, 235, 0.42) 2px, transparent 2px) 0 0 / 68px 40px,
                #ffffff;
            color: #123a7a;
        }}
        .toolbar {{
            position: sticky;
            top: 0;
            z-index: 5;
            display: flex;
            gap: 10px;
            align-items: center;
            justify-content: space-between;
            padding: 14px 18px;
            background: rgba(255, 255, 255, 0.94);
            border-bottom: 2px solid #2563eb;
            box-shadow: 0 10px 24px rgba(37,99,235,0.14);
        }}
        html.embedded .toolbar {{
            display: none;
        }}
        html.embedded .canvas-wrap {{
            margin: 12px auto 24px;
        }}
        .toolbar-title {{ font-weight: 800; }}
        .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .btn {{
            border: 1px solid #2563eb;
            background: #2563eb;
            color: white;
            padding: 10px 14px;
            border-radius: 8px;
            font-weight: 700;
            cursor: pointer;
            text-decoration: none;
        }}
        .btn.secondary {{ background: white; color: #1d4ed8; }}
        .canvas-wrap {{
            width: min(1900px, calc(100vw - 28px));
            margin: 18px auto;
            background: #ffffff;
            border: 2px solid #2563eb;
            border-radius: 12px;
            overflow: auto;
            box-shadow: 0 16px 38px rgba(37,99,235,0.16);
        }}
        #mindmap-svg {{ display: block; width: 100%; min-width: 1180px; height: auto; }}
        .node-content {{
            width: 100%;
            height: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 4px;
            padding: 2px 4px;
            text-align: center;
            font-family: "Segoe UI", Arial, sans-serif;
            line-height: 1.14;
            overflow: hidden;
        }}
        .node-title {{
            max-width: 100%;
            font-size: 15px;
            font-weight: 800;
            overflow-wrap: anywhere;
        }}
        .node-subtitle {{
            max-width: 100%;
            font-size: 12px;
            font-weight: 700;
            opacity: 0.72;
            overflow-wrap: anywhere;
        }}
        .node-formula {{
            width: 100%;
            max-width: 100%;
            min-height: 34px;
            max-height: 56px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            color: #0f172a;
        }}
        .node-formula mjx-container {{
            margin: 0 !important;
            max-width: 100% !important;
            font-size: 110% !important;
            line-height: 1 !important;
            overflow: hidden !important;
        }}
        .node-formula mjx-container svg {{
            max-width: 100% !important;
            max-height: 52px !important;
            overflow: hidden !important;
        }}
        .node-formula.math-fallback {{
            font-size: 13px;
            font-weight: 700;
            white-space: normal;
            overflow-wrap: anywhere;
        }}
    </style>
</head>
<body>
    <div class="toolbar">
        <div class="toolbar-title">Sơ đồ tư duy: {html_lib.escape(data['title'])}</div>
        <div class="actions">
            <a href="/chatbot" class="btn secondary">← Quay lại chatbot</a>
            <button class="btn" onclick="downloadPng()">Tải PNG</button>
        </div>
    </div>
    <div class="canvas-wrap">{svg}</div>
    <script>
        function normalizeFormulaText() {{
            document.querySelectorAll('.node-formula').forEach((holder) => {{
                holder.childNodes.forEach((node) => {{
                    if (node.nodeType !== Node.TEXT_NODE) return;
                    node.textContent = node.textContent
                        .replace(/\\\\\\\\/g, '\\\\')
                        .replace(/\\\\\\(/g, '\\\\(')
                        .replace(/\\\\\\)/g, '\\\\)');
                }});
            }});
        }}

        async function renderMindmapMath() {{
            normalizeFormulaText();
            if (window.MathJax && window.MathJax.startup && window.MathJax.startup.promise) {{
                await window.MathJax.startup.promise;
                if (window.MathJax.typesetPromise) {{
                    for (const holder of document.querySelectorAll('.node-formula')) {{
                        try {{
                            await window.MathJax.typesetPromise([holder]);
                        }} catch (error) {{
                            holder.classList.add('math-fallback');
                        }}
                    }}
                }}
            }}
            fitMindmapFormulas();
            setTimeout(fitMindmapFormulas, 120);
        }}

        function fitMindmapFormulas() {{
            document.querySelectorAll('.node-formula mjx-container').forEach((math) => {{
                math.style.transform = '';
                math.style.transformOrigin = 'center center';
                const holder = math.closest('.node-formula');
                if (!holder) return;
                const holderBox = holder.getBoundingClientRect();
                const mathBox = math.getBoundingClientRect();
                if (!holderBox.width || !holderBox.height || !mathBox.width || !mathBox.height) return;
                const scale = Math.min(1, holderBox.width / mathBox.width, holderBox.height / mathBox.height);
                if (scale < 1) {{
                    math.style.transform = `scale(${{Math.max(scale, 0.62)}})`;
                }}
            }});
        }}

        window.addEventListener('DOMContentLoaded', renderMindmapMath);

        function saveCanvasAsPng(canvas) {{
            const link = document.createElement('a');
            link.download = 'tri-hand-so-do-tu-duy.png';
            link.href = canvas.toDataURL('image/png');
            document.body.appendChild(link);
            link.click();
            link.remove();
        }}

        function downloadSvgFallback() {{
            return new Promise((resolve, reject) => {{
                const svg = document.getElementById('mindmap-svg');
                const serializer = new XMLSerializer();
                const svgText = serializer.serializeToString(svg);
                const blob = new Blob([svgText], {{ type: 'image/svg+xml;charset=utf-8' }});
                const url = URL.createObjectURL(blob);
                const image = new Image();
                const timer = setTimeout(() => {{
                    URL.revokeObjectURL(url);
                    reject(new Error('SVG render timeout'));
                }}, 6000);

                image.onload = function() {{
                    clearTimeout(timer);
                    const canvas = document.createElement('canvas');
                    canvas.width = svg.viewBox.baseVal.width;
                    canvas.height = svg.viewBox.baseVal.height;
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = '#ffffff';
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    ctx.drawImage(image, 0, 0);
                    URL.revokeObjectURL(url);
                    saveCanvasAsPng(canvas);
                    resolve();
                }};

                image.onerror = function() {{
                    clearTimeout(timer);
                    URL.revokeObjectURL(url);
                    reject(new Error('Không vẽ được SVG lên canvas'));
                }};

                image.src = url;
            }});
        }}

        async function downloadPng() {{
            const button = document.querySelector('button[onclick="downloadPng()"]');
            const oldText = button ? button.textContent : '';
            if (button) {{
                button.disabled = true;
                button.textContent = 'Đang tải...';
            }}

            await renderMindmapMath();

            try {{
                if (window.html2canvas) {{
                    const element = document.querySelector('.canvas-wrap');
                    const previousWidth = element.style.width;
                    const previousOverflow = element.style.overflow;
                    element.style.width = `${{element.scrollWidth}}px`;
                    element.style.overflow = 'visible';

                    const canvas = await html2canvas(element, {{
                        backgroundColor: '#ffffff',
                        scale: 2,
                        useCORS: true,
                        logging: false,
                        width: element.scrollWidth,
                        height: element.scrollHeight,
                        windowWidth: element.scrollWidth + 40,
                        windowHeight: element.scrollHeight + 40
                    }});

                    element.style.width = previousWidth;
                    element.style.overflow = previousOverflow;
                    saveCanvasAsPng(canvas);
                }} else {{
                    await downloadSvgFallback();
                }}
            }} catch (error) {{
                console.error(error);
                try {{
                    await downloadSvgFallback();
                }} catch (fallbackError) {{
                    console.error(fallbackError);
                    alert('Chưa tải được PNG. Em thử tải lại sau khi trang render xong hoặc tạo sơ đồ mới nhé.');
                }}
            }} finally {{
                if (button) {{
                    button.disabled = false;
                    button.textContent = oldText || 'Tải PNG';
                }}
            }}
        }}
    </script>
</body>
</html>'''


@app.route('/chatbot/create_mindmap', methods=['POST'])
def create_chatbot_mindmap():
    chat_history = session.get('chat_history', [])
    topic = request.form.get('mindmap_topic', '').strip()
    is_ajax_request = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    history_text = '\n'.join(
        f"Học sinh: {item.get('user', '')}\nAI: {item.get('bot', '')}"
        for item in chat_history[-6:]
    )
    source_text = topic or history_text

    if not source_text.strip():
        message = 'Em hãy hỏi hoặc trao đổi một nội dung trước, sau đó bấm Tạo sơ đồ.'
        if is_ajax_request:
            return jsonify({
                'success': False,
                'bot': message
            }), 400

        session['chat_history'] = [{
            'user': '[Tạo sơ đồ tư duy]',
            'bot': message,
            'timestamp': datetime.now().strftime("%H:%M")
        }]
        session.modified = True
        return redirect(url_for('chatbot'))

    prompt = f"""
Hãy tạo dữ liệu JSON cho một SƠ ĐỒ TƯ DUY học tập từ nội dung sau.
Chỉ trả về JSON hợp lệ, không markdown.

Yêu cầu sư phạm:
- Luôn viết tiếng Việt có dấu đầy đủ trong mọi trường chữ: title, summary, note, children.title.
- Tóm tắt đúng kiến thức đã trao đổi, không thêm đáp án giải hoàn chỉnh nếu là bài tập.
- Mỗi nhánh ngắn gọn, rõ ý, dùng tiếng Việt tự nhiên.
- Tạo 4 đến 6 nhánh chính, mỗi nhánh có 2 đến 3 ý con để sơ đồ thoáng, không rối.
- Đặt title ngắn, summary một câu.
- title và note chỉ viết chữ thường ngắn gọn, TUYỆT ĐỐI không chèn LaTeX vào title hoặc note.
- Mọi công thức, ký hiệu Toán phải đặt riêng trong trường "formula".
- Nếu nội dung có công thức Toán, BẮT BUỘC đưa công thức quan trọng vào trường "formula".
- Công thức phải viết bằng LaTeX MathJax dạng INLINE \\(...\\), không dùng \\[...\\] trong sơ đồ vì ô nhỏ.
- Vì đang trả về JSON, mỗi dấu backslash trong LaTeX nên viết thành \\\\, ví dụ "\\\\frac{{a}}{{b}}".
- Với các mục như trung bình cộng, tổng, xác suất, đạo hàm, tích phân, hình học... hãy ưu tiên chèn công thức vào nhánh phù hợp.
- Có thể gợi ý visual_style/palette nhưng không bắt buộc.

Schema:
{{
  "title": "...",
  "summary": "...",
  "branches": [
    {{
      "title": "...",
      "note": "...",
      "formula": "\\\\(công thức nếu có\\\\)",
      "children": [
        {{"title": "...", "formula": "\\\\(công thức nếu có\\\\)"}},
        {{"title": "...", "formula": ""}}
      ]
    }}
  ]
}}

NỘI DUNG:
{source_text[:6000]}
"""

    try:
        ai_response = model.generate_content(prompt)
        raw_json = strip_json_fences(ai_response.text)
        raw_data = load_mindmap_json(raw_json)
    except Exception:
        raw_data = {
            'title': topic or 'Sơ đồ tư duy',
            'summary': 'Tóm tắt từ nội dung trao đổi',
            'branches': []
        }

    mindmap_data = normalize_mindmap_data(raw_data, source_text)
    html_content = render_mindmap_html(mindmap_data)

    os.makedirs(MINDMAP_DIR, exist_ok=True)
    filename = f"mindmap_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.html"
    file_path = os.path.join(MINDMAP_DIR, filename)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    mindmap_url = url_for('static', filename=f'chatbot_mindmaps/{filename}')

    if is_ajax_request:
        return jsonify({
            'success': True,
            'url': mindmap_url,
            'title': mindmap_data.get('title', 'Sơ đồ tư duy'),
            'filename': filename
        })

    return redirect(mindmap_url)


# Route cho chatbot
@app.route('/chatbot', methods=['GET', 'POST'])
def chatbot():
    if 'chat_history' not in session:
        session['chat_history'] = []

    response_text = None

    if request.method == 'POST':
        user_message = request.form.get('message', '').strip()
        uploaded_file = request.files.get('file')
        is_ajax_request = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        user_display = user_message if user_message else '[Đã gửi file]'

        # Đọc dữ liệu từ data.txt
        knowledge_base = ""
        try:
            with open('data.txt', 'r', encoding='utf-8') as f:
                knowledge_base = f.read()
        except FileNotFoundError:
            knowledge_base = "Không tìm thấy file data.txt"

        # Xây dựng prompt chi tiết cho AI
        system_prompt = f"""Bạn là trợ lý AI thông minh hỗ trợ học sinh trong học tập.

KIẾN THỨC CƠ SỞ (từ data.txt):
{knowledge_base}

VAI TRÒ CỦA BẠN:
- Bạn là giáo viên/gia sư AI thân thiện, kiên nhẫn và nhiệt tình
- Hướng dẫn học sinh tự giải quyết vấn đề, phát triển tư duy độc lập
- Phân tích bài làm, hình ảnh bài tập học sinh gửi lên
- KHÔNG đưa ra đáp án trực tiếp - chỉ gợi ý và hướng dẫn cách giải

NGUYÊN TẮC QUAN TRỌNG:
1. KHI HỌC SINH HỎI BÀI (chưa làm):
   - TUYỆT ĐỐI KHÔNG đưa đáp án trực tiếp
   - TUYỆT ĐỐI KHÔNG giải chi tiết từng bước ra kết quả
   - CHỈ hướng dẫn phương pháp, công thức, định lý cần dùng
   - CHỈ gợi ý hướng tư duy, cách tiếp cận bài toán
   - Khuyến khích học sinh tự thực hiện các bước tính toán

2. KHI HỌC SINH GỬI ẢNH BÀI LÀM/ĐỀ TRẮC NGHIỆM:
   - Kiểm tra xem học sinh đã làm bài chưa (có khoanh/viết đáp án không)
   - NẾU ĐÃ LÀM (có đánh dấu/khoanh/ghi đáp án):
     * Chỉ ra câu nào đúng, câu nào sai
     * Giải thích tại sao sai và cách suy nghĩ đúng
     * Hướng dẫn cách cải thiện
   - NẾU CHƯA LÀM (đề trắng, chưa khoanh):
     * TUYỆT ĐỐI KHÔNG cho đáp án
     * CHỈ hướng dẫn kiến thức, phương pháp để giải từng câu
     * Gợi ý cách phân tích, loại trừ đáp án
     * Khuyến khích học sinh tự làm trước

CÁCH TRẢ LỜI:
1. Luôn trả lời bằng tiếng Việt
2. Với câu hỏi chưa làm:
   - "Để giải bài này, em cần biết công thức/định lý..."
   - "Hướng tiếp cận: Bước 1... Bước 2... Em thử làm xem"
   - "Gợi ý: Em hãy chú ý đến... và áp dụng..."

3. Với bài đã làm:
   - "Câu 1: Em làm đúng/sai. Giải thích:..."
   - "Câu 2: Đáp án của em là... nhưng đáp án đúng là... vì..."

4. Với văn/ngữ văn:
   - Gợi ý cách phân tích tác phẩm, nhân vật
   - Hướng dẫn cấu trúc bài văn
   - KHÔNG viết sẵn đoạn văn mẫu

QUY TẮC TRÌNH BÀY:
- KHÔNG dùng **, ***, ##, ###, ````
- Công thức toán viết văn bản thường: (x + 2)/(x - 3) hoặc x^2 + 3x + 2
- Xuống dòng rõ ràng giữa các ý
- Dùng số thứ tự 1. 2. 3. hoặc dấu gạch đầu dòng -
- Giữ văn phong thân thiện, động viên

LƯU Ý:
- Luôn khuyến khích học sinh: "Em hãy thử làm theo hướng dẫn này nhé!"
- Nếu học sinh yêu cầu đáp án trực tiếp, giải thích: "Thầy/cô sẽ hướng dẫn em cách làm để em tự rèn luyện tư duy nhé!"

Hãy ưu tiên sử dụng thông tin từ KIẾN THỨC CƠ SỞ khi trả lời các câu hỏi liên quan.
"""
        system_prompt = (
            TUTOR_PERSONA_PROMPT
            + MATH_FORMAT_RULES
            + "\nKien thuc co so ngan gon:\n"
            + knowledge_base[:800]
        )

        try:
            # Xử lý nếu có file đính kèm
            if uploaded_file and uploaded_file.filename != '':
                original_filename = uploaded_file.filename
                file_ext = original_filename.rsplit(
                    '.', 1)[1].lower() if '.' in original_filename else ''

                # Lưu file tạm
                temp_filename = f"temp_{uuid.uuid4()}_{secure_filename(original_filename)}"
                temp_path = os.path.join(app.config['UPLOAD_FOLDER'],
                                         temp_filename)
                uploaded_file.save(temp_path)

                # Xử lý theo loại file
                if file_ext in ['pdf', 'docx', 'txt']:
                    file_text = extract_text_from_chatbot_file(
                        temp_path, file_ext)
                    full_prompt = build_chatbot_file_analysis_prompt(
                        system_prompt, file_text, user_message,
                        original_filename)
                    prompt_parts = [full_prompt]
                    if file_ext == 'docx':
                        docx_images = render_docx_pages_for_ai(temp_path)
                        image_source_note = (
                            "Hệ thống đã render một số trang đầu của DOCX thành ảnh. "
                            "Hãy dùng ảnh để đọc công thức MathType/WMF, hình vẽ và bảng biểu nếu nhìn rõ."
                        )
                        if not docx_images:
                            docx_images = extract_docx_images_for_ai(temp_path)
                            image_source_note = (
                                "Hệ thống đã gửi kèm một số ảnh PNG/JPG trích từ DOCX. "
                                "Hãy dùng chúng để đọc hình vẽ, bảng biểu hoặc công thức dạng ảnh nếu nhìn rõ."
                            )
                        if docx_images:
                            full_prompt += f"\n\n{image_source_note}"
                            prompt_parts = docx_images + [full_prompt]
                    response = model.generate_content(prompt_parts)
                    response_text = response.text

                elif file_ext in ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp']:
                    # Đọc ảnh
                    img = Image.open(temp_path)
                    full_prompt = f"""{system_prompt}

Học sinh gửi ảnh bài tập/đề thi.

QUAN TRỌNG:
- Hãy kiểm tra kỹ xem học sinh đã làm bài chưa (có đánh dấu, khoanh tròn, ghi đáp án không).
- Nếu ĐÃ LÀM: chấm bài, chỉ ra đúng/sai và giải thích ngắn.
- Nếu CHƯA LÀM hoặc đây là đề kiểm tra trắng: không cho đáp án trực tiếp; hãy phân tích các dạng bài, kiến thức liên quan, công thức cần nhớ và phương pháp giải.
- Nếu học sinh yêu cầu tóm tắt/tạo sơ đồ tư duy từ ảnh, hãy nêu rõ các nhánh kiến thức có thể đưa vào sơ đồ.

Câu hỏi thêm: {user_message if user_message else 'Hãy phân tích kiến thức liên quan và hướng dẫn em'}
"""
                    response = model.generate_content([img, full_prompt])
                    response_text = response.text

                else:
                    response_text = "Định dạng file không được hỗ trợ. Em có thể gửi ảnh (.png, .jpg, .jpeg), PDF, DOCX hoặc TXT."

                # Xóa file tạm
                try:
                    os.remove(temp_path)
                except:
                    pass

            else:
                # Chỉ có text message
                if user_message:
                    full_prompt = f"{system_prompt}\n\nHọc sinh hỏi: {user_message}\n\nLƯU Ý: Chỉ hướng dẫn phương pháp, không đưa đáp án trực tiếp."
                    response = model.generate_content([full_prompt])
                    response_text = response.text
                else:
                    response_text = "Vui lòng nhập câu hỏi hoặc gửi file."

            # Làm sạch output
            response_text = clean_ai_output(response_text)

            # Lưu vào lịch sử chat
            session['chat_history'].append({
                'user':
                user_message if user_message else '[Đã gửi file]',
                'bot':
                response_text,
                'timestamp':
                datetime.now().strftime("%H:%M")
            })
            session.modified = True

        except Exception as e:
            response_text = f"Lỗi: {sanitize_gemini_error(e)}"

    if request.method == 'POST' and locals().get('is_ajax_request'):
        return jsonify({
            'success': not str(response_text).lower().startswith(('loi:', 'lỗi:')),
            'user': locals().get('user_display', ''),
            'bot': response_text,
            'timestamp': datetime.now().strftime("%H:%M")
        })

    return render_template('chatbot.html',
                           chat_history=session.get('chat_history', []),
                           response=response_text)


@app.route('/clear_chat', methods=['POST'])
def clear_chat():
    session['chat_history'] = []
    session.modified = True
    return redirect(url_for('chatbot'))


####
# Thêm vào file Flask


# Route đăng nhập cho chuyên gia
@app.route('/expert_login', methods=['GET', 'POST'])
def expert_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        # Đọc danh sách chuyên gia từ file
        try:
            with open('experts.json', 'r', encoding='utf-8') as f:
                experts = json.load(f)
        except FileNotFoundError:
            experts = []

        # Kiểm tra đăng nhập
        def expert_password_matches(stored_password, input_password):
            if stored_password.startswith(('pbkdf2:', 'scrypt:', 'bcrypt:')):
                return check_password_hash(stored_password, input_password)
            return stored_password == input_password

        expert = next(
            (e for e in experts
             if e['username'] == username and expert_password_matches(e['password'], password)), None)

        if expert:
            session['expert_logged_in'] = True
            session['expert_name'] = expert['name']
            session['expert_username'] = username
            session['expert_specialty'] = expert.get('specialty', 'Sức khỏe')
            flash('Đăng nhập thành công!', 'success')
            return redirect(url_for('health_support'))
        else:
            flash('Sai tên đăng nhập hoặc mật khẩu!', 'error')

    return render_template('expert_login.html')


@app.route('/expert_logout')
def expert_logout():
    session.pop('expert_logged_in', None)
    session.pop('expert_name', None)
    session.pop('expert_username', None)
    session.pop('expert_specialty', None)
    flash('Đã đăng xuất!', 'info')
    return redirect(url_for('health_support'))


def parse_ai_json_response(raw_text):
    """Parse JSON text returned by the AI, even when wrapped in markdown."""
    if not raw_text:
        raise ValueError("Empty AI response")

    cleaned = raw_text.strip()
    cleaned = re.sub(r'^```json\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'^```\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)

    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    return json.loads(cleaned)


def fallback_health_triage(question_text):
    """Keyword-based fallback triage used when structured AI output is unavailable."""
    text = unicodedata.normalize('NFKD', question_text.lower())
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))

    critical_keywords = [
        'tu tu', 'muon chet', 'khong muon song', 'muon bien mat',
        'cuu voi', 'bat tinh', 'chay mau', 'tai nan nghiem trong',
        'kho tho', 'co giat', 'uong thuoc qua lieu', 'nhay lau'
    ]
    high_keywords = [
        'tram cam', 'tuyet vong', 'hoang loan', 'khung hoang',
        'bi danh', 'bao luc', 'xam hai', 'lam dung',
        'mat ngu nhieu ngay', 'khong on', 'suy sup'
    ]
    medium_keywords = [
        'stress', 'cang thang', 'lo au', 'buon', 'ap luc',
        'met moi', 'khoc', 'co don', 'bi bat nat', 'so hai'
    ]

    risk_level = 'low'
    if any(keyword in text for keyword in critical_keywords):
        risk_level = 'critical'
    elif any(keyword in text for keyword in high_keywords):
        risk_level = 'high'
    elif any(keyword in text for keyword in medium_keywords):
        risk_level = 'medium'

    needs_escalation = risk_level in {'high', 'critical'}
    summary_map = {
        'low': 'Ca tu van thong thuong, chua thay dau hieu nguy co cao.',
        'medium': 'Hoc sinh co dau hieu can theo doi va dong vien som.',
        'high': 'Co dau hieu bat on tam ly ro, can GVCN/chuyen gia tiep nhan som.',
        'critical': 'Tinh huong co the khan cap, can kich hoat can thiep som ngay.'
    }
    note_map = {
        'low': 'AI fallback khong ghi nhan tu khoa nguy co cao trong noi dung.',
        'medium': 'AI fallback ghi nhan mot so dau hieu cang thang/tam ly.',
        'high': 'AI fallback ghi nhan tu khoa nguy co cao nen de xuat chuyen tuyen.',
        'critical': 'AI fallback ghi nhan tu khoa khan cap nen kich hoat canh bao ngay.'
    }

    if needs_escalation:
        student_notice = (
            'He thong da nhan dien day la ca can ho tro sau hon va '
            'da chuyen canh bao an danh toi GVCN/chuyen gia de ho tro ban som.'
        )
    else:
        student_notice = 'AI da tiep nhan va ho tro ban theo luong tu van thong thuong.'

    return {
        'risk_level': risk_level,
        'needs_escalation': needs_escalation,
        'escalation_target': 'gvcn_expert' if needs_escalation else None,
        'alert_summary': summary_map[risk_level],
        'ai_triage_note': note_map[risk_level],
        'student_notice': student_notice
    }


def triage_health_question(question_text):
    """Use AI to classify risk level and trigger the demo escalation flow."""
    fallback_result = fallback_health_triage(question_text)
    prompt = f"""You are a school safety triage system.

Task:
- Read the student's message.
- Classify risk_level as one of: low, medium, high, critical.
- Set needs_escalation=true for serious mental distress, self-harm, violence, abuse, or severe accident cases.
- escalation_target must be null or "gvcn_expert".

Return ONLY valid JSON with these keys:
{{
  "risk_level": "low|medium|high|critical",
  "needs_escalation": true,
  "escalation_target": "gvcn_expert",
  "alert_summary": "short one-sentence summary",
  "ai_triage_note": "1-2 sentence reason",
  "student_notice": "short reassuring notice saying the system connected the anonymous alert to homeroom teacher/expert when needed"
}}

Student message:
{question_text}
"""

    try:
        response = model.generate_content([prompt])
        triage_result = parse_ai_json_response(response.text)
    except Exception:
        triage_result = fallback_result

    risk_level = str(triage_result.get('risk_level', 'low')).strip().lower()
    if risk_level not in {'low', 'medium', 'high', 'critical'}:
        risk_level = fallback_result['risk_level']

    needs_escalation = bool(triage_result.get('needs_escalation', False))
    if risk_level in {'high', 'critical'}:
        needs_escalation = True

    alert_summary = str(triage_result.get('alert_summary', '')).strip()
    ai_triage_note = str(triage_result.get('ai_triage_note', '')).strip()
    student_notice = str(triage_result.get('student_notice', '')).strip()

    return {
        'risk_level': risk_level,
        'needs_escalation': needs_escalation,
        'escalation_target': 'gvcn_expert' if needs_escalation else None,
        'alert_summary': alert_summary or fallback_result['alert_summary'],
        'ai_triage_note': ai_triage_note or fallback_result['ai_triage_note'],
        'student_notice': student_notice or fallback_result['student_notice']
    }


def build_escalation_support_response(triage_result):
    """Short, safe message shown while the case is being escalated."""
    if triage_result.get('risk_level') == 'critical':
        return (
            "Minh nhan thay day co the la tinh huong khan cap. "
            "Ban hay tim den ngay mot nguoi lon dang tin cay, GVCN, "
            "phu huynh hoac nhan vien y te gan nhat. He thong da kich hoat "
            "ket noi an danh de GVCN/chuyen gia co the ho tro ban som."
        )

    return (
        "He thong nhan thay ban co the dang can ho tro sau hon. "
        "Minh da kich hoat ket noi an danh den GVCN/chuyen gia de "
        "ban duoc ho tro som. Trong luc cho, neu ban cam thay qua tai "
        "hoac khong an toan, hay tim ngay mot nguoi lon dang tin cay o gan ban."
    )


# Route trang tư vấn sức khỏe
@app.route('/health_support', methods=['GET', 'POST'])
def health_support():
    # Load câu hỏi từ file
    try:
        with open('health_questions.json', 'r', encoding='utf-8') as f:
            questions = json.load(f)
    except FileNotFoundError:
        questions = []

    ai_response = None

    if request.method == 'POST':
        student_name = request.form.get('student_name', '').strip()
        question = request.form.get('question', '').strip()
        consult_type = request.form.get('consult_type')  # 'ai' hoặc 'expert'
        is_anonymous = request.form.get(
            'is_anonymous') == 'on'  # Checkbox ẩn danh

        if not student_name or not question:
            flash('Vui lòng nhập đầy đủ thông tin!', 'error')
            return redirect(url_for('health_support'))

        question_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
        triage_result = triage_health_question(question)
        needs_escalation = triage_result['needs_escalation']

        if needs_escalation:
            is_anonymous = True

        new_question = {
            'id': question_id,
            'student_name': student_name,
            'question': question,
            'consult_type': consult_type,
            'timestamp': timestamp,
            'ai_response': None,
            'expert_responses': [],
            'status': 'pending',  # pending, answered
            'risk_level': triage_result['risk_level'],
            'needs_escalation': needs_escalation,
            'escalation_target': triage_result['escalation_target'],
            'handling_status': 'new' if needs_escalation else None,
            'alert_summary': triage_result['alert_summary'],
            'ai_triage_note': triage_result['ai_triage_note'],
            'student_notice': triage_result['student_notice'],
            'is_anonymous': is_anonymous  # Thêm trường ẩn danh
        }

        # Nếu chọn AI tư vấn
        if needs_escalation:
            new_question['ai_response'] = build_escalation_support_response(
                triage_result)
        elif consult_type == 'ai':
            try:
                # Đọc kiến thức về sức khỏe
                health_knowledge = ""
                try:
                    with open('health_data.txt', 'r', encoding='utf-8') as f:
                        health_knowledge = f.read()
                except FileNotFoundError:
                    health_knowledge = "Không có dữ liệu sức khỏe."

                prompt = f"""Bạn là chuyên gia tư vấn sức khỏe cho học sinh.

KIẾN THỨC VỀ SỨC KHỎE:
{health_knowledge}

VAI TRÒ:
- Tư vấn các vấn đề sức khỏe phổ biến ở học sinh
- Tâm lý học đường, stress, lo âu
- Dinh dưỡng, vận động, giấc ngủ
- Sức khỏe sinh sản (phù hợp lứa tuổi)

QUY TẮC:
1. Trả lời bằng tiếng Việt, thân thiện, dễ hiểu
2. Không thay thế bác sĩ - khuyên gặp bác sĩ nếu nghiêm trọng
3. Đưa lời khuyên phù hợp lứa tuổi học sinh
4. Tôn trọng, không phán xét
5. KHÔNG dùng **, ##, ````

Học sinh hỏi: {question}

Hãy tư vấn chi tiết, có lời khuyên cụ thể."""

                response = model.generate_content([prompt])
                ai_response = clean_ai_output(response.text)
                new_question['ai_response'] = ai_response
                new_question['status'] = 'answered'

            except Exception as e:
                ai_response = f"❌ Lỗi: {str(e)}"
                new_question['ai_response'] = ai_response

        # Lưu câu hỏi
        questions.insert(0, new_question)  # Thêm vào đầu danh sách

        # Giữ tối đa 100 câu hỏi
        if len(questions) > 100:
            questions = questions[:100]

        with open('health_questions.json', 'w', encoding='utf-8') as f:
            json.dump(questions, f, ensure_ascii=False, indent=2)

        flash('Câu hỏi đã được gửi!', 'success')
        if needs_escalation:
            flash(triage_result['student_notice'], 'info')
        return redirect(url_for('health_support'))

    # Kiểm tra xem user có phải chuyên gia không
    is_expert = session.get('expert_logged_in', False)

    # Lọc câu hỏi hiển thị theo quyền
    display_questions = []
    for q in questions:
        q.setdefault('risk_level', 'low')
        q.setdefault('needs_escalation', False)
        q.setdefault('escalation_target', None)
        q.setdefault('handling_status', 'new' if q.get('needs_escalation')
                     else None)
        q.setdefault('alert_summary', None)
        q.setdefault('ai_triage_note', None)
        q.setdefault('student_notice', None)

        if q.get('needs_escalation') and not is_expert:
            continue

        if q.get('is_anonymous', False):
            # Nếu câu hỏi ẩn danh
            if is_expert:
                # Chuyên gia thấy đầy đủ
                display_questions.append(q)
            else:
                # Người khác chỉ thấy câu hỏi đã được trả lời và ẩn thông tin
                if q['status'] == 'answered' and (q.get('ai_response') or
                                                  q.get('expert_responses')):
                    hidden_q = q.copy()
                    hidden_q['student_name'] = 'Ẩn danh'
                    hidden_q[
                        'question'] = '[Câu hỏi riêng tư - chỉ chuyên gia xem được]'
                    display_questions.append(hidden_q)
        else:
            # Câu hỏi công khai - tất cả đều thấy
            display_questions.append(q)

    if is_expert:
        risk_priority = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        display_questions.sort(
            key=lambda q: (
                0 if q.get('needs_escalation') else 1,
                risk_priority.get(q.get('risk_level', 'low'), 3)))

    return render_template('health_support.html',
                           questions=display_questions,
                           is_expert=is_expert,
                           expert_name=session.get('expert_name'))


#######################


# Route chuyên gia trả lời
@app.route('/expert_answer/<question_id>', methods=['POST'])
def expert_answer(question_id):
    if not session.get('expert_logged_in'):
        flash('Bạn cần đăng nhập với tư cách chuyên gia!', 'error')
        return redirect(url_for('expert_login'))

    answer = request.form.get('answer', '').strip()

    if not answer:
        flash('Vui lòng nhập câu trả lời!', 'error')
        return redirect(url_for('health_support'))

    try:
        with open('health_questions.json', 'r', encoding='utf-8') as f:
            questions = json.load(f)
    except FileNotFoundError:
        questions = []

    # Tìm câu hỏi
    question = next((q for q in questions if q['id'] == question_id), None)

    if question:
        expert_response = {
            'expert_name': session.get('expert_name'),
            'specialty': session.get('expert_specialty', 'Sức khỏe'),
            'answer': answer,
            'timestamp': datetime.now().strftime("%d/%m/%Y %H:%M")
        }

        question['expert_responses'].append(expert_response)
        question['status'] = 'answered'
        if question.get('needs_escalation'):
            question['handling_status'] = 'contacted'

        with open('health_questions.json', 'w', encoding='utf-8') as f:
            json.dump(questions, f, ensure_ascii=False, indent=2)

        flash('Đã gửi câu trả lời!', 'success')
    else:
        flash('Không tìm thấy câu hỏi!', 'error')

    return redirect(url_for('health_support'))


@app.route('/health_case_status/<question_id>', methods=['POST'])
def health_case_status(question_id):
    if not session.get('expert_logged_in'):
        flash('Ban can dang nhap voi tu cach GVCN/chuyen gia!', 'error')
        return redirect(url_for('expert_login'))

    new_status = request.form.get('handling_status', '').strip().lower()
    allowed_statuses = {
        'new': 'Moi tiep nhan',
        'contacted': 'Da tiep nhan',
        'monitoring': 'Dang theo doi',
        'closed': 'Da dong ca'
    }

    if new_status not in allowed_statuses:
        flash('Trang thai xu ly khong hop le!', 'error')
        return redirect(url_for('health_support'))

    try:
        with open('health_questions.json', 'r', encoding='utf-8') as f:
            questions = json.load(f)
    except FileNotFoundError:
        questions = []

    question = next((q for q in questions if q['id'] == question_id), None)

    if not question:
        flash('Khong tim thay ca canh bao!', 'error')
        return redirect(url_for('health_support'))

    question['handling_status'] = new_status
    if new_status == 'closed':
        question['status'] = 'answered'

    with open('health_questions.json', 'w', encoding='utf-8') as f:
        json.dump(questions, f, ensure_ascii=False, indent=2)

    flash(f"Da cap nhat trang thai: {allowed_statuses[new_status]}", 'success')
    return redirect(url_for('health_support'))


#####


def generate_feedback(text):
    """Tạo feedback từ text bằng AI"""
    try:
        prompt = f"Đây là nội dung bài làm của học sinh:\n\n{text}\n\nHãy phân tích, chỉ ra lỗi sai và đề xuất cải thiện. Trả lời bằng tiếng Việt."
        response = model.generate_content([prompt])
        return response.text
    except Exception as e:
        return f"❌ Lỗi khi tạo feedback: {str(e)}"


def generate_score_feedback(text):
    """Tạo feedback chấm điểm từ text bằng AI"""
    try:
        prompt = f"""Dựa trên bài làm của học sinh sau:

{text}

Hãy chấm điểm theo các tiêu chí sau:
1. Nội dung đầy đủ (0–10)
2. Trình bày rõ ràng (0–10)
3. Kỹ thuật chính xác (0–10)
4. Thái độ học tập (0–10)

Sau đó, tổng kết điểm trung bình và đưa ra nhận xét ngắn gọn. Trả lời bằng tiếng Việt."""
        response = model.generate_content([prompt])
        return response.text
    except Exception as e:
        return f"❌ Lỗi khi chấm điểm: {str(e)}"


def extract_average_from_feedback(feedback: str):
    """
    Thử tìm số điểm trung bình trong chuỗi feedback của AI.
    Ví dụ: 'Tổng điểm trung bình: 8.5' -> 8.5
    Nếu không tìm thấy thì trả về None.
    """
    if not feedback:
        return None
    match = re.search(r'(\d+(\.\d+)?)', feedback)
    if match:
        try:
            return float(match.group(1))
        except:
            return None
    return None


###########


###
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/enter_nickname")
def enter_nickname():
    return render_template("nickname.html")


@app.route("/start_game", methods=["POST"])
def start_game():
    nickname = request.form["nickname"]
    bai = request.form["bai"]
    session["nickname"] = nickname
    session["bai"] = bai
    return redirect("/game")


@app.route("/game")
def game():
    if "nickname" not in session or "bai" not in session:
        return redirect("/enter_nickname")
    return render_template("game.html")


@app.route("/bridge_game")
def bridge_game():
    return render_template("bridge_game.html")


@app.route("/get_questions")
def get_questions():
    bai = session.get("bai", "bai_1")
    with open("questions.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    questions = data.get(bai, [])
    random.shuffle(questions)
    for q in questions:
        random.shuffle(q["options"])
    return jsonify(questions[:20])


@app.route("/submit_score", methods=["POST"])
def submit_score():
    nickname = session.get("nickname")
    bai = session.get("bai")
    score = request.json["score"]

    if not nickname:
        return jsonify({"status": "error", "message": "No nickname found"})
    if not bai:
        return jsonify({"status": "error", "message": "No bai found"})

    if not os.path.exists("scores.json"):
        with open("scores.json", "w", encoding="utf-8") as f:
            json.dump([], f)

    with open("scores.json", "r+", encoding="utf-8") as f:
        scores = json.load(f)
        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        existing = next((s for s in scores
                         if s["nickname"] == nickname and s.get("bai") == bai),
                        None)

        if existing:
            if score > existing["score"]:
                existing["score"] = score
                existing["time"] = now
        else:
            scores.append({
                "nickname": nickname,
                "score": score,
                "time": now,
                "bai": bai
            })

        filtered = [s for s in scores if s.get("bai") == bai]
        top50 = sorted(filtered, key=lambda x: x["score"], reverse=True)[:50]

        others = [s for s in scores if s.get("bai") != bai]
        final_scores = others + top50

        f.seek(0)
        json.dump(final_scores, f, ensure_ascii=False, indent=2)
        f.truncate()

    return jsonify({"status": "ok"})


@app.route("/leaderboard")
def leaderboard():
    bai = session.get("bai")

    if not bai:
        bai = "bai_1"

    if not os.path.exists("scores.json"):
        top5 = []
    else:
        with open("scores.json", "r", encoding="utf-8") as f:
            scores = json.load(f)

        filtered = [s for s in scores if s.get("bai") == bai]
        top5 = sorted(filtered, key=lambda x: x["score"], reverse=True)[:5]

    return render_template("leaderboard.html", players=top5, bai=bai)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/enter_nickname")


# Đường dẫn file dữ liệu
DATA_FOLDER = 'data'
EXAM_FILE = os.path.join(DATA_FOLDER, 'exam_data.json')
PROJECTS_FILE = os.path.join(DATA_FOLDER, 'projects.json')
PROJECT_IMAGES_FILE = os.path.join(DATA_FOLDER, 'project_images.json')
GENERAL_IMAGES_FILE = os.path.join(DATA_FOLDER, 'data.json')
GEOMETRY_STEM_FILE = os.path.join(DATA_FOLDER, 'geometry_stem_problems.json')
GEOMETRY_STEM_PROMPT_FILE = os.path.join(DATA_FOLDER, 'geometry_stem_critic_prompt.txt')


def load_exam(de_id):
    with open(EXAM_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data.get(de_id)


def load_projects():
    with open(PROJECTS_FILE, 'r', encoding='utf-8') as f:
        projects = json.load(f)

    if not any(p["id"] == "general" for p in projects):
        projects.append({
            "id": "general",
            "title": "Bài tập nhóm",
            "description": "Các nhóm làm bài và nộp tại đây."
        })

    return projects


def load_project_images():
    try:
        with open(PROJECT_IMAGES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}


def save_project_images(data):
    with open(PROJECT_IMAGES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_general_images():
    try:
        with open(GENERAL_IMAGES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []


def save_general_images(data):
    with open(GENERAL_IMAGES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_geometry_stem_problems():
    try:
        with open(GEOMETRY_STEM_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except:
        return []


def save_geometry_stem_problems(data):
    with open(GEOMETRY_STEM_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_geometry_stem_prompt():
    try:
        with open(GEOMETRY_STEM_PROMPT_FILE, 'r', encoding='utf-8') as f:
            return f.read()
    except:
        return (
            "Bạn là Tri-hand, chuyên gia phản biện đề bài Hình học STEM. "
            "Không giải bài, chỉ kiểm tra yếu tố Hình học, số liệu vật lý, "
            "dữ kiện còn thiếu và gợi ý học sinh tự chỉnh sửa."
        )


def parse_rating(value):
    score = float(value)
    if score < 0 or score > 10:
        raise ValueError("score out of range")
    return score


def update_geometry_stem_average(problem):
    ratings = problem.get('ratings', [])
    if not ratings:
        problem['average_score'] = None
        problem['average_breakdown'] = {}
        return

    keys = ['originality', 'application', 'clarity', 'integrity']
    problem['average_score'] = round(
        sum(r.get('average', 0) for r in ratings) / len(ratings), 2
    )
    problem['average_breakdown'] = {
        key: round(sum(r.get(key, 0) for r in ratings) / len(ratings), 2)
        for key in keys
    }


@app.route('/exam/<de_id>')
def exam(de_id):
    questions = load_exam(de_id)
    if not questions:
        return "Không tìm thấy đề thi."
    return render_template('exam.html', questions=questions, de_id=de_id)


@app.route('/projects')
def projects():
    project_list = load_projects()
    return render_template('projects.html', projects=project_list)


@app.route('/geometry_stem')
def geometry_stem():
    problems = load_geometry_stem_problems()
    problems = sorted(problems, key=lambda item: item.get('created_at', ''), reverse=True)
    return render_template(
        'geometry_stem.html',
        problems=problems,
        focus_id=request.args.get('focus', '')
    )


@app.route('/geometry_stem/review', methods=['POST'])
def geometry_stem_review():
    author = request.form.get('author', '').strip()
    title = request.form.get('title', '').strip()
    context = request.form.get('context', '').strip()
    geometry_element = request.form.get('geometry_element', '').strip()
    data_points = request.form.get('data_points', '').strip()
    problem_text = request.form.get('problem_text', '').strip()
    question = request.form.get('question', '').strip()

    if not author or not title or not context or not geometry_element or not problem_text:
        flash("Vui lòng nhập đủ tên, tiêu đề, bối cảnh, yếu tố Hình học và nội dung đề bài.")
        return redirect(url_for('geometry_stem'))

    critic_prompt = load_geometry_stem_prompt()
    full_prompt = f"""{critic_prompt}

THÔNG TIN ĐỀ BÀI HỌC SINH GỬI:
- Tác giả/nhóm: {author}
- Tiêu đề: {title}
- Bối cảnh thực tiễn: {context}
- Yếu tố Hình học dự kiến: {geometry_element}
- Số liệu/đơn vị đã có: {data_points}
- Nội dung đề bài nháp: {problem_text}
- Câu hỏi muốn cả lớp giải: {question}

Hãy phản biện theo đúng vai trò chuyên gia Hình học STEM. Không giải bài, không đưa đáp án cuối.
"""

    try:
        response = model.generate_content([full_prompt])
        ai_review = clean_ai_output(response.text)
    except Exception as e:
        ai_review = f"Lỗi AI phản biện: {sanitize_gemini_error(e)}"

    problems = load_geometry_stem_problems()
    problem_id = str(uuid.uuid4())
    problems.append({
        "id": problem_id,
        "author": author,
        "title": title,
        "context": context,
        "geometry_element": geometry_element,
        "data_points": data_points,
        "problem_text": problem_text,
        "question": question,
        "ai_review": ai_review,
        "status": "reviewed",
        "ratings": [],
        "average_score": None,
        "average_breakdown": {},
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    save_geometry_stem_problems(problems)
    flash("AI đã phản biện bản nháp. Nếu thấy ổn, em có thể đăng đề cho cả lớp.")
    return redirect(url_for('geometry_stem', focus=problem_id))


@app.route('/geometry_stem/<problem_id>/publish', methods=['POST'])
def geometry_stem_publish(problem_id):
    problems = load_geometry_stem_problems()
    for problem in problems:
        if problem.get('id') == problem_id:
            problem['status'] = 'published'
            problem['published_at'] = datetime.now().strftime("%Y-%m-%d %H:%M")
            save_geometry_stem_problems(problems)
            flash("Đã đăng đề bài cho cả lớp cùng giải và đánh giá.")
            return redirect(url_for('geometry_stem', focus=problem_id))

    flash("Không tìm thấy đề bài.")
    return redirect(url_for('geometry_stem'))


@app.route('/geometry_stem/<problem_id>/rate', methods=['POST'])
def geometry_stem_rate(problem_id):
    student_name = request.form.get('student_name', '').strip()
    comment_text = request.form.get('comment_text', '').strip()

    if not student_name or not comment_text:
        flash("Vui lòng nhập tên và nhận xét.")
        return redirect(url_for('geometry_stem', focus=problem_id))

    try:
        rating = {
            "student_name": student_name,
            "comment_text": comment_text,
            "originality": parse_rating(request.form.get('originality', '')),
            "application": parse_rating(request.form.get('application', '')),
            "clarity": parse_rating(request.form.get('clarity', '')),
            "integrity": parse_rating(request.form.get('integrity', '')),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    except:
        flash("Điểm đánh giá phải nằm trong khoảng 0 - 10.")
        return redirect(url_for('geometry_stem', focus=problem_id))

    rating['average'] = round((
        rating['originality'] + rating['application'] + rating['clarity'] + rating['integrity']
    ) / 4, 2)

    problems = load_geometry_stem_problems()
    for problem in problems:
        if problem.get('id') == problem_id:
            if problem.get('status') != 'published':
                flash("Đề bài này chưa được đăng cho lớp đánh giá.")
                return redirect(url_for('geometry_stem', focus=problem_id))
            problem.setdefault('ratings', []).append(rating)
            update_geometry_stem_average(problem)
            save_geometry_stem_problems(problems)
            flash("Đã ghi nhận đánh giá của em.")
            return redirect(url_for('geometry_stem', focus=problem_id))

    flash("Không tìm thấy đề bài.")
    return redirect(url_for('geometry_stem'))


@app.route('/submit/<de_id>', methods=['GET', 'POST'])
def submit(de_id):
    if request.method != 'POST':
        return redirect(url_for('exam', de_id=de_id))

    questions = load_exam(de_id)
    if not questions:
        return "Không tìm thấy đề thi."

    correct_count = 0
    total_questions = 0
    feedback = []
    results = []

    for i, q in enumerate(questions.get("multiple_choice", [])):
        user_answer = request.form.get(f"mc_{i}")
        correct = q["answer"]
        total_questions += 1
        if user_answer and user_answer.strip().lower() == correct.strip(
        ).lower():
            correct_count += 1
            results.append({"status": "Đúng", "note": ""})
        else:
            msg = f"Câu {i+1} sai. Đáp án đúng là: {correct}"
            results.append({"status": "Sai", "note": msg})
            feedback.append(msg)

    for i, tf in enumerate(questions.get("true_false", [])):
        for j, correct_tf in enumerate(tf["answers"]):
            user_tf_raw = request.form.get(f"tf_{i}_{j}", "").lower()
            user_tf = user_tf_raw == "true"
            total_questions += 1
            if user_tf == correct_tf:
                correct_count += 1
                results.append({"status": "Đúng", "note": ""})
            else:
                msg = f"Câu {i+1+len(questions['multiple_choice'])}, ý {j+1} sai."
                results.append({"status": "Sai", "note": msg})
                feedback.append(msg)

    detailed_errors = "\n".join(feedback)

    prompt = f"""Học sinh làm đúng {correct_count} / {total_questions} câu.

Danh sách lỗi:
{detailed_errors}

Bạn là giáo viên Toán. Hãy:
1. Nhận xét tổng thể về kết quả (giọng văn tích cực, khích lệ)
2. Phân tích từng lỗi sai: giải thích lý do sai, kiến thức liên quan, cách sửa
3. Đề xuất ít nhất 3 dạng bài tập cụ thể để luyện tập
4. Chấm điểm trên thang 10

QUY TẮC TRÌNH BÀY:
- Công thức toán dùng LaTeX:
  + Inline (trong dòng): $x^2 + 3x + 2$
  + Hiển thị riêng: $$\\sqrt{{x-3}} \\geq 0$$
- Các ký hiệu LaTeX:
  + Căn: \\sqrt{{x}}
  + Phân số: \\frac{{a}}{{b}}
  + Lớn hơn/bằng: \\geq
  + Nhỏ hơn/bằng: \\leq
  + Nhân: \\times
  + Pi: \\pi
- KHÔNG dùng **, ##, ###, ```
- Xuống dòng rõ ràng giữa các ý
- Dùng 1. 2. 3. hoặc dấu gạch đầu dòng -

VÍ DỤ TRÌNH BÀY ĐÚNG:

Câu 3 sai. Đáp án đúng: $x \\geq 3$

Giải thích: Căn thức $\\sqrt{{x-3}}$ xác định khi biểu thức trong căn không âm, tức là:
$$x - 3 \\geq 0$$
$$x \\geq 3$$

Câu 4 sai. Đáp án đúng: $\\frac{{3}}{{2}}$

Phương trình $2x^2 - 3x - 5 = 0$ có:
- $\\Delta = b^2 - 4ac = 9 + 40 = 49$
- Tổng 2 nghiệm: $x_1 + x_2 = -\\frac{{b}}{{a}} = \\frac{{3}}{{2}}$

Trả lời bằng tiếng Việt, thân thiện."""

    try:
        response = model.generate_content([prompt])
        # KHÔNG dùng clean_ai_output vì cần giữ nguyên LaTeX
        ai_feedback = response.text
    except Exception as e:
        ai_feedback = f"❌ Lỗi: {str(e)}"

    return render_template('result.html',
                           score=correct_count,
                           feedback=feedback,
                           ai_feedback=ai_feedback,
                           total_questions=total_questions,
                           results=results)


@app.route('/project/<project_id>', methods=['GET', 'POST'])
def project(project_id):
    projects = load_projects()
    project_info = next((p for p in projects if p["id"] == project_id), None)
    if not project_info:
        return "Không tìm thấy đề bài."

    all_images = load_project_images()
    images = all_images.get(project_id, [])
    ai_feedback = None

    if request.method == 'POST':
        image = request.files.get('image')
        group_name = request.form.get('group_name')
        note = request.form.get('note', '').strip()

        if not image or image.filename == '' or not group_name:
            return render_template('project.html',
                                   project=project_info,
                                   images=images,
                                   feedback="❌ Thiếu ảnh hoặc tên nhóm.")

        image_id = str(uuid.uuid4())
        filename = f"{image_id}_{image.filename}"
        image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        image.save(image_path)

        try:
            img = Image.open(image_path)
            prompt = (
                f"Đây là ảnh bài làm của học sinh. "
                f"Hãy phân tích nội dung, chỉ ra lỗi sai nếu có, và đề xuất cải thiện, chấm bài làm trên thang 10."
            )
            response = model.generate_content([img, prompt])
            ai_feedback = response.text
        except Exception as e:
            ai_feedback = f"❌ Lỗi khi xử lý ảnh: {str(e)}"

        new_image = {
            "id": image_id,
            "filename": filename,
            "group_name": group_name,
            "note": note,
            "ai_feedback": ai_feedback,
            "comments": []
        }
        images.append(new_image)
        all_images[project_id] = images
        save_project_images(all_images)

    return render_template('project.html',
                           project=project_info,
                           images=images,
                           feedback=ai_feedback)


@app.route('/comment/<project_id>/<image_id>', methods=['POST'])
def comment(project_id, image_id):
    student_name = request.form.get('student_name', '').strip()
    comment_text = request.form.get('comment_text', '').strip()
    score = request.form.get('score', '').strip()

    if not student_name or not comment_text or not score:
        flash("Vui lòng nhập đầy đủ tên, bình luận và điểm số.")
        return redirect(url_for('project', project_id=project_id))

    try:
        score = float(score)
        if score < 0 or score > 10:
            flash("Điểm phải nằm trong khoảng 0 - 10.")
            return redirect(url_for('project', project_id=project_id))
    except ValueError:
        flash("Điểm phải là số hợp lệ.")
        return redirect(url_for('project', project_id=project_id))

    all_images = load_project_images()
    images = all_images.get(project_id)

    if images is None:
        flash("Đề bài không tồn tại.")
        return redirect(url_for('home'))

    target_image = next((img for img in images if img.get("id") == image_id),
                        None)

    if target_image is None:
        flash("Không tìm thấy ảnh để bình luận.")
        return redirect(url_for('project', project_id=project_id))

    for c in target_image.get("comments", []):
        if (c["student_name"] == student_name
                and c["comment_text"] == comment_text
                and c.get("score") == score):
            flash("Bình luận đã tồn tại.")
            return redirect(url_for('project', project_id=project_id))

    target_image.setdefault("comments", []).append({
        "student_name": student_name,
        "comment_text": comment_text,
        "score": score
    })

    scores = [
        c["score"] for c in target_image.get("comments", []) if "score" in c
    ]
    avg_score = round(sum(scores) / len(scores), 2) if scores else 0
    target_image["average_score"] = avg_score

    all_images[project_id] = images
    save_project_images(all_images)

    flash(f"Bình luận đã được thêm. Điểm trung bình hiện tại: {avg_score}")
    return redirect(url_for('project', project_id=project_id))


@app.route('/upload_image', methods=['GET', 'POST'])
def upload_image():
    ai_feedback = None
    score_feedback = None
    all_images = load_project_images()
    images = all_images.get("general", [])

    if request.method == 'POST':
        uploaded_file = request.files.get('image')
        group_name = request.form.get('group_name')

        if not uploaded_file or uploaded_file.filename == '' or not group_name:
            return render_template('upload_image.html',
                                   feedback="❌ Thiếu file hoặc tên nhóm.",
                                   images=images)

        if not allowed_file(uploaded_file.filename):
            return render_template(
                'upload_image.html',
                feedback="❌ File không hợp lệ. Chỉ chấp nhận ảnh hoặc PDF.",
                images=images)

        file_ext = uploaded_file.filename.rsplit('.', 1)[1].lower()
        file_id = str(uuid.uuid4())
        filename = f"{file_id}_{uploaded_file.filename}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        uploaded_file.save(file_path)

        try:
            if file_ext == 'pdf':
                text = extract_text_from_pdf(file_path)
                if not text.strip():
                    ai_feedback = "❌ Không tìm thấy nội dung trong file PDF."
                    score_feedback = ""
                else:
                    ai_feedback = generate_feedback(text)
                    score_feedback = generate_score_feedback(text)

            elif file_ext in ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp']:
                img = Image.open(file_path)

                # ===== PROMPT CẢI THIỆN CHO PHẢN HỒI AI =====
                ai_response = model.generate_content([
                    img,
                    """Bạn là giáo viên đang chấm bài học sinh. Hãy phân tích bài làm trong ảnh và đưa ra nhận xét chi tiết.

NHIỆM VỤ:
1. Mô tả ngắn gọn nội dung bài làm
2. Chỉ ra các điểm làm đúng (nếu có)
3. Chỉ ra các lỗi sai cụ thể (nếu có)
4. Đề xuất cách cải thiện

QUY TẮC TRÌNH BÀY QUAN TRỌNG:
• TUYỆT ĐỐI KHÔNG dùng: **, ***, ##, ###, ````
• Công thức toán viết văn bản thường, ví dụ: (3x + 6)/(4x - 8) hoặc x^2 + 2x + 1
• Mỗi ý PHẢI xuống dòng rõ ràng
• Dùng dấu đầu dòng đơn giản: - hoặc số thứ tự 1. 2. 3.
• Không viết quá dài, mỗi đoạn tối đa 3-4 dòng

VÍ DỤ TRÌNH BÀY ĐÚNG:

Nội dung bài làm:
Học sinh đã giải phương trình (x + 2)(x - 3) = 0

Điểm tốt:
- Nhận diện đúng dạng phương trình tích
- Áp dụng đúng quy tắc tích bằng 0

Lỗi sai:
- Bước 2: Viết x + 2 = 0 hoặc x - 3 = 0 (thiếu chữ "hoặc")
- Kết luận thiếu tập nghiệm S = {-2; 3}

Đề xuất cải thiện:
Cần ghi rõ "hoặc" khi tách nhân tử. Luôn viết tập nghiệm ở cuối.

Trả lời bằng tiếng Việt, ngắn gọn, dễ hiểu."""
                ])
                ai_feedback = clean_ai_output(ai_response.text)

                # ===== PROMPT CẢI THIỆN CHO CHẤM ĐIỂM =====
                score_response = model.generate_content([
                    img,
                    """Hãy chấm điểm bài làm của học sinh theo 4 tiêu chí sau:

TIÊU CHÍ CHẤM ĐIỂM:
1. Nội dung (0-10): Độ đầy đủ, đúng đắn của bài làm
2. Trình bày (0-10): Sạch sẽ, rõ ràng, dễ đọc
3. Phương pháp (0-10): Cách giải, logic tư duy
4. Kết quả (0-10): Đáp án cuối cùng có chính xác không

QUY TẮC TRÌNH BÀY:
• KHÔNG dùng **, ***, ##, ###, ````
• Mỗi tiêu chí ghi trên 1 dòng riêng
• Format: Tên tiêu chí: X/10 - Lý do ngắn gọn
• Cuối cùng ghi điểm trung bình và nhận xét chung

VÍ DỤ TRÌNH BÀY ĐÚNG:

Nội dung: 8/10 - Làm đầy đủ các bước, có một chỗ thiếu
Trình bày: 7/10 - Khá rõ ràng nhưng chữ hơi nhỏ
Phương pháp: 9/10 - Áp dụng đúng công thức và logic tốt
Kết quả: 6/10 - Đáp án sai do nhầm dấu ở bước cuối

Điểm trung bình: 7.5/10

Nhận xét chung:
Bài làm khá tốt, phương pháp đúng. Cần cẩn thận hơn ở bước tính toán cuối cùng để tránh sai số.

Trả lời bằng tiếng Việt."""
                ])
                score_feedback = clean_ai_output(score_response.text)

            else:
                ai_feedback = "❌ Định dạng file không hỗ trợ."
                score_feedback = ""

        except Exception as e:
            ai_feedback = f"❌ Lỗi khi xử lý file: {str(e)}"
            score_feedback = ""

        ai_score = extract_average_from_feedback(score_feedback)

        new_image = {
            "id": file_id,
            "filename": filename,
            "group_name": group_name,
            "file_type": file_ext,
            "ai_feedback": ai_feedback,
            "score_feedback": score_feedback,
            "comments": [],
            "scores": [],
            "average_score": None
        }

        if ai_score is not None:
            new_image["scores"].append(ai_score)
            new_image["average_score"] = ai_score

        images.append(new_image)

        all_images["general"] = images
        save_project_images(all_images)

    for img in images:
        if "scores" in img and img["scores"]:
            avg = sum(img["scores"]) / len(img["scores"])
            img["average_score"] = round(avg, 2)
        else:
            img["average_score"] = None

    return render_template('upload_image.html',
                           feedback=ai_feedback,
                           score=score_feedback,
                           images=images)


# ===== HÀM HỖ TRỢ LÀM SẠCH OUTPUT CỦA AI =====
def clean_ai_output(text):
    """
    Làm sạch output của AI để hiển thị đẹp hơn
    """
    import re

    # Loại bỏ các dấu markdown không mong muốn
    text = re.sub(r'\*\*\*', '', text)  # Loại bỏ ***
    text = re.sub(r'\*\*', '', text)  # Loại bỏ **
    text = re.sub(r'#{1,6}\s', '', text)  # Loại bỏ ##, ###

    # Loại bỏ code blocks
    text = re.sub(r'```[a-z]*\n', '', text)
    text = re.sub(r'```', '', text)

    # Chuẩn hóa xuống dòng (loại bỏ xuống dòng thừa)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Loại bỏ khoảng trắng thừa đầu/cuối dòng
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)

    return text.strip()

####
@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint đơn giản để ping service giữ server thức"""
    return jsonify({
        "status": "ok",
        "message": "Server is running",
        "timestamp": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    }), 200

# Hoặc đơn giản hơn:
@app.route('/ping')
def ping():
    return "pong", 200
###
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
