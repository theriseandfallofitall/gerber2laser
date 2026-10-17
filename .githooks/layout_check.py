#!/usr/bin/env python3
"""Repo layout check: the rules that keep a repo easy to find your way round, on the tracked files.

Copied into a repo as .githooks/layout_check.py by the organise-repo / start-repo skills and run by
.githooks/pre-commit on every commit (and by the repo's test command if it has one). Settings live
in .layout.json at the repo root; every key is optional:

  rootFiles     files allowed at the root (README.md, CLAUDE.md, .gitignore, .layout.json are always allowed)
  ruleDocs      UPPER-CASE rule docs allowed in docs/ (e.g. "docs/SECURITY.md")
  docsSubdirs   allowed docs/ subfolders (default: research, archive)
  docsSkip      doc paths not checked for names or the Docs index (e.g. worked examples)
  groupedDirs   {"tools": ["build", "checks"]}: files only inside those group folders, never loose
  codeMap       {"file": "docs/code.md", "dirs": ["src", "tools"]}: every file directly in a group
                folder or listed dir is named (backticked) in that file; a backticked glob covers several
  tests         {"dir": "test", "pattern": "^[a-z0-9-]+\\.test\\.(mjs|js|ts|py)$",
                 "subdirs": ["helpers", "fixtures"], "runners": ["package.json"], "manual": []}
                every test file matches the pattern and is named in one of the runner files
                (skip that part with "runners": [] when the runner discovers tests itself)
  untrackedOk   paths that docs may name though git does not track them (build output, scratch)

Checks: root files; every top-level folder in README "## Repo map"; every doc in README "## Docs";
README links resolve; doc names lower-case kebab (rule docs UPPER-CASE); no tracked junk; grouped
folders; code map; tests; every backticked repo path in README.md, CLAUDE.md, AGENTS.md, TODO.md and
docs/ exists (docs/archive/ and docs/moves.md exempt: history); docs/moves.md exists.
Exit 1 with one line per problem. Fix the layout (move, document, log the move in docs/moves.md);
do not loosen the check without the owner's say-so.
"""
import json
import os
import re
import subprocess
import sys

os.chdir(subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=True).stdout.strip())
cfg = json.load(open('.layout.json', encoding='utf-8')) if os.path.exists('.layout.json') else {}
files = [f for f in subprocess.run(['git', 'ls-files', '-z'], capture_output=True, text=True, check=True).stdout.split('\0')
         if f and os.path.exists(f)]
fail = []
read = lambda p: open(p, encoding='utf-8', errors='replace').read() if os.path.exists(p) else ''


def section(md, h):
    i = md.find(f'\n## {h}\n')
    if i < 0:
        return ''
    j = md.find('\n## ', i + 1)
    return md[i:] if j < 0 else md[i:j]


readme = read('README.md')

# root files
root_ok = set(cfg.get('rootFiles', [])) | {'README.md', 'CLAUDE.md', '.gitignore', '.layout.json'}
for f in files:
    if '/' not in f and f not in root_ok:
        fail.append(f'{f}: not an allowed root file (move it into a folder, or add it to .layout.json rootFiles and README Repo map)')

# every top-level folder in the Repo map
rmap = section(readme, 'Repo map')
if not rmap:
    fail.append('README.md has no "## Repo map"')
for d in sorted({f.split('/')[0] for f in files if '/' in f}):
    if f'`{d}/' not in rmap:
        fail.append(f'{d}/: not in README Repo map')

# docs: listed, named, placed
docs_index = section(readme, 'Docs')
if not docs_index:
    fail.append('README.md has no "## Docs"')
skip = tuple(cfg.get('docsSkip', []))
rule_docs = set(cfg.get('ruleDocs', []))
subdirs = set(cfg.get('docsSubdirs', ['research', 'archive']))
docs = [f for f in files if f.startswith('docs/') and f.endswith('.md') and not f.startswith(skip)]
for f in docs:
    if f'({f})' not in docs_index:
        fail.append(f'{f}: not in README Docs')
    base = f.rsplit('/', 1)[1]
    if f in rule_docs:
        continue
    if not re.fullmatch(r'[a-z0-9]+(-[a-z0-9]+)*\.md', base) and base != 'README.md':
        fail.append(f'{f}: doc names are lower-case kebab (rule docs UPPER-CASE, listed in .layout.json ruleDocs)')
