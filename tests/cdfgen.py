"""测试用经典CDF合成构造器：可自动核算布局，也可手工指定vsize/begin。"""
import struct

FMT={1:'b',2:None,3:'h',4:'i',5:'f',6:'d'}
ESIZE={1:1,2:1,3:2,4:4,5:4,6:8}

def round4(n):
    return (n+3)&~3

def pad4(b):
    return b+b'\0'*((-len(b))%4)

def name_bytes(s):
    b=s.encode('utf8')
    return struct.pack('>I',len(b))+pad4(b)

def attr_bytes(name,nc_type,values):
    if nc_type==2:
        body=bytes(values);n=len(body)
    else:
        n=len(values);body=struct.pack('>%d%s'%(n,FMT[nc_type]),*values)
    return name_bytes(name)+struct.pack('>II',nc_type,n)+pad4(body)

def attrs_bytes(attrs):
    if not attrs:return struct.pack('>II',0,0)
    return struct.pack('>II',12,len(attrs))+b''.join(attr_bytes(*a) for a in attrs)

def header_bytes(version,numrecs,dims,gatts,var_headers):
    """var_headers:(name,dimids,attrs,nc_type,vsize,begin)，begin按版本取32/64位。"""
    out=b'CDF'+bytes([version])+struct.pack('>I',numrecs)
    if dims:
        out+=struct.pack('>II',10,len(dims))
        for n,s in dims:out+=name_bytes(n)+struct.pack('>I',s)
    else:
        out+=struct.pack('>II',0,0)
    out+=attrs_bytes(gatts)
    if var_headers:
        out+=struct.pack('>II',11,len(var_headers))
        for n,dimids,attrs,tc,vsize,begin in var_headers:
            out+=name_bytes(n)+struct.pack('>I',len(dimids))
            for d in dimids:out+=struct.pack('>I',d)
            out+=attrs_bytes(attrs)+struct.pack('>II',tc,vsize)
            out+=struct.pack('>Q' if version==2 else '>I',begin)
    else:
        out+=struct.pack('>II',0,0)
    return out

def build(version,numrecs,dims,gatts,vars):
    """按经典布局构造合法文件，返回(bytes,info)。

    vars:dict(name,dimids,attrs=[],nc_type,data=bytes)为固定变量；
    dict(...,records=[每记录bytes])为record变量。info含各变量begin/vsize、
    recbase、recsize与header_size，便于断言。
    """
    prepared=[]
    for v in vars:
        tc=v['nc_type'];dimids=v['dimids'];es=ESIZE[tc]
        sizes=[dims[d][1] for d in dimids]
        is_rec=bool(dimids) and sizes[0]==0
        per=es
        for s in (sizes[1:] if is_rec else sizes):per*=s
        prepared.append((v,tc,is_rec,per))
    rec_vars=[p for p in prepared if p[2]]
    if len(rec_vars)==1:recsize=rec_vars[0][3]
    else:recsize=sum(round4(p[3]) for p in rec_vars)
    vh=[]
    for v,tc,is_rec,per in prepared:
        vh.append([v['name'],v['dimids'],v.get('attrs',[]),tc,round4(per),0])
    header_size=len(header_bytes(version,numrecs,dims,gatts,[tuple(x) for x in vh]))
    cur=header_size;begins={}
    for i,(v,tc,is_rec,per) in enumerate(prepared):
        if is_rec:continue
        begins[i]=cur;cur+=round4(per)
    recbase=cur
    off=0
    for i,(v,tc,is_rec,per) in enumerate(prepared):
        if not is_rec:continue
        begins[i]=recbase+off
        off+=per if len(rec_vars)==1 else round4(per)
    for i in range(len(vh)):vh[i][5]=begins[i]
    hdr=header_bytes(version,numrecs,dims,gatts,[tuple(x) for x in vh])
    data=b''
    for i,(v,tc,is_rec,per) in enumerate(prepared):
        if is_rec:continue
        blob=bytes(v['data']);assert len(blob)==per,'固定变量数据长度不符'
        data+=pad4(blob)
    for k in range(numrecs):
        for i,(v,tc,is_rec,per) in enumerate(prepared):
            if not is_rec:continue
            blob=bytes(v['records'][k]);assert len(blob)==per,'记录数据长度不符'
            data+=blob if len(rec_vars)==1 else pad4(blob)
    info={'header_size':header_size,'recbase':recbase if rec_vars else None,'recsize':recsize,
          'vars':{p[0]['name']:{'begin':begins[i],'vsize':vh[i][4],'is_record':p[2]}
                  for i,p in enumerate(prepared)}}
    return hdr+data,info
