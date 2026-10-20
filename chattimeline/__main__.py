"""merged-chat-timeline command line.

  merged-chat-timeline import [ARCHIVE]     build (or rebuild) the index from the files archive.toml describes
  merged-chat-timeline view [ARCHIVE]       browse it
  merged-chat-timeline suggest [ARCHIVE]    propose people.tsv merges (accounts that look like one person)
  merged-chat-timeline files [ARCHIVE]      list which files each [[source]] picks up, and which it does not

ARCHIVE is the folder holding archive.toml (default: $CHAT_ARCHIVE, else the nearest one above the current folder).
"""
import argparse, collections, os, sys

from .config import Archive, find_root


def main(argv=None):
    ap = argparse.ArgumentParser(prog='merged-chat-timeline', description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('command', choices=['import', 'view', 'suggest', 'files'])
    ap.add_argument('archive', nargs='?', default=os.environ.get('CHAT_ARCHIVE'))
    ap.add_argument('--config', help='archive.toml elsewhere than the archive folder')
    ap.add_argument('--limit', type=int, help='import only the first N files (testing)')
    a = ap.parse_args(argv)
    root = a.archive or find_root('.')
    ar = Archive(root, a.config)
    if not ar.sources and a.command in ('import', 'files'):
        sys.exit(f'no [[source]] entries in {ar.config_path}: see examples/archive.toml')
    if a.command == 'import':
        from .importer import build
        build(ar, limit=a.limit)
    elif a.command == 'view':
        from .viewer import run
        run(ar)
    elif a.command == 'suggest':
        from .suggest import main as suggest
        suggest(ar)
    elif a.command == 'files':
        got = collections.Counter(f for _, f, _ in ar.files())
        print('matched:', dict(got))
        matched = {p for p, _, _ in ar.files()}
        left = collections.Counter()
        for dp, dns, fns in os.walk(ar.root):
            for fn in fns:
                p = os.path.join(dp, fn)
                if p not in matched:
                    left[os.path.relpath(dp, ar.root)] += 1
        print('not matched by any source (folder: files):')
        for d, n in left.most_common(40):
            print(f'  {n:6d}  {d}')


if __name__ == '__main__':
    main()
