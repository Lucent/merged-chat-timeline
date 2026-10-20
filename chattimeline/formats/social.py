"""Social-network exports: what you posted (Reddit, Bluesky) as one conversation per kind, and direct messages
(Instagram, TikTok, Twitter/X) as conversations per contact. Each parser(path, opts) -> [(network, contact, kind,
fmt, rows)] (see formats/__init__.py). A path may be the export's .tar.xz / .zip itself: members are read in a
stream, nothing is unpacked. Source keys: account (your handle on that network; some formats find it themselves)."""
import csv, datetime, email.utils, html, json, os, re, tarfile, zipfile

from ..config import key
from . import MS, LINE, GUESS, CTX, local, h24, row

UTC = datetime.timezone.utc


def members(path, want):
    """(name, bytes) for every member of a tar/zip whose name matches want; a plain file is its own only member."""
    if re.search(r'\.(tar(\.(xz|gz|bz2))?|tgz|txz)$', path, re.I):
        with tarfile.open(path, 'r|*') as t:
            for m in t:
                if m.isfile() and want.search(m.name):
                    yield m.name, t.extractfile(m).read()
    elif path.lower().endswith('.zip'):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if want.search(n):
                    yield n, z.read(n)
    elif want.search(path.replace(os.sep, '/')):
        yield path, open(path, 'rb').read()


def iso(s):
    """ISO 8601 -> UTC epoch, else None. Without a zone it is read as UTC (what these exports write)."""
    try:
        d = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
        return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()
    except (ValueError, AttributeError):
        return None


def fix(t):
    """UTF-8 written out as cp1252 (Meta's HTML exports) -> the real text."""
    try:
        return t.encode('cp1252').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return t


def side(name, account=None):
    mine = key(name) == key(account) if account else CTX.is_owner(name)
    return ('<out>' if mine else '<in>') + name


META_DATE = re.compile(r'(\w{3}) (\d+), (\d{4}),? (\d+):(\d\d)(?::(\d\d))? ?([AP]M)')


def meta_date(s):
    """'Apr 11, 2022, 1:22 PM' (Meta's HTML exports; a narrow no-break space before AM/PM in 2024) -> wall-clock epoch."""
    m = META_DATE.match(re.sub(r'\s+', ' ', html.unescape(s or '').replace(' ', ' ')).strip())
    if not m:
        return None
    mo = datetime.datetime.strptime(m.group(1), '%b').month
    return local(int(m.group(3)), mo, int(m.group(2)), h24(m.group(4), m.group(7)), int(m.group(5)), int(m.group(6) or 0))


def td_pairs(block):
    """Label/value pairs from a Meta export table: <td>label</td><td>value</td> rows, or one <td>label<div>value</div></td>."""
    out = []
    for tr in re.findall(r'<tr>(.*?)</tr>', block, re.S):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) >= 2:
            out.append((ig_text(tds[0]), ig_text(tds[1])))
        elif tds:
            m = re.match(r'\s*([^<]*)<div>(.*)</div>\s*$', tds[0], re.S)
            if m:
                out.append((ig_text(m.group(1)), ig_text(m.group(2))))
    return out


