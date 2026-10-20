"""Suggest people.tsv merges from metadata only (account ids, message hashes and lengths, times); never message text.

Evidence per pair of entries (a person from people.tsv, or a lone account):
  handle   same handle across networks, one handle extending another, or one edit apart (long handles)
  shared   distinct long incoming messages (same exact-text hash, 25+ chars) received from both
  names    speaker display names in common (top-5 per account, mine excluded)
  handoff  one goes quiet within 60 days of the other starting (only counted with other evidence)"""
import collections, itertools, re, sqlite3

from .config import key

GENERIC = {'(null)', '', 'unknown', 'guest'}


def handle(net, contact):
    c = contact.lower()
    if net in ('facebook', 'sms', 'unknown') or c.isdigit() and net != 'icq':
        return None
    if re.search(r'@(public\.talk\.google\.com|fakeskypedomain)', c):
        c = c.split('@')[0] if 'fakeskypedomain' in c else None
        if c is None:
            return None
    c = c.split('@')[0]
    c = re.sub(r'[._\-]', '', c)
    return c if len(c) >= 4 else None


def edit1(a, b):
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    s, l = (a, b) if len(a) < len(b) else (b, a)
    return any(l[:i] + l[i + 1:] == s for i in range(len(l)))


def main(archive, min_score=2.5):
    db = sqlite3.connect(f'file:{archive.db}?mode=ro', uri=True)
    me = archive.load_me()
    people = archive.load_people()
    conv = {}
    for cid, net, contact, names, n, first, last, kind in db.execute(
            'SELECT id, network, contact, names, n, first, last, kind FROM convs WHERE n>0'):
        if kind == 'channel':
            continue
        key = people.get((net, key(contact))) or f'{net}:{contact}'
        conv[cid] = (key, net, contact, [x for x in (names or '').split('\t') if key(x) not in GENERIC
                                         and not archive.is_owner(x, me) and key(x) not in me], n, first, last)
    entry = collections.defaultdict(lambda: {'cids': [], 'handles': set(), 'names': set(), 'n': 0, 'first': None, 'last': None})
    for cid, (key, net, contact, names, n, first, last) in conv.items():
        e = entry[key]
        e['cids'].append(cid); e['n'] += n
        h = handle(net, contact)
        if h:
            e['handles'].add(h)
        e['names'] |= {key(x) for x in names}
        e['first'] = first if e['first'] is None or (first and first < e['first']) else e['first']
        e['last'] = last if e['last'] is None or (last and last > e['last']) else e['last']
    of = {cid: conv[cid][0] for cid in conv}

    score = collections.defaultdict(lambda: collections.Counter())
    # 1. handles
    hs = collections.defaultdict(set)
    for k, e in entry.items():
        for h in e['handles']:
            hs[h].add(k)
    allh = sorted(hs)
    for h in allh:
        for x, y in itertools.combinations(sorted(hs[h]), 2):
            score[(x, y)]['handle same'] += 1
    for h1, h2 in itertools.combinations(allh, 2):
        if len(h1) >= 5 and len(h2) >= 5 and (h1.startswith(h2) or h2.startswith(h1)) and abs(len(h1) - len(h2)) <= 4 \
                or len(h1) >= 7 and edit1(h1, h2):
            for x in hs[h1]:
                for y in hs[h2]:
                    if x != y:
                        score[tuple(sorted((x, y)))]['handle near'] += 1
    # 2. shared long incoming messages (hash + length only)
    seen = collections.defaultdict(set)
    for cid, hs_, spk, d in db.execute("SELECT m.conv, r.hs, m.speaker, m.dir FROM msgs m JOIN raw r ON r.id=m.rid "
                                       "WHERE (m.dir='in' OR m.dir IS NULL) AND length(r.text) >= 25"):
        if cid in of and (d == 'in' or not (archive.is_owner(spk, me))):
            seen[hs_].add(of[cid])
    distinct = collections.Counter()
    for hs_, ks in seen.items():
        for k in ks:
            distinct[k] += 1
        if 2 <= len(ks) <= 2:
            x, y = sorted(ks)
            score[(x, y)]['shared'] += 1
    # 3. display names
    nm = collections.defaultdict(set)
    for k, e in entry.items():
        for x in e['names']:
            nm[x].add(k)
    for x, ks in nm.items():
        if 2 <= len(ks) <= 4 and len(x) >= 4:
            for p, q in itertools.combinations(sorted(ks), 2):
                score[(p, q)]['names'] += 1

    rows = []
    for (x, y), ev in score.items():
        if x == y:
            continue
        ex, ey = entry[x], entry[y]
        frac = ev['shared'] / max(1, min(distinct[x], distinct[y]))      # share of the smaller one's long messages
        if ev['shared']:
            ev['shared%'] = round(100 * frac)
        s = 3 * min(ev['handle same'], 1) + 2 * min(ev['handle near'], 1) + min(frac * 20, 4) * (ev['shared'] >= 3) \
            + min(ev['names'], 3)
        if ex['first'] and ey['first'] and ex['last'] and ey['last']:
            gap = min(abs(ex['last'] - ey['first']), abs(ey['last'] - ex['first'])) / 86400
            if gap <= 60 and s > 0:
                ev['handoff'] = 1; s += 0.5
        if s >= min_score:
            rows.append((s, x, y, ev))
    rows.sort(key=lambda r: -r[0])
    for s, x, y, ev in rows:
        def lab(k):
            e = entry[k]
            return f"{key(k) if ':' in k else k} ({e['n']})"
        print(f"{s:4.1f}  {lab(x)}  +  {lab(y)}   " + ', '.join(f'{k} {v}' for k, v in ev.items() if v))
    print(f'{len(rows)} proposals')

