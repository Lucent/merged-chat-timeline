"""Log formats. Each parser takes (path, opts) and returns [(network, contact, kind, fmt, rows[, label])]:
  label an optional name for the other party, for sources that know it even when they never wrote
  kind  'im' (one-to-one), 'channel' (IRC channel, group chat) or 'status' (sign-on logs, server windows: hidden)
  rows  [ts_utc, prec, naive, speaker, strict_hash, text, session, session_start]
        prec: 4 exact, 3 the line's own clock, 1 session start only, 0 guessed
        speaker: '<out>name' / '<in>name' when the source says who sent it, '<system>' for notices, else a plain name
opts are the captures and fixed values of the matching [[source]] in archive.toml (network, contact, account, ...).
Times without a zone are read in the archive's wall-clock zone (CTX.zone)."""
import datetime

from .trillian_log import strict_hash, unescape_all, MON

MS, LINE, SESSION, GUESS = 4, 3, 1, 0


class Context:
    """Set once per process (see importer): the wall-clock zone and the owner test."""
    zone = datetime.timezone.utc
    archive = None

    def is_owner(self, name):
        return self.archive.is_owner(name, self.archive.load_me()) if self.archive else False


CTX = Context()


def set_archive(archive):
    CTX.archive = archive
    CTX.zone = archive.wall_zone


def local(y, mo, d, h=0, mi=0, s=0):
    """Wall-clock time in the archive's wall-clock zone -> UTC epoch."""
    return datetime.datetime(y, mo, d, h, mi, s, tzinfo=CTX.zone).timestamp()


def naive_to_utc(t):
    """'Seconds as if local were UTC' (Trillian header arithmetic) -> true UTC."""
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).replace(tzinfo=CTX.zone).timestamp()


def h24(h, ap):
    h = int(h)
    return h % 12 + (12 if ap and ap.upper().startswith('P') else 0) if ap else h


def row(ts, prec, naive, spk, text, html_src=False, shown=None, si=0, st=None):
    return [ts, prec, int(naive), spk, strict_hash(text, html_src), shown if shown is not None else text, si, st]


def registry():
    from . import trillian, other, social
    return {'trillian': trillian.parse, **other.PARSERS, **social.PARSERS}