for f in files:
    if f.startswith('docs/') and f.count('/') >= 2 and not f.startswith(skip) and f.split('/')[1] not in subdirs:
        fail.append(f'{f}: docs/ subfolders are {", ".join(sorted(subdirs))} (.layout.json docsSubdirs)')
for m in re.finditer(r'\]\(([^)#\s]+)\)', readme):
    if not re.match(r'https?:|mailto:', m.group(1)) and not os.path.exists(m.group(1)):
        fail.append(f'README.md links {m.group(1)}, which does not exist')
if 'docs/moves.md' not in files:
    fail.append('docs/moves.md (the log of moved paths) is missing')

# junk
junk = re.compile(r'(^|/)(__pycache__/|\.DS_Store$|Thumbs\.db$|desktop\.ini$|\.ipynb_checkpoints/)|\.(pyc|orig|rej|tmp|bak|log|swp)$|~$')
for f in files:
    if junk.search(f):
        fail.append(f'{f}: generated or leftover file tracked (delete it, add it to .gitignore)')

# grouped folders
for top, groups in cfg.get('groupedDirs', {}).items():
    for f in files:
        if f.startswith(top + '/'):
            p = f.split('/')
            if len(p) < 3 or p[1] not in groups:
                fail.append(f'{f}: files in {top}/ live in {top}/<group>/ ({", ".join(groups)}); a new group goes in .layout.json groupedDirs')

# code map
cm = cfg.get('codeMap')
if cm:
    text = read(cm['file'])
    globs = [re.compile('^' + re.escape(g).replace(r'\*', '[^/]*') + '$') for g in re.findall(r'`([^`\s]*\*[^`\s]*)`', text)]
    dirs = set(cm.get('dirs', []))
    for f in files:
        p = f.split('/')
        direct = (len(p) == 2 and p[0] in dirs) or (len(p) == 3 and p[0] in cfg.get('groupedDirs', {}) and p[0] in dirs)
        if direct and f'`{f}`' not in text and not any(g.match(f) for g in globs):
            fail.append(f'{f}: no row in {cm["file"]}')

# tests
t = cfg.get('tests')
if t:
    pat = re.compile(t.get('pattern', r'^[a-z0-9-]+\.test\.(mjs|js|ts|py)$'))
    runners = ''.join(read(r) for r in t.get('runners', []))
    for f in files:
        if not f.startswith(t['dir'] + '/'):
            continue
        rest = f[len(t['dir']) + 1:]
        if rest.split('/')[0] in t.get('subdirs', ['helpers', 'fixtures']) and '/' in rest:
            continue
        if '/' in rest or not pat.match(rest):
            fail.append(f'{f}: {t["dir"]}/ holds tests named {pat.pattern} only; helpers and data go in its subfolders')
        elif t.get('runners') and f not in runners and f not in t.get('manual', []):
            fail.append(f'{f}: not run by the test command ({", ".join(t["runners"])}), and not in .layout.json tests.manual')

# backticked repo paths exist
tops = {f.split('/')[0] for f in files}
known = set(files)
for f in files:
    p = f.split('/')
    for i in range(1, len(p)):
        known.add('/'.join(p[:i]) + '/')
untracked_ok = tuple(cfg.get('untrackedOk', []))
prose = [d for d in ['README.md', 'CLAUDE.md', 'AGENTS.md', 'TODO.md'] if d in files] + \
        [d for d in docs if not d.startswith('docs/archive/') and d != 'docs/moves.md']
for doc in prose:
    for m in re.finditer(r'`([^`\s]+)`', read(doc)):
        p = m.group(1).rstrip('),.:;')
        if '/' not in p or re.search(r'[*<>{}|$\\]|\.\.\.|^\.\.?/|^/|^~|://', p):
            continue
        head = p.split('/')[0]
        if head not in tops or head == p:
            continue
        if not (p in known or p + '/' in known or p.rstrip('/') in known or p.startswith(untracked_ok) or os.path.exists(p)):
            fail.append(f'{doc}: `{p}` does not exist (moved or renamed? update the doc, log it in docs/moves.md)')

for f in fail:
    print('FAIL', f)
print(f'layout: {len(fail)} problems' if fail else f'layout: {len(files)} files, all in place')
sys.exit(1 if fail else 0)
