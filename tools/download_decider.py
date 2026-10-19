"""Bounded HTTP range downloads with strict Content-Range and final hashes."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_paths import PERSISTENT_ROOT
from setup_decider import disk_preflight, disk_issue

ROOT = PERSISTENT_ROOT / 'decider'
REVISION = '6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d'
TARGET = ROOT / 'models' / REVISION
TARGET.mkdir(parents=True, exist_ok=True)
BASE = f'https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.2/resolve/{REVISION}/'
manifest_path = TARGET / 'release-manifest.json'
if not manifest_path.is_file():
    with urllib.request.urlopen(BASE+'release-manifest.json', timeout=60) as response:
        manifest_path.write_bytes(response.read())
if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != 'b92d6898a1a0cebeec8573c3ba34cfbb810a2920fc49da60097f3a8e2de24ebc':
    raise RuntimeError('Pinned official manifest hash mismatch')
manifest = json.loads(manifest_path.read_text('utf-8'))
CHUNK = 64*1024*1024


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(8*1024*1024),b''): digest.update(data)
    return digest.hexdigest()


def piece(job):
    name, start, end, total = job
    directory=TARGET/(name+'.ranges');directory.mkdir(exist_ok=True)
    target=directory/f'{start}-{end}.chunk'
    if target.is_file() and target.stat().st_size==end-start+1: return job
    temporary=target.with_suffix('.partial')
    for attempt in range(4):
        offset=start+(temporary.stat().st_size if temporary.is_file() else 0)
        if offset>end: break
        request=urllib.request.Request(BASE+name+f'?range_start={offset}&range_end={end}',headers={'Range':f'bytes={offset}-{end}'})
        try:
            with urllib.request.urlopen(request,timeout=45) as response:
                wanted=f'bytes {offset}-{end}/{total}'
                if response.status!=206 or response.headers.get('Content-Range')!=wanted:
                    raise RuntimeError('Server did not honor the exact byte range')
                with temporary.open('ab') as stream:
                    count=offset
                    while data:=response.read(2*1024*1024):
                        if count+len(data)>end+1: raise RuntimeError('Range response too long')
                        stream.write(data);count+=len(data)
            if temporary.stat().st_size==end-start+1: break
        except Exception as exc:
            print(json.dumps({'file':name,'range_start':start,'attempt':attempt+1,'error':str(exc)[:120]}),flush=True)
            if attempt==3: raise
            time.sleep(1)
    if not temporary.is_file() or temporary.stat().st_size!=end-start+1:
        raise RuntimeError('Incomplete range')
    temporary.replace(target)
    print(json.dumps({'file':name,'range_completed':start,'bytes':end-start+1}),flush=True)
    return job


plans=[];small=[]
for name,expected in manifest['files'].items():
    path=TARGET/name
    if path.is_file() and path.stat().st_size==expected['bytes'] and sha(path)==expected['sha256']: continue
    if expected['bytes']<100*1024*1024:
        small.append((name,expected));continue
    temporary=path.with_name(path.name+'.part')
    prefix=temporary.stat().st_size if temporary.is_file() else 0
    if prefix>expected['bytes']: raise RuntimeError('Unexpected partial length')
    jobs=[(name,start,min(start+CHUNK,expected['bytes'])-1,expected['bytes']) for start in range(prefix,expected['bytes'],CHUNK)]
    plans.append((name,expected,prefix,jobs))
# A same-size existing file can still fail its hash. Recheck the full download
# budget after verification, before fetching any weight ranges or small files.
disk = disk_preflight(ROOT, model_reusable=not plans and not small)
problem = disk_issue(disk)
if problem:
    print(json.dumps({'disk': disk}, ensure_ascii=False), flush=True)
    raise RuntimeError(problem)
print(json.dumps({'prefixes_preserved':{p[0]:p[2] for p in plans},'ranges':sum(len(p[3]) for p in plans),'parallel':4}),flush=True)
with ThreadPoolExecutor(max_workers=4) as pool:
    futures=[pool.submit(piece,job) for _,_,_,jobs in plans for job in jobs]
    for future in as_completed(futures): future.result()
for name,expected,prefix,jobs in plans:
    path=TARGET/name;temporary=path.with_name(path.name+'.part')
    if (temporary.stat().st_size if temporary.is_file() else 0)!=prefix: raise RuntimeError('Preserved prefix changed')
    with temporary.open('ab') as output:
        for _,start,end,_ in jobs:
            chunk=TARGET/(name+'.ranges')/f'{start}-{end}.chunk'
            with chunk.open('rb') as stream: shutil.copyfileobj(stream,output,8*1024*1024)
    if temporary.stat().st_size!=expected['bytes'] or sha(temporary)!=expected['sha256']:
        raise RuntimeError('Full model hash mismatch: '+name)
    temporary.replace(path)
    for _,start,end,_ in jobs: (TARGET/(name+'.ranges')/f'{start}-{end}.chunk').unlink()
    print(json.dumps({'file':name,'status':'downloaded_verified','bytes':expected['bytes']}),flush=True)
for name,expected in small:
    path=TARGET/name;path.parent.mkdir(parents=True,exist_ok=True)
    with urllib.request.urlopen(BASE+name,timeout=60) as response: data=response.read()
    if len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']: raise RuntimeError('Small artifact mismatch '+name)
    path.write_bytes(data)
    print(json.dumps({'file':name,'status':'downloaded_verified'}),flush=True)
assert all((TARGET/name).stat().st_size==expected['bytes'] and sha(TARGET/name)==expected['sha256'] for name,expected in manifest['files'].items())
receipt={'repo':'perplexity-ai/pplx-decider-flash-v1.2','revision':REVISION,'verified_files':len(manifest['files']),
         'total_bytes':sum(v['bytes'] for v in manifest['files'].values()),'status':'all_sha256_verified'}
(ROOT/'download-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
print(json.dumps(receipt),flush=True)
