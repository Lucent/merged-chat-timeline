"""Curses viewer over an archive's index database.

Keys  Tab switch pane   c/C next/previous view: all people, each network, channels   a everyone by time (toggle)
      w everyone at this moment
      / search this person   ? search everyone   f filter   s sort   g go to date   n/N next/previous hit   Enter open
      p sources of the message   o open link (or click it)   S show/hide status changes
      t hour marks / minute times   z time zone   q quit
Mouse  click a person, a message, a link, a year on the ruler or a spot on the scrollbar map; the wheel scrolls.
Maps read at start-up (edit, then restart; no rebuild): people.tsv, me.tsv next to archive.toml.
"""
import bisect, collections, curses, datetime, fnmatch, os, re, shutil, sqlite3, subprocess, sys, unicodedata

from . import links
from .config import Archive, key, fold

UTC = datetime.timezone.utc
NAV_KEYS = (curses.KEY_DOWN, curses.KEY_UP, curses.KEY_NPAGE, curses.KEY_PPAGE, curses.KEY_HOME, curses.KEY_END)
HEAT_BG = (234, 236, 238, 241)                     # xterm-256 near-black .. dark grey: the activity band, low .. high
GAP_DAYS = 30                                      # '— 3 months later —' when a conversation pauses this long
SESSION_MINUTES = 60                               # a faint line where a conversation pauses this long within a day
NET_GLYPH = {'aim': '🏃', 'icq': '🌼', 'msn': '🪟', 'yahoo': '💜', 'irc': '#', 'jabber': '💡', 'google': '💬',
             'facebook': '📘', 'astra': '⭐', 'skype': '📞', 'sms': '📱', 'rendezvous': '🥐', 'whatsapp': '🟢',
             'discord': '🎮', 'reddit': '👽', 'bluesky': '🦋', 'instagram': '📷', 'tiktok': '🎵', 'twitter': '🐦',
             'tinder': '🔥', 'unknown': '❓'}                          # each a different colour and shape; none needs a variation selector
NET_LABEL = {'aim': 'AIM', 'icq': 'ICQ', 'msn': 'MSN', 'yahoo': 'Yahoo', 'irc': 'IRC', 'jabber': 'Jabber',
             'google': 'Google Talk', 'facebook': 'Facebook', 'astra': 'Astra', 'skype': 'Skype', 'sms': 'SMS',
             'rendezvous': 'Bonjour', 'whatsapp': 'WhatsApp', 'discord': 'Discord', 'reddit': 'Reddit',
             'bluesky': 'Bluesky', 'instagram': 'Instagram', 'tiktok': 'TikTok', 'twitter': 'Twitter', 'tinder': 'Tinder',
             'unknown': '?'}
NET_COLORS = {                                      # network -> (xterm-256 colour, 8-colour fallback)
    'aim': (220, curses.COLOR_YELLOW), 'icq': (76, curses.COLOR_GREEN), 'msn': (39, curses.COLOR_CYAN),
    'yahoo': (135, curses.COLOR_MAGENTA), 'irc': (203, curses.COLOR_RED), 'jabber': (37, curses.COLOR_CYAN),
    'google': (33, curses.COLOR_BLUE), 'facebook': (69, curses.COLOR_BLUE), 'astra': (208, curses.COLOR_YELLOW),
    'skype': (45, curses.COLOR_CYAN), 'sms': (114, curses.COLOR_GREEN), 'rendezvous': (250, curses.COLOR_WHITE),
    'whatsapp': (35, curses.COLOR_GREEN), 'discord': (99, curses.COLOR_MAGENTA), 'reddit': (202, curses.COLOR_RED),
    'bluesky': (75, curses.COLOR_BLUE), 'instagram': (170, curses.COLOR_MAGENTA), 'tiktok': (213, curses.COLOR_MAGENTA),
    'twitter': (117, curses.COLOR_CYAN), 'tinder': (209, curses.COLOR_RED), 'unknown': (244, curses.COLOR_WHITE)}
GROUP_COLORS = (213, 214, 120, 81, 229, 177, 203, 159)   # groups.tsv groups without a colour of their own, in file order
GROUP_COLORS_8 = (curses.COLOR_MAGENTA, curses.COLOR_YELLOW, curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_RED)
STATUS_LIKE = ['%signed on%', '%signed off%', '% is away%', '% is now away%', '% is idle%', '% is now idle%',
               '%is no longer away%', '%is no longer idle%', '% is back%', '% is now online%', '% is now offline%',
               '%has gone away%', '%has returned%', '%went away%', '%went idle%', '%logged on%', '%logged off%',
               '%changed status%', '%changed his status%', '%changed her status%', '%is now available%', '%is now known as%']
FULL_NAME = re.compile(r"^[A-Z][a-zA-Z'\-]+(?: [A-Z]\.?)?(?: [A-Z][a-zA-Z'\-]+)+$")
RAW_ID = re.compile(r'^(-?\d+@chat\.facebook\.com|[0-9a-f]{32,40})$')
IRC_MODE = re.compile(r'^[~&@%+]+(?=[^~&@%+\s])')


def compact(n):
    return str(n) if n < 1000 else f'{n / 1000:.0f}k' if n < 999500 else f'{n / 1e6:.1f}M'


def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 0 if unicodedata.combining(c) else 1 for c in s)


def clip(s, w):
    out, n = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in 'WF' else 1
        if n + cw > w:
            break
        out.append(c); n += cw
    return ''.join(out)


def printable(s):
    return ''.join(c if c >= ' ' or c == '\n' else ' ' for c in s.replace('\t', '    '))


def later(seconds):
    d = seconds / 86400
    if d >= 365 * 1.5:
        return f'{d / 365.25:.0f} years later'
    if d >= 365:
        return 'a year later'
    if d >= 60:
        return f'{d / 30.4:.0f} months later'
    return f'{d:.0f} days later'


def wrap(para, avail):
    """Wrap one paragraph into lines of [(piece, url)], never splitting a link (too-long links are shortened)."""
    lines, cur, n = [], [], 0
    for piece, url in links.parse(para):
        toks = [piece] if url else re.findall(r'\s+|\S+', piece)
        for t in toks:
            tw = width(t)
            if n + tw > avail and cur:
                if t.isspace():
                    lines.append(cur); cur, n = [], 0
                    continue
                lines.append(cur); cur, n = [], 0
            if url and tw > avail:
                t = links.shorten(t, avail); tw = width(t)
            while not url and tw > avail:
                head = clip(t, avail)
                lines.append([(head, None)])
                t = t[len(head):]; tw = width(t)
            cur.append((t, url)); n += tw
    lines.append(cur)
    return lines


def hyperlinks(shown, cursor):
    """Overlay OSC 8 hyperlinks (ctrl+click in Windows Terminal, iTerm2, GNOME Terminal...) on link text curses has
    just drawn, then put the cursor back where curses believes it is."""
    if not shown:
        return
    out = [f'\x1b[{y + 1};{x + 1}H\x1b[{sgr}m\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\\x1b[0m' for y, x, text, url, sgr in shown]
    out.append(f'\x1b[{cursor[0] + 1};{cursor[1] + 1}H')
    os.write(sys.stdout.fileno(), ''.join(out).encode('utf-8', 'replace'))