# ---------------- Reddit (data request: comments.csv, posts.csv, messages.csv) ----------------
def reddit(path, opts):
    """Reddit's data export. comments.csv and posts.csv become the conversations 'comments' and 'posts' (every line
    yours); messages.csv is private messages, one conversation per other user, and chat_history.csv the chat (direct
    chats by the other person, rooms as channels); both need account = your username."""
    name = os.path.basename(path).lower()
    me = opts.get('account') or 'me'
    rows = []
    with open(path, encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            m = re.match(r'(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)', r.get('date', ''))
            ts = datetime.datetime(*map(int, m.groups()), tzinfo=UTC).timestamp() if m else None
            sub = f"[r/{r['subreddit']}] " if r.get('subreddit') else ''
            if name == 'comments.csv':
                text = sub + (r.get('body') or '').strip() + '\n' + (r.get('permalink') or '')
                rows.append(('comments', 'im', row(ts, MS if ts else GUESS, 0, '<out>' + me, text.strip())))
            elif name == 'posts.csv':
                parts = [sub + (r.get('title') or '').strip(), (r.get('body') or '').strip()]
                if r.get('url') and r['url'] != r.get('permalink'):
                    parts.append(r['url'])
                parts.append(r.get('permalink') or '')
                rows.append(('posts', 'im', row(ts, MS if ts else GUESS, 0, '<out>' + me, '\n'.join(p for p in parts if p))))
            elif name == 'messages.csv' and opts.get('account'):
                frm, to = (re.sub(r'^/?(u/)?', '', (r.get(k) or '').strip()) for k in ('from', 'to'))   # '/u/name' = 'name'; '/r/sub' -> 'r/sub'
                other = to if key(frm) == key(me) else frm
                text = ((f"[{r['subject']}] " if r.get('subject') else '') + (r.get('body') or '')).strip()
                rows.append((key(other), 'im', row(ts, MS if ts else GUESS, 0, side(frm, me), text)))
            elif name == 'chat_history.csv' and opts.get('account'):
                m = re.match(r'(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)', r.get('created_at', ''))
                ts = datetime.datetime(*map(int, m.groups()), tzinfo=UTC).timestamp() if m else None
                direct = (r.get('conversation_type') or '').lower() == 'direct'
                contact = r.get('channel_name') or re.sub(r'^.*/room/!?([^:]{8}).*$', r'chat \1', r.get('channel_url') or '')
                rows.append((key(contact), 'im' if direct else 'channel',
                             row(ts, MS if ts else GUESS, 0, side(r.get('username') or '', me), (r.get('message') or '').strip())))
    if name == 'chat_history.csv':                     # a direct chat is named by whoever in it is not me
        chats = {}
        for contact, kind, rw in rows:
            chats.setdefault((contact, kind), []).append(rw)
        rows = []
        for (contact, kind), rs in chats.items():
            if kind == 'im':
                others = {r[3][4:] for r in rs if r[3].startswith('<in>')}
                contact = key(', '.join(sorted(others))) or contact
            rows += [(contact, kind, r) for r in rs]
    out = {}
    for contact, kind, rw in rows:
        out.setdefault((contact, kind), []).append(rw)
    return [('reddit', c, k, 'reddit', rs) for (c, k), rs in out.items()]


# ---------------- Bluesky (repository export as JSON: {"app.bsky.feed.post": [...], ...}) ----------------
def bluesky(path, opts):
    """A Bluesky repo exported to JSON (collection name -> records). Your posts become the conversation 'posts';
    replies are marked, embedded links and images noted."""
    d = json.load(open(path, encoding='utf-8'))
    me = opts.get('account') or re.split(r'\s*\(', os.path.basename(path))[0]
    rows = []
    for p in d.get('app.bsky.feed.post', []):
        text = p.get('text') or ''
        if 'reply' in p:
            text = '↩ ' + text
        e = p.get('embed') or {}
        for e in (e, e.get('media') or {}):
            t = e.get('$type', '')
            if t.endswith('embed.external'):
                text += '\n' + (e.get('external') or {}).get('uri', '')
            elif t.endswith('embed.images'):
                text += ''.join(f"\n[image{(': ' + i['alt']) if i.get('alt') else ''}]" for i in e.get('images', []))
            elif t.endswith('embed.video'):
                text += '\n[video]'
            elif t.endswith('embed.record') or t.endswith('embed.recordWithMedia'):
                text += '\n[quote post]'
        if p.get('rkey'):
            text += f"\nhttps://bsky.app/profile/{me}/post/{p['rkey']}"
        ts = iso(p.get('createdAt', ''))
        rows.append(row(ts, MS if ts else GUESS, 0, '<out>' + me, text.strip()))
    return [('bluesky', 'posts', 'im', 'bluesky', rows)] if rows else []


# ---------------- Instagram (Download Your Information: messages.json 2019, messages/inbox/*/message_N.html) ----------------
IG_MEMBER = re.compile(r'(?:^|/)(messages|media|comments)\.json$|messages/(?:inbox|message_requests)/[^/]+/message_\d+\.html$'
                       r'|/(?:content/posts_\d+|comments/(?:post_comments(?:_\d+)?|reels_comments))\.html$')
IG_BLOCK = re.compile(r'<div class="pam _3-95 _2ph- [^"]*uiBoxWhite noborder">')
IG_MEDIA = re.compile(r'(?:src|href)="([^"]+\.(jpe?g|png|gif|webp|mp4|m4a|aac|wav))"', re.I)


def ig_text(h):
    return fix(html.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'(?i)<br\s*/?>', '\n', h)))).strip()


