import os
import hashlib
import secrets
import pyotp
from functools import wraps
from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, Response
)
import db

import subprocess

def _get_version():
    """版本号规则: vYYYYMMDD-N (UTC+8)"""
    from datetime import datetime, timezone, timedelta
    tz8 = timezone(timedelta(hours=8))
    date_str = datetime.now(tz8).strftime('%Y%m%d')
    return f'v{date_str}-1'

app = Flask(__name__, static_folder='static')
APP_VERSION = os.environ.get('APP_VERSION') or _get_version()
def _load_secret_key():
    """持久化 secret_key 到 /data，避免重启后 session 失效"""
    key_file = os.path.join(DATA_DIR, '.flask_secret_key')
    try:
        with open(key_file, 'r') as f:
            return f.read().strip()
    except (OSError, IOError):
        pass
    # 首次生成
    key = secrets.token_hex(32)
    try:
        with open(key_file, 'w') as f:
            f.write(key)
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


DATA_DIR = os.environ.get('MIRROR_DATA_DIR', '/data')

app.secret_key = _load_secret_key()
_env_admin_pw = os.environ.get('MIRROR_ADMIN_PW', '')


# ── 初始化 ──

def ensure_init():
    db.init_db()
    pw = db.get_config('admin_password', '')
    if not pw and _env_admin_pw:
        db.set_config('admin_password', hashlib.sha256(_env_admin_pw.encode()).hexdigest())
    if not pw and not _env_admin_pw:
        random_pw = secrets.token_urlsafe(8)
        db.set_config('admin_password', hashlib.sha256(random_pw.encode()).hexdigest())
        pw_file = os.path.join(DATA_DIR, '.initial_password')
        with open(pw_file, 'w') as f:
            f.write(random_pw)
        os.chmod(pw_file, 0o600)


ensure_init()


# ── 鉴权 ──

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            auth = request.headers.get('Authorization', '')
            if auth.startswith('Bearer '):
                token = auth[7:]
                stored = db.get_config('admin_password', '')
                if stored and hashlib.sha256(token.encode()).hexdigest() == stored:
                    session['logged_in'] = True
                else:
                    return jsonify({'error': 'unauthorized'}), 401
            else:
                return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ── 鉴权端点 (给 nginx auth_request 用) ──

import re

def _parse_pull_uri(uri):
    """从 /v2/<image>/manifests/<tag> 或 /v2/<image>/blobs/<digest> 解析拉取信息"""
    m = re.match(r'^/v2/(.+)/(manifests|blobs)/(.+)$', uri)
    if not m:
        return None, None, None, False
    image_name, kind, ref = m.group(1), m.group(2), m.group(3)
    # tag 可能是 tag name 也可能是 sha256:digest
    is_manifest = kind == 'manifests'
    is_direct_manifest = is_manifest and not ref.startswith('sha256:')
    return image_name, ref, is_direct_manifest, is_manifest


def _detect_upstream(uri):
    """从 URI 前缀判断上游"""
    if uri.startswith('/ghcr/'):
        return 'ghcr'
    elif uri.startswith('/gcr/'):
        return 'gcr'
    return 'hub'


def _check_cache_exists(upstream, image_name, ref):
    """检查 registry 存储中是否已缓存该镜像 tag 或 manifest"""
    repo_dir = os.path.join(DATA_DIR, 'registry', upstream, 'docker', 'registry', 'v2', 'repositories', image_name)
    # 如果是 tag name
    tag_link = os.path.join(repo_dir, '_manifests', 'tags', ref, 'current', 'link')
    if os.path.exists(tag_link):
        return True
    # 如果是 digest，检查 revisions 目录
    revisions_dir = os.path.join(repo_dir, '_manifests', 'revisions', 'sha256')
    if os.path.exists(revisions_dir):
        digest_hash = ref.split(':')[-1] if ':' in ref else ref
        for h in os.listdir(revisions_dir):
            if h == digest_hash:
                link_path = os.path.join(revisions_dir, h, 'link')
                if os.path.exists(link_path):
                    return True
    # fallback: 只要这个 image 目录存在就算命中
    return os.path.exists(os.path.join(repo_dir, '_manifests'))


