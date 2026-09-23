import os
import time
import shutil
import logging
import subprocess
from apscheduler.schedulers.background import BackgroundScheduler
import db

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('cache-manager')

DATA_DIR = os.environ.get('MIRROR_DATA_DIR', '/data')
REGISTRY_DIR = os.path.join(DATA_DIR, 'registry')
CONFIG_DIR = os.path.join(DATA_DIR, 'registry-config')

# registry 二进制路径；容器内安装或从官方 registry:2 复制
REGISTRY_BIN = os.environ.get('REGISTRY_BIN', 'docker-registry')


def get_disk_usage(path):
    """返回 (总字节, 已用字节)"""
    stat = os.statvfs(path)
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    used = total - free
    return total, used


def get_dir_size(path):
    """递归计算目录大小"""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def get_cache_sizes():
    """各上游缓存大小"""
    sizes = {}
    for upstream in ['hub', 'ghcr', 'gcr']:
        p = os.path.join(REGISTRY_DIR, upstream)
        if os.path.exists(p):
            sizes[upstream] = get_dir_size(p)
        else:
            sizes[upstream] = 0
    return sizes


def run_registry_gc(upstream):
    """运行 registry garbage collect"""
    config_file = os.path.join(CONFIG_DIR, f'{upstream}.yml')
    if not os.path.exists(config_file):
        log.warning(f'config not found: {config_file}')
        return False

    try:
        result = subprocess.run(
            [REGISTRY_BIN, 'garbage-collect', '--delete-untagged', config_file],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            log.info(f'gc completed for {upstream}')
            return True
        else:
            log.error(f'gc failed for {upstream}: {result.stderr[-200:]}')
            return False
    except FileNotFoundError:
        log.warning(f'registry binary not found, skipping gc for {upstream}')
        return False
    except subprocess.TimeoutExpired:
        log.error(f'gc timeout for {upstream}')
        return False


def cleanup_lru():
    """LRU 清理：磁盘超阈值时删除最旧的缓存层"""
    max_pct = int(db.get_config('max_disk_pct', '80'))
    target_pct = int(db.get_config('target_disk_pct', '70'))

    total, used = get_disk_usage(DATA_DIR)
    pct = round(used / total * 100, 1) if total > 0 else 0
    log.info(f'disk usage: {pct}% (threshold: {max_pct}%)')

    if pct < max_pct:
        return  # 未超阈值，不需要清理

    target_bytes = int(total * target_pct / 100)
    need_free = used - target_bytes
    log.info(f'cleanup needed: free {need_free} bytes ({need_free // 1024 // 1024} MB)')

    # 收集所有 blob 文件并按 atime 排序
    blobs = []
    for upstream in ['hub', 'ghcr', 'gcr']:
        blob_dir = os.path.join(REGISTRY_DIR, upstream, 'blobs', 'sha256')
        if not os.path.exists(blob_dir):
            continue
        for prefix in os.listdir(blob_dir):
            prefix_dir = os.path.join(blob_dir, prefix)
            if not os.path.isdir(prefix_dir):
                continue
            for f in os.listdir(prefix_dir):
                fp = os.path.join(prefix_dir, f)
                if not os.path.isfile(fp):
                    continue
                try:
                    stat = os.stat(fp)
                    blobs.append((stat.st_atime, stat.st_size, fp, upstream))
                except OSError:
                    pass

    # 按 atime 升序（最旧在前）
    blobs.sort(key=lambda x: x[0])

    freed = 0
    deleted = 0
    for atime, size, filepath, upstream in blobs:
        if freed >= need_free:
            break
        try:
            os.remove(filepath)
            freed += size
            deleted += 1
        except OSError as e:
            log.warning(f'failed to delete {filepath}: {e}')

    log.info(f'freed {freed // 1024 // 1024} MB, deleted {deleted} blobs')

    # 清理后运行 GC 重建元数据
    for upstream in ['hub', 'ghcr', 'gcr']:
        run_registry_gc(upstream)


def _read_link(path):
    """读取 registry 的 link 文件内容（digest）"""
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except (OSError, IOError):
        return ''


def _get_blob_size(blob_root, digest):
    """根据 digest 获取 blob 文件大小"""
    algo, hashpart = digest.split(':', 1)
    prefix = hashpart[:2]
    blob_path = os.path.join(blob_root, algo, prefix, hashpart, 'data')
    try:
        return os.path.getsize(blob_path)
    except OSError:
        return 0


def _read_blob_json(blob_root, digest):
    """读取 blob 内容并解析 JSON"""
    import json
    algo, hashpart = digest.split(':', 1)
    prefix = hashpart[:2]
    blob_path = os.path.join(blob_root, algo, prefix, hashpart, 'data')
    try:
        with open(blob_path, 'r') as f:
            return json.load(f)
    except (OSError, IOError, ValueError):
        return None


def _calc_manifest_size(blob_root, digest):
    """递归跟随 manifest 链，累计实际存在的 blob 大小"""
    total = _get_blob_size(blob_root, digest)
    manifest = _read_blob_json(blob_root, digest)
    if not manifest:
        return total

    # Case 1: OCI index / Docker manifest list (多架构)
    if 'manifests' in manifest:
        for m in manifest['manifests']:
            sub_digest = m['digest']
            sub = _read_blob_json(blob_root, sub_digest)
            if not sub:
                continue  # 未拉取的架构，跳过
            total += _get_blob_size(blob_root, sub_digest)
            if 'layers' in sub:
                for layer in sub['layers']:
                    total += _get_blob_size(blob_root, layer['digest'])
            if 'config' in sub:
                total += _get_blob_size(blob_root, sub['config']['digest'])
        return total

    # Case 2: 单架构 manifest (直接有 layers)
    if 'layers' in manifest:
        for layer in manifest['layers']:
            total += _get_blob_size(blob_root, layer['digest'])
        if 'config' in manifest:
            total += _get_blob_size(blob_root, manifest['config']['digest'])

    return total


def _find_image_dirs(repo_base):
    """递归找到所有包含 _manifests 的目录，返回 image_name 列表"""
    results = []
    for root, dirs, _ in os.walk(repo_base):
        if '_manifests' in dirs:
            image_name = os.path.relpath(root, repo_base)
            results.append(image_name)
            dirs.clear()
        else:
            dirs[:] = [d for d in dirs if not d.startswith('_')]
    return results


def scan_cache():
    """扫描缓存目录，更新 DB 中的镜像统计"""
    upstreams = [u['name'] for u in db.get_upstreams()] or ['hub', 'ghcr', 'gcr']

    for upstream in upstreams:
        repo_dir = os.path.join(REGISTRY_DIR, upstream, 'docker', 'registry', 'v2', 'repositories')
        blob_root = os.path.join(REGISTRY_DIR, upstream, 'docker', 'registry', 'v2', 'blobs')
        if not os.path.exists(repo_dir):
            continue

        image_names = _find_image_dirs(repo_dir)
        log.info(f'scanning {upstream}: found {len(image_names)} images')

        for image_name in image_names:
            manifests_dir = os.path.join(repo_dir, image_name, '_manifests', 'tags')
            if not os.path.exists(manifests_dir):
                continue

            for tag_name in os.listdir(manifests_dir):
                if tag_name.startswith('_'):
                    continue
                tag_dir = os.path.join(manifests_dir, tag_name)
                if not os.path.isdir(tag_dir):
                    continue

                current_link = os.path.join(tag_dir, 'current', 'link')
                digest = _read_link(current_link)
                if not digest:
                    continue

                # 递归跟随 manifest 链计算真实大小
                size = _calc_manifest_size(blob_root, digest)

                db.upsert_cached_image(upstream, image_name, tag_name, digest, size)
                log.info(f'  {upstream}/{image_name}:{tag_name} ({size // 1024} KB)')

    log.info('cache scan completed')


def scheduled_scan():
    """定时扫描任务"""
    try:
        scan_cache()
    except Exception as e:
        log.error(f'scan error: {e}')


def scheduled_cleanup():
    """定时清理任务"""
    try:
        scan_cache()
        cleanup_lru()
    except Exception as e:
        log.error(f'cleanup error: {e}')


def main():
    log.info('cache-manager starting')

    # 确保数据库已初始化
    db.init_db()

    interval = int(db.get_config('cleanup_interval', '300'))

    from datetime import datetime, timedelta

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        scheduled_cleanup, 'interval', seconds=interval,
        id='cleanup', next_run_time=datetime.now() + timedelta(seconds=10)
    )
    scheduler.start()
    log.info(f'scheduler started, interval={interval}s')

    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info('cache-manager stopped')


if __name__ == '__main__':
    main()
