#!/usr/bin/env python3
"""Render baseline and candidate in the same real browser at 100% and produce audit screenshots.

Usage:
  python scripts/visual_compare.py \
    --baseline-url http://127.0.0.1:9001/reference/approved-interactive.html \
    --candidate-url http://127.0.0.1:9002/ \
    --out ./visual-qa

You must separately serve both pages and ensure equivalent fixture data. For live data/photo
mismatches, use side-by-side/layout review, not blind global pixel thresholds.
"""
import argparse
import json
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw
from playwright.sync_api import sync_playwright

VIEWPORTS = [(2048,1152), (1440,900), (390,844), (320,700)]
SELECTORS = ['.poster', '.mast', '.headline', '.mainstage', '.visual', '.visual-cut', '.figures', '.finance', '.fin-percent', '.coverage', '.trend', '.site-nav']


def visit(browser, url, width, height, path):
    page = browser.new_page(viewport={'width':width,'height':height}, device_scale_factor=1, reduced_motion='reduce')
    console_errors=[]
    page.on('pageerror', lambda exc: console_errors.append(str(exc)))
    page.on('console', lambda msg: console_errors.append(msg.text) if msg.type=='error' else None)
    page.goto(url, wait_until='domcontentloaded', timeout=30000)
    page.evaluate('''async () => { await document.fonts.ready; await Promise.all(Array.from(document.images).map(i=>i.complete?Promise.resolve():new Promise(r=>{i.addEventListener('load',r,{once:true});i.addEventListener('error',r,{once:true});setTimeout(r,5000)}))); }''')
    page.wait_for_timeout(200)
    page.screenshot(path=str(path), full_page=True)
    metrics=page.evaluate('''(sels) => {
      const result={width:innerWidth,scrollWidth:document.documentElement.scrollWidth,devicePixelRatio:devicePixelRatio,bodyText:document.body.innerText.length,boxes:{}};
      for(const s of sels){const e=document.querySelector(s);if(!e)continue;const r=e.getBoundingClientRect();const z=getComputedStyle(e);result.boxes[s]={x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height),font:z.fontSize};}
      return result;
    }''', SELECTORS)
    metrics['consoleErrors']=console_errors
    metrics['horizontalOverflow']=(metrics['scrollWidth'] > metrics['width']+1)
    page.close()
    return metrics


def show_comparison(a,b,out):
    im1,im2=Image.open(a).convert('RGB'),Image.open(b).convert('RGB')
    W=max(im1.width,im2.width);H=max(im1.height,im2.height)
    left=Image.new('RGB',(W,H),'#F0EEE6');right=Image.new('RGB',(W,H),'#F0EEE6')
    left.paste(im1,(0,0));right.paste(im2,(0,0))
    canvas=Image.new('RGB',(W*2,H+32),'#FFFFFF');canvas.paste(left,(0,32));canvas.paste(right,(W,32))
    ImageDraw.Draw(canvas).text((12,9),'APPROVED REFERENCE',fill='black');ImageDraw.Draw(canvas).text((W+12,9),'CANDIDATE',fill='black')
    canvas.save(out/'side_by_side.png')
    Image.blend(left,right,0.5).save(out/'overlay_50pct.png')
    ImageChops.difference(left,right).save(out/'difference.png')


def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline-url',required=True);p.add_argument('--candidate-url',required=True);p.add_argument('--out',default='visual-qa');a=p.parse_args()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        for w,h in VIEWPORTS:
            folder=out/f'{w}x{h}';folder.mkdir(exist_ok=True)
            base=visit(browser,a.baseline_url,w,h,folder/'baseline.png')
            cand=visit(browser,a.candidate_url,w,h,folder/'candidate.png')
            show_comparison(folder/'baseline.png',folder/'candidate.png',folder)
            (folder/'metrics.json').write_text(json.dumps({'reference':base,'candidate':cand},ensure_ascii=False,indent=2))
            print(f'{w}x{h}: overflow={cand["horizontalOverflow"]} console_errors={len(cand["consoleErrors"])}; comparison={folder}')
        browser.close()
    print('Manual visual approval is required: a screenshot test alone cannot judge the artistic composition.')

if __name__=='__main__':main()
