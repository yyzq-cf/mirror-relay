import sqlite3
import os
import ipaddress
from datetime import datetime, timezone

DB_PATH = os.environ.get('MIRROR_DB_PATH', '/data/mirror.db')


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    """首次初始化数据库表 + 默认配置"""
    conn = get_conn()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS cached_images (
            id INTEGER PRIMARY KEY,
            upstream TEXT NOT NULL,
            image_name TEXT NOT NULL,
            tag TEXT,
            digest TEXT,
            size_bytes INTEGER DEFAULT 0,
            first_pulled_at DATETIME,
            last_pulled_at DATETIME,
            pull_count INTEGER DEFAULT 1,
            UNIQUE(upstream, image_name, tag)
        );

        CREATE TABLE IF NOT EXISTS pull_logs (
            id INTEGER PRIMARY KEY,
            upstream TEXT,
            image_name TEXT,
            tag TEXT,
            cache_hit INTEGER DEFAULT 0,
            client_ip TEXT,
            pulled_at DATETIME,
            status TEXT DEFAULT 'allowed',
            reason TEXT
        );

        CREATE TABLE IF NOT EXISTS whitelist (
            id INTEGER PRIMARY KEY,
            ip_or_cidr TEXT NOT NULL UNIQUE,
            label TEXT,
            created_at DATETIME
        );

        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS upstreams (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            prefix TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at DATETIME
        );
    ''')

    # 默认配置
    defaults = {
        'whitelist_enabled': '0',
        'max_disk_pct': '80',
        'target_disk_pct': '70',
        'cleanup_interval': '300',
        'admin_password': '',
    }
    for k, v in defaults.items():
        conn.execute(
            'INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)', (k, v)
        )

    # 默认上游
    now = datetime.now(timezone.utc).isoformat()
    default_upstreams = [
        ('hub', 'https://registry-1.docker.io', '', 1),
        ('ghcr', 'https://ghcr.io', 'ghcr', 1),
        ('gcr', 'https://gcr.io', 'gcr', 1),
    ]
    for name, url, prefix, enabled in default_upstreams:
        conn.execute(
            'INSERT OR IGNORE INTO upstreams(name, url, prefix, enabled, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (name, url, prefix, enabled, now)
        )

    # 迁移：给旧的 pull_logs 表补字段
    try:
        conn.execute('ALTER TABLE pull_logs ADD COLUMN status TEXT DEFAULT "allowed"')
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute('ALTER TABLE pull_logs ADD COLUMN reason TEXT')
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


# ── 配置 ──

def get_config(key, default=''):
    conn = get_conn()
    row = conn.execute('SELECT value FROM config WHERE key = ?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_config(key, value):
    conn = get_conn()
    conn.execute(
        'INSERT INTO config(key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = ?',
        (key, value, value)
    )
    conn.commit()
    conn.close()


# ── 白名单 ──

def get_whitelist():
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM whitelist ORDER BY created_at DESC'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_whitelist(ip_or_cidr, label=''):
    conn = get_conn()
    try:
        conn.execute(
            'INSERT INTO whitelist(ip_or_cidr, label, created_at) VALUES (?, ?, ?)',
            (ip_or_cidr, label, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_whitelist(ip_or_cidr):
    conn = get_conn()
    conn.execute('DELETE FROM whitelist WHERE ip_or_cidr = ?', (ip_or_cidr,))
    conn.commit()
    conn.close()


def is_ip_whitelisted(client_ip):
    """检查 IP 是否在白名单中（支持 CIDR）"""
    entries = get_whitelist()
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    for entry in entries:
        cidr = entry['ip_or_cidr']
        try:
            if '/' in cidr:
                network = ipaddress.ip_network(cidr, strict=False)
                if addr in network:
                    return True
            else:
                if addr == ipaddress.ip_address(cidr):
                    return True
        except ValueError:
            continue
    return False


def whitelist_enabled():
    return get_config('whitelist_enabled') == '1'


# ── 拉取日志 ──

def log_pull(upstream, image_name, tag, cache_hit, client_ip, status='allowed', reason=None):
    conn = get_conn()
    conn.execute(
        'INSERT INTO pull_logs(upstream, image_name, tag, cache_hit, client_ip, pulled_at, status, reason) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (upstream, image_name, tag, 1 if cache_hit else 0, client_ip,
         datetime.now(timezone.utc).isoformat(), status, reason)
    )
    conn.commit()
    conn.close()


def get_pull_logs(limit=100):
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM pull_logs ORDER BY pulled_at DESC LIMIT ?', (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    conn = get_conn()
    total = conn.execute('SELECT COUNT(*) as c FROM pull_logs').fetchone()['c']
    hits = conn.execute(
        'SELECT COUNT(*) as c FROM pull_logs WHERE cache_hit = 1'
    ).fetchone()['c']
    cached = conn.execute(
        'SELECT COUNT(*) as c FROM cached_images'
    ).fetchone()['c']
    conn.close()
    hit_rate = round(hits / total * 100, 1) if total > 0 else 0
    return {
        'total_pulls': total,
        'cache_hits': hits,
        'cache_misses': total - hits,
        'cached_images': cached,
        'hit_rate': hit_rate,
    }


# ── 缓存镜像 ──

def upsert_cached_image(upstream, image_name, tag, digest='', size_bytes=0):
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('''
        INSERT INTO cached_images(upstream, image_name, tag, digest, size_bytes,
                                  first_pulled_at, last_pulled_at, pull_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(upstream, image_name, tag) DO UPDATE SET
            last_pulled_at = ?,
            pull_count = pull_count + 1,
            size_bytes = CASE WHEN excluded.size_bytes > 0
                              THEN excluded.size_bytes ELSE cached_images.size_bytes END
    ''', (upstream, image_name, tag, digest, size_bytes, now, now, now))
    conn.commit()
    conn.close()


def get_cached_images(upstream=None, search=''):
    conn = get_conn()
    sql = 'SELECT * FROM cached_images WHERE 1=1'
    params = []
    if upstream:
        sql += ' AND upstream = ?'
        params.append(upstream)
    if search:
        sql += ' AND image_name LIKE ?'
        params.append(f'%{search}%')
    sql += ' ORDER BY last_pulled_at DESC'
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_cached_image(image_id):
    conn = get_conn()
    conn.execute('DELETE FROM cached_images WHERE id = ?', (image_id,))
    conn.commit()
    conn.close()


# ── 上游管理 ──

def get_upstreams():
    conn = get_conn()
    rows = conn.execute('SELECT * FROM upstreams ORDER BY id').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_upstream(name, url, prefix):
    conn = get_conn()
    conn.execute(
        'INSERT INTO upstreams(name, url, prefix, enabled, created_at) VALUES (?, ?, ?, 1, ?)',
        (name, url, prefix, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def remove_upstream(name):
    conn = get_conn()
    conn.execute('DELETE FROM upstreams WHERE name = ?', (name,))
    conn.commit()
    conn.close()
