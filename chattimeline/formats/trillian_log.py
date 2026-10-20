"""Lossless parsers for Trillian IM .log files and Trillian/mIRC-style IRC logs.

Every physical line lands in exactly one category, counted in stats['lines_*'] (they sum to
stats['lines_total']). Nothing is dropped: system lines, orphans and unparseable headers become
messages with speakers '<system>', '<orphan>', '<badheader>'.

parse_*(data, spans=None) -> (sessions, msgs, stats)
  sessions: dicts(start, end, tz, medium, a, b, n, raw_start_hdr)
  msgs:     (session_index, ts, speaker, hash, normalized_length)
  spans:    optional list receiving (first_line, last_line) per message, 0-based
"""
import re, html, hashlib, datetime, collections, urllib.parse, unicodedata
from multiprocessing import Pool

LINK = re.compile(r'\(Link: [^)]*\)')
TAG = re.compile(r'<[^>]+>')
HREF = re.compile(r'<a\s[^>]*?href="([^"]*)"', re.I)
NONALNUM = re.compile(r'[\W_]+', re.U)
MIRC = re.compile(r'\x03(?:\d{1,2}(?:,\d{1,2})?)?|[\x02\x0f\x16\x1d\x1f]')


def unescape_all(t):
    """HTML-unescape until stable (Trillian sometimes stores entities escaped twice: &amp;lt;)."""
    for _ in range(4):
        u = html.unescape(t)
        if u == t:
            break
        t = u
    return t


def strip_links(t):
    """Remove Trillian's '(Link: URL)' insertions, but keep the URL if it would otherwise vanish
    (image-only messages are logged as a bare '(Link: URL)')."""
    base = LINK.sub('', t)
    extra = [m.group(0)[7:-1] for m in LINK.finditer(t) if m.group(0)[7:-1] not in base]
    return base + (' ' + ' '.join(extra) if extra else '')


URL = re.compile(r'(?:https?|ftp)://[^\s<>"\']+', re.I)


def norm_text(text, html_src=False):
    """Normalized comparison key: letters/digits of the text, then the message's distinct URLs in a
    canonical form (percent-decoded, lowercased, alnum only). Link markup differs by source --
    '.log: (Link: U)U', '(Link: U)' for images, XML '<a href="U">shown</a>' -- and all reduce to
    'text + {U}'. html_src: text is HTML (XML sidecars, server/cloud history): hrefs are kept, tags
    stripped, entities unescaped; plain .log/IRC text is literal ('<yawn>' is text, not a tag)."""
    if html_src:
        hrefs = [unescape_all(u) for u in HREF.findall(text)]
        t = unescape_all(TAG.sub(' ', text)) + ' ' + ' '.join(hrefs)
    else:
        t = strip_links(text)
    urls, seen = [], set()
    for u in URL.findall(t):
        c = NONALNUM.sub('', urllib.parse.unquote(u.rstrip('.,;:!?)')).lower())
        if c and c not in seen:
            seen.add(c)
            urls.append(c)
    body = NONALNUM.sub('', URL.sub(' ', t)).lower()
    # drop URLs that are a prefix of another (display text cut short)
    urls = [u for u in urls if not any(v != u and v.startswith(u) for v in urls)]
    return body + ''.join(sorted(urls))


def strict_text(text, html_src=False):
    """Exact comparison key: same link/URL canonicalization as norm_text, but keeps case, punctuation,
    emoji and symbols; only whitespace is collapsed and Unicode NFC-normalized."""
    if html_src:
        hrefs = [unescape_all(u) for u in HREF.findall(text)]
        t = unescape_all(TAG.sub(' ', text)) + ' ' + ' '.join(hrefs)
    else:
        t = strip_links(text)
    urls, seen = [], set()
    for u in URL.findall(t):
        c = NONALNUM.sub('', urllib.parse.unquote(u.rstrip('.,;:!?)')).lower())
        if c and c not in seen:
            seen.add(c)
            urls.append(c)
    urls = [u for u in urls if not any(v != u and v.startswith(u) for v in urls)]
    body = ' '.join(unicodedata.normalize('NFC', URL.sub(' ', t)).split())
    return body + ' ' + ' '.join(sorted(urls)) if urls else body


def strict_hash(text, html_src=False):
    t = strict_text(text, html_src)
    return int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(), 'big', signed=True) if t else 0


def norm_hash(text, html_src=False):
    t = norm_text(text, html_src)
    if not t:
        return 0, 0
    return int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(), 'big', signed=True), len(t)


