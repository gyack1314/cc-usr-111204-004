"""显示资料库存与原始字段摘要，并按索引窗口做稀疏range采集。"""
import argparse
import json
import math
from pathlib import Path
from .io import load,asset,integer
from .domain import summarize
from .source import VirtualRangeSource,CheckedRemote
from .window import load_header,acquire

def _safe(obj):
    if isinstance(obj,float) and not math.isfinite(obj):
        return 'NaN' if math.isnan(obj) else ('Infinity' if obj>0 else '-Infinity')
    if isinstance(obj,dict):return {k:_safe(v) for k,v in obj.items()}
    if isinstance(obj,list):return [_safe(v) for v in obj]
    return obj

def _window_config(value):
    if not isinstance(value.get('file'),str) or not value['file']:raise ValueError('缺少file')
    if not isinstance(value.get('strong_etag'),str) or not value['strong_etag']:
        raise ValueError('缺少strong_etag')
    wins=value.get('windows')
    if not isinstance(wins,dict) or not wins:raise ValueError('windows为空或非法')
    for name,w in wins.items():
        if not isinstance(name,str) or not isinstance(w,dict):raise ValueError('windows非法')
        for f in ('start','count','stride'):
            seq=w.get(f)
            if not isinstance(seq,list) or any(type(x) is not int for x in seq):
                raise ValueError('窗口%s的%s必须为整数列表'%(name,f))
    return wins,integer(value.get('max_range_bytes'),1),integer(value.get('max_overread_bytes'),0)

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['inspect','window'])
    p.add_argument('file');a=p.parse_args()
    value,digest=load(a.file)
    root=Path(a.file).parent
    if a.command=='inspect':
        print(json.dumps({'source_sha256':digest,'summary':summarize(value,root)},ensure_ascii=False,indent=2))
        return
    wins,mrb,mob=_window_config(value)
    raw,fdigest=asset(root,value['file'])
    source=VirtualRangeSource(data=raw,etag=value['strong_etag'])
    remote=CheckedRemote(source,expected_etag=value['strong_etag'])
    header,header_bytes=load_header(remote)
    result=acquire(remote,header,wins,max_range_bytes=mrb,max_overread_bytes=mob)
    totals=result['totals']
    totals['header_bytes']=header_bytes
    totals['received_bytes']=remote.bytes_received
    out={'source_sha256':digest,'file_sha256':fdigest,'etag':remote.etag,
         'format':header.version,'numrecs':header.numrecs,
         'windows':result['windows'],'totals':totals}
    print(json.dumps(_safe(out),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