def instagram(path, opts):
    """An Instagram export, packed or unpacked: direct messages, plus your posts and comments as the conversations
    <account>/posts and <account>/comments. account = the export's username (needed for the 2019 JSON, whose senders
    are usernames); HTML senders are display names, judged against [me]."""
    account = opts.get('account')
    mine = '<out>' + (account or 'me')
    convs = []
    for name, data in members(path, IG_MEMBER):
        base = name.rsplit('/', 1)[-1]
        if base in ('media.json', 'comments.json'):          # 2019: photos and stories with captions; your comments
            d, rows = json.loads(data.decode('utf-8')), []
            if base == 'media.json':
                for what, items in (('photo', d.get('photos') or []), ('story', d.get('stories') or [])):
                    for it in items:
                        ts = iso(it.get('taken_at', ''))
                        text = (fix(it.get('caption') or '') + f"\n[{what} {(it.get('path') or '').rsplit('/', 1)[-1]}]").strip()
                        rows.append(row(ts, MS if ts else GUESS, 0, mine, text))
            else:
                for it in d.get('media_comments') or []:     # [time, text, post owner]
                    if len(it) >= 2:
                        ts, owner = iso(it[0]), it[2] if len(it) > 2 else ''
                        rows.append(row(ts, MS if ts else GUESS, 0, mine, fix(it[1]) + (f"\n(on {owner}'s post)" if owner else '')))
            if rows:
                convs.append(('instagram', f"{account}/{'posts' if base == 'media.json' else 'comments'}", 'im', 'instagram', rows))
            continue
        if base.startswith(('posts_', 'post_comments', 'reels_comments')):
            h, rows, posts = data.decode('utf-8', 'replace'), [], base.startswith('posts_')
            for b in IG_BLOCK.split(h)[1:]:
                pairs = td_pairs(b)
                cap = re.search(r'^<div class="_3-95 _2pim[^"]*">(.*?)</div>', b)
                d = re.search(r'<div class="_3-94 [^"]*">([^<]*)<', b)
                ts = (meta_date(d.group(1)) if d else None) or next((t for _, v in pairs for t in [meta_date(v)] if t), None)
                if posts:
                    text = ig_text(cap.group(1)) if cap else ''
                    text += ''.join(f"\n[{'video' if ext.lower() == 'mp4' else 'photo'} {u.rsplit('/', 1)[-1]}]"
                                    for u, ext in dict.fromkeys(IG_MEDIA.findall(b)))
                else:
                    text = next((v for k, v in pairs if 'comment' in k.lower()), '') or (ig_text(cap.group(1)) if cap else '')
                    owner = next((v for k, v in pairs if 'owner' in k.lower()), '')
                    text += f"\n(on {owner}'s post)" if owner else ''
                if text.strip():
                    rows.append(row(ts, LINE if ts else GUESS, 1, mine, text.strip()))
            if rows:
                convs.append(('instagram', f"{account}/{'posts' if posts else 'comments'}", 'im', 'instagram', rows[::-1]))
            continue
        if base == 'messages.json':
            for th in json.loads(data.decode('utf-8')):
                people = [p for p in th.get('participants', []) if key(p) != key(account)]
                kind = 'channel' if len(people) > 1 else 'im'
                rows = []
                for c in th.get('conversation', []):
                    text = fix(c.get('text') or '')
                    tags = [k for k in ('media', 'media_url', 'voice_media', 'story_share', 'profile_share', 'animated_media_images') if c.get(k)]
                    if not text and not tags and not c.get('heart') and not c.get('likes'):
                        continue
                    text = text or ('[' + ', '.join(t.replace('_', ' ') for t in tags) + ']' if tags else '[like]')
                    ts = iso(c.get('created_at', ''))
                    rows.append(row(ts, MS if ts else GUESS, 0, side(c.get('sender') or '', account), text))
                if rows:
                    convs.append(('instagram', ', '.join(people) or key(account or ''), kind, 'instagram', rows))
            continue
        folder = name.rsplit('/', 2)[-2]                  # username_<thread id>: 10 alphanumerics (2021-22) or digits (2024)
        contact = re.sub(r'_(\d+|[A-Za-z0-9]{10})$', '', folder)
        h = data.decode('utf-8', 'replace')
        rows, senders = [], set()
        for b in IG_BLOCK.split(h)[1:]:
            s = re.search(r'^<div class="_3-95 _2pim[^"]*">(.*?)</div>', b)
            s = ig_text(s.group(1)) if s else ''
            d = re.search(r'<div class="_3-94 [^"]*">([^<]*)<', b)
            m = re.match(r'(\w{3}) (\d+), (\d{4}),? (\d+):(\d\d)(?::(\d\d))? ?([AP]M)',
                         re.sub(r'\s+', ' ', html.unescape(d.group(1)).replace(' ', ' ')).strip()) if d else None
            if not m:
                continue
            mo = datetime.datetime.strptime(m.group(1), '%b').month
            ts = local(int(m.group(3)), mo, int(m.group(2)), h24(m.group(4), m.group(7)), int(m.group(5)), int(m.group(6) or 0))
            body = re.search(r'<div class="_3-95 _(?:2let|a6-p)"><div><div></div><div>(.*?)</div>', b, re.S)
            text = ig_text(body.group(1)) if body else ''
            media = IG_MEDIA.findall(b)
            if media:
                text += ''.join(f"\n[{'video' if ext.lower() in ('mp4',) else 'audio' if ext.lower() in ('m4a', 'aac', 'wav') else 'photo'} {u.rsplit('/', 1)[-1]}]"
                                for u, ext in media)
            if not text.strip():
                continue
            senders.add(key(s))
            rows.append(row(ts, LINE, 1, side(s), text.strip()))
        if rows:
            convs.append(('instagram', contact, 'channel' if len(senders) > 2 else 'im', 'instagram', rows[::-1]))
    return convs


