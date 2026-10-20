"""archive.toml: everything specific to one chat-log archive. It lives in the archive's root folder; paths in it are
relative to that folder. See examples/archive.toml for every key.

    database = "~/.local/share/merged-chat-timeline/mylogs.db"     # where the index goes (default shown, named after the root)

    [time]
    wall_clock = "America/New_York"     # zone of the PCs/phones that wrote logs with no zone in them
    then = [["1990-01-01", "America/New_York"], ["2009-06-01", "Europe/Berlin"]]   # "local time then" timeline

    [me]
    names = ["myhandle", "my.email@example.com"]     # names that mean me (case and spaces ignored)
    patterns = ['^myhandle[_-]?\\w{0,3}$']            # regexes over the same normalized form

    [display]
    abbreviate = { "@corp\\.example\\.com" = "@EX" }   # shown shorter; people.tsv may use either form

    [networks]
    rename = { examplecorp = "jabber" }               # a Trillian medium that was really another network

    [[fold]]                                          # one account written two ways
    network = "jabber"
    suffix = "@example.com"
    into = "@corp.example.com"

    [[source]]                                        # which files, in which format; first match wins
    format = "trillian"
    path = "Logs {machine}/{network}/**/{contact}.{ext:log|xml|txt}"
    exclude = ["**/assets/**"]

Path templates: {name} matches one path segment (or the part of it up to the next literal text), {name:regex} uses
your regex, * is any text within a segment and ** any number of folders. Captured names feed the format (network,
contact, account, ...); a source may also fix them, e.g. network = "icq", and may use the captures: contact = "{name}".
Next to archive.toml: people.tsv (Person <TAB> network:contact), me.tsv (one name per line) and groups.tsv (Group <TAB>
person or network:contact pattern [<TAB> colour]); all optional.
"""
import datetime, os, re, tomllib, unicodedata, zoneinfo

CONFIG_NAME = 'archive.toml'


def key(name):
    """Comparison form of a name: lower case, no whitespace."""
    return re.sub(r'\s', '', (name or '').lower())


def fold(text):
    """Search folding: lower case, accents removed, letters like ð/ø/ß/æ spelled out."""
    t = unicodedata.normalize('NFKD', text or '')
    t = ''.join(c for c in t if not unicodedata.combining(c))
    for a, b in (('ð', 'd'), ('đ', 'd'), ('ø', 'o'), ('ß', 'ss'), ('æ', 'ae'), ('œ', 'oe'), ('þ', 'th'), ('ł', 'l'), ('ı', 'i')):
        t = t.replace(a, b).replace(a.upper(), b)
    return t.lower()


def compile_template(t):
    """Path template -> regex (see module docstring)."""
    out, i = ['^'], 0
    while i < len(t):
        if t.startswith('**/', i):
            out.append('(?:.*/)?'); i += 3
        elif t.startswith('**', i):
            out.append('.*'); i += 2
        elif t[i] == '*':
            out.append('[^/]*'); i += 1
        elif t[i] == '{':
            j = t.index('}', i)
            name, _, rx = t[i + 1:j].partition(':')
            out.append(f'(?P<{name}>{rx or "[^/]+?"})'); i = j + 1
        else:
            out.append(re.escape(t[i])); i += 1
    return re.compile(''.join(out) + '$')


class Source:
    def __init__(self, d):
        self.format = d['format']
        self.root = d.get('root')                        # a folder outside the archive that path is relative to
        self.path = compile_template(d['path'])
        self.exclude = [compile_template(x) for x in d.get('exclude', [])]
        self.fixed = {k: v for k, v in d.items() if k not in ('format', 'path', 'exclude', 'root')}

    def match(self, rel):
        m = self.path.match(rel)
        if not m or any(x.match(rel) for x in self.exclude):
            return None
        caps = {k: v for k, v in m.groupdict().items() if v is not None}
        opts = dict(caps)
        for k, v in self.fixed.items():
            opts[k] = v.format(**caps) if isinstance(v, str) else v
        return opts


