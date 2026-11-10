#!/usr/bin/env python3
"""keepsake · 泡泡库的服务端（单文件，只用标准库）。

    python3 keepsakes.py --db chat.db --port 8787 [--uploads ./public]

三张自己的表 + 七个接口，见 docs/01-one-library.md。
它挂在你已有的两张表旁边，不改它们：

    chat_messages(id, session_id, role, content, created_at)    你的聊天记录
    treasures(id, collector, role, quote, why, message_id, photo_top, created_at)   你原来的单句收藏

库里没有这两张表的话，会建一份最小的，方便直接跑 demo。
你自己的表字段名不一样，改 _adapt_* 那几句 SQL 就行，别的地方不碰它们。

谁：'her' = 人类那边，'me' = AI 那边。一颗泡的 key：'t:<treasures.id>' 是小泡（一句），
'k:<keepsakes.id>' 是大泡（一段）。meta 和评论都只认 key，所以大小泡共用一套。
"""
import argparse
import json
import os
import re
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_PATH = 'chat.db'
UPLOAD_ROOT = '.'          # 图片路径 /uploads/x.jpg 在磁盘上 = UPLOAD_ROOT + /uploads/x.jpg


def get_db():
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.row_factory = sqlite3.Row
    return db


# ── 你已有的表（没有就建最小的一份）──────────────────────────────────
def _adapt_existing(db):
    db.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER, role TEXT, content TEXT,
        created_at TEXT DEFAULT (datetime('now')))""")
    db.execute("""CREATE TABLE IF NOT EXISTS treasures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collector TEXT DEFAULT 'me',      -- 'me' = AI 自己收的（=珍藏），'her' = 你收的
        role TEXT,                        -- 'user' / 'assistant' / 'dialogue'（多句，每行「我：…」「她：…」）
        quote TEXT, why TEXT DEFAULT '',
        message_id INTEGER, photo_top TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')))""")