# ---------------- TikTok (Download your data: JSON, or the TXT folder with Direct Messages/Direct Messages.txt) ----------------
TT_LINE = re.compile(r'^\[?(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\]? ([^:]+): ?(.*)$')


def tiktok(path, opts):
    """TikTok's export: direct messages, and your comments as <account>/comments (JSON, or the TXT folder's
    Direct Messages/Direct Messages.txt and Comments/Comments.txt). Times are UTC. The account's username comes from
    the export's own Profile section."""
    account = opts.get('account')
    threads, comments = [], []
    if path.lower().endswith('.json'):
        d = json.load(open(path, encoding='utf-8'))
        prof = (d.get('Profile') or {}).get('Profile Information') or (d.get('Profile') or {}).get('Profile Info') or {}
        account = account or (prof.get('ProfileMap') or prof).get('userName')
        dm = d.get('Direct Messages') or d.get('Direct Message') or {}
        for title, msgs in ((dm.get('Chat History') or {}).get('ChatHistory') or {}).items():
            threads.append((title, [(m.get('Date', ''), m.get('From', ''), m.get('Content', '')) for m in msgs]))
        for c in ((d.get('Comment') or {}).get('Comments') or {}).get('CommentsList') or []:
            comments.append((c.get('date', ''), c.get('comment', ''), c.get('url', '')))
    else:
        if not account:
            for fn in ('Profile Info.txt', 'Profile Information.txt'):
                p = os.path.join(os.path.dirname(os.path.dirname(path)), 'Profile', fn)
                if os.path.exists(p):
                    m = re.search(r'(?mi)^Username: *(\S+)', open(p, encoding='utf-8', errors='replace').read())
                    account = m.group(1) if m else None
        text = open(path, encoding='utf-8', errors='replace').read().replace('\r', '')
        if 'comment' in os.path.basename(path).lower():   # blank-line separated blocks of 'Field: value' lines
            for block in re.split(r'\n\s*\n', text):
                f = dict(re.findall(r'(?m)^([A-Za-z ]+): ?(.*)$', block))
                if f.get('Comment'):
                    comments.append((f.get('Date', ''), f['Comment'], f.get('Url') or f.get('Video Link') or f.get('Photo') or ''))
            text = ''
        cur = None
        for line in text.split('\n'):
            if line.startswith('>>> '):
                cur = (line[4:], [])
                threads.append(cur)
            elif cur is None:
                continue
            else:
                m = TT_LINE.match(line)
                if m:
                    cur[1].append((f'{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}',
                                   m.group(7), m.group(8)))
                elif cur[1] and line.strip():
                    d, f, c = cur[1][-1]
                    cur[1][-1] = (d, f, c + '\n' + line)
    convs = []
    for title, msgs in threads:
        contact = re.sub(r'^Chat History with ', '', title).strip().rstrip(':')
        rows = []
        for date, frm, text in msgs:
            m = re.match(r'(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)', date)
            ts = datetime.datetime(*map(int, m.groups()), tzinfo=UTC).timestamp() if m else None
            if text.strip():
                rows.append(row(ts, MS if ts else GUESS, 0, side(frm, account), text.strip()))
        if rows:
            convs.append(('tiktok', key(contact), 'im', 'tiktok', rows))
    rows = []
    for date, text, url in comments:
        m = re.match(r'(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)', date)
        ts = datetime.datetime(*map(int, m.groups()), tzinfo=UTC).timestamp() if m else None
        if text.strip():
            rows.append(row(ts, MS if ts else GUESS, 0, '<out>' + (account or 'me'), (text.strip() + '\n' + url).strip()))
    if rows:
        convs.append(('tiktok', f'{account}/comments', 'im', 'tiktok', rows))
    return convs


