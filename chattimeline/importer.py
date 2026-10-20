"""Build the archive's index database from the files its archive.toml describes.

Reads source files only and writes only the database, rebuilt from scratch through a temporary file. Copies of one
message found in several files (a .log and its XML sidecar, a server export, a cache, a second computer) are merged
into one message that remembers every copy. Prints counts, never message text."""
import bisect, collections, datetime, os, re, sqlite3, time
from multiprocessing import Pool

from . import formats
from .config import Archive, key, fold
from .formats import MS, LINE, SESSION, GUESS
from .formats import trillian_log

# which copy's text to show when copies differ (lower wins): plain-text logs keep what was actually displayed
RANK = {'log': 0, 'irc': 0, 'client_xml': 1, 'hybrid_xml': 1, 'server_xml': 2, 'cloud_xml': 3, 'digsby': 0,
        'jabber_html': 0, 'wlm_xml': 1, 'msn_paste': 2, 'whatsapp': 0, 'icq_db': 0, 'icq_txt': 1, 'skype_xml': 0,
        'discord': 0, 'mirc': 0, 'sms_eml': 0, 'reddit': 0, 'bluesky': 0, 'instagram': 0, 'tiktok': 0, 'twitter': 0, 'tinder': 0}

SCHEMA = '''
CREATE TABLE sources(id INTEGER PRIMARY KEY, path TEXT UNIQUE, fmt TEXT, network TEXT, contact TEXT, kind TEXT, n INT);
CREATE TABLE convs(id INTEGER PRIMARY KEY, network TEXT, contact TEXT, kind TEXT, names TEXT, n INT, first REAL, last REAL,
                   UNIQUE(network, contact));
CREATE TABLE raw(id INTEGER PRIMARY KEY, src INT, pos INT, conv INT, ts REAL, prec INT, naive INT, dir TEXT,
                 speaker TEXT, hs INT, text TEXT, mid INT);
CREATE TABLE msgs(id INTEGER PRIMARY KEY, conv INT, ts REAL, prec INT, dir TEXT, speaker TEXT, rid INT, nsrc INT);
CREATE TABLE months(conv INT, month INT, n INT);     -- messages per conversation per month (month = year*12 + month-1)
CREATE VIRTUAL TABLE fts USING fts5(body, content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
'''


def conv_key(net, name, archive):
    """Canonical (network, contact): lower case, no spaces; Facebook chat ids and DeskSMS numbers unwrapped."""
    n = re.sub(r'\s', '', name).lower()
    net = archive.network_rename.get(net.lower(), net.lower())
    if n.endswith('@chat.facebook.com'):
        return 'facebook', n.split('@')[0].lstrip('-')
    if n.endswith('@desksms.appspotchat.com'):
        return 'sms', n.split('@')[0]
    return net, n


_parsers = None


def _init(root, config_path, known):
    global _parsers
    formats.set_archive(Archive(root, config_path))
    trillian_log.KNOWN_SPEAKERS.update(known)
    _parsers = formats.registry()


def work(item):
    path, fmt, opts = item
    try:
        convs = _parsers[fmt](path, opts)
    except Exception as e:                            # one unreadable file must not stop the build
        return item, 'error:' + type(e).__name__, []
    return item, convs[0][3] if convs else 'empty', convs


def dir_of(spk):
    """-> (dir, speaker). dir: 'out'/'in' when the source says so outright, 'sys' for system lines, else None
    (the viewer decides me/them from the speaker's name)."""
    m = re.match(r'^<(out|in)>', spk)
    if m:
        return m.group(1), spk[m.end():]
    if spk.startswith('<') and spk.endswith('>'):
        return 'sys', ''
    return None, spk


def compatible(a, b, long_text=False):
    """a, b: (ts, prec, naive). The same message seen by two sources?"""
    d = abs(a[0] - b[0])
    if long_text and LINE in (a[1], b[1]) and MS in (a[1], b[1]) and d >= 86340 \
            and (d % 86400 <= 60 or d % 86400 >= 86340):
        return True                                    # a replayed log line put on the wrong day
    if min(a[1], b[1]) <= SESSION:
        return d <= 14 * 3600
    if a[2] or b[2]:                                   # a wall-clock log may sit whole hours off (zone misset, DST)
        r = d % 3600
        return d <= 14 * 3600 + 120 and (r <= 120 or r >= 3480)
    return d <= 120


