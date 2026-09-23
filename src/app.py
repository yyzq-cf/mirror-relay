import os
import hashlib
import secrets
from functools import wraps
from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, Response
)
import db

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

DATA_DIR = os.environ.get('MIRROR_DATA_DIR', '/data')
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

@app.route('/api/auth/check', methods=['GET', 'POST'])
def auth_check():
    if not db.whitelist_enabled():
        return '', 200
    client_ip = request.headers.get('X-Real-IP', '')
    if not client_ip:
        client_ip = request.remote_addr
    if db.is_ip_whitelisted(client_ip):
        return '', 200
    return '', 403


# ── 页面路由 ──

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        password = request.form.get('password', '')
        stored = db.get_config('admin_password', '')
        if stored and hashlib.sha256(password.encode()).hexdigest() == stored:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        error = '密码错误'
    return render_template('login.html', error=error)


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
    return render_template('dashboard.html', stats=stats,
                           disk_total=disk_total, disk_used=disk_used,
                           cache_sizes=cache_sizes)


@app.route('/cache')
@login_required
def cache_list():
    upstream = request.args.get('upstream', '')
    search = request.args.get('search', '')
    images = db.get_cached_images(upstream=upstream or None, search=search or '')
    return render_template('cache_list.html', images=images,
                           upstream=upstream, search=search)


@app.route('/logs')
@login_required
def logs():
    pull_logs = db.get_pull_logs(100)
    return render_template('logs.html', logs=pull_logs)


@app.route('/whitelist')
@login_required
def whitelist_page():
    entries = db.get_whitelist()
    enabled = db.whitelist_enabled()
    return render_template('whitelist.html', entries=entries, enabled=enabled)


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
    return render_template('settings.html', config=config, upstreams=upstreams)


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