@app.route('/api/auth/check', methods=['GET', 'POST'])
def auth_check():
    # 解析拉取信息并记录日志
    original_uri = request.headers.get('X-Original-URI', '')
    client_ip = request.headers.get('X-Real-IP', '')
    if not client_ip:
        client_ip = request.remote_addr

    if original_uri:
        image_name, ref, is_direct_manifest, is_manifest = _parse_pull_uri(original_uri)
        if image_name and is_manifest:
            upstream = _detect_upstream(original_uri)
            cache_hit = _check_cache_exists(upstream, image_name, ref)
            # 只对 tag 拉取记录日志（digest 拉取是 docker 内部行为）
            if is_direct_manifest:
                db.log_pull(upstream, image_name, ref, cache_hit, client_ip)
            # 如果是首次拉取（新镜像），触发一次缓存扫描更新 DB
            if not cache_hit:
                try:
                    import threading
                    from cache_manager import scan_cache
                    threading.Thread(target=scan_cache, daemon=True).start()
                except Exception:
                    pass

    # 白名单检查
    if not db.whitelist_enabled():
        return '', 200
    if db.is_ip_whitelisted(client_ip):
        return '', 200

    # 被白名单拒绝，记录日志
    if original_uri and is_manifest and is_direct_manifest:
        db.log_pull(upstream, image_name, ref, False, client_ip,
                    status='denied', reason='IP 不在白名单中')
    elif original_uri and is_manifest and not is_direct_manifest:
        db.log_pull(upstream, image_name, ref, False, client_ip,
                    status='denied', reason='IP 不在白名单中')

    return '', 403


# ── 页面路由 ──

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    client_ip = request.headers.get('X-Real-IP', '') or request.remote_addr

    # 暴力破解检查
    if db.check_login_rate(client_ip):
        remaining = db.get_login_lock_remaining(client_ip)
        error = f'登录失败次数过多，请 {remaining} 秒后再试'
        return render_template('login.html', error=error, twofa_enabled=db.get_config('totp_secret') != '')

    if request.method == 'POST':
        password = request.form.get('password', '')
        totp_code = request.form.get('totp_code', '').strip()
        stored = db.get_config('admin_password', '')

        # 验证密码
        if not stored or hashlib.sha256(password.encode()).hexdigest() != stored:
            db.log_login_attempt(client_ip, 'admin', False)
            error = '密码错误'
            return render_template('login.html', error=error, twofa_enabled=db.get_config('totp_secret') != '')

        # 验证 2FA（如果已启用）
        totp_secret = db.get_config('totp_secret', '')
        if totp_secret:
            if not totp_code:
                # 密码正确但需要 2FA
                session['pending_2fa'] = True
                db.log_login_attempt(client_ip, 'admin', True)
                return render_template('login.html', error='', twofa_enabled=True, need_2fa=True)
            if not pyotp.TOTP(totp_secret).verify(totp_code, valid_window=1):
                db.log_login_attempt(client_ip, 'admin', False)
                error = '2FA 验证码错误'
                return render_template('login.html', error=error, twofa_enabled=True, need_2fa=True)

        # 登录成功
        db.clear_login_attempts(client_ip)
        session['logged_in'] = True
        session.pop('pending_2fa', None)
        return redirect(url_for('dashboard'))

    return render_template('login.html', error=error, twofa_enabled=db.get_config('totp_secret') != '')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    stats = db.get_stats()
    disk_total, disk_used = get_disk_info()
    cache_sizes = get_cache_dir_sizes()
    return render_template('dashboard.html', stats=stats, version=APP_VERSION,
                           disk_total=disk_total, disk_used=disk_used,
                           cache_sizes=cache_sizes, whitelist_enabled=db.whitelist_enabled())


@app.route('/cache')
@login_required
def cache_list():
    upstream = request.args.get('upstream', '')
    search = request.args.get('search', '')
    images = db.get_cached_images(upstream=upstream or None, search=search or '')
    return render_template('cache_list.html', images=images, version=APP_VERSION,
                           upstream=upstream, search=search)


@app.route('/logs')
@login_required
def logs():
    pull_logs = db.get_pull_logs(100)
    return render_template('logs.html', logs=pull_logs, version=APP_VERSION)


@app.route('/whitelist')
@login_required
def whitelist_page():
    entries = db.get_whitelist()
    enabled = db.whitelist_enabled()
    return render_template('whitelist.html', entries=entries, version=APP_VERSION, enabled=enabled)


@app.route('/settings')
@login_required
def settings():
    config = {
        'max_disk_pct': db.get_config('max_disk_pct', '80'),
        'target_disk_pct': db.get_config('target_disk_pct', '70'),
        'cleanup_interval': db.get_config('cleanup_interval', '300'),
        'whitelist_enabled': db.get_config('whitelist_enabled', '0'),
    }
    upstreams = db.get_upstreams()
    twofa_enabled = bool(db.get_config('totp_secret', ''))
    return render_template('settings.html', config=config, version=APP_VERSION, upstreams=upstreams, twofa_enabled=twofa_enabled)


# ── API ──

@app.route('/api/whitelist/toggle', methods=['POST'])
@login_required
def whitelist_toggle():
    enabled = request.json.get('enabled', False)
    db.set_config('whitelist_enabled', '1' if enabled else '0')
    return jsonify({'ok': True, 'enabled': enabled})


@app.route('/api/whitelist/add', methods=['POST'])
@login_required
def whitelist_add():
    data = request.json
    ip = data.get('ip', '').strip()
    label = data.get('label', '').strip()
    if not ip:
        return jsonify({'error': 'IP 不能为空'}), 400
    ok = db.add_whitelist(ip, label)
    if not ok:
        return jsonify({'error': '已存在'}), 409
    return jsonify({'ok': True})