WSL = bool(shutil.which('explorer.exe'))


def file_url(path):
    """A local file as a URL the terminal and the default browser understand (Windows paths under WSL)."""
    if WSL:
        try:
            w = subprocess.run(['wslpath', '-w', path], capture_output=True, text=True, timeout=5).stdout.strip()
            return 'file:///' + w.replace('\\', '/')
        except (OSError, subprocess.SubprocessError):
            pass
    return 'file://' + path


def open_url(url):
    if WSL:
        cmd = ['explorer.exe', url[8:].replace('/', '\\') if url.startswith('file:///') else url]
    else:
        cmd = ['open' if sys.platform == 'darwin' else 'xdg-open', url]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Viewer:
    HELP = '(Tab) pane  (a)ll  (w)hen  (c)ycle  find (/)here (?)all  (g)oto  (p)rovenance  (o)pen  (S)tatus  time(z)one  (q)uit'

    def __init__(self, scr, archive):
        self.scr, self.ar = scr, archive
        self.db = sqlite3.connect(f'file:{archive.db}?mode=ro', uri=True)
        v = archive.raw.get('viewer', {})
        self.gap_days = v.get('gap_days', GAP_DAYS)
        self.session_minutes = v.get('session_minutes', SESSION_MINUTES)
        self.zones = ('then', archive.wall_zone.key, 'UTC')
        self.me = archive.load_me()
        self.people = {(n, self.norm_contact(n, c)): p for (n, c), p in archive.load_people().items()}
        self.groups = archive.load_groups()[:50]        # colour pairs 50-99
        self.media = self.index_media(v.get('media', []))
        links.RESOLVE = self.resolve_link
        self.zone_mode = 0
        self.shown_links, self.had_links = [], False
        self.blocks, self.blocks_key = {}, None
        self.pending = False
        self.page = None
        self.minutes = False                            # t: hour marks (default) or a time on each new minute
        self.view = None                                # c: sidebar lists all people (None), one network, or 'channels'
        self.nets = [r[0] for r in self.db.execute('SELECT network, sum(n) FROM convs WHERE n>0 GROUP BY network ORDER BY 2 DESC')]
        self.everyone = False                           # a: the message pane shows everyone by time
        self.show_status = False                        # S: show sign-ons/aways instead of folding them
        self.sort = 0                                   # 0 most messages, 1 most recent, 2 name
        self.filter = ''
        self.focus = 0                                  # 0 people, 1 messages
        self.months = collections.defaultdict(dict)
        for c, m, n in self.db.execute('SELECT conv, month, n FROM months'):
            self.months[c][m] = n
        self.load_convs()
        self.ci = 0; self.ctop = 0
        self.mids, self.mts, self.mi, self.mtop, self.conv = [], [], 0, 0, None
        self.collapsed = {}
        self.more = (False, False)                      # everyone view: more messages before / after the window
        self.hits, self.hi, self.hit_ts = [], -1, []
        self.cache = {}
        self.status = self.HELP
        curses.curs_set(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)
        curses.use_default_colors()
        for i, c in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_MAGENTA), 1):
            curses.init_pair(i, c, -1)
        self.net_attr = {}
        for i, (net, (c256, c8)) in enumerate(NET_COLORS.items(), 20):
            curses.init_pair(i, c256 if curses.COLORS >= 256 else c8, -1)
            self.net_attr[net] = curses.color_pair(i)
        self.group_attr = {}                            # group -> colour attribute for its people's names
        for i, (g, _, col) in enumerate(self.groups):
            curses.init_pair(50 + i, (col if col is not None else GROUP_COLORS[i % len(GROUP_COLORS)]) if curses.COLORS >= 256
                             else GROUP_COLORS_8[i % len(GROUP_COLORS_8)], -1)
            self.group_attr[g] = curses.color_pair(50 + i)
        # (pair, level) -> pair with that background: made on first use from pair 100 up, since curses attributes can
        # carry only pairs 0-255 (higher numbers wrap onto other pairs). Level pairs for plain text come first.
        self.heat, self.next_pair = {}, 100
        self.heat_ok = curses.COLORS >= 256 and curses.COLOR_PAIRS >= 256
        if self.heat_ok:
            for lv in range(len(HEAT_BG)):
                self.heat_pair(0, lv)
        self.open_conv()

    def heat_pair(self, pair, lv):
        """The colour pair showing text of pair on heat level lv's background; plain text's pair when none is left."""
        k = (pair, lv)
        if k not in self.heat:
            if self.next_pair <= 255:
                curses.init_pair(self.next_pair, curses.pair_content(pair)[0] if pair else -1, HEAT_BG[lv])
                self.heat[k] = self.next_pair
                self.next_pair += 1
            else:
                self.heat[k] = self.heat[(0, lv)]
        return self.heat[k]

    # ---------- data ----------
    def norm_contact(self, net, c):
        return self.ar.norm_contact(net, c)

    def person_of(self, net, contact):
        return self.people.get((net, self.norm_contact(net, contact)))

    def their_name(self, names, contact):
        """A full name among the conversation's top speakers who are not me, else the most frequent such speaker,
        else the account name."""
        cands = []
        for n in (names or '').split('\t'):
            n = IRC_MODE.sub('', n) or n
            if n and not self.ar.is_owner(n, self.me) and not RAW_ID.match(n):
                cands.append(n)
        return next((n for n in cands if FULL_NAME.match(n)), cands[0] if cands else contact)

    def shown(self, is_channel, net):
        """Does the sidebar's current view list this conversation? All people, one network (its people and channels)
        or all channels."""
        if self.view is None:
            return not is_channel
        if self.view == 'channels':
            return is_channel
        return net == self.view

    def view_name(self):
        return 'People' if self.view is None else 'Channels' if self.view == 'channels' else NET_LABEL.get(self.view, self.view)

    def cycle_view(self, step=1):
        order = [None] + self.nets + ['channels']
        self.view = order[(order.index(self.view) + step) % len(order)]

    def group_of(self, g):
        """The groups.tsv group (its index) of a sidebar entry: named as a person, or any of its accounts matching a
        network:contact pattern."""
        name = g[0].lower() if isinstance(g[0], str) else None
        accts = [f'{self.conv_net[c]}:{self.norm_contact(self.conv_net[c], self.conv_contact[c])}' for c in g[6]]
        for gi, (_, members, _) in enumerate(self.groups):
            for m in members:
                m = m.lower()
                if (name and m == name) or (':' in m and any(fnmatch.fnmatchcase(a, m) for a in accts)):
                    return gi
        return None

    def load_convs(self):
        """Sidebar entries (key, name, networks, n, first, last, conv ids, group): conversations grouped by people.tsv,
        ordered by groups.tsv group then the chosen sort."""
        groups = {}
        self.conv_name, self.conv_net, self.conv_contact, self.group_conv, self.hidden, self.label = {}, {}, {}, set(), set(), {}
        self.conv_group = {}
        for cid, net, contact, names, n, first, last, kind in self.db.execute(
                'SELECT id, network, contact, names, n, first, last, kind FROM convs WHERE n>0'):
            self.conv_net[cid], self.conv_contact[cid] = net, contact
            if kind == 'status' or net == 'irc' and re.match(r'status(\.|$)', contact):
                self.hidden.add(cid)                    # sign-on logs and IRC server windows, not chats
                continue
            person = None if kind == 'channel' else self.person_of(net, contact)
            name = contact if kind == 'channel' else person or self.their_name(names, contact)
            self.conv_name[cid] = self.label[cid] = name
            if kind == 'channel':
                self.group_conv.add(cid)
            if not self.shown(kind == 'channel', net):
                continue
            acct = (net, contact[1:] if net == 'facebook' and re.fullmatch(r'u\d+', contact) else contact)
            g = groups.setdefault(person or acct, [person or acct, name, collections.Counter(), 0, None, None, [], ''])
            g[2][net] += n; g[3] += n; g[6].append(cid)
            g[4] = first if g[4] is None or (first is not None and first < g[4]) else g[4]
            g[5] = last if g[5] is None or (last is not None and last > g[5]) else g[5]
            g[7] += f' {net} {contact} {names or ""}'
        ts = [t for g in groups.values() for t in (g[4], g[5]) if t and t > 631152000]     # ignore pre-1990 junk
        self.span = (min(ts), max(ts)) if ts else None
        f = fold(self.filter)
        rows = [g for g in groups.values() if not f or f in fold(f'{g[1]} {g[7]}')]
        for g in rows:
            g.append(self.group_of(g))
            for c in g[6]:
                self.conv_group[c] = g[8]
        within = (lambda g: -g[3], lambda g: -(g[5] or 0), lambda g: fold(g[1]))[self.sort]
        rows.sort(key=lambda g: (g[8] if g[8] is not None else len(self.groups), within(g)))
        self.convs = [(g[0], g[1], ','.join(x for x, _ in g[2].most_common()), g[3], g[4], g[5], g[6], g[8]) for g in rows]
        self.heat_cache = {}

    def status_ids(self, ids):
        """Sign-on/off and away/idle notices in these conversations."""
        like = ' OR '.join('r.text LIKE ?' for _ in STATUS_LIKE)
        q = (f'SELECT m.id FROM msgs m JOIN raw r ON r.id=m.rid WHERE m.conv IN ({",".join("?" * len(ids))}) '
             f"AND m.dir='sys' AND ({like})")
        return {r[0] for r in self.db.execute(q, list(ids) + STATUS_LIKE)}

    def open_conv(self, mid=None):
        if self.everyone:
            return self.open_everyone(mid)
        if not self.convs:
            self.conv, self.mids, self.mts = None, [], []
            return
        self.ci = max(0, min(self.ci, len(self.convs) - 1))
        cid = self.convs[self.ci][0]
        if cid != self.conv or mid == 'reload':
            self.conv = cid
            ids = self.convs[self.ci][6]
            rows = self.db.execute(f'SELECT id, ts FROM msgs WHERE conv IN ({",".join("?" * len(ids))})', ids).fetchall()
            rows.sort(key=lambda r: (r[1] or 0, r[0]))
            self.collapsed = {}
            if not self.show_status:                    # fold each run of status notices into its first one
                st = self.status_ids(ids)
                kept, run = [], None
                for r in rows:
                    if r[0] in st:
                        if run is not None:
                            self.collapsed[run] += 1
                            continue
                        run = r[0]; self.collapsed[run] = 1
                    else:
                        run = None
                    kept.append(r)
                rows = kept
                self.collapsed = {k: v for k, v in self.collapsed.items() if v > 1}
            self.mids, self.mts = [r[0] for r in rows], [r[1] or 0 for r in rows]
            self.cache = {}
            self.mi = len(self.mids) - 1; self.mtop = max(0, self.mi)
            self.set_hit_ts()
            if mid == 'reload':
                mid = None
        if mid is not None:
            self.mi = self.index_of(mid)
            self.mtop = self.mi

    def index_of(self, mid):
        try:
            return self.mids.index(mid)
        except ValueError:
            ts = self.db.execute('SELECT ts FROM msgs WHERE id=?', (mid,)).fetchone()
            return min(bisect.bisect_left(self.mts, ts[0] or 0), len(self.mids) - 1) if ts and self.mids else 0

    def set_hit_ts(self):
        self.hit_ts = []
        if self.hits and not self.everyone and self.convs:
            ids = set(self.convs[self.ci][6])
            hs = self.hits
            for i in range(0, len(hs), 900):
                ch = hs[i:i + 900]
                self.hit_ts += [t for c, t in self.db.execute(
                    f'SELECT conv, ts FROM msgs WHERE id IN ({",".join("?" * len(ch))})', ch) if c in ids and t]
            self.hit_ts.sort()

    # ---- everyone, by time: a window of messages loaded around a moment ----
    WINDOW = 700

    def everyone_filter(self):
        excl = self.hidden | {c for c in self.conv_name if not self.shown(c in self.group_conv, self.conv_net[c])}
        return (f'conv NOT IN ({",".join(map(str, excl))}) AND ' if excl else '') + "ts IS NOT NULL AND (dir IS NULL OR dir<>'sys')"

    def open_everyone(self, mid=None, ts=None, where=None):
        """Load the window around message mid, or time ts, or the start/end (where='start'/'end')."""
        f = self.everyone_filter()
        if mid is not None and ts is None:
            r = self.db.execute('SELECT ts FROM msgs WHERE id=?', (mid,)).fetchone()
            ts = r[0] if r else None
        if where == 'start' or (ts is None and where != 'end' and False):
            before, after = [], self.db.execute(f'SELECT id, ts FROM msgs WHERE {f} ORDER BY ts, id LIMIT ?', (2 * self.WINDOW,)).fetchall()
        elif where == 'end' or ts is None:
            before = self.db.execute(f'SELECT id, ts FROM msgs WHERE {f} ORDER BY ts DESC, id DESC LIMIT ?', (2 * self.WINDOW,)).fetchall()
            after = []
        else:
            pid = mid if mid is not None else 0
            before = self.db.execute(f'SELECT id, ts FROM msgs WHERE {f} AND (ts < ? OR (ts = ? AND id < ?)) '
                                     'ORDER BY ts DESC, id DESC LIMIT ?', (ts, ts, pid, self.WINDOW)).fetchall()
            after = self.db.execute(f'SELECT id, ts FROM msgs WHERE {f} AND (ts > ? OR (ts = ? AND id >= ?)) '
                                    'ORDER BY ts, id LIMIT ?', (ts, ts, pid, self.WINDOW)).fetchall()
        rows = before[::-1] + after
        self.more = (len(before) >= self.WINDOW and where != 'start', len(after) >= self.WINDOW and where != 'end')
        if where == 'start':
            self.more = (False, len(after) >= 2 * self.WINDOW)
        if where == 'end' or (ts is None and where is None):
            self.more = (len(before) >= 2 * self.WINDOW, False)
        self.conv = ('everyone', self.view)
        self.mids, self.mts = [r[0] for r in rows], [r[1] for r in rows]
        self.collapsed, self.cache, self.hit_ts = {}, {}, []
        self.blocks, self.blocks_key = {}, None
        if not self.mids:
            self.mi = self.mtop = 0
            return
        if where == 'start':
            self.mi = 0
        elif where == 'end' or ts is None:
            self.mi = len(self.mids) - 1
        else:
            self.mi = len(before) if mid is None else self.index_of(mid)
        self.mi = max(0, min(self.mi, len(self.mids) - 1))
        self.mtop = self.mi

    def slide_everyone(self):
        """Keep the cursor away from the window's edges by reloading around it."""
        if not self.everyone or not self.mids:
            return
        if (self.mi < 80 and self.more[0]) or (self.mi > len(self.mids) - 80 and self.more[1]):
            top_mid, cur = self.mids[self.mtop], self.mids[self.mi]
            self.open_everyone(cur)
            self.mi = self.index_of(cur)
            self.mtop = min(self.index_of(top_mid), self.mi)

    # ---- messages ----
    def msg(self, mid):
        m = self.cache.get(mid)
        if m is None:
            if len(self.cache) > 6000:
                self.cache.clear()
            ts, prec, d, spk, text, nsrc, cid = self.db.execute(
                'SELECT m.ts, m.prec, m.dir, m.speaker, r.text, m.nsrc, m.conv FROM msgs m JOIN raw r ON r.id=m.rid WHERE m.id=?',
                (mid,)).fetchone()
            spk = IRC_MODE.sub('', spk or '') or spk
            side = ('me' if d == 'out' else 'them' if d == 'in' else 'sys' if d == 'sys'
                    else 'me' if self.ar.is_owner(spk, self.me) else 'them')
            m = (ts, prec, side, self.ar.short(spk) or (self.conv_name.get(cid) if side == 'them' else ''), text, nsrc, cid)
            self.cache[mid] = m
        return m

    def local(self, ts):
        if ts is None:
            return None
        mode = self.zones[self.zone_mode]
        z = UTC if mode == 'UTC' else self.ar.wall_zone if self.zone_mode == 1 else self.ar.zone_at(ts)
        return datetime.datetime.fromtimestamp(ts, z)

    # ---- media links: Trillian file-transfer links -> the downloaded copy on disk ----
    def index_media(self, dirs):
        idx = {}
        for d in dirs:
            for p in ([d] if '*' not in d else []):
                base = os.path.join(self.ar.root, p)
                for dp, dns, fns in os.walk(base):
                    for fn in fns:
                        idx.setdefault(fn.lower(), os.path.join(dp, fn))
            if '*' in d:                                # e.g. "**/assets": every folder with that name
                name = d.rstrip('/').split('/')[-1]
                for dp, dns, fns in os.walk(self.ar.root):
                    if os.path.basename(dp) == name:
                        for fn in fns:
                            idx.setdefault(fn.lower(), os.path.join(dp, fn))
        return idx

    def resolve_link(self, url):
        if not self.media or '://' not in url:
            return url
        seg = re.sub(r'[?#].*$', '', url).rstrip('/').split('/')[-1].lower()
        p = self.media.get(seg) or next((v for k, v in self.media.items() if seg and k.startswith(seg + '.')), None)
        return file_url(p) if p and len(seg) >= 6 else url

    # ---------- drawing ----------
    @staticmethod
    def clock(d, prec):
        if d is None or prec == 0:
            return ''
        return ('~' if prec <= 1 else '') + f"{d.hour % 12 or 12}:{d.minute:02d}{'a' if d.hour < 12 else 'p'}"

    def msg_lines(self, mid, w, show=True):
        """One-to-one: mine '  text', theirs '> text'. Group chats: 'name: text'. Everyone view: 'Person  text'.
        -> [(head, [(piece, url)], colour key, nsrc)]"""
        ts, prec, side, spk, text, nsrc, cid = self.msg(mid)
        margin = ((self.clock(self.local(ts), prec) if show else '').rjust(7) + ' ') if self.minutes else ''
        if mid in self.collapsed:
            return [(margin + '* ', [(f'· {self.collapsed[mid]} status changes (S shows them)', None)], 'sys', nsrc)]
        if self.everyone:
            who = clip(printable(self.ar.short(self.label.get(cid, '?'))), 14).ljust(14)
            lead, ckey = who + (' » ' if side == 'me' else '   '), 'p:' + self.label.get(cid, '')
            if cid in self.group_conv and side != 'me':
                text = f'{spk}: {text}'
        elif cid in self.group_conv:
            who = 'me' if side == 'me' else '*' if side == 'sys' else (spk or '?')
            lead, ckey = f'{who}: ', side if side != 'them' else spk
        else:
            lead, ckey = {'me': '  ', 'them': '> ', 'sys': '* '}[side], side
        head = margin + lead
        out = []
        avail = max(10, w - width(head))
        for para in printable(text or '').split('\n'):
            for segs in wrap(para, avail):
                out.append((head if not out else ' ' * width(head), segs, ckey, nsrc))
        return out

    def draw(self):
        scr = self.scr
        scr.erase()
        H, W = scr.getmaxyx()
        lw = min(36, W // 3)
        self.lw = lw
        self.draw_people(0, 0, H - 1, lw)
        for y in range(H - 1):
            scr.addstr(y, lw, '│', curses.A_DIM)
        self.draw_msgs(0, lw + 1, H - 1, W - lw - 1)
        scr.addstr(H - 1, 0, clip(self.status, W - 1).ljust(W - 1), curses.A_REVERSE)
        if self.had_links:
            scr.redrawwin()
        scr.refresh()
        self.had_links = bool(self.shown_links)
        hyperlinks(self.shown_links, scr.getyx())

    def columns(self, w):
        """Time span of each sidebar column, as month numbers [lo, hi)."""
        t0, t1 = self.span
        d0 = datetime.datetime.fromtimestamp(t0, UTC); d1 = datetime.datetime.fromtimestamp(t1, UTC)
        m0, m1 = d0.year * 12 + d0.month - 1, d1.year * 12 + d1.month
        return [(m0 + (m1 - m0) * i // w, m0 + (m1 - m0) * (i + 1) // w) for i in range(w)], m0, m1

    def heat_levels(self, ids, w):
        """Activity per column for these conversations: 0 none, 1-4 from quiet to busiest (relative to this person)."""
        ck = (tuple(ids), w)
        if ck in self.heat_cache:
            return self.heat_cache[ck]
        cols, _, _ = self.columns(w)
        per = collections.Counter()
        for c in ids:
            for m, n in self.months.get(c, {}).items():
                per[m] += n
        vals = [sum(per.get(m, 0) for m in range(lo, max(hi, lo + 1))) for lo, hi in cols]
        top = max(vals) or 1
        lv = [0 if v == 0 else 1 if v < 0.12 * top else 2 if v < 0.35 * top else 3 if v < 0.7 * top else 4 for v in vals]
        self.heat_cache[ck] = lv
        return lv

    def draw_people(self, y0, x0, h, w):
        rows = h - 2                                    # title, year ruler, then entries
        if self.ci < self.ctop:
            self.ctop = self.ci
        if self.ci >= self.ctop + rows:
            self.ctop = self.ci - rows + 1
        title = f" {self.view_name()} ({len(self.convs)}) {('by count', 'by recent', 'by name')[self.sort]}" + \
            (f" ~{self.filter}" if self.filter else '')
        self.scr.addstr(y0, x0, clip(title, w).ljust(w), curses.A_BOLD | (curses.A_REVERSE if self.focus == 0 else 0))
        self.draw_ruler(y0 + 1, x0, w)
        for i in range(rows):
            k = self.ctop + i
            if k >= len(self.convs):
                break
            _, name, net, n, first, last, ids, grp = self.convs[k]
            yrs = f"{self.local(first):%y}-{self.local(last):%y}" if first and last else ''
            right = f'{compact(n):>5} {yrs:5}'
            nets = net.split(',')[:5]
            glyphs = [NET_GLYPH.get(nt, '?') for nt in nets]
            gx = x0 + w - len(right) - 1 - sum(width(g) for g in glyphs)
            left = clip(printable(self.ar.short(name)), max(4, gx - x0 - 1))
            attr = curses.A_REVERSE if k == self.ci and self.focus == 0 else curses.A_BOLD if k == self.ci else 0
            yy = y0 + 2 + i
            cells = [(' ', attr)] * w
            def put(x, text, a):
                for ch in text:
                    cw = 2 if unicodedata.east_asian_width(ch) in 'WF' else 1
                    if 0 <= x and x + cw <= w:
                        cells[x] = (ch, a)
                        if cw == 2:
                            cells[x + 1] = (None, a)
                    x += cw
            put(0, left, attr | (self.group_attr[self.groups[grp][0]] if grp is not None else 0))
            x = gx - x0
            for nt, g in zip(nets, glyphs):
                put(x, g, attr | curses.A_BOLD | self.net_attr.get(nt, 0))
                x += width(g)
            put(w - len(right), right, attr | curses.A_DIM)
            lv = self.heat_levels(ids, w) if self.span and self.heat_ok and not attr & curses.A_REVERSE else [0] * w
            x = 0
            while x < w:
                start, level, a = x, lv[x], cells[x][1]
                run = []
                while x < w and cells[x][1] == a and lv[x] == level:
                    if cells[x][0] is not None:
                        run.append(cells[x][0])
                    x += 1
                if level:
                    a = (a & ~curses.A_COLOR) | curses.color_pair(self.heat_pair(curses.pair_number(a & curses.A_COLOR), level - 1))
                try:
                    self.scr.addstr(yy, x0 + start, ''.join(run), a)
                except curses.error:
                    pass

    def draw_ruler(self, y, x0, w):
        """Year marks over the activity bands; click one to jump there."""
        self.ruler = None
        if not self.span:
            return
        cols, m0, m1 = self.columns(w)
        years = list(range(m0 // 12 + 1, (m1 - 1) // 12 + 1))
        step = max(1, -(-3 * len(years) // max(1, w)))
        line = [' '] * w
        for yv in years:
            x = next((i for i, (lo, hi) in enumerate(cols) if lo <= yv * 12 < max(hi, lo + 1)), None)
            if x is None:
                continue
            line[x] = '┊' if line[x] == ' ' else line[x]
            if yv % step == 0 and x + 2 < w and all(c in ' ┊' for c in line[x:x + 3]):
                line[x:x + 2] = list(f'{yv % 100:02d}')
        self.scr.addstr(y, x0, ''.join(line), curses.A_DIM)
        self.ruler = (y, x0, w, cols)

    def draw_msgs(self, y0, x0, h, w):
        if self.pending and self.convs and not self.everyone:
            self.scr.addstr(y0, x0, clip(f" {self.ar.short(self.convs[self.ci][1])}  …", w).ljust(w), curses.A_BOLD)
            return
        base = curses.A_BOLD | (curses.A_REVERSE if self.focus == 1 else 0)
        if self.everyone:
            pieces = [(f" Everyone ({self.view_name().lower()})  (Enter) open  (a) back  ", base),
                      (f"times: {self.zones[self.zone_mode]}", base & ~curses.A_BOLD)]
        elif self.convs and self.mids:
            c = self.convs[self.ci]
            pieces = [(f' {self.ar.short(c[1])}  ', base)]
            for net, k in self.db.execute(f'SELECT network, contact FROM convs WHERE id IN ({",".join("?" * len(c[6]))}) '
                                          'ORDER BY n DESC', c[6]):
                col = self.net_attr.get(net, 0)
                pieces += [(f'{NET_GLYPH.get(net, "?")}:', base | col), (f'{self.ar.short(k)} ', (base & ~curses.A_BOLD) | col)]
            pieces.append((f' {len(self.mids)} messages  times: {self.zones[self.zone_mode]}', base & ~curses.A_BOLD))
        else:
            return
        self.scr.addstr(y0, x0, ' ' * w, base)
        x = x0
        for t, at in pieces:
            t = clip(printable(t), x0 + w - x)
            if not t:
                break
            self.scr.addstr(y0, x, t, at)
            x += width(t)
        if not self.mids:
            return
        rows = h - 1
        tw = w - 1                                      # the last column is the scrollbar map
        self.mi = max(0, min(self.mi, len(self.mids) - 1))
        ck = (self.conv, tw, self.minutes, self.zone_mode, len(self.mids), self.mids[0])
        if ck != self.blocks_key or len(self.blocks) > 20000:
            self.blocks, self.blocks_key = {}, ck
        bl = lambda k: len(self.block(k, tw))
        if self.mi < self.mtop:
            self.mtop = self.mi
        else:
            tot, k = 0, self.mtop
            while k <= self.mi and tot <= rows:
                tot += bl(k); k += 1
            if tot > rows:
                k, tot = self.mi, bl(self.mi)
                while k > 0 and tot + bl(k - 1) <= rows:
                    k -= 1; tot += bl(k)
                self.mtop = k
        tot, k = 0, self.mtop
        while k < len(self.mids) and tot < rows:
            tot += bl(k); k += 1
        if k == len(self.mids) and not (self.everyone and self.more[1]):
            while self.mtop > 0 and tot + bl(self.mtop - 1) <= rows:
                self.mtop -= 1; tot += bl(self.mtop)
        lines = self.layout(self.mtop, tw, rows)
        self.page = (tw, rows, lines[-1][0] if lines else self.mtop)
        self.shown_links = []
        hl = self.hits[self.hi] if 0 <= self.hi < len(self.hits) else None
        self.line_msg = {}
        for y, (k, kind, a, b, side, nsrc) in enumerate(lines):
            yy = y0 + 1 + y
            self.line_msg[yy] = k
            if kind == 'day':
                self.scr.addstr(yy, x0, clip(a.center(tw, '─'), tw), curses.A_DIM)
                continue
            if kind == 'gap':
                self.scr.addstr(yy, x0, clip(a.center(tw, '═'), tw), curses.A_BOLD | curses.color_pair(3))
                continue
            if kind == 'switch':
                self.scr.addstr(yy, x0, clip(a.center(tw, '╌'), tw), curses.A_DIM | (self.net_attr.get(side, 0)))
                continue
            if kind == 'session':
                self.scr.addstr(yy, x0, clip('  ' + '┄' * min(24, tw - 4), tw), curses.A_DIM)
                continue
            if kind == 'hour':
                self.scr.addstr(yy, x0, clip(a, tw), curses.A_DIM | curses.color_pair(3))
                continue
            grp = self.conv_group.get(self.msg(self.mids[k])[6]) if side == 'them' else None
            col = (0 if side == 'me' else curses.color_pair(4) | curses.A_DIM if side == 'sys'
                   else self.group_attr[self.groups[grp][0]] if grp is not None
                   else curses.color_pair(1) if side == 'them' else curses.color_pair(1 + sum(map(ord, side or '')) % 3))
            sel = curses.A_REVERSE if k == self.mi and self.focus == 1 else 0
            mark = curses.A_BOLD if hl == self.mids[k] else 0
            self.scr.addstr(yy, x0, clip(a, tw), col | sel | mark)
            x = x0 + min(tw - 1, width(a))
            for piece, url in b:
                room = x0 + tw - x
                if room <= 0:
                    break
                t = clip(piece, room)
                self.scr.addstr(yy, x, t, col | sel | mark | (curses.A_UNDERLINE if url else 0))
                if url:
                    sgr = ['4'] + (['7'] if sel else []) + (['1'] if mark else []) + {
                        'me': [], 'sys': ['35', '2'], 'them': ['36']}.get(side, [('36', '32', '33')[sum(map(ord, side or '')) % 3]])
                    self.shown_links.append((yy, x, t, url, ';'.join(sgr)))
                x += width(t)
        self.draw_map(y0 + 1, x0 + w - 1, rows, lines)

    def draw_map(self, y0, x, rows, lines):
        """Scrollbar map: message density over the time this pane covers, the part on screen highlighted, search hits
        as dots. Click a spot to go there."""
        self.map = None
        if rows < 3 or not self.mts:
            return
        if self.everyone:
            t0, t1 = self.span if self.span else (self.mts[0], self.mts[-1])
        else:
            t0, t1 = self.mts[0], self.mts[-1]
        if t1 <= t0:
            return
        edges = [t0 + (t1 - t0) * i / rows for i in range(rows + 1)]
        if self.everyone:
            per = collections.Counter()
            for c, ms in self.months.items():
                if c in self.conv_name and self.shown(c in self.group_conv, self.conv_net[c]):
                    for m, n in ms.items():
                        per[m] += n
            def month_of(t):
                d = datetime.datetime.fromtimestamp(t, UTC)
                return d.year * 12 + d.month - 1
            counts = [sum(per.get(m, 0) for m in range(month_of(edges[i]), month_of(edges[i + 1]) + 1)) for i in range(rows)]
        else:
            counts = [bisect.bisect_left(self.mts, edges[i + 1]) - bisect.bisect_left(self.mts, edges[i]) for i in range(rows)]
        top = max(counts) or 1
        shown = [self.mts[l[0]] for l in lines if l[0] < len(self.mts)]
        v0, v1 = (min(shown), max(shown)) if shown else (None, None)
        hits = {min(rows - 1, int((t - t0) / (t1 - t0) * rows)) for t in self.hit_ts if t0 <= t <= t1}
        for i in range(rows):
            c = counts[i]
            ch = ' ' if c == 0 else '░' if c < 0.15 * top else '▒' if c < 0.45 * top else '▓' if c < 0.8 * top else '█'
            on = v0 is not None and edges[i + 1] >= v0 and edges[i] <= v1
            attr = curses.A_REVERSE if on else curses.A_DIM
            if i in hits:
                ch, attr = '•', attr | curses.A_BOLD | curses.color_pair(3)
            try:
                self.scr.addstr(y0 + i, x, ch, attr)
            except curses.error:
                pass
        self.map = (y0, x, rows, t0, t1)

    def stamp_key(self, k):
        if k < 0:
            return None
        m = self.msg(self.mids[k])
        d = self.local(m[0])
        if d is None or m[1] == 0:
            return None
        return (d.replace(second=0, microsecond=0), m[1] <= 1) if self.minutes else d.replace(minute=0, second=0, microsecond=0)

    def block(self, k, w):
        """Screen lines for message k: gap, day, service-switch, session and hour markers first, then the message."""
        b = self.blocks.get(k)
        if b is not None:
            return b
        out = []
        m = self.msg(self.mids[k])
        d = self.local(m[0])
        pm = self.msg(self.mids[k - 1]) if k > 0 else None
        pd = self.local(pm[0]) if pm else None
        gap = (m[0] - pm[0]) if pm and m[0] and pm[0] else 0
        if gap >= self.gap_days * 86400 and not self.everyone:
            out.append((k, 'gap', f' {later(gap)} ', '', None, 0))
        if d and (pd is None or d.date() != pd.date()):
            out.append((k, 'day', f' {d:%A %B %-d, %Y} · {d:%Z} ', '', None, 0))
        if pm and not self.everyone and self.conv_net.get(m[6]) != self.conv_net.get(pm[6]):
            a, b_ = self.conv_net.get(pm[6], '?'), self.conv_net.get(m[6], '?')
            out.append((k, 'switch', f' {NET_LABEL.get(a, a)} → {NET_LABEL.get(b_, b_)} ', '', b_, 0))
        elif pm and d and pd and d.date() == pd.date() and gap >= self.session_minutes * 60 and m[1] >= 3:
            out.append((k, 'session', '', '', None, 0))
        cur, prev = self.stamp_key(k), self.stamp_key(k - 1)
        show = cur is not None and (cur != prev or (out and not self.minutes))
        if show and not self.minutes:
            out.append((k, 'hour', f"{cur.hour % 12 or 12}{'a' if cur.hour < 12 else 'p'}", '', None, 0))
        for a, segs, side, nsrc in self.msg_lines(self.mids[k], w - 1, show):
            out.append((k, 'msg', a, segs, side, nsrc))
        self.blocks[k] = out
        return out

    def layout(self, top, w, rows):
        out, k = [], top
        while len(out) < rows and k < len(self.mids):
            out += self.block(k, w)
            k += 1
        return out[:rows]

    # ---------- interaction ----------
    def prompt(self, label, initial=''):
        H, W = self.scr.getmaxyx()
        curses.curs_set(1)
        s = initial
        while True:
            self.scr.addstr(H - 1, 0, clip(label + s, W - 1).ljust(W - 1), curses.A_REVERSE)
            self.scr.move(H - 1, min(W - 2, width(label + s)))
            ch = self.scr.get_wch()
            if ch in ('\n', '\r', curses.KEY_ENTER):
                break
            if ch == '\x1b':
                s = None; break
            if ch in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                s = s[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                s += ch
        curses.curs_set(0)
        return s

    def search(self, local=False):
        """Full-text search everywhere, or (local) only in the open person's conversations."""
        scope = None
        if local and self.convs and not self.everyone:
            scope = set(self.convs[self.ci][6])
        q = self.prompt(f'Search {self.ar.short(self.convs[self.ci][1])}: ' if scope else 'Search all: ')
        if not q:
            return
        toks = re.findall(r'\w+\*?|"[^"]+"', fold(q))
        if not toks:
            return
        expr = ' '.join(t if t.startswith('"') else '"' + t.rstrip('*') + '"' + ('*' if t.endswith('*') else '') for t in toks)
        try:
            ids = [r[0] for r in self.db.execute('SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rowid', (expr,))]
        except sqlite3.OperationalError as e:
            self.status = f'search error: {e}'; return
        if not ids:
            self.status = f'no matches for {q}'; return
        rows = []
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            rows += self.db.execute(f'SELECT id, conv, ts FROM msgs WHERE id IN ({",".join("?" * len(chunk))})', chunk).fetchall()
        rows = [r for r in rows if r[1] not in self.hidden and (scope is None or r[1] in scope)]
        if not rows:
            self.status = f'no matches for {q} here (? searches everywhere)'; return
        rows.sort(key=lambda r: (r[2] or 0, r[0]))
        self.hits_list(q, rows)

    def hits_list(self, q, rows):
        sel, top = 0, 0
        f = [w.strip('"*') for w in re.findall(r'\w+\*?|"[^"]+"', fold(q))]
        while True:
            H, W = self.scr.getmaxyx()
            self.scr.erase()
            self.scr.addstr(0, 0, clip(f' {len(rows)} messages match "{q}"  Enter open  Esc/q back', W).ljust(W - 1), curses.A_REVERSE)
            sel = max(0, min(sel, len(rows) - 1))
            if sel < top:
                top = sel
            if sel >= top + H - 2:
                top = sel - H + 3
            for y in range(H - 2):
                k = top + y
                if k >= len(rows):
                    break
                mid, cid, ts = rows[k]
                text = printable(self.db.execute('SELECT r.text FROM msgs m JOIN raw r ON r.id=m.rid WHERE m.id=?', (mid,)).fetchone()[0] or '').replace('\n', ' ')
                pos = fold(text).find(f[0]) if f else 0
                snip = ('…' + text[max(0, pos - 30):]) if pos > 40 else text
                d = self.local(ts)
                head = f"{d:%Y-%m-%d %H:%M} " if d else '                 '
                who = clip(printable(self.ar.short(self.label.get(cid, '?'))), 16).ljust(16)
                self.scr.addstr(1 + y, 0, clip(f'{head}{who} {snip}', W - 1), curses.A_REVERSE if k == sel else 0)
            self.scr.refresh()
            ch = self.scr.get_wch()
            if ch in ('q', '\x1b'):
                return
            if ch == curses.KEY_DOWN: sel += 1
            elif ch == curses.KEY_UP: sel -= 1
            elif ch == curses.KEY_NPAGE: sel += H - 3
            elif ch == curses.KEY_PPAGE: sel -= H - 3
            elif ch == curses.KEY_HOME: sel = 0
            elif ch == curses.KEY_END: sel = len(rows) - 1
            elif ch in ('\n', '\r', curses.KEY_ENTER):
                self.hits = [r[0] for r in rows]; self.hi = sel
                self.jump(rows[sel][0], rows[sel][1])
                return

    def jump(self, mid, cid):
        """Open the person (or channel) holding message mid, at that message."""
        self.filter = ''
        self.everyone = False
        if not self.shown(cid in self.group_conv, self.conv_net.get(cid)):
            self.view = 'channels' if cid in self.group_conv else None
        self.load_convs()
        self.ci = next((i for i, c in enumerate(self.convs) if cid in c[6]), 0)
        self.conv = None
        self.open_conv(mid)
        H, _ = self.scr.getmaxyx()
        self.mtop = max(0, self.mi - (H // 3) // 2)
        self.focus = 1
        self.status = f'hit {self.hi + 1}/{len(self.hits)}  (n)ext (N)prev' if self.hits else self.HELP

    def next_hit(self, step):
        if not self.hits:
            return
        self.hi = (self.hi + step) % len(self.hits)
        mid = self.hits[self.hi]
        cid = self.db.execute('SELECT conv FROM msgs WHERE id=?', (mid,)).fetchone()[0]
        self.jump(mid, cid)

    def goto_time(self, ts):
        if self.everyone:
            self.open_everyone(ts=ts)
        elif self.mids:
            self.mi = self.mtop = min(bisect.bisect_left(self.mts, ts), len(self.mids) - 1)
        self.focus = 1

    def goto_date(self):
        s = self.prompt('Go to date (YYYY-MM-DD): ')
        try:
            d = datetime.date.fromisoformat((s or '').strip())
        except ValueError:
            return
        z = self.ar.wall_zone
        self.goto_time(datetime.datetime(d.year, d.month, d.day, tzinfo=z).timestamp())

    def toggle_everyone(self, at_message=True):
        mid = self.mids[self.mi] if self.mids and at_message else None
        if self.everyone:
            if mid is not None:
                cid = self.msg(mid)[6]
                return self.jump(mid, cid)
            self.everyone = False
            self.conv = None
            self.status = self.HELP
            return self.open_conv()
        self.everyone = True
        self.open_everyone(mid)
        self.focus = 1
        self.status = 'everyone: (Enter) open person  (a) back'

    def leave_everyone(self):
        if self.everyone:
            self.everyone = False
            self.conv = None
            self.status = self.HELP

    def sources(self):
        if not self.mids:
            return
        mid = self.mids[self.mi]
        rows = self.db.execute('SELECT s.path, s.fmt, r.pos, r.ts, r.prec, r.naive, coalesce(r.dir || char(32), char(32)) || r.speaker, r.id = m.rid FROM raw r '
                               'JOIN sources s ON s.id=r.src JOIN msgs m ON m.id=r.mid WHERE r.mid=? ORDER BY r.id', (mid,)).fetchall()
        lines = [f'Message {mid}: {len(rows)} source(s)', '']
        for path, fmt, pos, ts, prec, naive, spk, shown in rows:
            d = datetime.datetime.fromtimestamp(ts, UTC).strftime('%Y-%m-%d %H:%M:%S UTC') if ts is not None else 'no time'
            how = {4: 'exact', 3: 'line clock', 1: 'session start', 0: 'guessed'}.get(prec, '?') + \
                (f', wall clock read as {self.ar.wall_zone.key}' if naive else '')
            lines += [('* ' if shown else '  ') + path, f'    {fmt} message #{pos + 1}  {d} ({how})  as "{spk}"', '']
        lines.append('* = text shown.  Any key to close')
        H, W = self.scr.getmaxyx()
        w = min(W - 4, max(width(l) for l in lines) + 4)
        h = min(H - 2, len(lines) + 2)
        win = curses.newwin(h, w, (H - h) // 2, (W - w) // 2)
        win.box()
        for i, l in enumerate(lines[:h - 2]):
            win.addstr(1 + i, 2, clip(printable(l), w - 4))
        win.refresh()
        win.get_wch()

    def open_links(self):
        if not self.mids:
            return
        text = self.msg(self.mids[self.mi])[4] or ''
        urls = list(dict.fromkeys(u for _, u in links.parse(printable(text)) if u))
        if not urls:
            self.status = 'no links in this message'
            return
        pick = urls[0]
        if len(urls) > 1:
            keys = '123456789abcdefghijklmnopqrstuvwxyz'
            H, W = self.scr.getmaxyx()
            rows = [f'{keys[i]}  {links.shorten(u, W - 12)}' for i, u in enumerate(urls[:len(keys)])]
            h, w = min(H - 2, len(rows) + 4), min(W - 4, max(map(len, rows)) + 6)
            win = curses.newwin(h, w, (H - h) // 2, (W - w) // 2)
            win.box()
            win.addstr(1, 2, 'Open which link? (Esc cancels)')
            for i, r in enumerate(rows[:h - 4]):
                win.addstr(3 + i, 2, clip(r, w - 4))
            win.refresh()
            ch = win.get_wch()
            if not isinstance(ch, str) or ch not in keys[:len(rows)]:
                return
            pick = urls[keys.index(ch)]
        open_url(pick)
        self.status = 'opened ' + links.shorten(pick, 80)

    def mouse(self):
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return
        up = b & curses.BUTTON4_PRESSED
        down = b & getattr(curses, 'BUTTON5_PRESSED', 0x200000)
        click = b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)
        if click and getattr(self, 'ruler', None) and y == self.ruler[0] and self.ruler[1] <= x < self.ruler[1] + self.ruler[2]:
            lo = self.ruler[3][x - self.ruler[1]][0]           # month number -> that month's first moment
            self.goto_time(datetime.datetime(lo // 12, lo % 12 + 1, 1, tzinfo=UTC).timestamp())
            return
        if click and getattr(self, 'map', None) and x == self.map[1] and self.map[0] <= y < self.map[0] + self.map[2]:
            y0, _, rows, t0, t1 = self.map
            self.goto_time(t0 + (t1 - t0) * (y - y0) / rows)
            return
        if click:
            for ly, lx, text, url, _ in self.shown_links:
                if ly == y and lx <= x < lx + width(text):
                    open_url(url)
                    self.status = 'opened ' + links.shorten(url, 80)
                    return
        if x < getattr(self, 'lw', 0):
            if up or down:
                self.leave_everyone()
                self.ci = max(0, min(len(self.convs) - 1, self.ci + (-3 if up else 3)))
                self.focus = 0
                self.pending = True
            elif click and y >= 2 and self.ctop + y - 2 < len(self.convs):
                self.ci = self.ctop + y - 2
                self.focus = 0
                self.pending = False
                if self.everyone:
                    self.everyone = False
                self.conv = None
                self.open_conv()
        else:
            if up or down:
                self.mi = max(0, min(len(self.mids) - 1, self.mi + (-3 if up else 3)))
                if up and self.mi < self.mtop:
                    self.mtop = self.mi
                self.focus = 1
                self.slide_everyone()
            elif click and y in getattr(self, 'line_msg', {}):
                self.mi = self.line_msg[y]
                self.focus = 1

    def run(self):
        while True:
            self.draw()
            H, _ = self.scr.getmaxyx()
            if self.pending:
                self.scr.timeout(150)
                try:
                    ch = self.scr.get_wch()
                except curses.error:
                    ch = None
                self.scr.timeout(-1)
                if ch is None:
                    self.pending = False
                    self.open_conv()
                    continue
            else:
                ch = self.scr.get_wch()
            if self.status != self.HELP and not self.everyone and not self.hits:
                self.status = self.HELP                 # a notice ('no matches', 'opened …') lasts one keypress
            if self.pending and not (self.focus == 0 and ch in NAV_KEYS):
                self.pending = False
                self.open_conv()
            if ch == curses.KEY_MOUSE:
                self.mouse()
                continue
            if ch == 'q':
                return
            if ch == '\t':
                self.focus ^= 1
            elif ch == '/':
                self.search(local=True)
            elif ch == '?':
                self.search()
            elif ch == 'n':
                self.next_hit(1)
            elif ch == 'N':
                self.next_hit(-1)
            elif ch == 'o':
                self.open_links()
            elif ch == 't':
                self.minutes = not self.minutes
            elif ch == 'z':
                self.zone_mode = (self.zone_mode + 1) % len(self.zones)
            elif ch == 'p':
                self.sources()
            elif ch == 'g':
                self.goto_date()
            elif ch == 'a':
                self.toggle_everyone(at_message=self.everyone or self.focus == 1)
            elif ch == 'w':
                if not self.everyone:
                    self.toggle_everyone()
            elif ch == 'S':
                self.show_status = not self.show_status
                cur = self.mids[self.mi] if self.mids else None
                self.open_conv('reload')
                if cur is not None:
                    self.mi = self.mtop = self.index_of(cur)
                self.status = 'status changes shown' if self.show_status else 'status changes folded'
            elif ch == 'f':
                s = self.prompt('Filter: ', self.filter)
                if s is not None:
                    self.filter = s; self.load_convs(); self.ci = 0; self.ctop = 0; self.conv = None; self.open_conv()
            elif ch in ('c', 'C'):
                self.cycle_view(1 if ch == 'c' else -1); self.load_convs(); self.ci = 0; self.ctop = 0; self.conv = None; self.open_conv()
            elif ch == 's':
                self.sort = (self.sort + 1) % 3; self.load_convs(); self.ci = 0; self.ctop = 0; self.conv = None; self.open_conv()
            elif self.focus == 0:
                step = {curses.KEY_DOWN: 1, curses.KEY_UP: -1, curses.KEY_NPAGE: H - 4, curses.KEY_PPAGE: -(H - 4),
                        curses.KEY_HOME: -10 ** 9, curses.KEY_END: 10 ** 9}.get(ch)
                if step:
                    self.leave_everyone()               # browsing people from everyone view opens them
                    self.ci = max(0, min(len(self.convs) - 1, self.ci + step))
                    self.pending = True
                elif ch in ('\n', '\r', curses.KEY_ENTER, curses.KEY_RIGHT):
                    self.focus = 1
            else:
                if self.everyone and ch in ('\n', '\r', curses.KEY_ENTER) and self.mids:
                    mid = self.mids[self.mi]
                    self.jump(mid, self.msg(mid)[6])
                    continue
                if self.everyone and ch in (curses.KEY_HOME, curses.KEY_END):
                    self.open_everyone(where='start' if ch == curses.KEY_HOME else 'end')
                    continue
                step = {curses.KEY_DOWN: 1, curses.KEY_UP: -1, curses.KEY_HOME: -10 ** 9, curses.KEY_END: 10 ** 9}.get(ch)
                if ch in (curses.KEY_NPAGE, curses.KEY_PPAGE) and self.mids and self.page:
                    w, rows, last = self.page
                    if ch == curses.KEY_NPAGE:
                        self.mtop = min(max(last, self.mtop + 1), len(self.mids) - 1)
                    else:
                        k, tot = self.mtop, 0
                        while k > 0 and tot + len(self.block(k - 1, w)) <= rows:
                            k -= 1; tot += len(self.block(k, w))
                        self.mtop = k if k < self.mtop else max(0, self.mtop - 1)
                    self.mi = self.mtop
                    self.slide_everyone()
                elif step:
                    self.mi = max(0, min(len(self.mids) - 1, self.mi + step))
                    if step < 0 and self.mi < self.mtop:
                        self.mtop = self.mi
                    self.slide_everyone()
                elif ch == curses.KEY_LEFT:
                    self.focus = 0


def run(archive):
    import locale
    locale.setlocale(locale.LC_ALL, '')
    if not os.path.exists(archive.db):
        sys.exit(f'{archive.db} missing: run "merged-chat-timeline import" first')
    os.environ.setdefault('ESCDELAY', '25')
    curses.wrapper(lambda scr: Viewer(scr, archive).run())
