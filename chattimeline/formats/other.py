"""Chat log formats other than Trillian. Each parser(path, opts) -> [(network, contact, kind, fmt, rows)] (see
formats/__init__.py). opts come from the matching [[source]] in archive.toml."""
import collections, datetime, email, email.utils, glob, html, html.parser, json, os, re
import xml.etree.ElementTree as ET

from ..config import key
from . import MS, LINE, SESSION, GUESS, CTX, local, h24, row
from . import trillian_log as tl


def plain(h):
    """HTML fragment -> display text (links kept as text, <br> as newlines)."""
    h = re.sub(r'(?i)<br\s*/?>', '\n', h)
    h = re.sub(r'(?is)<a\s[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
               lambda m: m.group(2) if tl.unescape_all(m.group(1)) in tl.unescape_all(m.group(2))
               else f'{m.group(2)} ({m.group(1)})', h)
    return tl.unescape_all(re.sub(r'<[^>]+>', '', h)).strip()


# ---------------- Digsby HTML (AIM / MSN / Yahoo / ICQ / Jabber accounts) ----------------
DIGSBY_MSG = re.compile(r"<span class='(ImSend|ImReceive)'>\[(\d\d?):(\d\d)(?::(\d\d))?(?: ?([AP]M))?\] (.*?)</span>: (.*?)<br ?/?>"
                        r"(?=\s*(?:<span class='Im|</body>|</html>|$))", re.S | re.I)


def digsby(path, opts):
    """Digsby logs: <service>/<account>/<buddy>/YYYY-MM-DD HH.MM.SS.html (ImSend is the account owner), and its newer
    XHTML pages (div.message outgoing/incoming with UTC timestamps). Source keys: network, contact."""
    s = open(path, encoding='utf-8', errors='replace').read()
    if "class='ImSend'" not in s and "class='ImReceive'" not in s and ('class="outgoing message"' in s or 'class="incoming message"' in s):
        return _digsby_xhtml(path, opts, s)
    net = opts.get('network', 'unknown').lower()
    buddy = opts.get('contact') or os.path.basename(os.path.dirname(path))
    m = re.match(r'(\d{4})-(\d\d)-(\d\d) (\d\d)\.(\d\d)\.(\d\d)', os.path.basename(path))
    start = local(*map(int, m.groups())) if m else None
    rows, last = [], start
    for mm in DIGSBY_MSG.finditer(s):
        side, hh, mi, ss, ap, name, body = mm.groups()
        ts = None
        if start is not None:
            d = datetime.datetime.fromtimestamp(last, CTX.zone)
            ts = local(d.year, d.month, d.day, h24(hh, ap), int(mi), int(ss or 0))
            if ts < last - 60:                       # past midnight
                d += datetime.timedelta(days=1)
                ts = local(d.year, d.month, d.day, h24(hh, ap), int(mi), int(ss or 0))
            last = ts
        spk = ('<out>' if side == 'ImSend' else '<in>') + plain(name)
        rows.append(row(ts, LINE if ts else GUESS, 1, spk, body, True, plain(body), 0, start))
    return [(net, buddy, 'im', 'digsby', rows)] if rows else []


# ---------------- Jabber XHTML (div.message incoming/outgoing with timestamp) ----------------
JAB_MSG = re.compile(r'<div class="(outgoing|incoming) message"[^>]*?timestamp="(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)"[^>]*>'
                     r'(.*?)</div>', re.S)


def _digsby_xhtml(path, opts, s):
    buddy = opts.get('contact') or os.path.basename(os.path.dirname(path))
    rows = []
    for mm in JAB_MSG.finditer(s):
        side, *t, inner = mm.groups()
        name = re.search(r'<span class="buddy"[^>]*>(.*?)</span>', inner, re.S)
        body = re.search(r'<span class="msgcontent"[^>]*>(.*)</span>\s*$', inner, re.S)
        b = body.group(1) if body else ''
        spk = ('<out>' if side == 'outgoing' else '<in>') + (plain(name.group(1)) if name else '')
        ts = datetime.datetime(*map(int, t), tzinfo=datetime.timezone.utc).timestamp()   # page converts UTC to local on load
        rows.append(row(ts, LINE, 0, spk, b, True, plain(b)))
    return [(opts.get('network', 'jabber').lower(), buddy, 'im', 'jabber_html', rows)] if rows else []