MON = {m: i for i, m in enumerate('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(), 1)}
_DATE = r'\w{3} (\w{3}) +(\d+) (\d\d):(\d\d):(\d\d) (\d{4})'
HDR = re.compile(r'^Session (Start|Close) \((.*)\): ' + _DATE + r'(?: ([+-]\d{4}))?\s*$')
IRC_HDR = re.compile(r'^Session (Start|Close): ' + _DATE + r'\s*$')
IRC_META = re.compile(r'^Session (Ident|Time): ')
TZ_SPLIT = re.compile(r'^ ([+-]\d{4})(.*)$')
LINE_TS = re.compile(r'^\[?(\d\d?):(\d\d)(?::(\d\d))?(?: ?([AaPp][Mm]))?\]? ')
NICK = re.compile(r'^(?:<([^>]{1,60})> |([^\s:<][^:]{0,60}?): )')
IRC_MSG = re.compile(r'^\[(\d\d):(\d\d)(?::(\d\d))?\] <([^>]+)> ')
IRC_TS = re.compile(r'^\[(\d\d):(\d\d)(?::(\d\d))?\] ')
IRC_MSG_NOTS = re.compile(r'^<([^>\s]{1,40})> ')
IRC_SYS_NOTS = re.compile(r'^(?:-|\*)')

KNOWN_SPEAKERS = set()   # archive-wide speakers; the importer fills it (compute_known) before parsing


def _epoch(mon, day, hh, mi, ss, yr):
    return int(datetime.datetime(int(yr), MON[mon], int(day), int(hh), int(mi), int(ss),
                                 tzinfo=datetime.timezone.utc).timestamp())


def _tz_seconds(tz):
    return (int(tz[1:3]) * 60 + int(tz[3:])) * 60 * (1 if tz[0] == '+' else -1)


def hdr_time(m):
    """-> (epoch-or-naive-seconds, has_tz). Naive = seconds as if local were UTC."""
    t = _epoch(*m.group(3, 4, 5, 6, 7, 8))
    tz = m.group(9)
    return (t - _tz_seconds(tz), 1) if tz else (t, 0)


def unclosed_open(name):
    """True if an opening bracket is never closed ('(Link', '(Note'). Extra closers are fine (';)')."""
    depth = 0
    for ch in name:
        if ch in '([{':
            depth += 1
        elif ch in ')]}' and depth:
            depth -= 1
    return depth > 0


def speaker_ok(name):
    return not unclosed_open(name) and not name.startswith('(Link')


def _speaker_key(name):
    return re.sub(r'\W', '', name).lower()


def _clean(raw):
    s = raw.decode('utf-8', 'replace').rstrip('\r')
    if s.startswith('﻿'):
        s = s[1:]
    return MIRC.sub('', s)


def _strip_ts(s):
    mt = LINE_TS.match(s)
    return (s[mt.end():], mt) if mt else (s, None)


def _colon_prefixes(body, limit=240):
    """All prefixes of body that end right before a ': ' (candidate display names containing colons)."""
    i = body.find(': ')
    while 0 < i <= limit:
        yield body[:i]
        i = body.find(': ', i + 1)


def candidate_speakers(lines):
    """Counter of 'Nick: ' names and of longer colon-containing prefixes, over non-header lines."""
    nick, pref = collections.Counter(), collections.Counter()
    prev_hdr_no_tz = False
    for s in lines:
        if s.startswith('Session '):
            m = HDR.match(s)
            if m:
                prev_hdr_no_tz = m.group(9) is None
                continue
            if IRC_HDR.match(s) or IRC_META.match(s):
                continue
        if prev_hdr_no_tz:
            mz = TZ_SPLIT.match(s)
            if mz:
                s = mz.group(2)
        prev_hdr_no_tz = False
        body, _ = _strip_ts(s)
        if not body or body[0].isspace() or body.startswith(('*** ', '* ')):
            continue
        mn = NICK.match(body)
        if mn:
            nick[mn.group(1) or mn.group(2)] += 1
        for p in _colon_prefixes(body):
            if ':' in p or len(p) > 60:
                pref[p] += 1
    return nick, pref


def parse_log(data, spans=None, texts=None):
    lines = [_clean(r) for r in data.split(b'\n')]
    nick, pref = candidate_speakers(lines)
    participants = set()
    for s in lines:
        m = HDR.match(s) if s.startswith('Session ') else None
        if m:
            inner = m.group(2)
            rest = inner.partition(' - ')[2] if ' - ' in inner else inner
            participants.update(_speaker_key(p) for p in rest.split(':'))
    real = {n for n, c in nick.items()
            if speaker_ok(n) and (c >= 2 or _speaker_key(n) in participants or n in KNOWN_SPEAKERS)}
    # per-session line style: do message lines carry a [hh:mm] prefix? A 'Name: ' line whose style is
    # the minority (<10%) in a session of >=5 such lines is pasted text, e.g. a copied '[23:31] X: ...'
    seg_style = []
    for s in lines:
        if s.startswith('Session ') and HDR.match(s) and s.startswith('Session Start'):
            seg_style.append([0, 0])
            continue
        body, mt = _strip_ts(s)
        if seg_style and NICK.match(body) and not body.startswith(('*** ', '* ')):
            seg_style[-1][1 if mt else 0] += 1
    seg = -1
    colon_names = {p for p, c in pref.items() if c >= 2 and speaker_ok(p) and not p[0].isspace()}

    sessions, msgs = [], []
    clocks = []             # per message: local seconds-of-day from its [hh:mm] stamp, or None
    stats = collections.Counter()
    cur = None
    pending = None          # [sess_idx, ts, spk, [lines], first_lineno]
    last_hdr = None         # (kind, match) of the header on the previous line, if it lacked a tz

    def flush():
        nonlocal pending
        if pending:
            si, ts, spk, body_lines, first = pending[:5]
            clocks.append(pending[5] if len(pending) > 5 else None)
            if spans is not None:
                spans.append((first, first + len(body_lines) - 1))
            joined = '\n'.join(body_lines)
            h, n = norm_hash(joined)
            msgs.append((si, ts, spk, h, n, strict_hash(joined)))
            if texts is not None:
                texts.append(joined)
            sessions[si]['n'] += 1
            pending = None

    def new_session(**kw):
        nonlocal cur
        cur = dict(start=None, end=None, tz=0, medium='', a='', b='', n=0, raw_start_hdr=0)
        cur.update(kw)
        sessions.append(cur)

    def ensure_session():
        if cur is None:
            stats['text_before_first_session'] += 1
            new_session()

    def line_ts(mt):
        hh, mi, ss, ap = mt.groups()
        hh = int(hh) % 12 + (12 if ap.upper() == 'PM' else 0) if ap else int(hh)
        base = cur['start'] if cur['start'] is not None else cur['end']
        if base is None:
            return None
        off = cur.get('off', 0)              # line clocks are local: work on the local day, then back to UTC
        last = cur.get('last')
        ref = (last if last is not None else base) + off
        ts = ref - ref % 86400 + hh * 3600 + int(mi) * 60 + int(ss or 0)
        if last is None:
            # first timed line: up to 20 h before the session start is history playback (Facebook/Astra replay
            # recent messages with their original times); further back means it is just past midnight
            if ts < ref - 20 * 3600:
                ts += 86400
        elif ts < ref - 60:                  # the clock went backwards: midnight passed
            ts += 86400
        cur['last'] = ts - off
        return ts - off

    def content(s, lineno):
        """Handle a non-header, non-blank line (or remainder of a tz-split line)."""
        nonlocal pending
        ensure_session()
        si = len(sessions) - 1
        body, mt = _strip_ts(s)
        ts = line_ts(mt) if mt else None
        clk = None
        if mt:
            hh_, mi_, ss_, ap_ = mt.groups()
            clk = (int(hh_) % 12 + (12 if ap_.upper() == 'PM' else 0) if ap_ else int(hh_)) * 3600 + int(mi_) * 60 + int(ss_ or 0)
        if mt:
            stats['line_ts'] += 1
        if body.startswith(('*** ', '* ')):
            flush()
            pending = [si, ts, '<system>', [body], lineno, clk]   # may continue (multi-line auto-responses)
            return 'system'
        if not body[:1].isspace():
            mn = NICK.match(body)
            spk = (mn.group(1) or mn.group(2)) if mn else None
            if spk is not None and pending and 0 <= seg < len(seg_style):
                plain_n, ts_n = seg_style[seg]
                mine, other = (ts_n, plain_n) if mt else (plain_n, ts_n)
                if mine + other >= 5 and mine * 10 < (mine + other):
                    stats['demoted_style'] += 1
                    spk = None
            if spk is not None and spk in real:
                flush()
                pending = [si, ts, spk, [body[mn.end():]], lineno, clk]
                return 'msgstart'
            for p in sorted(_colon_prefixes(body), key=len, reverse=True):
                if p in colon_names:
                    flush()
                    pending = [si, ts, p, [body[len(p) + 2:]], lineno, clk]
                    return 'msgstart_colonname'
            if spk is not None:
                stats['demoted_speaker:' + spk[:30]] += 1
        if pending:
            pending[3].append(body)
            return 'continuation'
        pending = [si, ts, '<orphan>', [body], lineno, clk]
        return 'orphan'

    for lineno, s in enumerate(lines):
        prev_hdr, last_hdr = last_hdr, None
        if s.startswith('Session '):
            m = HDR.match(s)
            if m:
                flush()
                stats['lines_header'] += 1
                t, tz = hdr_time(m)
                off = _tz_seconds(m.group(9)) if m.group(9) else 0
                if m.group(1) == 'Start':
                    seg += 1
                    if cur is not None and cur['end'] is None:
                        stats['start_without_close'] += 1
                    inner = m.group(2)
                    med, _, rest = inner.partition(' - ') if ' - ' in inner else ('', '', inner)
                    a, _, b = rest.partition(':')
                    new_session(start=t, tz=tz, off=off, medium=med, a=a, b=b, raw_start_hdr=1)
                elif cur is None or cur['end'] is not None:
                    stats['close_without_start'] += 1
                    new_session(end=t, tz=tz, off=off, b=m.group(2))
                else:
                    cur['end'] = t
                if not tz:
                    last_hdr = (m.group(1), cur)
                continue
            mi = IRC_HDR.match(s)
            if mi:
                flush()
                stats['lines_header_irc'] += 1
                t = _epoch(*mi.group(2, 3, 4, 5, 6, 7))
                if mi.group(1) == 'Start':
                    new_session(start=t, medium='IRC', raw_start_hdr=1)
                elif cur is None or cur['end'] is not None:
                    stats['close_without_start'] += 1
                    new_session(end=t, medium='IRC')
                else:
                    cur['end'] = t
                continue
            if IRC_META.match(s):
                flush()
                ensure_session()
                stats['lines_irc_meta'] += 1
                pending = [len(sessions) - 1, None, '<system>', [s], lineno]
                flush()
                continue
            if s.startswith(('Session Start', 'Session Close')):
                flush()
                ensure_session()
                stats['lines_badheader'] += 1
                pending = [len(sessions) - 1, None, '<badheader>', [s], lineno]
                flush()
                continue
        if prev_hdr:
            mz = TZ_SPLIT.match(s)
            if mz:
                # Trillian (late 2008) wrote the header's ' -0500' at the start of the next line
                kind, sess = prev_hdr
                off = _tz_seconds(mz.group(1))
                if kind == 'Start' and sess['start'] is not None:
                    sess['start'] -= off
                elif sess['end'] is not None:
                    sess['end'] -= off
                sess['tz'] = 1
                sess['off'] = off
                stats['lines_tzsplit'] += 1
                if mz.group(2).strip():
                    content(mz.group(2), lineno)
                continue
        if not s.strip():
            stats['lines_blank'] += 1
            if pending:
                pending[3].append(s)
            continue
        stats['lines_' + content(s, lineno)] += 1
    flush()
    # Sessions with a close time: assign days walking back from the close, so history replayed at session start
    # (Facebook/Astra, original times, possibly from earlier days) and midnight rollovers both land on the right day.
    by_sess = collections.defaultdict(list)
    for i, m in enumerate(msgs):
        if clocks[i] is not None:
            by_sess[m[0]].append(i)
    for si, idx in by_sess.items():
        sess = sessions[si]
        if sess['end'] is None or sess['start'] is None or sess['end'] < sess['start']:
            continue
        off = sess.get('off', 0)
        nxt = sess['end'] + off + 60                 # local time bound for the following line
        for i in reversed(idx):
            t = nxt - nxt % 86400 + clocks[i]
            if t > nxt:
                t -= 86400
            nxt = t + 60
            if msgs[i][1] != t - off:
                stats['ts_backdated'] += 1
                msgs[i] = (msgs[i][0], t - off) + msgs[i][2:]
    stats['lines_total'] = len(lines)
    return sessions, msgs, stats


def parse_irc(data, spans=None, texts=None):
    lines = [_clean(r) for r in data.split(b'\n')]
    sessions, msgs = [], []
    stats = collections.Counter()
    cur, pending = None, None

    def flush():
        nonlocal pending
        if pending:
            si, ts, spk, body_lines, first = pending
            if spans is not None:
                spans.append((first, first + len(body_lines) - 1))
            joined = '\n'.join(body_lines)
            h, n = norm_hash(joined)
            msgs.append((si, ts, spk, h, n, strict_hash(joined)))
            if texts is not None:
                texts.append(joined)
            sessions[si]['n'] += 1
            pending = None

    def ensure_session():
        nonlocal cur
        if cur is None:
            stats['text_before_first_session'] += 1
            cur = dict(start=None, end=None, tz=0, medium='IRC', a='', b='', n=0, raw_start_hdr=0)
            sessions.append(cur)

    def ts_of(mt):
        base = cur['start'] if cur['start'] is not None else cur['end']
        if base is None:
            return None
        ts = base - base % 86400 + int(mt.group(1)) * 3600 + int(mt.group(2)) * 60 + int(mt.group(3) or 0)
        return ts + 86400 if ts < base - 60 else ts

    for lineno, s in enumerate(lines):
        m = IRC_HDR.match(s)
        if m:
            flush()
            stats['lines_header'] += 1
            t = _epoch(*m.group(2, 3, 4, 5, 6, 7))
            if m.group(1) == 'Start':
                if cur is not None and cur['end'] is None:
                    stats['start_without_close'] += 1
                cur = dict(start=t, end=None, tz=0, medium='IRC', a='', b='', n=0, raw_start_hdr=1)
                sessions.append(cur)
            elif cur is not None and cur['end'] is None:
                cur['end'] = t
            else:
                stats['close_without_start'] += 1
                cur = dict(start=None, end=t, tz=0, medium='IRC', a='', b='', n=0, raw_start_hdr=0)
                sessions.append(cur)
            continue
        if not s.strip():
            stats['lines_blank'] += 1
            if pending:
                pending[3].append(s)
            continue
        ensure_session()
        si = len(sessions) - 1
        if IRC_META.match(s):
            flush()
            stats['lines_meta'] += 1
            if s.startswith('Session Ident:'):
                cur['b'] = s.split(':', 1)[1].strip()[:80]
            pending = [si, None, '<system>', [s], lineno]
            flush()
            continue
        mm = IRC_MSG.match(s)
        if mm:
            flush()
            stats['lines_msgstart'] += 1
            pending = [si, ts_of(mm), mm.group(4), [s[mm.end():]], lineno]
            continue
        mt = IRC_TS.match(s)
        if mt:
            flush()
            stats['lines_system'] += 1   # joins, quits, modes, actions, server notices
            pending = [si, ts_of(mt), '<system>', [s[mt.end():]], lineno]
            flush()
            continue
        mn = IRC_MSG_NOTS.match(s)
        if mn:
            flush()
            stats['lines_msgstart_nots'] += 1
            pending = [si, None, mn.group(1), [s[mn.end():]], lineno]
            continue
        if IRC_SYS_NOTS.match(s):
            flush()
            stats['lines_system_nots'] += 1   # server notices / MOTD lines / actions without timestamps
            pending = [si, None, '<system>', [s], lineno]
            flush()
            continue
        if pending:
            stats['lines_continuation'] += 1
            pending[3].append(s)
        else:
            stats['lines_orphan'] += 1
            pending = [si, None, '<orphan>', [s], lineno]
    flush()
    stats['lines_total'] = len(lines)
    return sessions, msgs, stats


IM_HDR_ANY = re.compile(rb'(?m)^(?:\xef\xbb\xbf)?Session (?:Start|Close) \(')
IRC_HDR_ANY = re.compile(rb'(?m)^Session (?:Start|Close): ')


def text_log_format(data):
    """'trillian_log', 'irc_log', or None, by which header style dominates the whole file."""
    im, irc = len(IM_HDR_ANY.findall(data)), len(IRC_HDR_ANY.findall(data))
    if not im and not irc:
        return None
    return 'trillian_log' if im >= irc else 'irc_log'


def _cand_work(path):
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return collections.Counter()
    if text_log_format(data) != 'trillian_log':
        return collections.Counter()
    nick, _ = candidate_speakers([_clean(r) for r in data.split(b'\n')])
    return nick


def compute_known(paths):
    """Speakers occurring at least twice in at least two files, never an unclosed-bracket fragment."""
    logs = [p for p in paths if p.lower().endswith(('.log', '.txt'))]
    files_with = collections.Counter()
    with Pool(16) as pool:
        for c in pool.imap_unordered(_cand_work, logs, chunksize=16):
            for n, k in c.items():
                if k >= 2:
                    files_with[n] += 1
    return {n for n, k in files_with.items() if k >= 2 and speaker_ok(n)}