# ── 泡泡自己的三张表 ───────────────────────────────────────────────
# 原来的收藏表一行不动（上面可能挂着触发器、别的页面在读），新东西全挂在旁边。
# 「珍藏」不是贴上去的标签，是 collector='me' 自己长出来的分类，撕不掉。
def _ensure_keepsakes(db):
    _adapt_existing(db)
    db.execute("""CREATE TABLE IF NOT EXISTS keepsakes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT (datetime('now')),
        collector TEXT DEFAULT 'her',
        title TEXT DEFAULT '',
        session_id INTEGER,
        lines_json TEXT NOT NULL,          -- 按消息快照：原消息以后改了删了，这里不跟着变
        from_treasure INTEGER,             -- 从哪颗小泡长大的
        removed_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS keepsake_meta (
        key TEXT PRIMARY KEY,
        title TEXT DEFAULT '',
        tags_json TEXT DEFAULT '[]',
        pre_her TEXT DEFAULT '',           -- 前情：你那一栏
        pre_me TEXT DEFAULT '',            -- 前情：ta 那一栏
        shots_json TEXT DEFAULT '[]',
        photo TEXT DEFAULT '',
        last_visit TEXT,
        ask_me INTEGER DEFAULT 0,          -- 你点了「叫 ta 写经过」
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS keepsake_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL,
        parent_id INTEGER DEFAULT 0,       -- 楼中楼：回的是哪条；负数 = 回珍藏那句「为什么留下」
        author TEXT NOT NULL,
        text TEXT NOT NULL,
        fog_days INTEGER,                  -- 写这句之前它雾了几天（聊天卡上「12 DAYS · CLEAR」）
        sent INTEGER,                      -- 1=发给 ta / 0=只留在泡里、不拿来催 ta / NULL=有开关之前写的，照旧算发了
        created_at TEXT DEFAULT (datetime('now')),
        removed_at TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_keepsake_comments_key ON keepsake_comments(key)")


# ── 清洗：存之前过一遍（docs/06 第 10 条：卡上露出原码）──────────────────
# 聊天正文里混着 app 自己认的标记。存进泡里原样留着，卡片上就是一串乱码。
# 改成你自己 app 的标记写法。
_KS_MARK = re.compile(r'\[[^\]\n]{0,12}:[^\]]*\]|⟪[^⟫]*⟫|<[^>]{1,40}>|/uploads/\S+|/img/\S+')
_KS_QUOTE = re.compile(r'⟦引用[^⟧]*⟧.*?⟦/引用⟧|⟦[^⟧]*⟧', re.S)          # 引用块 = 重复上一句，整块不要
_KS_CARD = re.compile(r'\[card:[^|\]]*\|[^|\]]*\|([^|\]]*)(?:\|[^\]]*)?\]')   # [card:类型|标题|正文|…] → 留正文

def _ks_clean(s, n=0):
    # 🔴 顺序有讲究：先留卡片正文、再擦别的标记。反过来卡片会被整张擦成空行。
    s = _KS_CARD.sub(lambda m: m.group(1), _KS_QUOTE.sub('', s or ''))
    s = re.sub(r'\n{3,}', '\n\n', _KS_MARK.sub('', s)).strip()
    return (s[:n] + '…') if n and len(s) > n else s


def _ks_neighbors(db, mid):
    """原话所在那条消息的前一条/后一条（同会话），各取挨着的那一段，当淡淡的上下文。"""
    row = db.execute("SELECT session_id FROM chat_messages WHERE id=?", (mid,)).fetchone()
    if not row: return [], []
    def one(op, order):
        r = db.execute(f"SELECT role,content FROM chat_messages WHERE session_id=? AND id{op}? AND role IN ('user','assistant') "
                       f"ORDER BY id {order} LIMIT 1", (row['session_id'], mid)).fetchone()
        if not r: return []
        paras = [p for p in (_ks_clean(x) for x in (r['content'] or '').split('\n')) if p]
        if not paras: return []
        # 前一条取它的最后一段、后一条取第一段：挨着的那一段才是上下文
        return [['her' if r['role'] == 'user' else 'me', _ks_clean(paras[-1] if op == '<' else paras[0], 60)]]
    return one('<', 'DESC'), one('>', 'ASC')


def _ks_meta(db, key):
    r = db.execute("SELECT * FROM keepsake_meta WHERE key=?", (key,)).fetchone()
    def js(v):
        try: return json.loads(v or '[]')
        except Exception: return []
    if not r: return {'title': '', 'tags': [], 'pre': {'her': '', 'me': ''}, 'shots': [], 'photo': '', 'last_visit': None, 'ask_me': 0}
    return {'title': r['title'] or '', 'tags': js(r['tags_json']), 'pre': {'her': r['pre_her'] or '', 'me': r['pre_me'] or ''},
            'shots': js(r['shots_json']), 'photo': r['photo'] or '', 'last_visit': r['last_visit'], 'ask_me': r['ask_me'] or 0}


def _ks_item(db, key, who, created, lines, ctx, after, photo_fallback, extra_tags, first_note, comments, title=''):
    meta = _ks_meta(db, key)
    first = next((ln for w, t in lines for ln in t.split('\n') if ln.strip()), '')
    notes = ([first_note] if first_note else []) + comments.get(key, [])
    # touch = 最后一次被碰（存下 / 来看 / 写一句）—— 雾就按这个算（docs/05）
    touch = max([created] + [n['at'] for n in notes if n.get('at')] + ([meta['last_visit']] if meta['last_visit'] else []))
    return {'key': key, 'who': who, 'at': created, 't': meta['title'] or title or _ks_clean(first, 12),
            'lines': lines, 'ctx': ctx, 'ctxAfter': after,
            'tags': extra_tags + [t for t in meta['tags'] if t not in extra_tags],
            'pre': meta['pre'], 'shots': meta['shots'], 'photo': meta['photo'] or photo_fallback or '',
            'notes': notes, 'touch': touch, 'askMe': meta['ask_me']}


def _ks_all():
    db = get_db(); _ensure_keepsakes(db)
    comments = {}
    for c in db.execute("SELECT id,key,parent_id,author,text,created_at FROM keepsake_comments WHERE removed_at IS NULL ORDER BY id").fetchall():
        comments.setdefault(c['key'], []).append({'id': c['id'], 'w': c['author'], 't': c['text'], 'at': c['created_at'], 're': c['parent_id'] or 0})
    grown = {r[0] for r in db.execute("SELECT from_treasure FROM keepsakes WHERE from_treasure IS NOT NULL AND removed_at IS NULL")}
    out = []
    for t in db.execute("SELECT * FROM treasures ORDER BY id").fetchall():
        if t['id'] in grown: continue          # 小泡已经长成大泡了，只留大的那颗
        key = f"t:{t['id']}"
        mine = (t['collector'] or 'me') == 'me'
        if t['role'] == 'dialogue':
            lines = []
            for ln in (t['quote'] or '').split('\n'):
                w, _, s = ln.partition('：')
                if s and w.strip() in ('我', '她'): lines.append(['me' if w.strip() == '我' else 'her', _ks_clean(s)])
                elif ln.strip() and lines: lines[-1][1] += ' ' + _ks_clean(ln)
            ctx, after = [], []
        else:
            lines = [['her' if t['role'] == 'user' else 'me', _ks_clean(t['quote'])]]
            ctx, after = _ks_neighbors(db, t['message_id']) if t['message_id'] else ([], [])
        # 珍藏当时写的「为什么留下」，显示成 ta 在这颗泡底下的第一条评论（可以回，不能在这里删：它属于原来那张表）
        first = {'id': -t['id'], 'w': 'me', 't': t['why'], 'at': t['created_at'], 're': 0, 'fixed': 1} if mine and (t['why'] or '').strip() else None
        out.append(_ks_item(db, key, 'me' if mine else 'her', t['created_at'], lines, ctx, after,
                            t['photo_top'], ['珍藏'] if mine else [], first, comments))
    for k in db.execute("SELECT * FROM keepsakes WHERE removed_at IS NULL ORDER BY id").fetchall():
        try: raw = json.loads(k['lines_json'])
        except Exception: raw = []
        lines = [[x.get('w'), _ks_clean(x.get('t', ''))] for x in raw if not x.get('ctx')]
        lines = [l for l in lines if l[1]] or lines
        ctx = [[x.get('w'), x.get('t', '')] for x in raw if x.get('ctx') == 'before']
        after = [[x.get('w'), x.get('t', '')] for x in raw if x.get('ctx') == 'after']
        mine = k['collector'] == 'me'
        out.append(_ks_item(db, f"k:{k['id']}", 'me' if mine else 'her', k['created_at'], lines, ctx, after,
                            '', [], None, comments, k['title'] or ''))   # 整段 ta 存的不算「珍藏」，珍藏只是单句那几条
    db.close()
    return out


# 存段的时候 _ks_clean 把 [图片:…] 擦了。这里从那几条你的原消息里把照片捞回来，追加进这颗泡的截图那排。
# 🔴 不自动带：带不带、带哪几张是 ta 自己挑的（⟪封存⟫ 第五格写「全部」或「1,3」），不写就不带。
_KS_IMG = re.compile(r'\[图片:(/uploads/[^\]\s]+)\]')

def _ks_attach_her_photos(db, key, mids, pick):
    pick = str(pick or '').strip()
    if not mids or not pick: return 0
    q = ','.join('?' * len(mids))
    ps = []
    for r in db.execute(f"SELECT content FROM chat_messages WHERE id IN ({q}) AND role='user' ORDER BY id", list(mids)).fetchall():
        for p in _KS_IMG.findall(r['content'] or ''):
            if p not in ps and os.path.exists(UPLOAD_ROOT + p): ps.append(p)
    if pick not in ('全部', 'all'):
        nums = [int(n) for n in re.findall(r'\d+', pick)]
        ps = [ps[n - 1] for n in nums if 1 <= n <= len(ps)]
    if not ps: return 0
    db.execute("INSERT OR IGNORE INTO keepsake_meta (key) VALUES (?)", (key,))
    row = db.execute("SELECT shots_json FROM keepsake_meta WHERE key=?", (key,)).fetchone()
    try: shots = json.loads(row['shots_json'] or '[]')
    except Exception: shots = []
    shots += [p for p in ps if p not in shots]
    db.execute("UPDATE keepsake_meta SET shots_json=?, updated_at=datetime('now') WHERE key=?", (json.dumps(shots, ensure_ascii=False), key))
    return len(ps)


def _find_by_start_end(db, start, end):
    """ta 在聊天里看不到消息号，只能抄开头结尾几个字。在最近 300 条里往回找。

    🔴 docs/06 第 4 条：你说「从 XXX 那句开始存」，这句请求本身也含开头那几个字 →
    取最新一条命中的话，开头落在这条请求上、结尾在它前面找不到，截到最新 = 只存了两句。
    所以开头有好几处命中时，挑「结尾落得最早」的那一对（同一个结尾取离它最近的开头）；
    一对都配不上才退回最新那条命中。"""
    st, en = start.strip()[:30], end.strip()[:30]
    recent = db.execute("SELECT id,session_id,content FROM chat_messages WHERE role IN ('user','assistant') ORDER BY id DESC LIMIT 300").fetchall()
    cands = [r for r in recent if st in (r['content'] or '')]
    def _end_after(a):   # a 之后（含 a）最早一条含结尾的
        return next((r for r in reversed(recent) if r['session_id'] == a['session_id'] and r['id'] >= a['id'] and en in (r['content'] or '')), None)
    pairs = [(c, _end_after(c)) for c in cands] if en else []
    pairs = [(c, e) for c, e in pairs if e]
    a = min(pairs, key=lambda p: (p[1]['id'], -p[0]['id']))[0] if pairs else (cands[0] if cands else None)
    if not a: return []
    b = _end_after(a) if en else None
    hi = b['id'] if b else a['id']
    if en and not b:   # 结尾那句可能就在 ta 正在写的这条里（还没落库）→ 截到这个会话最新一条
        hi = max(r['id'] for r in recent if r['session_id'] == a['session_id'])
    return [r[0] for r in db.execute("SELECT id FROM chat_messages WHERE session_id=? AND id BETWEEN ? AND ? AND role IN ('user','assistant') ORDER BY id LIMIT 30",
                                     (a['session_id'], a['id'], hi)).fetchall()]


def _dup_within_10min(db, lines, session_id=None):
    # 同一段十分钟内别存两次（标记重放 / 两条线各执行一次 / 手抖点两下）
    q = "SELECT id FROM keepsakes WHERE removed_at IS NULL AND lines_json=? AND created_at>datetime('now','-10 minutes')"
    args = [json.dumps(lines, ensure_ascii=False)]
    if session_id is not None: q += " AND session_id=?"; args.append(session_id)
    return db.execute(q, args).fetchone()


def _set_pre(db, key, field, text):
    text = str(text or '').strip()
    if not text: return
    db.execute("INSERT OR IGNORE INTO keepsake_meta (key) VALUES (?)", (key,))
    db.execute(f"UPDATE keepsake_meta SET {field}=?, updated_at=datetime('now') WHERE key=?", (text[:2000], key))


def keepsake_new(body):
    """存一段。三种写法：{msg_ids:[...]} / {clips:[{mid,w,t}]} / {start,end}。
    其余可带：collector:'her'|'me', title, from_treasure, pre_her, pre_me, photos。"""
    body = body or {}
    collector = 'me' if body.get('collector') == 'me' else 'her'
    db = get_db(); _ensure_keepsakes(db)
    try:
        ids = [int(x) for x in (body.get('msg_ids') or []) if str(x).isdigit()]
        if not ids and str(body.get('start') or '').strip():
            ids = _find_by_start_end(db, str(body['start']), str(body.get('end') or ''))

        # 你在 app 里一段段勾的：存你勾的那几段原文，不是整条消息（一条长消息你可能只要其中两段）
        clips = [c for c in (body.get('clips') or []) if isinstance(c, dict) and str(c.get('t') or '').strip()
                 and str(c.get('mid') or '').lstrip('-').isdigit()]
        if clips:
            mids = sorted({int(c['mid']) for c in clips if int(c['mid']) > 0})
            if not mids:
                return 400, {'ok': False, 'error': '还没落服务器的消息存不了'}
            first = db.execute("SELECT session_id FROM chat_messages WHERE id=?", (mids[0],)).fetchone()
            lines = [{'w': 'me' if c.get('w') == 'me' else 'her', 't': str(c['t']).strip()[:2000], 'mid': int(c['mid'])} for c in clips]
            before, after = _ks_neighbors(db, mids[0])[0], _ks_neighbors(db, mids[-1])[1]
            lines = [{'w': w, 't': t, 'ctx': 'before'} for w, t in before] + lines + [{'w': w, 't': t, 'ctx': 'after'} for w, t in after]
            same = _dup_within_10min(db, lines)
            if same: return 200, {'ok': True, 'key': f"k:{same['id']}", 'skipped': True}
            cur = db.execute("INSERT INTO keepsakes (collector,title,session_id,lines_json) VALUES (?,?,?,?)",
                             (collector, str(body.get('title') or '')[:40], first['session_id'] if first else None, json.dumps(lines, ensure_ascii=False)))
            key = f'k:{cur.lastrowid}'
            _set_pre(db, key, 'pre_her', body.get('pre_her'))
            _ks_attach_her_photos(db, key, mids, body.get('photos'))
            db.commit()
            return 200, {'ok': True, 'key': key}

        if not ids:
            # 🔴 失败要说人话：这句会原样贴回聊天里（docs/03「失败留一行字」），ta 下一轮看得懂才改得了
            return 400, {'ok': False, 'error': '找不到那几句（开头那句要原样抄几个字）'}
        q = ','.join('?' * len(ids))
        rows = db.execute(f"SELECT id,session_id,role,content,created_at FROM chat_messages WHERE id IN ({q}) AND role IN ('user','assistant') ORDER BY id", ids).fetchall()
        if not rows:
            return 404, {'ok': False, 'error': '那几条消息找不到'}
        lines = [{'w': 'her' if r['role'] == 'user' else 'me', 't': _ks_clean(r['content']), 'mid': r['id'], 'at': r['created_at']} for r in rows]
        lines = [x for x in lines if x['t']] or lines   # 只剩表情包/图片的那条不占一行
        before, after = _ks_neighbors(db, rows[0]['id'])[0], _ks_neighbors(db, rows[-1]['id'])[1]
        lines = [{'w': w, 't': t, 'ctx': 'before'} for w, t in before] + lines + [{'w': w, 't': t, 'ctx': 'after'} for w, t in after]
        same = _dup_within_10min(db, lines, rows[0]['session_id'])
        if same: return 200, {'ok': True, 'key': f"k:{same['id']}", 'skipped': True}
        ft = body.get('from_treasure')
        ft = int(ft) if str(ft or '').isdigit() else None
        cur = db.execute("INSERT INTO keepsakes (collector,title,session_id,lines_json,from_treasure) VALUES (?,?,?,?,?)",
                         (collector, str(body.get('title') or '')[:40], rows[0]['session_id'], json.dumps(lines, ensure_ascii=False), ft))
        key = f'k:{cur.lastrowid}'
        if ft:   # 长大的那颗把小泡的标签/前情/评论/截图都带上
            db.execute("INSERT OR IGNORE INTO keepsake_meta (key,title,tags_json,pre_her,pre_me,shots_json,photo,last_visit) "
                       "SELECT ?,title,tags_json,pre_her,pre_me,shots_json,photo,datetime('now') FROM keepsake_meta WHERE key=?", (key, f't:{ft}'))
            db.execute("UPDATE keepsake_comments SET key=? WHERE key=?", (key, f't:{ft}'))
        # ta 封的时候顺手写的经过（⟪封存⟫ 第四格）→ 前情 ta 那一栏。存了泡没有前情，过几天谁都想不起来是怎么来的
        _set_pre(db, key, 'pre_me', body.get('pre_me'))
        _set_pre(db, key, 'pre_her', body.get('pre_her'))
        _ks_attach_her_photos(db, key, [r['id'] for r in rows], body.get('photos'))
        db.commit()
        return 200, {'ok': True, 'key': key}
    finally:
        db.close()


_KS_KEY = re.compile(r'^[tk]:\d+$')

def keepsake_meta(body):
    """{key, title?, tags?, pre_her?, pre_me?, shots?, photo?, ask_me?, visit?:true} —— 只改带来的那几样。"""
    body = body or {}
    key = str(body.get('key') or '')
    if not _KS_KEY.match(key): return 400, {'ok': False, 'error': 'key 不对'}
    def _img(p): return isinstance(p, str) and (p == '' or p.startswith('/uploads/') or p.startswith('/img/'))
    sets = {}
    if 'title' in body: sets['title'] = str(body['title'] or '')[:40]
    if 'tags' in body:
        # 「珍藏」撕不掉也贴不上：它是事实不是分类
        tags = [str(x).strip()[:12] for x in (body['tags'] or []) if str(x).strip() and str(x).strip() != '珍藏']
        sets['tags_json'] = json.dumps(list(dict.fromkeys(tags)), ensure_ascii=False)
    for k in ('pre_her', 'pre_me'):
        if k in body: sets[k] = str(body[k] or '')[:2000]
    if 'ask_me' in body: sets['ask_me'] = 1 if body['ask_me'] else 0
    if str(sets.get('pre_me') or '').strip(): sets['ask_me'] = 0   # ta 写了经过，你那声「叫 ta 写」就算答了
    if 'shots' in body:
        shots = body['shots'] or []
        if not isinstance(shots, list) or not all(_img(x) and x for x in shots):
            return 400, {'ok': False, 'error': '截图要是服务器上的图'}
        sets['shots_json'] = json.dumps(shots)
    if 'photo' in body:
        if not _img(body['photo'] or ''): return 400, {'ok': False, 'error': '照片要是服务器上的图'}
        sets['photo'] = body['photo'] or ''
    db = get_db(); _ensure_keepsakes(db)
    db.execute("INSERT OR IGNORE INTO keepsake_meta (key) VALUES (?)", (key,))
    if body.get('visit'): db.execute("UPDATE keepsake_meta SET last_visit=datetime('now') WHERE key=?", (key,))
    for k, v in sets.items():   # k 只可能是上面白名单里的列名
        db.execute(f"UPDATE keepsake_meta SET {k}=?, updated_at=datetime('now') WHERE key=?", (v, key))
    db.commit(); db.close()
    return 200, {'ok': True}


def keepsake_comment(body):
    """{key, text, author:'her'|'me', parent_id?, send?}"""
    body = body or {}
    key, text = str(body.get('key') or ''), str(body.get('text') or '').strip()
    if not _KS_KEY.match(key) or not text: return 400, {'ok': False, 'error': 'key 或内容不对'}
    db = get_db(); _ensure_keepsakes(db)
    dup = db.execute("SELECT id FROM keepsake_comments WHERE key=? AND text=? AND removed_at IS NULL AND created_at>datetime('now','-10 minutes')", (key, text)).fetchone()
    if dup:
        db.close(); return 200, {'ok': True, 'id': dup['id'], 'skipped': True}
    fog = _ks_fog_days(db, key)
    sent = (1 if body.get('send') else 0) if 'send' in body else None
    cur = db.execute("INSERT INTO keepsake_comments (key,parent_id,author,text,fog_days,sent) VALUES (?,?,?,?,?,?)",
                     (key, int(body.get('parent_id') or 0), 'me' if body.get('author') == 'me' else 'her', text[:4000], fog, sent))
    # 写一句 = 碰了它一下，雾散
    db.execute("INSERT OR IGNORE INTO keepsake_meta (key) VALUES (?)", (key,))
    db.execute("UPDATE keepsake_meta SET last_visit=datetime('now') WHERE key=?", (key,))
    cid = cur.lastrowid
    at = db.execute("SELECT created_at FROM keepsake_comments WHERE id=?", (cid,)).fetchone()[0]
    db.commit(); db.close()
    return 200, {'ok': True, 'id': cid, 'at': at}


def keepsake_comment_del(cid):
    db = get_db(); _ensure_keepsakes(db)
    db.execute("UPDATE keepsake_comments SET removed_at=datetime('now') WHERE id=?", (cid,))
    db.commit(); db.close()
    return 200, {'ok': True}


def keepsake_pending(params):
    """给 ta 看的事实（网关每轮贴一行，自由时间也用）。只摆事实，回了就自己消失：
    her   = 你写了、ta 还没回的评论（最近 14 天、最多 3 条），带这颗泡底下前面两句，ta 回的时候知道接的是哪儿；
    ask   = 你点了「叫 ta 写经过」、还没写的；
    fresh = 你新存的泡，ta 底下一句都没写过的；
    foggy = ?foggy=N 时给雾最重的 N 颗，自由时间挑一颗去看。"""
    db = get_db(); _ensure_keepsakes(db)
    her = []
    # 🔴 sent=0 的不进来：你选了「只留在这里」，就不该拿来催 ta
    for c in db.execute("SELECT c.id,c.key,c.text,c.created_at FROM keepsake_comments c WHERE c.author='her' AND c.removed_at IS NULL AND COALESCE(c.sent,1)=1 "
                        "AND c.created_at > datetime('now','-14 days') "
                        "AND NOT EXISTS (SELECT 1 FROM keepsake_comments r WHERE r.parent_id=c.id AND r.author='me' AND r.removed_at IS NULL) "
                        "ORDER BY c.id DESC LIMIT 3").fetchall():
        th = db.execute("SELECT author,text FROM keepsake_comments WHERE key=? AND id<? AND removed_at IS NULL ORDER BY id DESC LIMIT 2", (c['key'], c['id'])).fetchall()
        her.append({'key': c['key'], 'id': c['id'], 'text': c['text'][:120], 'at': c['created_at'], 'title': _ks_title(db, c['key']),
                    'thread': ' / '.join(('她：' if t['author'] == 'her' else '我：') + _ks_clean(t['text'], 40) for t in reversed(th))})
    ask = [{'key': r['key'], 'title': _ks_title(db, r['key'])}
           for r in db.execute("SELECT key FROM keepsake_meta WHERE ask_me=1 AND COALESCE(pre_me,'')='' ORDER BY updated_at DESC LIMIT 2").fetchall()]
    # 只看「她写了 ta 没回」会漏掉一种：你新存了泡发给 ta，底下根本还没有评论可回
    fresh = []
    for r in db.execute("SELECT id,title,lines_json,created_at FROM keepsakes k WHERE collector='her' "
                        "AND created_at > datetime('now','-14 days') "
                        "AND NOT EXISTS (SELECT 1 FROM keepsake_comments c WHERE c.key='k:'||k.id AND c.author='me' AND c.removed_at IS NULL) "
                        "ORDER BY id DESC LIMIT 2").fetchall():
        try: ls = json.loads(r['lines_json'] or '[]')
        except Exception: ls = []
        ls = [l for l in ls if not l.get('ctx')]   # 前后文不算你存的
        said = ' / '.join(('她：' if l.get('w') == 'her' else '我：') + _ks_clean(l.get('t'), 40) for l in ls[-4:])
        fresh.append({'key': f"k:{r['id']}", 'title': r['title'] or _ks_title(db, f"k:{r['id']}"), 'at': r['created_at'], 'lines': said})
    db.close()
    out = {'her': her, 'ask': ask, 'fresh': fresh}
    n = (params.get('foggy') or [''])[0]
    if n:
        items = sorted(_ks_all(), key=lambda x: x['touch'])
        out['foggy'] = items[:max(1, min(5, int(n or 1)))]
    return 200, out


def _ks_title(db, key):
    k, _, i = key.partition(':')
    meta = db.execute("SELECT title FROM keepsake_meta WHERE key=?", (key,)).fetchone()
    if meta and meta['title']: return meta['title']
    if k == 't':
        r = db.execute("SELECT quote FROM treasures WHERE id=?", (int(i),)).fetchone()
        q = next((ln for ln in ((r['quote'] if r else '') or '').split('\n') if ln.strip()), '')
        return _ks_clean(q, 12)
    r = db.execute("SELECT title,lines_json FROM keepsakes WHERE id=?", (int(i),)).fetchone()
    if not r: return ''
    if r['title']: return r['title']
    # 没起名的大泡：拿它自己存的第一句当名字，不然提醒里是一个空的《》
    try: ls = [l for l in json.loads(r['lines_json'] or '[]') if not l.get('ctx')]
    except Exception: ls = []
    return _ks_clean(ls[0].get('t', ''), 12) if ls else ''


def _ks_fog_days(db, key):
    """这颗泡最后一次被碰（存下 / 来看 / 评论）到现在几天。"""
    k, _, i = key.partition(':')
    tbl = 'treasures' if k == 't' else 'keepsakes'
    r = db.execute(f"SELECT created_at FROM {tbl} WHERE id=?", (int(i),)).fetchone()
    ts = [r[0]] if r and r[0] else []
    v = db.execute("SELECT last_visit FROM keepsake_meta WHERE key=?", (key,)).fetchone()
    if v and v[0]: ts.append(v[0])
    c = db.execute("SELECT MAX(created_at) FROM keepsake_comments WHERE key=? AND removed_at IS NULL", (key,)).fetchone()
    if c and c[0]: ts.append(c[0])
    if not ts: return None
    d = db.execute("SELECT CAST(julianday('now') - julianday(?) AS INTEGER)", (max(ts),)).fetchone()[0]
    return max(0, int(d or 0))


def keepsake_card(params):
    """聊天里那张泡泡卡要的东西：?key=t:12&cid=34 → 泡名/哪天存的/谁存的/泡里那几句/这条评论/回的是哪条。"""
    key = (params.get('key') or [''])[0]
    cid = int((params.get('cid') or ['0'])[0] or 0)
    if not _KS_KEY.match(key): return 400, {'error': 'key 不对'}
    item = next((x for x in _ks_all() if x['key'] == key), None)
    if not item: return 404, {'error': '这颗泡没了'}
    db = get_db(); _ensure_keepsakes(db)
    c = db.execute("SELECT id,author,text,created_at,parent_id,fog_days FROM keepsake_comments WHERE id=? AND key=?", (cid, key)).fetchone()
    parent = None
    if c and c['parent_id']:
        pr = db.execute("SELECT author,text FROM keepsake_comments WHERE id=?", (c['parent_id'],)).fetchone()
        if pr: parent = {'w': pr['author'], 't': pr['text']}
        elif c['parent_id'] < 0:   # 回的是珍藏那条「为什么留下」
            fn = next((n for n in item['notes'] if n['id'] == c['parent_id']), None)
            if fn: parent = {'w': 'me', 't': fn['t']}
    db.close()
    return 200, {'key': key, 'title': item['t'], 'who': item['who'], 'at': item['at'],
                 'lines': [[w, t.replace('\n', ' ')] for w, t in item['lines']][:6],
                 'comment': ({'id': c['id'], 'w': c['author'], 't': c['text'], 'at': c['created_at'], 'fog_days': c['fog_days']} if c else None),
                 'parent': parent}


# ── 最小的 HTTP 外壳（换成你自己的框架时，只搬上面那几个函数）────────────
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        try: return json.loads(self.rfile.read(n) or b'{}')
        except Exception: return {}

    def do_GET(self):
        u = urlparse(self.path); p = parse_qs(u.query)
        if u.path == '/api/keepsakes':
            return self._send(200, {'items': _ks_all(), 'now': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')})
        if u.path == '/api/keepsakes/pending': return self._send(*keepsake_pending(p))
        if u.path == '/api/keepsakes/card': return self._send(*keepsake_card(p))
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == '/api/keepsakes': return self._send(*keepsake_new(self._body()))
        if u.path == '/api/keepsakes/comments': return self._send(*keepsake_comment(self._body()))
        self._send(404, {'error': 'not found'})

    def do_PUT(self):
        if urlparse(self.path).path == '/api/keepsakes/meta': return self._send(*keepsake_meta(self._body()))
        self._send(404, {'error': 'not found'})

    def do_DELETE(self):
        m = re.match(r'^/api/keepsakes/comments/(\d+)$', urlparse(self.path).path)
        if m: return self._send(*keepsake_comment_del(int(m.group(1))))
        self._send(404, {'error': 'not found'})


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='chat.db')
    ap.add_argument('--port', type=int, default=8787)
    ap.add_argument('--uploads', default='.')
    a = ap.parse_args()
    DB_PATH, UPLOAD_ROOT = a.db, a.uploads.rstrip('/')
    db = get_db(); _ensure_keepsakes(db); db.commit(); db.close()
    print(f'keepsake on :{a.port}  db={DB_PATH}')
    ThreadingHTTPServer(('127.0.0.1', a.port), Handler).serve_forever()