class Archive:
    def __init__(self, root, config_path=None):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.config_path = config_path or os.path.join(self.root, CONFIG_NAME)
        c = {}
        if os.path.exists(self.config_path):
            with open(self.config_path, 'rb') as f:
                c = tomllib.load(f)
        self.raw = c
        here = os.path.dirname(os.path.abspath(self.config_path))
        default_db = os.path.join('~', '.local', 'share', 'merged-chat-timeline', re.sub(r'\W+', '-', os.path.basename(self.root)).strip('-') + '.db')
        self.db = os.path.expanduser(c.get('database', default_db))
        t = c.get('time', {})
        self.wall_zone = zoneinfo.ZoneInfo(t.get('wall_clock', 'UTC'))
        then = t.get('then') or [['1970-01-01', t.get('wall_clock', 'UTC')]]
        self.timeline = sorted((datetime.datetime.fromisoformat(d).replace(tzinfo=datetime.timezone.utc).timestamp(),
                                zoneinfo.ZoneInfo(z)) for d, z in then)
        m = c.get('me', {})
        self.owner_names = {key(n) for n in m.get('names', [])}
        self.owner_rx = [re.compile(p) for p in m.get('patterns', [])]
        self.abbreviate = [(re.compile(p, re.I), r) for p, r in c.get('display', {}).get('abbreviate', {}).items()]
        self.network_rename = {k.lower(): v.lower() for k, v in c.get('networks', {}).get('rename', {}).items()}
        self.folds = c.get('fold', [])
        self.sources = [Source(s) for s in c.get('source', [])]
        self.people_file = os.path.join(here, c.get('people', 'people.tsv'))
        self.me_file = os.path.join(here, c.get('me_file', 'me.tsv'))
        self.groups_file = os.path.join(here, c.get('groups', 'groups.tsv'))

    # ---- names ----
    def is_owner(self, name, extra=()):
        k = re.sub(r'says$', '', key(name))              # 'Me says' from MSN text pasted into a chat
        return k in self.owner_names or k in extra or any(r.match(k) for r in self.owner_rx)

    def short(self, name):
        for rx, rep in self.abbreviate:
            name = rx.sub(rep, name or '')
        return name

    def norm_contact(self, net, contact):
        """A conversation's contact as people.tsv writes it: abbreviated, lower case, Facebook ids without the u."""
        c = self.short(contact).lower()
        return re.sub(r'^u(?=\d+$)', '', c) if net == 'facebook' else c

    def load_me(self):
        return {key(r[0]) for r in _lines(self.me_file)}

    def load_people(self):
        """-> {(network, contact key): person}"""
        out = {}
        for r in _lines(self.people_file):
            if len(r) >= 2 and ':' in r[1]:
                net, _, c = r[1].partition(':')
                out[(net.strip().lower(), key(c))] = r[0].strip()
        return out

    def load_groups(self):
        """-> [(group, [members], xterm colour or None)] in file order. groups.tsv lines: group <TAB> member [<TAB> colour];
        a member is a person named in people.tsv or network:contact with * wildcards (the contact as the viewer shows it)."""
        out, idx = [], {}
        for r in _lines(self.groups_file):
            if len(r) < 2 or not r[1].strip():
                continue
            g = r[0].strip()
            if g not in idx:
                idx[g] = len(out)
                out.append([g, [], None])
            out[idx[g]][1].append(r[1].strip())
            if len(r) > 2 and r[2].strip().isdigit():
                out[idx[g]][2] = int(r[2])
        return [tuple(g) for g in out]

    def zone_at(self, ts):
        z = self.timeline[0][1]
        for t, zz in self.timeline:
            if ts >= t:
                z = zz
        return z

    def files(self):
        """-> [(absolute path, format, options)] for every file some [[source]] matches (first match wins). Sources
        with a root are matched under that folder (relative to the archive root) instead."""
        out, roots = [], {}
        for s in self.sources:
            roots.setdefault(os.path.normpath(os.path.join(self.root, os.path.expanduser(s.root or ''))), []).append(s)
        for root, sources in roots.items():
            for dp, dns, fns in os.walk(root):
                dns.sort()
                for fn in sorted(fns):
                    p = os.path.join(dp, fn)
                    rel = os.path.relpath(p, root).replace(os.sep, '/')
                    for s in sources:
                        opts = s.match(rel)
                        if opts is not None:
                            out.append((p, s.format, opts))
                            break
        return out


def _lines(path):
    if not os.path.exists(path):
        return []
    return [l.rstrip('\n').split('\t') for l in open(path, encoding='utf-8') if l.strip() and not l.startswith('#')]


def find_root(start='.'):
    """The nearest folder at or above start that holds an archive.toml."""
    d = os.path.abspath(os.path.expanduser(start))
    while True:
        if os.path.exists(os.path.join(d, CONFIG_NAME)):
            return d
        up = os.path.dirname(d)
        if up == d:
            return os.path.abspath(os.path.expanduser(start))
        d = up