# ---------------- Windows Live Messenger XML ----------------
_wlm_mine = {}


def wlm_owner_names(path):
    """The log owner's friendly names: those in 3+ different files under the same parent (one file per contact)."""
    base = os.path.dirname(os.path.dirname(path))
    if base not in _wlm_mine:
        files = collections.Counter()
        for f in glob.glob(os.path.join(glob.escape(base), '*', '*.xml')) + glob.glob(os.path.join(glob.escape(base), '*.xml')):
            try:
                names = {u.get('FriendlyName') for u in ET.parse(f).iter('User')}
            except ET.ParseError:
                continue
            files.update(n for n in names if n)
        _wlm_mine[base] = {n for n, c in files.items() if c >= 3}
    return _wlm_mine[base]


def wlm_xml(path, opts):
    """Windows Live Messenger message history XML. The file name is the contact's login plus an 8-10 digit account
    number; the importer matches it to known logins."""
    contact = opts.get('contact') or os.path.splitext(os.path.basename(path))[0]
    mine = wlm_owner_names(path)
    rows = []
    for el in ET.parse(path).getroot():
        dt = el.get('DateTime')
        ts = datetime.datetime.fromisoformat(dt.replace('Z', '+00:00')).timestamp() if dt else None
        frm = el.find('From/User')
        name = frm.get('FriendlyName', '') if frm is not None else ''
        text = ''.join((el.find('Text').itertext())) if el.find('Text') is not None else ''
        if el.tag != 'Message':
            spk, text = '<system>', f'[{el.tag}] {text}'.strip()
        else:
            spk = ('<out>' if name in mine else '<in>') + name
        rows.append(row(ts, MS, 0, spk, text))
    return [(opts.get('network', 'msn'), contact, 'im', 'wlm_xml', rows)] if rows else []


# ---------------- MSN Messenger pastes ("Name says:") ----------------
SAYS = re.compile(r'^\s*(.{1,80}?) says(?: \((\d\d?):(\d\d)(?::(\d\d))?(?: ?([AP]M))?\))?:\s*$', re.I)


