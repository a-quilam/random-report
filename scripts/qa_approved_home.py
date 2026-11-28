#!/usr/bin/env python3
"""Local homepage QA. Run with the dedicated Playwright/Pillow interpreter.

The supplied visual_compare.py CLI runs unchanged. A scoped Playwright adapter
serves only a frozen public-file map (no sockets, DNS, or browser profile reuse)
and measures the same page immediately before its full-page screenshot.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import json
import mimetypes
import os
from pathlib import Path
import runpy
import sys
from urllib.parse import unquote, urlsplit
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image, ImageDraw
from playwright.sync_api import Browser

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
SIZES = [(2048, 1152), (1440, 900), (390, 844), (320, 700)]
STATIC = {'.css', '.js', '.jpg', '.jpeg', '.png', '.webp', '.svg', '.gif', '.avif', '.woff', '.woff2', '.ttf', '.ico'}
ORIGINS = {f'127.0.0.1:{port}': role for port, role in enumerate(('approved', 'rejected', 'candidate'), 19001)}
PHOTO_HASH = 'd9ee85ae4735748aef038cf0837106020c474ab6fa2731485c7c696eb4d266f5'
SELECTORS = ['.poster', '.mast', '.brand', '.headline', '.intro-line', '.mainstage', '.visual', '.visual img', '.figures', '.events', '.subs', '.events .number', '.subs .number', '.events .tag', '.subs .tag', '.finance', '.fin-percent', '.bar, .coverage-bar', '.source-sheet, .fields', '.trend', '.stripe-heading', '.site-nav', '.sections']
MEASURE = '''sels => {
 const d=document.documentElement, rect=r=>Object.fromEntries(['x','y','width','height'].map(k=>[k,Math.round(r[k]*10)/10]));
 const boxes=Object.fromEntries(sels.map(s=>{const e=document.querySelector(s);return [s,e?{...rect(e.getBoundingClientRect()),font:getComputedStyle(e).fontSize}:null]}));
 const outside=[]; const walk=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
 while(walk.nextNode()) { const n=walk.currentNode,e=n.parentElement;
 if(!n.textContent.trim()||e.closest('script,style,[aria-hidden="true"],.sr-only,.visually-hidden')||!e.checkVisibility({visibilityProperty:true,opacityProperty:true}))continue;
 const range=document.createRange();range.selectNodeContents(n);for(const r of range.getClientRects())if(r.width&&r.height&&(r.left< -1||r.right>d.clientWidth+1)) {outside.push({element:e.tagName+'.'+e.className,...rect(r)});break;} }
 return {viewport:[innerWidth,innerHeight],dpr:devicePixelRatio,scale:visualViewport.scale,clientWidth:d.clientWidth,scrollWidth:d.scrollWidth,scrollHeight:d.scrollHeight,rootZoom:getComputedStyle(d).zoom,boxes,outsideText:outside,images:[...document.images].map(i=>({natural:[i.naturalWidth,i.naturalHeight],...rect(i.getBoundingClientRect())})),mainstageChars:document.querySelector('.mainstage')?.innerText.trim().length};
}'''


def digest(data): return sha256(data).hexdigest()


def dump(p, data): p.write_text(json.dumps(data, indent=2, ensure_ascii=False)+'\n')


def inputs(args):
    refs = ROOT/'reference'
    files = {'approved': {'/index.html': (refs/'approved-interactive.html').read_bytes()}, 'rejected': {}}
    with ZipFile(args.archive) as z:
        for n in z.namelist():
            if n == 'site/index.html' or (n.startswith('site/static/') and Path(n).suffix.lower() in STATIC):
                files['rejected'][n[4:]] = z.read(n)
    expected={'/index.html':'8bf5dd6e56456f90c84c46246b5d1cd249a6a349b665f204e34b3a25e1910a28','/static/site.css':'f14b4647f7ca04b91abad5989c9d3424f6812da0ba0e2490697120fe3059d375','/static/basement-photo.jpg':PHOTO_HASH}
    if any(digest(files['rejected'].get(k,b''))!=v for k,v in expected.items()):raise ValueError('Unexpected rejected export: HTML, stylesheet or photo does not match 16e92b5 site snapshot')
    root = args.candidate
    if (root/'index.html').is_file():
        if (root/'index.html').is_symlink() or (root/'static').is_symlink(): raise ValueError('Candidate inputs must be regular local files')
        files['candidate'] = {'/index.html': (root/'index.html').read_bytes()}
        for p in (root/'static').rglob('*'):
            if p.is_file() and p.suffix.lower() in STATIC and not p.is_symlink() and p.resolve().is_relative_to(root.resolve()/'static'):
                files['candidate']['/'+p.relative_to(root).as_posix()] = p.read_bytes()
    return files


def compare_native(paths, labels, target):
    # Never scale or align features: pad bottom/right only, distinct grey outside frame.
    with ExitStack() as stack:
        ims=[stack.enter_context(Image.open(p)).convert('RGB') for p in paths]
        canvas=Image.new('RGB',(sum(i.width for i in ims),max(i.height for i in ims)+30),'#d4d4d4');x=0
        for im,label in zip(ims, labels):
            ImageDraw.Draw(canvas).text((x+5,8), f'{label}  {im.width} x {im.height}',fill='black'); canvas.paste(im,(x,30));x+=im.width
        canvas.save(target)


def audit(args):
    files=inputs(args); out=args.out/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ');out.mkdir(parents=True)
    versions={k:importlib.metadata.version(k) for k in ('playwright','Pillow')}; records={}; findings=[]
    original=Browser.new_page
    def guarded(browser, **opts):
        versions['chromium']=browser.version
        page=original(browser,service_workers='block',**opts);log=[];problems=[];local_photo=[]
        def route(r):
            u=urlsplit(r.request.url);role=ORIGINS.get(u.netloc);key=unquote(u.path) or '/index.html'
            # Only the explicitly authorized asset may use the old localhost origin.
            if u.scheme=='http' and u.netloc in ('localhost:8099','127.0.0.1:8099') and key=='/static/basement-photo.jpg':role='rejected'
            if u.scheme!='http' or not role or role not in files or r.request.method!='GET':
                problems.append('blocked non-fixture request ('+r.request.resource_type+')');return r.abort()
            data=files[role].get(key); log.append([key,200 if data is not None else 404]);
            if data is None: problems.append('missing: '+key);return r.fulfill(status=404,body='Not in public snapshot')
            if digest(data)==PHOTO_HASH:local_photo.append(key)
            return r.fulfill(body=data,content_type=(mimetypes.guess_type(key)[0] or 'application/octet-stream')+('; charset=utf-8' if key.endswith(('.html','.css','.js')) else ''))
        page.context.route('**/*',route);page.context.route_web_socket('**/*',lambda ws:(problems.append('blocked websocket'),ws.close()))
        page.on('pageerror',lambda _:problems.append('page error'));page.on('console',lambda m:problems.append('console error') if m.type=='error' else None)
        shoot=page.screenshot
        def measured(**kwargs):
            role=ORIGINS[urlsplit(page.url).netloc]; w,h=opts['viewport']['width'],opts['viewport']['height'];size=f'{w}x{h}'
            folder=out/'captures'/size; folder.mkdir(parents=True,exist_ok=True)
            assert page.locator('body').inner_text().strip() and page.locator('.headline,h1').count(), f'Not a homepage: {role}'
            page.evaluate('''async()=>{await document.fonts.ready; await Promise.race([Promise.allSettled([...document.images].map(i=>i.decode())),new Promise(r=>setTimeout(r,5000))]);await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))}''')
            m=page.evaluate(MEASURE, SELECTORS);assert (m['viewport'],m['dpr'],m['scale'])==([w,h],1,1),m['viewport']
            fold=folder/(role+'.png');shoot(path=str(fold),full_page=False);result=shoot(**kwargs)
            with Image.open(fold) as im: assert im.size==(w,h);im.verify()
            with Image.open(kwargs['path']) as im: assert im.width>=w and im.height>=h; im.verify()
            m.update(resources=log,approvedPhotoRequests=local_photo,problems=problems,full=str(Path(kwargs['path']).relative_to(out)))
            if m['scrollWidth']!=m['clientWidth']:problems.append('horizontal overflow')
            if any(t['element']!='A.skip' for t in m['outsideText']):problems.append('visible text extends outside viewport; inspect outsideText')
            if any(not i['natural'][0] for i in m['images']):problems.append('undecoded image')
            if role!='approved' and not local_photo:problems.append('approved JPEG not observed; check photo identity')
            records.setdefault(size,{})[role]=m
            return result
        page.screenshot=measured
        return page
    script=ROOT/'scripts/visual_compare.py'
    for role in files:
        if role=='approved':continue
        cli=[str(script),'--baseline-url','http://127.0.0.1:19001/index.html','--candidate-url',f'http://127.0.0.1:{19002 if role=="rejected" else 19003}/index.html','--out',str(out/('approved-vs-'+role))]
        with patch.object(Browser,'new_page',guarded),patch.object(sys,'argv',cli):runpy.run_path(str(script),run_name='__main__')
    refs=ROOT/'reference'; shotdata={}; names=list(files)
    for w,h in SIZES:
        size=f'{w}x{h}'; folder=out/'captures'/size; row=records[size];assert set(row)==set(names)
        paths=[folder/(r+'.png') for r in names];compare_native(paths,names,folder/'first-screens.png')
        if 'candidate' in files:
            for a in ['approved','rejected']:
                with Image.open(folder/(a+'.png')) as old,Image.open(paths[-1]) as new:
                    Image.blend(old.convert('RGB'),new.convert('RGB'),0.5).save(folder/(a+'-candidate-overlay.png'))
        if w in (2048,390):
            mobile=w==390;p=refs/('approved-mobile.png' if mobile else 'approved-desktop.png');srcs=[out/row[r]['full'] for r in names] if mobile else paths
            compare_native([p,*srcs],['supplied (native)',*names],folder/'supplied-vs-renders.png');shotdata[p.name]={'size':Image.open(p).size,'sha256':digest(p.read_bytes())}
        for r in names:
            for issue in set(row[r]['problems']):findings.append([size,r,issue])
        dump(folder/'audit.json',row)
    report=['# Homepage visual QA', '', 'Candidate: '+('captured; manual review required' if 'candidate' in files else 'not present; no after result'), '', 'CSS pixel measurements from the same frame used for screenshots:', '', '| Viewport | Render | Photo top | Finance top | Announcements top | Document height | Horizontal scroll overflow |', '|---|---|---:|---:|---:|---:|---|']
    for size,row in records.items():
        for name,m in row.items():
            ys=[(m['boxes'][s] or {}).get('y','missing') for s in ['.visual','.finance','.trend']];report.append('| '+' | '.join(map(str,[size,name,*ys,m['scrollHeight'],m['scrollWidth']>m['clientWidth']]))+' |')
    report.extend(['','## Flags',*['- '+': '.join(entry) for entry in findings],'','Native supplied PNG comparisons: `captures/2048x1152/supplied-vs-renders.png` and `captures/390x844/supplied-vs-renders.png`. See README.txt for browser chrome, differing page heights and data caveats.'])
    (out/'report.md').write_text('\n'.join(report)+'\n')
    verified={}
    for p in sorted(out.rglob('*.png')):
        with Image.open(p) as im: im.load(); verified[str(p.relative_to(out))]={'size':im.size,'sha256':digest(p.read_bytes())}
    for size in records:
        for r in names[1:]:
            base=f'approved-vs-{r}/{size}/';assert all(base+f in verified for f in ['baseline.png','candidate.png','side_by_side.png','overlay_50pct.png','difference.png']);assert (out/base/'metrics.json').is_file()
    dump(out/'manifest.json',{'versions':versions,'viewports':SIZES,'candidatePresent':'candidate' in files,'source':str(args.archive)+' (site/index.html and site/static only)','candidateDirectory':str(args.candidate),'inputSha256':{r:{k:digest(v) for k,v in tree.items()} for r,tree in files.items()},'providedScreenshots':shotdata,'comparisonScriptSha256':digest(script.read_bytes()),'findings':findings,'verifiedPNGs':verified})
    (out/'README.txt').write_text('All images at native scale. Each approved-vs-* directory is an unchanged visual_compare.py output: its candidate.png means the right-hand role in the DIRECTORY NAME, not necessarily the repair. Full-page pairs pad shorter pages; captures/* has exact first screens and same-page geometry/resource audits. Supplied desktop includes browser chrome; supplied mobile is a 390x1889 tall composition, not a known 390x844 viewport. No cropping, re-registration or pixel threshold for supplied images. Prototype demo figures and photo differ from public export. outsideText is a review list (can include intentionally offscreen skip links). No automatic artistic approval.\n')
    latest=args.out/'latest';tmp=args.out/('latest-'+out.name);tmp.symlink_to(out.name);tmp.replace(latest)
    print(f'{out}: {len(verified)} decoded PNGs; candidate={"present" if "candidate" in files else "PENDING"}; findings={findings}')
    return int(any(r=='candidate' for _,r,_ in findings))


if __name__=='__main__':
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,default=HOME/'.hermes/attachments/random-current-collage-16e92b5.zip');p.add_argument('--candidate',type=Path,default=HOME/'.hermes/cache/scratch/home-review');p.add_argument('--out',type=Path,default=HOME/'.hermes/attachments/home-repair-qa');sys.exit(audit(p.parse_args()))
