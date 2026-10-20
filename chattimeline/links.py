"""Find links in message text for display: clean Trillian's '(Link: U)' markup, trim trailing punctuation,
unwrap redirect wrappers. parse(text) -> [(piece, url_or_None)] whose pieces join to the display text."""
import html, re, urllib.parse

LINK_MARK = re.compile(r'\(Link: ([^)\s]+)\)')
URL = re.compile(r'''(?ix)
    (?: (?:https?|ftp)://[^\s<>"]+
      | www\.[a-z0-9-]+\.[^\s<>"]+
      | mailto:[^\s<>"]+ )''')
WRAPPERS = (('www.google.com', '/url', 'q'), ('www.google.com', '/url', 'url'), ('news.google.com', '/news/url', 'url'),
            ('www.facebook.com', '/l.php', 'u'), ('l.facebook.com', '/l.php', 'u'), ('m.facebook.com', '/l.php', 'u'),
            ('', '', 'url'))     # last: Outlook safelinks (*.safelinks.protection.outlook.com/?url=)


def _trim(u):
    """Drop trailing punctuation that belongs to the sentence; keep a ')' that closes a '(' inside the URL."""
    while u and u[-1] in '.,;:!?\'"*]}>':
        u = u[:-1]
    while u.endswith(')') and u.count(')') > u.count('('):
        u = u[:-1]
    return u


RESOLVE = None          # optional hook: url -> url (the viewer maps media links to local files)


def target(u):
    """Where clicking should go: scheme added, entities decoded, redirect wrappers unwrapped, then RESOLVE."""
    t = _target(u)
    return RESOLVE(t) if RESOLVE else t


def _target(u):
    u = html.unescape(u)
    if u.lower().startswith('www.'):
        u = 'http://' + u
    try:
        p = urllib.parse.urlsplit(u)
        q = urllib.parse.parse_qs(p.query)
        for host, path, key in WRAPPERS:
            if (host == p.netloc.lower() and p.path == path) or (not host and p.netloc.lower().endswith('safelinks.protection.outlook.com')):
                inner = q.get(key, [''])[0]
                if inner.lower().startswith(('http://', 'https://')):
                    return inner
    except ValueError:
        pass
    return u


def parse(text):
    """-> list of (piece, url or None). Trillian writes a link as '(Link: U)U' (U twice), '(Link: U)' (images) or
    '(Link: U)shown text'; the markup is dropped and the URL shown once."""
    out, pos = [], 0
    marks = []
    for m in LINK_MARK.finditer(text):
        u = m.group(1)
        if text.startswith(u, m.end()):                          # '(Link: U)U'
            marks.append((m.start(), m.end() + len(u), u, u))
        else:                                                    # '(Link: U)' or '(Link: U)shown text': where the
            sep = ' ' if text[m.end():m.end() + 1].strip() else ''   # shown text ends is unknown, so show U before it
            marks.append((m.start(), m.end(), u + sep, u))
    for s, e, shown, u in marks:
        out += _bare(text[pos:s])
        if shown.endswith(' '):
            out += [(shown[:-1], target(u)), (' ', None)]
        else:
            out.append((shown, target(u)))
        pos = e
    out += _bare(text[pos:])
    return [p for p in out if p[0]]


def _bare(t):
    out, pos = [], 0
    for m in URL.finditer(t):
        u = _trim(m.group(0))
        if not u or u.lower() in ('www.', 'mailto:'):
            continue
        out.append((t[pos:m.start()], None))
        out.append((u, target(u)))
        pos = m.start() + len(u)
    out.append((t[pos:], None))
    return out


def shorten(u, w):
    """Fit a URL's display into w columns: host + '…' + tail."""
    if len(u) <= w:
        return u
    m = re.match(r'(?i)((?:https?|ftp)://)?([^/]+)(.*)', u)
    host = m.group(2) if m else u[:w]
    keep = max(4, w - len(host) - 2)
    return (host + '/…' + u[-keep:])[:w] if len(host) + 6 <= w else u[:w - 1] + '…'