@app.route('/api/whitelist/remove', methods=['POST'])
@login_required
def whitelist_remove():
    ip = request.json.get('ip', '')
    db.remove_whitelist(ip)
    return jsonify({'ok': True})


@app.route('/api/whitelist/import', methods=['POST'])
@login_required
def whitelist_import():
    text = request.json.get('text', '')
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    added = 0
    skipped = 0
    for line in lines:
        parts = line.split('#', 1)
        ip = parts[0].strip()
        label = parts[1].strip() if len(parts) > 1 else ''
        if db.add_whitelist(ip, label):
            added += 1
        else:
            skipped += 1
    return jsonify({'ok': True, 'added': added, 'skipped': skipped})


@app.route('/api/settings', methods=['POST'])
@login_required
def update_settings():
    data = request.json
    for key in ['max_disk_pct', 'target_disk_pct', 'cleanup_interval']:
        if key in data:
            db.set_config(key, str(data[key]))
    return jsonify({'ok': True})


@app.route('/api/cache/delete', methods=['POST'])
@login_required
def cache_delete():
    image_id = request.json.get('id')
    db.delete_cached_image(image_id)
    return jsonify({'ok': True})


@app.route('/api/2fa/setup', methods=['POST'])
@login_required
def setup_2fa():
    """生成 2FA 密钥和二维码 URI"""
    secret = pyotp.random_base32()
    db.set_config('totp_secret', secret)
    uri = pyotp.TOTP(secret).provisioning_uri(name='admin', issuer_name='Mirror Relay')
    return jsonify({'ok': True, 'secret': secret, 'uri': uri})


@app.route('/api/2fa/verify', methods=['POST'])
@login_required
def verify_2fa():
    """验证 2FA 验证码"""
    code = request.json.get('code', '').strip()
    secret = db.get_config('totp_secret', '')
    if not secret:
        return jsonify({'error': '2FA 未设置'}), 400
    if pyotp.TOTP(secret).verify(code, valid_window=1):
        return jsonify({'ok': True})
    return jsonify({'error': '验证码错误'}), 400


@app.route('/api/2fa/disable', methods=['POST'])
@login_required
def disable_2fa():
    """关闭 2FA"""
    # 需要验证当前 2FA 码才能关闭
    code = request.json.get('code', '').strip()
    secret = db.get_config('totp_secret', '')
    if not secret:
        return jsonify({'error': '2FA 未设置'}), 400
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        return jsonify({'error': '验证码错误'}), 403
    db.set_config('totp_secret', '')
    return jsonify({'ok': True})


@app.route('/api/password/change', methods=['POST'])
@login_required
def change_password():
    old_pw = request.json.get('old_password', '')
    new_pw = request.json.get('new_password', '')
    if len(new_pw) < 8:
        return jsonify({'error': '新密码至少 8 位'}), 400
    stored = db.get_config('admin_password', '')
    if hashlib.sha256(old_pw.encode()).hexdigest() != stored:
        return jsonify({'error': '旧密码错误'}), 403
    db.set_config('admin_password', hashlib.sha256(new_pw.encode()).hexdigest())
    return jsonify({'ok': True})


@app.route('/api/gc/trigger', methods=['POST'])
@login_required
def trigger_gc():
    import subprocess
    for upstream in ['hub', 'ghcr', 'gcr']:
        config_file = os.path.join(DATA_DIR, 'registry-config', f'{upstream}.yml')
        if os.path.exists(config_file):
            try:
                subprocess.Popen(
                    ['docker-registry', 'garbage-collect', '--delete-untagged', config_file],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except FileNotFoundError:
                pass
    return jsonify({'ok': True, 'message': 'GC 已触发'})


@app.route('/api/upstream/add', methods=['POST'])
@login_required
def upstream_add():
    data = request.json
    name = data.get('name', '').strip()
    url = data.get('url', '').strip()
    prefix = data.get('prefix', '').strip()
    if not name or not url:
        return jsonify({'error': '名称和 URL 不能为空'}), 400
    try:
        db.add_upstream(name, url, prefix)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 409


@app.route('/api/upstream/remove', methods=['POST'])
@login_required
def upstream_remove():
    name = request.json.get('name', '')
    db.remove_upstream(name)
    return jsonify({'ok': True})


@app.route('/health')
def health():
    return jsonify({'ok': True})


# ── 辅助函数 ──

def get_disk_info():
    try:
        stat = os.statvfs(DATA_DIR)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        return total, used
    except OSError:
        return 0, 0


def get_cache_dir_sizes():
    sizes = {}
    registry_dir = os.path.join(DATA_DIR, 'registry')
    for upstream in ['hub', 'ghcr', 'gcr']:
        p = os.path.join(registry_dir, upstream)
        if os.path.exists(p):
            total = 0
            for dirpath, _, filenames in os.walk(p):
                for f in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
            sizes[upstream] = total
        else:
            sizes[upstream] = 0
    return sizes


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
