"""Trillian XML formats: client XML sidecars (one per .log), the Trillian server history export, and Trillian's
cloud-history caches. parse_*(data, texts=None[, kinds=None]) -> (sessions, msgs, stats), as in trillian_log."""
import collections, os, re, urllib.parse

from .trillian_log import norm_hash, strict_hash

ATTR = re.compile(r'([a-zA-Z_]+)="([^"]*)"')
CLIENT_TAG = re.compile(rb'<(session|message|status|secureim|filetransfer)\s([^>]*?)/>')
SERVER_MSG = re.compile(rb"<message timestamp='(\d+)' from='([^']*)'>(.*?)</message>", re.S)


def parse_xml(data, texts=None, kinds=None):
    """texts: optional list receiving each message's text, aligned with msgs. kinds: same, with the client
    message type (outgoing_privateMessage, ...; '' for server-format messages)."""
    sessions, msgs = [], []
    stats = collections.Counter()
    events = []  # (pos, kind, payload)
    for m in CLIENT_TAG.finditer(data):
        kind = m.group(1).decode()
        a = dict(ATTR.findall(m.group(2).decode('utf-8', 'replace')))
        events.append((m.start(), kind, a))
    for m in SERVER_MSG.finditer(data):
        events.append((m.start(), 'smsg', m))
    events.sort(key=lambda e: e[0])
    cur = None
    for _, kind, a in events:
        if kind == 'session':
            typ = a.get('type', '').lower()
            t = int(a['time']) if a.get('time', '').isdigit() else None
            if typ == 'start':
                if cur is not None and cur['end'] is None:
                    stats['start_without_stop'] += 1
                cur = dict(start=t, end=None, tz=1, medium=a.get('medium', ''), a=a.get('from', ''), b=a.get('to', ''), n=0, raw_start_hdr=1)
                sessions.append(cur)
            else:
                if cur is None or cur['end'] is not None:
                    stats['stop_without_start'] += 1
                    cur = dict(start=None, end=t, tz=1, medium=a.get('medium', ''), a=a.get('from', ''), b=a.get('to', ''), n=0, raw_start_hdr=0)
                    sessions.append(cur)
                else:
                    cur['end'] = t
            continue
        if kind in ('status', 'secureim', 'filetransfer'):
            stats[kind] += 1
            continue
        if cur is None:
            cur = dict(start=None, end=None, tz=1, medium='', a='', b='', n=0, raw_start_hdr=0)
            sessions.append(cur)
            stats['msgs_outside_session'] += 1
        si = len(sessions) - 1
        if kind == 'smsg':
            ts = int(a.group(1)) / 1000
            spk = a.group(2).decode('utf-8', 'replace')
            text = a.group(3).decode('utf-8', 'replace')
            stats['server_msgs'] += 1
        else:
            typ = a.get('type', '')
            if 'information' in typ or 'system' in typ:
                stats['info_msgs'] += 1
                continue
            ts = int(a['time']) + int(a.get('ms', 0) or 0) / 1000 if a.get('time', '').isdigit() else None
            spk = urllib.parse.unquote(a.get('from', ''))
            text = urllib.parse.unquote(a.get('text', ''))
            stats['client_msgs'] += 1
            stats['type:' + typ] += 1
        h, n = norm_hash(text, html_src=True)
        msgs.append((si, ts, spk, h, n, strict_hash(text, html_src=True)))
        if texts is not None:
            texts.append(text)
        if kinds is not None:
            kinds.append('' if kind == 'smsg' else typ)
        cur['n'] += 1
    return sessions, msgs, stats


CLOUD_MSG = re.compile(rb'<message time="(\d+)" type="([^"]*)" text="([^"]*)"')
CLOUD_HDR = re.compile(rb'<(history|log) version=')


def parse_cloud(data, texts=None):
    """Trillian cloud-history cache. One pseudo-session per <history>/<log> week header.
    Speaker is only direction (in/out); may also contain pasted server-format messages."""
    sessions, msgs = [], []
    stats = collections.Counter()
    ev = [(m.start(), 'h', m) for m in CLOUD_HDR.finditer(data)] + \
         [(m.start(), 'c', m) for m in CLOUD_MSG.finditer(data)] + \
         [(m.start(), 's', m) for m in SERVER_MSG.finditer(data)]
    ev.sort(key=lambda e: e[0])
    for _, k, m in ev:
        if k == 'h':
            stats['week_headers'] += 1
            sessions.append(dict(start=None, end=None, tz=1, medium='', a='', b='', n=0, raw_start_hdr=0))
            continue
        if not sessions:
            sessions.append(dict(start=None, end=None, tz=1, medium='', a='', b='', n=0, raw_start_hdr=0))
        if k == 'c':
            ts = int(m.group(1)) / 1000
            typ = m.group(2).decode()
            spk = '<out>' if typ.startswith('outgoing') else '<in>' if typ.startswith('incoming') else '<' + typ + '>'
            text = urllib.parse.unquote(m.group(3).decode('utf-8', 'replace'))
            stats['cloud_msgs'] += 1
        else:
            ts = int(m.group(1)) / 1000
            spk = m.group(2).decode('utf-8', 'replace')
            text = m.group(3).decode('utf-8', 'replace')
            stats['server_msgs'] += 1
        h, n = norm_hash(text, html_src=True)
        hs = strict_hash(text, html_src=True)
        cur = sessions[-1]
        cur['start'] = ts if cur['start'] is None else min(cur['start'], ts)
        cur['end'] = ts if cur['end'] is None else max(cur['end'], ts)
        msgs.append((len(sessions) - 1, ts, spk, h, n, hs))
        if texts is not None:
            texts.append(text)
        cur['n'] += 1
    return sessions, msgs, stats


def sniff(path, head):
    low = path.lower()
    if low.endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.heic', '.mp4', '.webp')):
        return 'media'
    if low.endswith(('.tar', '.xz', '.zip')):
        return 'archive'
    if b'Session Start (' in head or b'Session Close (' in head:
        return 'trillian_log'
    if b'<history version=' in head[:300] or b'<log version=' in head[:300]:
        return 'cloud_xml'
    if re.search(rb'(?m)^Session (Start|Close|Ident):', head):
        return 'irc_log'
    if b"<message timestamp='" in head and b'<session ' in head:
        return 'hybrid_xml'
    if b"<message timestamp='" in head or (b'<messages>' in head[:200]):
        return 'server_xml'
    if b'<session ' in head or b'<message type=' in head:
        return 'client_xml'
    if b'<asset ' in head:
        return 'assets_xml'
    if low.endswith('.xml') and len(head.strip()) == 0:
        return 'empty'
    ext = os.path.splitext(low)[1].lstrip('.') or 'noext'
    return 'other_' + ext
