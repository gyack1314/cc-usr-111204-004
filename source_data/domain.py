"""识别经典NetCDF文件签名和文件总长度。"""
import struct
from .io import asset

def summarize(v,root):
    raw,digest=asset(root,v['file'])
    if len(raw)<8 or raw[:4] not in (b'CDF\x01',b'CDF\x02'):raise ValueError('不是支持的经典CDF签名')
    records=struct.unpack('>I',raw[4:8])[0]
    return {'format':raw[3],'declared_numrecs':records,'file_bytes':len(raw),'sha256':digest,'requested_variables':sorted(v['windows'])}
