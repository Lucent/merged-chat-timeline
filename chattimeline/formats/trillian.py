"""Trillian: .log files (and the IRC logs Trillian/mIRC write), their XML sidecars, the Trillian server history export
and the cloud-history caches. One file -> one conversation; network and contact come from the [[source]] path.

Quirks handled here (all seen in real archives):
  * Facebook/Astra replay recent history into the .log at session start, with the original times;
  * some Trillian versions mark every message of a session 'outgoing' (or every one 'incoming');
  * the server export stores many incoming messages twice, under a second apart; caches repeat whole weeks;
  * 'MSN - me.log'-style per-account files and IRC server windows only hold sign-ons and notices (kind 'status');
  * old mIRC logs with no [hh:mm] stamps ('Start of #x buffer: date', DCC 'Chat with X ... Started on date')."""
import collections, os, re

from . import MS, LINE, SESSION, GUESS, naive_to_utc, local, row, CTX
from . import trillian_log as tl
from . import trillian_xml as tx

A_HREF = re.compile(r'<a\s[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
IRC_NET_SUFFIX = re.compile(r'\.(EFnet|freenode|DALnet|Undernet|IRCnet|\w+net)$', re.I)


def html_text(t):
    t = re.sub(r'(?i)<br\s*/?>', '\n', t)
    t = A_HREF.sub(lambda m: m.group(2) if tl.unescape_all(m.group(1)) in tl.unescape_all(m.group(2))
                   else f'{m.group(2)} ({m.group(1)})', t)
    return tl.unescape_all(tl.TAG.sub('', t)).strip()


def kind_of(path, network, contact):
    fn = os.path.basename(path)
    parts = path.replace(os.sep, '/').split('/')
    if re.match(r'[A-Z]+ - ', fn) or network.lower() == 'irc' and re.match(r'(?i)status(\.|$)', contact):
        return 'status'
    if 'Channel' in parts or contact.startswith(('#', '&')) or contact.lower().startswith('group conversation'):
        return 'channel'
    return 'im'


def parse(path, opts):
    network = opts.get('network', 'unknown')
    contact = opts.get('contact') or os.path.splitext(os.path.basename(path))[0]
    if network.lower() == 'irc':
        contact = IRC_NET_SUFFIX.sub('', contact)
    kind = opts.get('kind') or kind_of(path, network, contact)
    with open(path, 'rb') as f:
        data = f.read()
    texts, kinds = [], []
    fmt = tl.text_log_format(data) if path.lower().endswith(('.log', '.txt')) else tx.sniff(path, data[:4096])
    if fmt == 'trillian_log':
        sess, msgs, _ = tl.parse_log(data, texts=texts); fmt = 'log'
    elif fmt == 'irc_log':
        sess, msgs, _ = tl.parse_irc(data, texts=texts); fmt = 'irc'
    elif fmt in ('client_xml', 'hybrid_xml', 'server_xml'):
        sess, msgs, _ = tx.parse_xml(data, texts=texts, kinds=kinds)
    elif fmt == 'cloud_xml':
        sess, msgs, _ = tx.parse_cloud(data, texts=texts)
    elif path.lower().endswith(('.log', '.txt')) and network.lower() == 'irc':
        from .other import mirc_plain
        dcc = re.match(r'Chat--(.+?),', os.path.splitext(os.path.basename(path))[0])
        return mirc_plain(path, {'network': 'irc', 'contact': dcc.group(1) if dcc else contact, 'kind': kind})
    else:
        return []
    html_src = fmt not in ('log', 'irc')
    rows, last = [], None
    kinds += [''] * (len(msgs) - len(kinds))
    for (si, ts, spk, h, n, hs), text, k in zip(msgs, texts, kinds):
        s = sess[si]
        naive = fmt in ('log', 'irc') and not s['tz']
        prec = MS if html_src else LINE
        if ts is None:
            ts = s['start'] if s['start'] is not None else s['end']
            prec = SESSION
        if ts is not None and naive:
            ts = naive_to_utc(ts)
        if ts is None:
            ts, prec = last, GUESS
        last = ts
        if k.startswith(('outgoing', 'incoming')):        # client XML says the direction outright
            spk = ('<out>' if k.startswith('outgoing') else '<in>') + spk
        st = s['start']
        if st is not None and naive:
            st = naive_to_utc(st)
        rows.append([ts, prec, int(naive), spk, hs, html_text(text) if html_src else text.rstrip(), si, st])
    if fmt in ('client_xml', 'hybrid_xml'):
        # one direction but two or more different speakers in a session: the type is wrong there; let names decide
        by_si = collections.defaultdict(list)
        for r in rows:
            by_si[r[6]].append(r)
        for rs in by_si.values():
            dirs = {r[3][:5] for r in rs if r[3].startswith(('<out>', '<in>'))}
            names = {re.sub(r'^<(out|in)>', '', r[3]) for r in rs if r[3].startswith(('<out>', '<in>'))}
            if len(dirs) == 1 and len(names) >= 2:
                for r in rs:
                    r[3] = re.sub(r'^<(out|in)>', '', r[3])
    if fmt in ('cloud_xml', 'server_xml'):
        # repeats of the same sender and text within 5 s: keep the first
        last_seen, kept = {}, []
        for r in sorted(rows, key=lambda r: r[0] or 0):
            key = (r[3], r[4])
            t = last_seen.get(key)
            last_seen[key] = r[0]
            if t is not None and r[0] is not None and r[0] - t < 5:
                continue
            kept.append(r)
        rows = kept
    if fmt in ('log', 'irc'):
        rows = drop_replays(rows)
    for r in rows:                                     # leading messages with no time: take the first known one
        if r[0] is None:
            r[0] = next((x[0] for x in rows if x[0] is not None), None)
    return [(network, contact, kind, fmt, rows)]


def drop_replays(rows):
    """History replayed into a .log (Facebook/Astra): the same sender and text as an earlier line, at the same clock
    time, a whole number of days apart (the replayed day is ambiguous), or at the same moment in another session.
    Qualifies when timed before its session began, or when 12+ characters long (a replay can land just after the
    start, or mid-session with no new header). Short lines match within 60 days, long ones at any distance. The
    earlier copy always stays, so no text is lost; same-session repeats at the same date and time are real and stay."""
    seen, kept = collections.defaultdict(list), []          # (sender, text, clock minute) -> [(ts, session)]
    for r in rows:
        ts = r[0]
        if ts is not None and r[1] >= LINE:
            minute = int(ts % 86400 // 60)
            replayed = r[7] is not None and ts < r[7] + 60
            if replayed or len(r[5]) >= 12:
                hit = False
                for m in (minute - 1, minute, minute + 1):
                    for t, s_ in seen.get((r[3], r[4], m % 1440), ()):
                        d = abs(ts - t) % 86400
                        if (s_ != r[6] or abs(ts - t) >= 86340) and (d <= 60 or d >= 86340) \
                                and (abs(ts - t) < 60 * 86400 or len(r[5]) >= 12):
                            hit = True
                            break
                    if hit:
                        break
                if hit:
                    continue
            seen[(r[3], r[4], minute)].append((ts, r[6]))
        kept.append(r)
    return kept
