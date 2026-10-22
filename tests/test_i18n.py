"""Every text the editor translates has a German entry.

The page is written in English; German comes from dictionaries keyed by the
English text (see "Language" in the editor). A changed English text without a
changed key falls back to English without any error - this test catches that.
"""
import html
import re

from conftest import REPO

EDITOR = REPO / 'editor'
QUOTED = r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)\""""


def sources():
    files = [EDITOR / 'editor.html', *sorted((EDITOR / 'js').glob('*.js'))] if (EDITOR / 'js').is_dir() else [EDITOR / 'editor.html']
    return {f.name: f.read_text(encoding='utf-8') for f in files}


def unescape(s):
    return re.sub(r"\\(.)", r"\1", s)


def dictionary(text, name):
    """The keys of `const NAME = { ... };` (one entry per line)."""
    m = re.search(rf'const {name} = \{{\n(.*?)\n\}};', text, re.S)
    assert m, f'{name} not found'
    keys = set()
    for line in m.group(1).splitlines():
        km = re.match(rf'\s*(?:{QUOTED})\s*:', line)
        if km:
            keys.add(unescape(km.group(1) if km.group(1) is not None else km.group(2)))
    return keys


def all_text():
    return '\n'.join(sources().values())


def test_no_text_is_translated_twice():
    """A key twice in the dictionary: the second wins, silently."""
    m = re.search(r'const DE = \{\n(.*?)\n\};', all_text(), re.S)
    keys = [unescape(k.group(1) if k.group(1) is not None else k.group(2))
            for line in m.group(1).splitlines() for k in [re.match(rf'\s*(?:{QUOTED})\s*:', line)] if k]
    twice = sorted({k for k in keys if keys.count(k) > 1})
    assert not twice, twice


def test_t_calls_have_german():
    text = all_text()
    de = dictionary(text, 'DE')
    used = set()
    for m in re.finditer(rf'\bt\(\s*(?:{QUOTED})', text):
        used.add(unescape(m.group(1) if m.group(1) is not None else m.group(2)))
    # Labels and texts handed to t() later (tour steps, commands, tool-tip-ish labels).
    for m in re.finditer(rf'\b(?:title|text|label):\s*(?:{QUOTED})', text):
        used.add(unescape(m.group(1) if m.group(1) is not None else m.group(2)))
    missing = sorted(k for k in used if k not in de and re.search('[A-Za-z]{2}', k) and not k.startswith(('http', '#', '.')))
    assert not missing, 'No German for:\n  ' + '\n  '.join(missing)


def markup():
    text = sources()['editor.html']
    start = text.index('<body>')
    return text[start:text.index('<script', start)]


def test_html_blocks_have_german():
    de_html = dictionary(all_text(), 'DE_HTML')
    used = set(re.findall(r'data-i18n="([\w-]+)"', markup()))
    assert not used - de_html, f'No German HTML block for: {sorted(used - de_html)}'


# Names, and examples of what to type: the same in both languages.
SAME_IN_GERMAN = {'Streetview Tour Editor', 'Tour', 'Places API (New)', 'Geocoding API', 'https://'}


def test_static_texts_have_german():
    """Visible texts written straight into the page's markup."""
    body = re.sub(r'<(svg|style)\b.*?</\1>', '', markup(), flags=re.S)
    body = re.sub(r'<([a-z]+)[^>]*\b(data-no-i18n|data-i18n="[^"]*")[^>]*>.*?</\1>', '', body, flags=re.S)
    de = dictionary(all_text(), 'DE')
    found = {' '.join(m.group(1).split()) for m in re.finditer(r'>([^<>]+)<', body)}
    found |= set(re.findall(r'\btitle="([^"]+)"', body))
    found |= {p for p in re.findall(r'\bplaceholder="([^"]+)"', body) if ' ' in p}  # 'AIza…', 'ChIJ…': examples
    missing = sorted(s for s in map(html.unescape, found)
                     if re.search('[A-Za-z]{3}', s) and s not in de and s not in SAME_IN_GERMAN
                     and not re.fullmatch(r'[\w.-]+\.(jpg|json|html|vrtour|csv)|Ctrl .+|[A-Z]{2,}\b.*', s))
    assert not missing, 'No German for:\n  ' + '\n  '.join(missing)
