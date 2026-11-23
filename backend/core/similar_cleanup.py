"""Read-only similarity scan for archived posts. Never deletes or selects by itself."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

_cache: dict[str, tuple[int, int, tuple[int, ...]]] = {}
_lock = threading.Lock()
_url = re.compile(r"https?://\S+", re.I)
_space = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def _text(value: str) -> str:
    return _space.sub("", _url.sub("", value or "").lower())


def _hash(image: Image.Image) -> int:
    image = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(image.getdata())
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return value


def _average_hash(image: Image.Image) -> int:
    pixels = list(image.convert("L").resize((8, 8), Image.Resampling.LANCZOS).getdata())
    average = sum(pixels) / len(pixels)
    value = 0
    for pixel in pixels:
        value = (value << 1) | int(pixel > average)
    return value


def _same(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    return ((a[0] ^ b[0]).bit_count() <= 5 and
            (a[1] ^ b[1]).bit_count() <= 7 and
            abs(a[2] - b[2]) <= 100)


def _same_set(left: list[tuple[int, ...]], right: list[tuple[int, ...]]) -> bool:
    if len(left) != len(right):
        return False
    options = [tuple(j for j, other in enumerate(right) if _same(item, other)) for item in left]
    if any(not row for row in options):
        return False
    order = sorted(range(len(left)), key=lambda i: len(options[i]))
    def assign(pos: int, used: set[int]) -> bool:
        if pos == len(order):
            return True
        return any(assign(pos + 1, used | {j}) for j in options[order[pos]] if j not in used)
    return assign(0, set())


def _fingerprints(path: str, thumb_root: str, disk_cache: dict[str, tuple[int, int, tuple[int, ...]]]) -> tuple[int, ...]:
    try:
        stat = os.stat(path)
    except OSError:
        return ()
    with _lock:
        old = _cache.get(path)
    if old and old[:2] == (stat.st_mtime_ns, stat.st_size):
        disk_cache[path] = old
        return old[2]
    old = disk_cache.get(path)
    if old and old[:2] == (stat.st_mtime_ns, stat.st_size):
        with _lock:
            _cache[path] = old
        return old[2]
    key = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8", "ignore")).hexdigest()[:16]
    thumb = os.path.join(thumb_root, f"{key}_320_{int(stat.st_mtime)}_{stat.st_size}.webp")
    read_path = thumb if os.path.isfile(thumb) else path
    try:
        with Image.open(read_path) as source:
            source.draft("RGB", (384, 384))
            image = ImageOps.exif_transpose(source)
            image.thumbnail((384, 384))
            w, h = image.size
            if min(w, h) < 24:
                return ()
            full = _hash(image)
            # Crops are search hints only, never a basis for automatic deletion.
            crops = [
                image.crop((int(w * .1), int(h * .1), int(w * .9), int(h * .9))),
                image.crop((int(w * .2), int(h * .2), int(w * .8), int(h * .8))),
                image.crop((0, 0, int(w * .8), h)),
                image.crop((int(w * .2), 0, w, h)),
            ]
            result = (full, _average_hash(image), round(w / h * 1000),
                      *(_hash(crop) for crop in crops))
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return ()
    with _lock:
        _cache[path] = (stat.st_mtime_ns, stat.st_size, result)
    disk_cache[path] = (stat.st_mtime_ns, stat.st_size, result)
    return result


def _buckets(value: int):
    for band in range(4):
        yield band, (value >> (16 * band)) & 0xffff


def _simhash(value: str) -> int:
    # Character shingles work for Chinese and Japanese without a tokenizer.
    weights = [0] * 64
    for pos in range(max(0, len(value) - 2)):
        from hashlib import blake2b
        digest = int.from_bytes(blake2b(value[pos:pos + 3].encode(), digest_size=8).digest())
        for bit in range(64):
            weights[bit] += 1 if digest & (1 << bit) else -1
    return sum((1 << bit) for bit, weight in enumerate(weights) if weight > 0)


def scan(db_path: str) -> dict[str, Any]:
    """Group exact/near text and local-image matches across both platforms."""
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        posts = [dict(row) for row in conn.execute(
            "SELECT platform,mblog_id,uid,screen_name,created_at,created_ts,text,"
            "pic_count,video_count FROM posts")]
        media = [dict(row) for row in conn.execute(
            "SELECT platform,mblog_id,kind,path,url,status FROM media")]
    finally:
        conn.close()
    cache_path = os.path.join(os.path.dirname(db_path), '.similar_fingerprints.sqlite')
    cache_conn = sqlite3.connect(cache_path, timeout=15)
    try:
        cache_conn.execute("CREATE TABLE IF NOT EXISTS fingerprints (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, hashes TEXT)")
        disk_cache = {path: (mtime, size, tuple(int(v) for v in hashes.split(',')))
                      for path, mtime, size, hashes in cache_conn.execute(
                          "SELECT path,mtime_ns,size,hashes FROM fingerprints")}
    finally:
        cache_conn.close()
    prior_cache = dict(disk_cache)
    by_key = {(p['platform'], p['mblog_id']): p for p in posts}
    files: dict[tuple[str, str], list[dict]] = defaultdict(list)
    available_media: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {'image': 0, 'video': 0})
    for m in media:
        key = (m['platform'], m['mblog_id'])
        if key in by_key and m['path'] and m['status'] in ('ok', 'skipped') and os.path.isfile(m['path']):
            media_kind = 'video' if 'video' in str(m['kind'] or '') else 'image'
            available_media[key][media_kind] += 1
        if key in by_key and m['kind'] == 'image' and m['path']:
            files[key].append(m)
    keys = list(by_key)
    index = {key: i for i, key in enumerate(keys)}
    signatures: dict[int, list[tuple[int, ...]]] = {}
    thumb_root = os.path.join(os.path.dirname(db_path), '.thumbs')
    paths = list({m['path'] for images in files.values() for m in images})
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        path_hashes = dict(zip(paths, pool.map(lambda path: _fingerprints(path, thumb_root, disk_cache), paths)))
    for key, images in files.items():
        hashes = [path_hashes[m['path']] for m in images]
        signatures[index[key]] = [h for h in hashes if h]
    changed = [(path, mtime, size, ','.join(map(str, hashes)))
               for path, (mtime, size, hashes) in disk_cache.items()
               if prior_cache.get(path) != (mtime, size, hashes)]
    if changed:
        cache_conn = sqlite3.connect(cache_path, timeout=15)
        try:
            cache_conn.executemany("INSERT OR REPLACE INTO fingerprints VALUES (?,?,?,?)", changed)
            cache_conn.commit()
        finally:
            cache_conn.close()

    # Candidate generation is banded, so scanning 10k+ images does not compare all pairs.
    candidate: dict[tuple[int, int], set[str]] = defaultdict(set)
    text_exact: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(posts):
        normalized = _text(p['text'])
        if len(normalized) >= 12:
            text_exact[normalized].append(i)
    for members in text_exact.values():
        for pos, i in enumerate(members):
            for j in members[pos + 1:]:
                candidate[(i, j)].add('text')

    near_text: dict[tuple[int, int], list[int]] = defaultdict(list)
    text_signatures: dict[int, int] = {}
    for i, p in enumerate(posts):
        normalized = _text(p['text'])
        if len(normalized) < 24:
            continue
        value = _simhash(normalized)
        text_signatures[i] = value
        for bucket in _buckets(value):
            near_text[bucket].append(i)
    for members in near_text.values():
        if len(members) > 120:
            continue
        for pos, i in enumerate(members):
            for j in members[pos + 1:]:
                if (text_signatures[i] ^ text_signatures[j]).bit_count() <= 7:
                    candidate[(i, j)].add('near_text')

    hash_index: dict[tuple[int, int, int], list[tuple[int, int]]] = defaultdict(list)
    for i, hashes in signatures.items():
        for n, value in enumerate(h[0] for h in hashes):
            for band, part in _buckets(value):
                hash_index[(band, part, 0)].append((i, n))
        for n, h in enumerate(hashes):
            for value in h[3:]:
                for band, part in _buckets(value):
                    hash_index[(band, part, 1)].append((i, n))
    for (_band, _part, kind), members in hash_index.items():
        # Common flat/blank images can make a huge bucket. Skip such non-discriminating bands.
        if len(members) > 120:
            continue
        for pos, (i, _n) in enumerate(members):
            for j, _ in members[pos + 1:]:
                if i != j:
                    candidate[tuple(sorted((i, j)))].add('image' if kind == 0 else 'crop')

    parent = list(range(len(posts)))
    high_pairs: set[tuple[int, int]] = set()
    high_image_pairs: set[tuple[int, int]] = set()
    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a: int, b: int) -> None:
        parent[find(b)] = find(a)

    edges: list[tuple[int, int, str, str]] = []
    for (a, b), reasons in candidate.items():
        ah, bh = signatures.get(a, []), signatures.get(b, [])
        image_full = bool(ah and bh and len(ah) == len(files.get(keys[a], []))
                          and len(bh) == len(files.get(keys[b], []))
                          and len(ah) == int(posts[a]['pic_count'] or 0)
                          and len(bh) == int(posts[b]['pic_count'] or 0)
                          and int(posts[a]['video_count'] or 0) == int(posts[b]['video_count'] or 0)
                          and _same_set(ah, bh))
        image_partial = bool(ah and bh and any(
            (x[0] ^ y[0]).bit_count() <= 9 or
            any((v ^ y[0]).bit_count() <= 9 for v in x[3:]) or
            any((x[0] ^ v).bit_count() <= 9 for v in y[3:])
            for x in ah for y in bh))
        exact_text = 'text' in reasons
        # Exact text with different image sets is suspicious, never one-click safe.
        if image_full:
            grade, reason = 'high', '图片集合高度重复'
        elif (exact_text and not ah and not bh and
              all(int(posts[i][field] or 0) == 0 for i in (a, b)
                  for field in ('pic_count', 'video_count'))):
            grade, reason = 'high', '正文相同'
        elif image_partial:
            grade, reason = 'suspect', '图片局部相似'
        elif exact_text:
            grade, reason = 'suspect', '正文相同，图片不同'
        elif 'near_text' in reasons:
            grade, reason = 'suspect', '正文相似'
        else:
            continue
        edges.append((a, b, grade, reason))
        if grade == 'high':
            high_pairs.add((a, b))
            if reason == '图片集合高度重复':
                high_image_pairs.add((a, b))
            union(a, b)

    components: dict[int, set[int]] = defaultdict(set)
    for a, b, grade, _ in edges:
        if grade == 'high':
            components[find(a)].update((a, b))
    groups = []
    used_suspect: set[tuple[int, int]] = set()
    def public(i: int) -> dict:
        p = posts[i]
        return {k: p[k] for k in ('platform', 'mblog_id', 'uid', 'screen_name',
                'created_at', 'text', 'pic_count', 'video_count')} | {
                    'image_count': len(signatures.get(i, [])),
                    'previews': [m['path'] for m in files.get(keys[i], [])[:4]
                                 if path_hashes.get(m['path'])]}
    for members in components.values():
        ordered = sorted(members, key=lambda i: (
            -(len(signatures.get(i, [])) + int(posts[i]['video_count'] or 0)),
            int(posts[i]['created_ts'] or 0), i))
        def complete(i: int) -> bool:
            p = posts[i]
            found = available_media[keys[i]]
            return (found['image'] >= int(p['pic_count'] or 0)
                    and found['video'] >= int(p['video_count'] or 0))
        keeper = next((i for i in ordered if posts[i]['platform'] == 'x' and complete(i)), None)
        if keeper is None:
            keeper = next((i for i in ordered if complete(i)), None)
        drop = ([i for i in ordered if i != keeper and complete(i) and
                 tuple(sorted((i, keeper))) in high_pairs] if keeper is not None else [])
        groups.append({'grade': 'high', 'reason': '图片或正文高度重复',
                       'items': [public(i) for i in ordered],
                       'auto_delete': [public(i) for i in drop],
                       'auto_delete_image': [public(i) for i in drop
                                             if tuple(sorted((i, keeper))) in high_image_pairs],
                       'keep': [public(keeper)] if keeper is not None else []})
    for a, b, grade, reason in edges:
        if grade != 'suspect' or (a, b) in used_suspect or find(a) == find(b):
            continue
        used_suspect.add((a, b))
        groups.append({'grade': 'suspect', 'reason': reason,
                       'items': [public(a), public(b)], 'auto_delete': [], 'keep': []})
    groups.sort(key=lambda g: (g['grade'] != 'high', -len(g['items'])))
    return {'groups': groups, 'post_count': len(posts), 'image_count': sum(map(len, signatures.values()))}
