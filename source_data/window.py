"""按索引窗口采集：执行稀疏计划、解包数值并标注缺测。

保留原始整数或浮点位模式；_FillValue与missing_value在打包域比较
（浮点按位模式，NaN哨兵按NaN判定），命中先标缺测，再对非缺测应用
scale_factor与add_offset；char保持字符数组，不擅自变成数字；
不对文件中的时间或坐标做任何猜测转换。
"""
import math
import struct
from .cdf import CdfError,NeedMore,TYPES,parse_header
from .plan import plan_window

def load_header(remote,max_header_bytes=1<<26):
    """经身份核对的适配器增量拉取并解析头部，返回(header,头部字节数)。"""
    buf=b''
    while True:
        try:
            header=parse_header(buf)
            break
        except NeedMore as e:
            if e.offset>remote.length:raise CdfError('头部截断') from None
            target=min(max(e.offset,len(buf)+256,len(buf)*2),remote.length)
            if target>max_header_bytes:raise CdfError('头部超过上限')
            if target<=len(buf):raise CdfError('头部截断')
            buf+=remote.fetch(len(buf),target-1)
    header.validate(remote.length)
    return header,len(buf)

def _decode(var,buf):
    if var.nc_type==2:return chr(buf[0])
    return struct.unpack('>'+TYPES[var.nc_type][1],buf)[0]

def _matcher(var):
    """打包域缺测判定：整数按值、浮点按位模式、NaN哨兵按NaN、char按字节。"""
    tc=var.nc_type
    ints=set();packed=set();chars=set();nan=False
    for aname in ('_FillValue','missing_value'):
        a=var.attrs.get(aname)
        if a is None:continue
        if a.nc_type!=tc:raise CdfError('%s类型与变量%s不符'%(aname,var.name))
        if tc==2:
            chars.update(chr(b) for b in a.values)
        elif tc in (5,6):
            fmt='>'+TYPES[tc][1]
            for x in a.values:
                if math.isnan(x):nan=True
                else:packed.add(struct.pack(fmt,x))
        else:
            ints.update(a.values)
    if tc==2:return lambda x:x in chars
    if tc in (5,6):
        fmt='>'+TYPES[tc][1]
        return lambda x:(nan and math.isnan(x)) or struct.pack(fmt,x) in packed
    return lambda x:x in ints

def _scale_offset(var):
    if var.nc_type==2:return None,None
    scale=offset=None
    for aname in ('scale_factor','add_offset'):
        a=var.attrs.get(aname)
        if a is None:continue
        if a.nc_type==2 or len(a.values)!=1:raise CdfError('%s须为单值数值属性'%aname)
        if aname=='scale_factor':scale=float(a.values[0])
        else:offset=float(a.values[0])
    return scale,offset

def read_window(remote,header,var,start,count,stride,*,max_range_bytes,max_overread_bytes):
    plan=plan_window(header,var,start,count,stride,max_range_bytes,max_overread_bytes)
    raw=[None]*plan.out_size
    for r in plan.ranges:
        buf=remote.fetch(r.start,r.end-1)
        for out_i,rel in r.targets:
            raw[out_i]=_decode(var,buf[rel:rel+var.elem_size])
    hit=_matcher(var)
    scale,offset=_scale_offset(var)
    mask=[];values=[]
    for x in raw:
        m=hit(x)
        mask.append(m)
        if m:values.append(None)
        elif var.nc_type==2:values.append(x)
        elif scale is None and offset is None:values.append(x)
        else:values.append(x*(scale if scale is not None else 1.0)+(offset if offset is not None else 0.0))
    return {'variable':var.name,
            'dimensions':[header.dims[i].name for i in var.dimids],
            'shape':plan.out_shape,
            'selection':{'start':list(start),'count':list(count),'stride':list(stride)},
            'mask':mask,'raw':raw,'values':values,
            'byte_ranges':[[r.start,r.end] for r in plan.ranges],
            'transfer':{'requests':len(plan.ranges),'bytes':plan.total_bytes,
                        'overread_bytes':plan.overread_bytes}}

def acquire(remote,header,windows,*,max_range_bytes,max_overread_bytes):
    """按配置逐变量采集窗口；任一片段身份失效则整体失败，不产出拼接结果。"""
    if not isinstance(windows,dict) or not windows:raise ValueError('windows为空或非法')
    out={};totals={'requests':0,'bytes':0,'overread_bytes':0}
    for name,sel in windows.items():
        var=header.var(name)
        if not isinstance(sel,dict):raise ValueError('窗口选择非法')
        res=read_window(remote,header,var,sel.get('start'),sel.get('count'),sel.get('stride'),
                        max_range_bytes=max_range_bytes,max_overread_bytes=max_overread_bytes)
        out[name]=res
        t=res['transfer']
        totals['requests']+=t['requests'];totals['bytes']+=t['bytes']
        totals['overread_bytes']+=t['overread_bytes']
    return {'windows':out,'totals':totals}