# ---------------- Twitter / X (archive: data/direct-messages.js, direct-messages-group.js, account.js) ----------------
TW_MEMBER = re.compile(r'(?:^|/)data/(direct-messages(?:-group)?|account|tweets?)\.js$')
TW_FILES = ('account.js', 'direct-messages.js', 'direct-messages-group.js', 'tweets.js', 'tweet.js')


def ytd(data):
    s = data.decode('utf-8')
    return json.loads(s[s.index('['):] if s.lstrip().startswith('window.') else s)


def user_names(path, opts):
    """{id: shown name} from the users.tsv named by the source's names key (id, handle, full name per line), looked
    for in the export's folder and up to three above it."""
    fn, d = opts.get('names'), os.path.dirname(path)
    for _ in range(4):
        if fn and os.path.exists(os.path.join(d, fn)):
            out = {}
            for l in open(os.path.join(d, fn), encoding='utf-8'):
                f = l.rstrip('\n').split('\t')
                if not l.startswith('#') and len(f) >= 2 and f[1]:
                    out[f[0]] = f[2] if len(f) > 2 and f[2].strip() else '@' + f[1]
            return out
        d = os.path.dirname(d)
    return {}


def twitter(path, opts):
    """A Twitter archive (the zip, a tar of it, or its data/direct-messages.js or a tweets.js with the other data
    files beside it): direct messages, group chats, and your tweets as the conversation 'tweets'. The archive names
    people only by numeric id: names = "users.tsv" (see user_names) gives them names, and people.tsv can still group
    them (twitter:<id>)."""
    found, names = {}, user_names(path, opts)
    if path.lower().endswith('.js'):
        for fn in TW_FILES:
            p = os.path.join(os.path.dirname(path), fn)
            if os.path.exists(p):
                found[fn[:-3]] = ytd(open(p, 'rb').read())
    else:
        for name, data in members(path, TW_MEMBER):
            found[TW_MEMBER.search(name).group(1)] = ytd(data)
    found.setdefault('tweets', found.pop('tweet', None))
    acct = (found.get('account') or [{}])[0].get('account', {})
    me_id, me = acct.get('accountId'), opts.get('account') or acct.get('username') or 'me'

    def who(uid):
        return me if uid == me_id else names.get(uid, uid)

    def text_of(m):
        t = m.get('text') or ''
        for u in m.get('urls') or []:
            if u.get('url') and u.get('expanded'):
                t = t.replace(u['url'], u['expanded'])
        for u in m.get('mediaUrls') or []:
            if u not in t:
                t += '\n' + u
        return t.strip()

    convs = []
    for grp, key_ in ((False, 'direct-messages'), (True, 'direct-messages-group')):
        for c in found.get(key_) or []:
            c = c.get('dmConversation') or {}
            cid, rows, name = c.get('conversationId', ''), [], None
            for m in c.get('messages', []):
                for ev, v in m.items():
                    ts = iso(v.get('createdAt', ''))
                    if ev in ('messageCreate', 'welcomeMessageCreate'):
                        t = text_of(v)
                        if t:
                            spk = ('<out>' if v.get('senderId') == me_id else '<in>') + who(v.get('senderId', ''))
                            rows.append(row(ts, MS if ts else GUESS, 0, spk, t))
                    elif ev == 'conversationNameUpdate':
                        name = v.get('name') or name
                        rows.append(row(ts, MS if ts else GUESS, 0, '<system>', f"{who(v.get('initiatingUserId', ''))} named the group {v.get('name', '')}"))
                    elif ev == 'participantsLeave':
                        rows.append(row(ts, MS if ts else GUESS, 0, '<system>', ', '.join(map(who, v.get('userIds', []))) + ' left'))
                    elif ev == 'joinConversation':
                        rows.append(row(ts, MS if ts else GUESS, 0, '<system>', f"{who(v.get('initiatingUserId', ''))} added "
                                        + ', '.join(map(who, v.get('participantsSnapshot', [])))))
            if not rows:
                continue
            if grp:
                convs.append(('twitter', name or cid, 'channel', 'twitter', rows))
            else:
                other = [p for p in cid.split('-') if p != me_id]
                convs.append(('twitter', other[0] if other else cid, 'im', 'twitter', rows, names.get(other[0]) if other else None))
    rows = []
    for t in found.get('tweets') or []:
        t = t.get('tweet', t)
        text = text_of({'text': t.get('full_text') or '',
                        'urls': [{'url': u.get('url'), 'expanded': u.get('expanded_url')} for u in (t.get('entities') or {}).get('urls') or []]})
        if t.get('in_reply_to_status_id_str') or t.get('in_reply_to_screen_name'):
            text = '↩ ' + text
        if t.get('id_str'):
            text += f"\nhttps://x.com/{me}/status/{t['id_str']}"
        try:
            ts = datetime.datetime.strptime(t.get('created_at', ''), '%a %b %d %H:%M:%S %z %Y').timestamp()
        except ValueError:
            ts = None
        rows.append(row(ts, MS if ts else GUESS, 0, '<out>' + me, text.strip()))
    if rows:
        convs.append(('twitter', 'tweets', 'im', 'twitter', rows))
    return convs


# ---------------- Tinder (data.json from the data request) ----------------
def tinder(path, opts):
    """Tinder's data.json: your side of every match's chat (the export holds only what you sent), one conversation
    per match ('Match 12'; 'to' is only a numeric id in current exports, used as the name when it is a name)."""
    d = json.load(open(path, encoding='utf-8'))
    me = opts.get('account') or (d.get('User') or {}).get('name') or 'me'
    convs = []
    for th in d.get('Messages') or []:
        rows, name = [], ''
        for m in th.get('messages') or []:
            try:
                ts = email.utils.parsedate_to_datetime(m.get('sent_date', '')).timestamp()
            except (TypeError, ValueError):
                ts = None
            name = m['to'] if isinstance(m.get('to'), str) else name
            text = html.unescape(m.get('message') or '').strip()
            if text:
                rows.append(row(ts, MS if ts else GUESS, 0, '<out>' + me, text))
        if rows:
            convs.append(('tinder', key(th.get('match_id') or name), 'im', 'tinder', rows, name or None))
    return convs


PARSERS = {'reddit': reddit, 'bluesky': bluesky, 'instagram': instagram, 'tiktok': tiktok, 'twitter': twitter, 'tinder': tinder}