def merge(items):
    """items: [(raw_id, src, ts, prec, naive, rank, dir, length)] with one conversation and exact-text hash.
    -> clusters (lists of items). A cluster takes at most one message per source and never both 'in' and 'out'."""
    clusters, keys = [], []
    for it in sorted(items, key=lambda x: (-x[3], x[4], x[2] or 0)):
        ts = it[2] or 0
        long_text = it[7] >= 12 and len(items) <= 200     # long texts are rare: look whole days away too
        span = 1e10 if long_text else 14 * 3600 + 200
        lo = bisect.bisect_left(keys, (ts - span,))
        best = None
        for j in range(lo, len(keys)):
            k = keys[j]
            if k[0] > ts + span:
                break
            c = clusters[k[1]]
            if it[1] in c['srcs'] or not compatible(c['ref'], (ts, it[3], it[4]), long_text):
                continue
            if it[6] and c['dir'] and it[6] != c['dir']:
                continue
            d = abs(k[0] - ts)
            if best is None or d < best[0]:
                best = (d, k[1])
        if best:
            c = clusters[best[1]]
            c['m'].append(it); c['srcs'].add(it[1]); c['dir'] = c['dir'] or it[6]
        else:
            clusters.append({'ref': (ts, it[3], it[4]), 'm': [it], 'srcs': {it[1]}, 'dir': it[6]})
            bisect.insort(keys, (ts, len(clusters) - 1))
    return [c['m'] for c in clusters]


def fold_accounts(convs, archive, counts):
    """One account written two ways -> {conv id: conv id to fold it into}. Windows Messenger files are login + an 8-10
    digit account number ('lewis4583090981836' is lewis458@...); [[fold]] in archive.toml adds rules like x@a.com ->
    x@corp.a.com; and a person who, by people.tsv, has several accounts on one network (a renamed TikTok or Instagram
    account, a Facebook id and name) is folded into the account with the most messages, so copies of a message
    exported under two names merge."""
    by_local = collections.defaultdict(list)
    for cid, net, contact, kind in convs:
        if net == 'msn' and '@' in contact:
            by_local[contact.split('@')[0]].append(cid)
    ids = {(c[1], c[2]): c[0] for c in convs}
    alias = {}
    for cid, net, contact, kind in convs:
        if net == 'msn' and '@' not in contact:
            hits = {by_local[contact[:i]][0] for i in range(len(contact) - 10, len(contact) - 7)
                    if i > 0 and contact[i:].isdigit() and len(by_local.get(contact[:i], [])) == 1}
            if len(by_local.get(contact, [])) == 1:
                hits = {by_local[contact][0]}
            if len(hits) == 1:
                alias[cid] = hits.pop()
        for f in archive.folds:
            if net == f.get('network', net) and contact.endswith(f['suffix'].lower()):
                long_ = ids.get((net, contact[:-len(f['suffix'])] + f['into'].lower()))
                if long_:
                    alias[cid] = long_
    people, by_person = archive.load_people(), collections.defaultdict(list)
    for cid, net, contact, kind in convs:
        person = people.get((net, archive.norm_contact(net, contact)))
        if person and kind != 'channel' and cid not in alias:
            by_person[(person, net)].append(cid)
    for cids in by_person.values():
        if len(cids) > 1:
            main = max(cids, key=lambda c: counts.get(c, 0))
            alias.update({c: main for c in cids if c != main})
    return alias


