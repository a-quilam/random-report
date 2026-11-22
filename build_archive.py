"""Build standalone pages containing only transcribed public aggregates."""
from pathlib import Path
import sys
from history_archive import archive, render


def build(out):
    out.mkdir(parents=True, exist_ok=False)
    header = '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>А дорог ли РанДом? · Архив</title><style>body{background:#faf6ec;color:#27202e;font:1.15rem/1.6 system-ui;max-width:42rem;margin:4vh auto;padding:1rem}h1{line-height:1.15}a{color:#583170}dt{font-weight:bold}dd{margin:0 0 1em}</style><main>'
    links=[]
    for r in archive():
        slug=str(r.post)+'.html'
        (out/slug).write_text(header+render(r)+'<p><a href="index.html">Все отчёты</a></p></main></html>', encoding='utf-8')
        links.append(f'<li><a href="{slug}">{r.start} — {r.end}</a></li>')
    (out/'index.html').write_text(header+'<h1>А дорог ли РанДом?</h1><p>Архив опубликованных отчётов. Здесь не показаны текущие финансы.</p><ul>'+''.join(links)+'</ul></main></html>', encoding='utf-8')

if __name__=='__main__': build(Path(sys.argv[1]))