def msn_paste(path, opts):
    """Text copied out of an MSN Messenger window ('Name says:' then the message). No times of its own: the date comes
    from the file name 'YYYY-MM[-DD] Name.txt' if present. Source keys: contact (else from the file name)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r'(\d{4})-(\d\d)(?:-(\d\d))?\s+(.*)', stem)
    who = re.sub(r'\s*\(.*?\)|\s+\d+$', '', m.group(4) if m else stem).strip()
    start = local(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)) if m else None
    prec = SESSION if start else GUESS
    rows, cur = [], None
    for line in open(path, encoding='utf-8', errors='replace').read().replace('\r', '').split('\n'):
        mm = SAYS.match(line)
        if mm:
            if cur:
                rows.append(cur)
            name = mm.group(1).strip()
            ts = start
            if mm.group(2) and start:
                d = datetime.datetime.fromtimestamp(start, CTX.zone)
                ts = local(d.year, d.month, d.day, h24(mm.group(2), mm.group(5)), int(mm.group(3)), int(mm.group(4) or 0))
            cur = [ts, LINE if mm.group(2) else prec, 1, name, []]
        elif cur is not None:
            cur[4].append(line.strip())
        elif line.strip():
            rows.append([start, prec, 1, '<system>', [line.strip()]])
    if cur:
        rows.append(cur)
    out = []
    for ts, p, nv, spk, lines in rows:
        text = '\n'.join(lines).strip()
        out.append(row(ts, p, nv, spk, text))
    return [(opts.get('network', 'msn'), opts.get('contact') or key(who), 'im', 'msn_paste', out)] if out else []


# ---------------- WhatsApp: phone export and the "(… dump)" files ----------------
WA_EXPORT = re.compile(r'^‎?(\d{1,2})/(\d{1,2})/(\d{2,4}),? (\d{1,2}):(\d\d)(?::(\d\d))?\s?([AaPp]\.?[Mm]\.?)? [-–] (.*)$')
WA_DUMP = re.compile(r'^\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)(?:\.\d+)?\] ([^:]+?): ?(.*)$')


def whatsapp(path, opts):
    """WhatsApp 'Export chat' text ('M/D/YY, H:MM AM - Name: text') and '[YYYY-MM-DD HH:MM:SS.mmm] jid: text' dumps.
    File name 'WhatsApp <name> <+phone>[ (...)].txt' gives the contact; '(group)' in it marks a group. Source keys:
    contact, phone, group."""
    fn = os.path.basename(path)
    m = re.match(r'WhatsApp (.+?)(?: (\+[\d ()\-]+))?(?: \(.*\))?\.txt$', fn)
    name, phone = (m.group(1), re.sub(r'\D', '', m.group(2) or '')) if m else (fn, '')
    phone = re.sub(r'\D', '', str(opts.get('phone', ''))) or phone
    group = bool(opts.get('group')) or '(group)' in fn
    contact = opts.get('contact') or phone or key(name)
    rows, cur = [], None
    for line in open(path, encoding='utf-8', errors='replace').read().replace('\r', '').split('\n'):
        md = WA_DUMP.match(line)
        me_ = WA_EXPORT.match(line) if not md else None
        if md:
            y, mo, d, h, mi, s, jid, text = md.groups()
            ts = local(int(y), int(mo), int(d), int(h), int(mi), int(s))
            num = re.sub(r'\D', '', jid.split('@')[0])
            spk = ('<in>' if phone and num == phone else '<out>' if phone else '') + jid.split('@')[0]
            cur = [ts, LINE, 1, spk, [text]]
            rows.append(cur)
        elif me_:
            mo, d, y, h, mi, s, ap, rest = me_.groups()
            y = int(y) + (2000 if int(y) < 100 else 0)
            ts = local(y, int(mo), int(d), h24(h, ap and ap[0]), int(mi), int(s or 0))
            sm = re.match(r'([^:]{1,60}?): (.*)$', rest)
            if sm:
                who, text = sm.groups()
                spk = who if group else ('<out>' if CTX.is_owner(who) else '<in>') + who
            else:
                spk, text = '<system>', rest
            cur = [ts, LINE, 1, spk, [text]]
            rows.append(cur)
        elif cur is not None:
            cur[4].append(line)
    out = [row(ts, p, nv, spk, '\n'.join(t).strip()) for ts, p, nv, spk, t in rows]
    return [('whatsapp', contact, 'channel' if group else 'im', 'whatsapp', out)] if out else []


# ---------------- ICQ: decoded database (icqdb.py JSON) and the ICQ text export ----------------
def icq_jsonl(path, opts):
    """Incoming messages are named by the contact's nickname from ICQ's own records (contact entry, else archive)."""
    entries = [json.loads(l) for l in open(path, encoding='utf-8')]
    nick = {}
    def walk(n):
        if n.get('uin') and n.get('name'):
            nick.setdefault(str(n['uin']), n['name'])
        for k in n.get('children', []):
            walk(k)
    for e in entries:
        if e.get('kind') == 'event_folder':
            walk(e['folder'])
    for e in entries:
        if e.get('kind') == 'contact':
            for blk in e.get('property_blocks', []):
                p = blk['properties']
                if p.get('UIN'):
                    nm = p.get('NickName') or ' '.join(x for x in (p.get('FirstName'), p.get('LastName')) if x)
                    if nm:
                        nick[str(p['UIN'])] = nm
    by = collections.defaultdict(list)
    for e in entries:
        k = e.get('kind')
        if k not in ('message', 'long_message', 'file_request', 'chat_request') or e.get('uin') is None or 'time_utc' not in e:
            continue
        ts = datetime.datetime.strptime(e['time_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()
        if k == 'file_request':
            text = f"[file] {e.get('file_name', '')} ({e.get('file_size')} bytes)" + (f" - {e['text']}" if e.get('text') else '')
        elif k == 'chat_request':
            text = '[chat request] ' + e.get('text', '')
        elif e.get('subtype_name') == 'url':
            text = f"{e.get('url', '')}" + (f" - {e['description']}" if e.get('description') else '')
        elif e.get('subtype_name') not in (None, 'message'):
            text = f"[{e['subtype_name'].replace('_', ' ')}] {e.get('text', '')}"
        else:
            text = e.get('text_utf8') or e.get('text', '')
        text = text.replace('\r\n', '\n')
        spk = '<system>' if e.get('subtype_name') not in (None, 'message', 'url') and k == 'message' else \
            ('<out>' if e.get('direction') == 'sent' else '<in>' + nick.get(str(e['uin']), ''))
        by[str(e['uin'])].append(row(ts, MS, 0, spk, text))
    return [('icq', uin, 'im', 'icq_db', rows) for uin, rows in by.items()]


ICQ_TXT_HDR = re.compile(r'^(\S.*?)\s{2,}(\d{1,2})/(\d{1,2})/(\d{2,4}) (\d{1,2}):(\d\d)(?::(\d\d))? ?([AP]M)\b ?(.*)$', re.I)


def icq_txt(path, opts):
    """ICQ client's text history export ('ICQ History Log For: <uin> <name>'): a 'Name   M/D/YYYY H:MM PM  text' line, then indented continuation lines. Its contact is in
    the file name. The date sits in an 8-character column, so long dates lose the end of the year ('1/13/200',
    '12/31/20'): such a year is a prefix, resolved to the year closest in time to the nearest message with a full year."""
    stem = os.path.splitext(os.path.basename(path))[0]
    who = stem.split()[0]
    rows, cur = [], None
    for line in open(path, encoding='latin-1').read().replace('\r', '').split('\n'):
        m = ICQ_TXT_HDR.match(line)
        if m:
            name, mo, d, y, h, mi, s_, ap, first = m.groups()
            cur = [(y, int(mo), int(d), h24(h, ap), int(mi), int(s_ or 0)), name.strip(), [first.strip()]]
            rows.append(cur)
        elif cur is not None:
            cur[2].append(line.strip())
    full = [(i, local(int(r[0][0]), *r[0][1:])) for i, r in enumerate(rows) if len(r[0][0]) == 4]
    def resolve(i, parts):
        y, rest = parts[0], parts[1:]
        if len(y) == 4:
            return local(int(y), *rest)
        if len(y) == 2 and not y.startswith(('19', '20')):
            return local(int(y) + (2000 if int(y) < 70 else 1900), *rest)    # a real two-digit year
        if not full:
            return None
        ref = min(full, key=lambda f: abs(f[0] - i))[1]
        cands = [yy for yy in range(1990, 2030) if str(yy).startswith(y)]
        return min((local(yy, *rest) for yy in cands), key=lambda t: abs(t - ref))
    out = [row(resolve(i, r[0]), LINE, 1, r[1], '\n'.join(r[2]).strip()) for i, r in enumerate(rows)]
    uin = None
    for l in open(path, encoding='latin-1').read().split('\n')[:6]:
        mu = re.match(r'\s+(\d{4,})\s', l)
        if mu:
            uin = mu.group(1)
    contact = opts.get('contact') or uin or key(who)
    return [('icq', contact, 'im', 'icq_txt', out)] if out else []


# ---------------- Skype (SkypeLogView XML export) ----------------
def skype_xml(path, opts):
    """SkypeLogView XML export (UTF-16, <item> per event). The contact is the most frequent user_name that is not me."""
    s = open(path, encoding='utf-16').read()
    items = re.findall(r'<item>(.*?)</item>', s, re.S)
    get = lambda it, t: html.unescape((re.search(rf'<{t}>(.*?)</{t}>', it, re.S) or [None, ''])[1])
    users = collections.Counter(get(it, 'user_name') for it in items)
    contact = opts.get('contact') or next((u for u, _ in users.most_common() if u and not CTX.is_owner(u) and ' ' not in u), 'skype')
    rows = []
    for it in items:
        m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4}) (\d{1,2}):(\d\d):(\d\d) ?([AP]M)?', get(it, 'action_time'))
        ts = local(int(m.group(3)), int(m.group(1)), int(m.group(2)), h24(m.group(4), m.group(7)), int(m.group(5)), int(m.group(6))) if m else None
        typ, user, text = get(it, 'action_type'), get(it, 'user_name'), get(it, 'chat_message').replace('<br>', '\n')
        if typ != 'Chat Message':
            spk, text = '<system>', f'[{typ}] {text}'.strip()
        else:
            spk = ('<in>' if user == contact else '<out>' if user and ' ' not in user else '') + (get(it, 'display_name') or user)
        rows.append(row(ts, LINE, 1, spk, text))
    return [('skype', contact, 'im', 'skype_xml', rows)] if rows else []


# ---------------- Discord (DiscordChatExporter HTML) ----------------
class _Discord(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.msgs, self.author, self.ts = [], '', None
        self.mode, self.depth, self.buf = None, 0, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get('class', '') or ''
        if self.mode:
            self.depth += 1
            if tag == 'br':
                self.buf.append('\n')
            elif tag == 'img' and a.get('alt'):
                self.buf.append(a['alt'])
            elif tag == 'a' and a.get('href') and self.mode == 'attach':
                self.buf.append(a['href'])
            return
        if 'chatlog__author-name' in cls:
            self.author = a.get('title') or ''
            self.mode, self.depth, self.buf = 'author', 1, []
        elif 'chatlog__timestamp' in cls:
            self.mode, self.depth, self.buf = 'ts', 1, []
        elif re.search(r'\bchatlog__content\b', cls):
            self.mode, self.depth, self.buf = 'content', 1, []
        elif 'chatlog__attachment' in cls.split():
            self.mode, self.depth, self.buf = 'attach', 1, ['[attachment] ']
            if tag == 'a' and a.get('href'):
                self.buf.append(a['href'])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.mode and tag not in ('br', 'img'):
            self.handle_endtag(tag)
        elif self.mode:
            self.depth -= 1

    def handle_endtag(self, tag):
        if not self.mode:
            return
        self.depth -= 1
        if self.depth > 0:
            return
        text = ''.join(self.buf).strip()
        if self.mode == 'author' and not self.author:
            self.author = text
        elif self.mode == 'ts':
            self.ts = text
        elif self.mode in ('content', 'attach') and text:
            self.msgs.append((self.author, self.ts, text))
        self.mode = None

    def handle_data(self, data):
        if self.mode:
            self.buf.append(data)


def discord(path, opts):
    """DiscordChatExporter HTML. 'Direct Messages - Name [id].html' is a one-to-one chat; 'Server - Category - channel'
    is a channel, named server/channel (Discord's stock 'Text Channels' / 'Voice Channels' categories dropped)."""
    fn = os.path.splitext(os.path.basename(path))[0]
    fn = re.sub(r'\s*\[\d+\](\s*\(.*\))?$', '', fn)          # '[channel id]', maybe '(2019-07-23 to 2019-08-14)' after it
    parts = [p.strip() for p in fn.split(' - ')]
    if len(parts) > 2 and re.fullmatch(r'(text|voice)( channels)?', parts[1], re.I):
        del parts[1]
    dm = parts[0].lower() == 'direct messages'
    contact = key(parts[-1]) if dm else key(' / '.join(parts))
    p = _Discord()
    p.feed(open(path, encoding='utf-8', errors='replace').read())
    rows = []
    for author, t, text in p.msgs:
        t = (t or '').strip()
        m = re.match(r'(\d{1,2})-(\w{3})-(\d{2,4}) (\d{1,2}):(\d\d)(?::(\d\d))? ?([AP]M)?', t)     # 26-Apr-20 07:12 PM
        n = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4}) (\d{1,2}):(\d\d)(?::(\d\d))? ?([AP]M)?', t)      # 4/26/2020 7:12 PM
        ts = None
        if m:
            y = int(m.group(3)) + (2000 if int(m.group(3)) < 100 else 0)
            mo = tl.MON.get(m.group(2).title())
            ts = local(y, mo, int(m.group(1)), h24(m.group(4), m.group(7)), int(m.group(5)), int(m.group(6) or 0)) if mo else None
        elif n:
            ts = local(int(n.group(3)), int(n.group(1)), int(n.group(2)), h24(n.group(4), n.group(7)), int(n.group(5)), int(n.group(6) or 0))
        spk = (('<out>' if CTX.is_owner(author) else '<in>') + author) if dm else author
        rows.append(row(ts, LINE if ts else GUESS, 1, spk, text))
    return [('discord', contact, 'im' if dm else 'channel', 'discord', rows)] if rows else []