def build(archive, limit=None, workers=8):
    t0 = time.time()
    items = archive.files()[:limit]
    print(f'{len(items)} source files', flush=True)
    logs = [p for p, f, o in items if f == 'trillian' and p.lower().endswith(('.log', '.txt'))]
    known = trillian_log.compute_known(logs) if logs else set()
    os.makedirs(os.path.dirname(archive.db) or '.', exist_ok=True)
    tmp = archive.db + '.new'
    for p in (tmp, tmp + '-journal'):
        if os.path.exists(p):
            os.remove(p)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    results, fmts = [], collections.Counter()
    with Pool(workers, initializer=_init, initargs=(archive.root, archive.config_path, known)) as pool:
        for i, res in enumerate(pool.imap(work, items, chunksize=4), 1):
            results.append(res)
            fmts[res[1]] += 1
            if i % 1000 == 0:
                print(f'  parsed {i}/{len(items)}', flush=True)
    print('formats:', dict(fmts), flush=True)
    me = archive.load_me()

    conv_ids, convs, raw, srcs, labels = {}, [], [], [], {}
    for (path, fmt, opts), sfmt, cl in results:
        first = cl[0] if cl else (opts.get('network', ''), opts.get('contact', ''), '', '', [])
        srcs.append((len(srcs) + 1, os.path.relpath(path, archive.root), sfmt, first[0], first[1], first[2],
                     sum(len(c[4]) for c in cl)))
        pos = 0
        for net, contact, kind, f, rows, *extra in cl:
            ck = conv_key(net, contact, archive)
            if ck not in conv_ids:
                conv_ids[ck] = len(convs) + 1
                convs.append([conv_ids[ck], *ck, kind])
            cid = conv_ids[ck]
            if extra and isinstance(extra[0], str) and extra[0]:   # a parser's own name for the other party (they may never write)
                labels[cid] = extra[0]
            for ts, prec, naive, spk, hs, text, _, _ in rows:
                d, spk = dir_of(spk)
                raw.append((len(srcs), pos, cid, ts, prec, naive, d, spk, hs, text, RANK.get(f, 2)))
                pos += 1
            rows.clear()
    del results
    alias = fold_accounts(convs, archive, collections.Counter(r[2] for r in raw))
    if alias:
        raw = [r if r[2] not in alias else r[:2] + (alias[r[2]],) + r[3:] for r in raw]
        print(f'{len(alias)} accounts written two ways folded together', flush=True)
    db.executemany('INSERT INTO sources VALUES (?,?,?,?,?,?,?)', srcs)
    print(f'{len(raw)} raw messages in {len(convs)} conversations ({time.time() - t0:.0f}s)', flush=True)

    groups = collections.defaultdict(list)
    for rid, r in enumerate(raw, 1):
        groups[(r[2], r[8])].append((rid, r[0], r[3], r[4], r[5], r[10], r[6], len(r[9] or '')))
    merged = []
    for (cid, hs), its in groups.items():
        for cl in merge(its):
            best_t = max(cl, key=lambda x: (x[3], -x[4]))             # most precise, then not wall-clock
            best_x = min(cl, key=lambda x: (x[5], -x[3]))             # preferred text source
            d = next((x[6] for x in cl if x[6]), None)
            merged.append((cid, best_t[2], best_t[3], d, best_x[0], len(cl), [x[0] for x in cl]))
    del groups
    merged.sort(key=lambda m: (m[0], m[1] or 0, raw[m[4] - 1][0], raw[m[4] - 1][1]))
    mid = [0] * (len(raw) + 1)
    stats, months = {}, collections.Counter()
    for i, m in enumerate(merged, 1):
        for x in m[6]:
            mid[x] = i
        st = stats.setdefault(m[0], [0, None, None])
        st[0] += 1
        if m[1] is not None:
            st[1] = m[1] if st[1] is None else min(st[1], m[1]); st[2] = m[1] if st[2] is None else max(st[2], m[1])
            d = datetime.datetime.fromtimestamp(m[1], datetime.timezone.utc)
            months[(m[0], d.year * 12 + d.month - 1)] += 1
    print(f'{len(merged)} merged messages; copies per message: '
          f'{dict(sorted(collections.Counter(min(m[5], 4) for m in merged).items()))} ({time.time() - t0:.0f}s)', flush=True)

    db.executemany('INSERT INTO raw(id, src, pos, conv, ts, prec, naive, dir, speaker, hs, text, mid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   ((i, *r[:10], mid[i]) for i, r in enumerate(raw, 1)))
    db.executemany('INSERT INTO msgs(id, conv, ts, prec, dir, speaker, rid, nsrc) VALUES (?,?,?,?,?,?,?,?)',
                   ((i, m[0], m[1], m[2], m[3], raw[m[4] - 1][7], m[4], m[5]) for i, m in enumerate(merged, 1)))
    db.executemany('INSERT INTO fts(rowid, body) VALUES (?,?)', ((i, fold(raw[m[4] - 1][9])) for i, m in enumerate(merged, 1)))
    db.executemany('INSERT INTO months VALUES (?,?,?)', ((c, mo, n) for (c, mo), n in months.items()))
    names = collections.defaultdict(collections.Counter)
    for r in raw:
        if r[6] != 'sys' and r[7] and not archive.is_owner(r[7], me):
            names[r[2]][r[7]] += 10 if r[6] == 'in' else 1      # a sender the source marks incoming names it best
    for cid, label in labels.items():
        names[cid][label] += 1
    db.executemany('INSERT INTO convs VALUES (?,?,?,?,?,?,?,?)',
                   ((c[0], c[1], c[2], c[3], '\t'.join(n for n, _ in names[c[0]].most_common(5)), *stats.get(c[0], (0, None, None)))
                    for c in convs))
    db.execute('CREATE INDEX msgs_conv ON msgs(conv, id)')
    db.execute('CREATE INDEX msgs_ts ON msgs(ts)')
    db.execute('CREATE INDEX raw_mid ON raw(mid)')
    db.execute('CREATE INDEX months_conv ON months(conv)')
    db.executemany('INSERT INTO meta VALUES (?, ?)', [('built', datetime.datetime.now().isoformat(timespec='seconds')),
                                                      ('root', archive.root)])
    db.commit()
    db.close()
    os.replace(tmp, archive.db)
    print(f'done in {time.time() - t0:.0f}s -> {archive.db}')