# ---------------- old mIRC logs without [hh:mm] stamps ('Start of #x buffer', DCC 'Started on') ----------------
MIRC_START = re.compile(r'^(?:Start of .+? buffer: |Session Start: |.*\bStarted on )\w{3} (\w{3}) +(\d+) (\d\d):(\d\d):(\d\d) (\d{4})')
MIRC_END = re.compile(r'^(?:End of .+? buffer\s+|Session Close: )\w{3} (\w{3}) +(\d+) (\d\d):(\d\d):(\d\d) (\d{4})')


def mirc_plain(path, opts):
    """Old mIRC logs with no [hh:mm] stamps: each message takes its session's start time."""
    net, contact, kind = opts.get('network', 'irc'), opts.get('contact') or os.path.splitext(os.path.basename(path))[0], opts.get('kind', 'im')
    data = open(path, 'rb').read().decode('latin-1').replace('\r', '')
    rows, si, start = [], -1, None
    for line in data.split('\n'):
        line = tl.MIRC.sub('', line)
        m = MIRC_START.match(line)
        if m:
            mo, d, h, mi, s, y = m.groups()
            start = local(int(y), tl.MON[mo], int(d), int(h), int(mi), int(s))
            si += 1
            continue
        if MIRC_END.match(line) or not line.strip() or line.startswith(('-----', 'Session Ident')):
            continue
        mm = re.match(r'^<([^>]{1,40})> ?(.*)$', line)
        if mm:
            spk, text = re.sub(r'^[~&@%+]+', '', mm.group(1)), mm.group(2)
        elif rows and not line.startswith(('*', '-', '[')):
            r = rows[-1]                              # continuation of the previous line
            r[5] += '\n' + line
            r[4] = tl.strict_hash(r[5])
            continue
        else:
            spk, text = '<system>', line
        rows.append(row(start, SESSION if start else GUESS, 1, spk, text, si=max(si, 0), st=start))
    return [(net, contact, kind, 'mirc', rows)] if rows else []


# ---------------- SMS saved as e-mail: one received text per .eml ----------------
def sms_eml(path, opts):
    """Phone text messages exported as e-mail (From = sender, Date = time): received messages."""
    from email import policy
    m = email.message_from_bytes(open(path, 'rb').read(), policy=policy.default)
    frm = m.get('From', '')
    name, addr = email.utils.parseaddr(frm)
    who = (name or addr or frm).strip()
    digits = re.sub(r'\D', '', who)
    contact = digits if len(digits) >= 7 and len(digits) >= len(re.sub(r'\s', '', who)) - 2 else key(who)
    try:
        ts = email.utils.parsedate_to_datetime(m['Date']).timestamp()
    except (TypeError, ValueError):
        ts = None
    body = m.get_body(preferencelist=('plain', 'html'))
    text = body.get_content() if body else ''
    if body is not None and body.get_content_type() == 'text/html':
        text = plain(text)
    text = text.strip() or (m.get('Subject') or '').strip()
    return [(opts.get('network', 'sms'), contact, 'im', 'sms_eml', [row(ts, MS if ts else GUESS, 0, '<in>' + who, text)])]


PARSERS = {'digsby': digsby, 'wlm-xml': wlm_xml, 'msn-paste': msn_paste, 'whatsapp': whatsapp, 'icqdb-json': icq_jsonl,
           'icq-history-txt': icq_txt, 'skypelogview-xml': skype_xml, 'discord-html': discord, 'sms-eml': sms_eml,
           'mirc': mirc_plain}
