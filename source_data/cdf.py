"""经典NetCDF(CDF-1/CDF-2)头部解析与整体结构核对。

仅支持CDF-1与CDF-2；CDF-2只把变量begin扩为64位，其余字段与CDF-1一致。
不支持CDF-5、HDF5与numrecs=0xffffffff的流式声明，多字节字段全部大端。

布局规则独立核算，不信任任何单一字段：
- 固定变量自begin连续存放，占round4(元素数*元素宽)字节；
- record变量以无限维为首维，记录之间按record步长交错；
- record步长为各record变量单记录补齐字节之和；只有一个record变量时
  记录尾不补齐，步长取该变量单记录真实字节数（其声明vsize仍可为补齐值，
  也兼容未补齐写法，两者布局一致）；
- 名称、属性值与数据尾部的四字节补齐只属于存储布局，不算真实元素。

结构不合法、变量重叠或越界一律在此拒绝，不靠读到的数值"像不像"判定。
"""
import struct

NC_DIMENSION=10
NC_VARIABLE=11
NC_ATTRIBUTE=12
STREAMING_NUMRECS=0xffffffff
# 类型号:(名称,struct格式,元素宽)；char无数值格式，保持字符
TYPES={1:('byte','b',1),2:('char',None,1),3:('short','h',2),4:('int','i',4),5:('float','f',4),6:('double','d',8)}

class CdfError(ValueError):
    """经典CDF结构非法。"""

class NeedMore(Exception):
    """头部解析还需要更多字节；offset为需要的绝对偏移（不含）。"""
    def __init__(self,offset):
        super().__init__(offset)
        self.offset=offset

def round4(n):
    return (n+3)&~3

class Dim:
    __slots__=('name','size')
    def __init__(self,name,size):
        self.name=name;self.size=size
    @property
    def is_record(self):
        return self.size==0

class Attr:
    __slots__=('name','nc_type','values')
    def __init__(self,name,nc_type,values):
        self.name=name;self.nc_type=nc_type;self.values=values

class Var:
    __slots__=('name','nc_type','type_name','elem_size','dimids','attrs','vsize','begin',
               'is_record','per_record','fixed_bytes')
    def __init__(self,name,nc_type,dimids,attrs,vsize,begin):
        self.name=name;self.nc_type=nc_type
        self.type_name,_,self.elem_size=TYPES[nc_type]
        self.dimids=dimids;self.attrs=attrs;self.vsize=vsize;self.begin=begin
        self.is_record=False;self.per_record=0;self.fixed_bytes=0

class Header:
    def __init__(self,version,numrecs,dims,gatts,vars,header_size):
        self.version=version;self.numrecs=numrecs
        self.dims=dims;self.gatts=gatts;self.vars=vars
        self.header_size=header_size
        self.recbase=None;self.recsize=0
        self._index={}
        for v in vars:
            if v.name in self._index:raise CdfError('变量名重复:%r'%v.name)
            self._index[v.name]=v
    def dim_size(self,dimid):
        d=self.dims[dimid]
        return self.numrecs if d.size==0 else d.size
    def var(self,name):
        try:return self._index[name]
        except KeyError:raise CdfError('变量不存在:%r'%name) from None
    def validate(self,length):
        """与文件长度整体核对vsize/begin/数据范围，拒绝重叠与越界。"""
        if type(length) is not int or length<0:raise CdfError('文件长度非法')
        rec_vars=[v for v in self.vars if v.is_record]
        fixed=[v for v in self.vars if not v.is_record]
        for v in self.vars:
            if v.is_record:
                expect=round4(v.per_record)
                ok=v.vsize in (expect,v.per_record) if len(rec_vars)==1 else v.vsize==expect
                if not ok:raise CdfError('变量%s的vsize与布局不符'%v.name)
            elif v.vsize!=round4(v.fixed_bytes):
                raise CdfError('变量%s的vsize与布局不符'%v.name)
            if v.begin<self.header_size:raise CdfError('变量%s的begin落入头部'%v.name)
        extents=sorted((v.begin,v.begin+v.vsize,v.name) for v in fixed)
        for i,(s,e,n) in enumerate(extents):
            if e>length:raise CdfError('变量%s的数据越出文件'%n)
            if i and s<extents[i-1][1]:raise CdfError('变量数据区重叠')
        if rec_vars:
            self.recbase=min(v.begin for v in rec_vars)
            if extents and extents[-1][1]>self.recbase:raise CdfError('固定变量与记录区重叠')
            slots=sorted((v.begin-self.recbase,v.per_record,v.name) for v in rec_vars)
            for i,(off,sz,n) in enumerate(slots):
                if i and off<slots[i-1][0]+slots[i-1][1]:raise CdfError('record变量槽位重叠')
            if len(rec_vars)==1:
                self.recsize=slots[0][1]
            else:
                self.recsize=sum(round4(v.per_record) for v in rec_vars)
            if slots[-1][0]+slots[-1][1]>self.recsize:raise CdfError('record槽位超出记录步长')
            if self.recbase+self.numrecs*self.recsize>length:raise CdfError('记录区越出文件')
        return self

class _Cursor:
    __slots__=('buf','pos')
    def __init__(self,buf):
        self.buf=buf;self.pos=0
    def u32(self):
        if self.pos+4>len(self.buf):raise NeedMore(self.pos+4)
        v=struct.unpack_from('>I',self.buf,self.pos)[0];self.pos+=4;return v
    def u64(self):
        if self.pos+8>len(self.buf):raise NeedMore(self.pos+8)
        v=struct.unpack_from('>Q',self.buf,self.pos)[0];self.pos+=8;return v
    def take(self,n):
        if self.pos+n>len(self.buf):raise NeedMore(self.pos+n)
        b=self.buf[self.pos:self.pos+n];self.pos+=n;return b
    def name(self):
        n=self.u32()
        if n<1:raise CdfError('名称为空')
        raw=self.take(round4(n))[:n]
        try:return raw.decode('utf8')
        except UnicodeDecodeError:raise CdfError('名称不是合法UTF-8') from None

def _dims(cur):
    tag=cur.u32();count=cur.u32()
    if tag==0:
        if count!=0:raise CdfError('维度列表标记与计数不符')
        return []
    if tag!=NC_DIMENSION:raise CdfError('维度列表标记非法')
    dims=[];seen=set()
    for _ in range(count):
        nm=cur.name()
        if nm in seen:raise CdfError('维度名重复:%r'%nm)
        seen.add(nm)
        dims.append(Dim(nm,cur.u32()))
    zeros=[i for i,d in enumerate(dims) if d.size==0]
    if len(zeros)>1:raise CdfError('无限维至多一个')
    if zeros and zeros[0]!=0:raise CdfError('无限维必须是首维')
    return dims

def _attrs(cur):
    tag=cur.u32();count=cur.u32()
    if tag==0:
        if count!=0:raise CdfError('属性列表标记与计数不符')
        return []
    if tag!=NC_ATTRIBUTE:raise CdfError('属性列表标记非法')
    out=[];seen=set()
    for _ in range(count):
        nm=cur.name()
        if nm in seen:raise CdfError('属性名重复:%r'%nm)
        seen.add(nm)
        tc=cur.u32()
        if tc not in TYPES:raise CdfError('属性类型非法')
        ne=cur.u32()
        es=TYPES[tc][2]
        raw=cur.take(round4(ne*es))[:ne*es]
        if tc==2:values=raw
        else:values=list(struct.unpack('>%d%s'%(ne,TYPES[tc][1]),raw)) if ne else []
        out.append(Attr(nm,tc,values))
    return out

def parse_header(buf):
    """从字节串解析头部；字节不足抛NeedMore，结构非法抛CdfError。"""
    cur=_Cursor(buf)
    magic=cur.take(4)
    if magic[:3]!=b'CDF':raise CdfError('不是经典CDF签名')
    version=magic[3]
    if version not in (1,2):raise CdfError('仅支持CDF-1/CDF-2')
    numrecs=cur.u32()
    if numrecs==STREAMING_NUMRECS:raise CdfError('不支持流式numrecs')
    dims=_dims(cur)
    gatts=_attrs(cur)
    tag=cur.u32();count=cur.u32()
    vars=[]
    if tag==0:
        if count!=0:raise CdfError('变量列表标记与计数不符')
    elif tag!=NC_VARIABLE:raise CdfError('变量列表标记非法')
    else:
        record_dim=0 if dims and dims[0].size==0 else None
        for _ in range(count):
            nm=cur.name()
            nd=cur.u32()
            if nd>len(dims):raise CdfError('变量%s的维数超过维度表'%nm)
            dimids=[cur.u32() for _ in range(nd)]
            for d in dimids:
                if d>=len(dims):raise CdfError('变量%s的维度下标越界'%nm)
            if len(set(dimids))!=len(dimids):raise CdfError('变量%s的维度重复'%nm)
            if record_dim is not None and record_dim in dimids[1:]:
                raise CdfError('无限维必须是变量%s的首维'%nm)
            attrs={a.name:a for a in _attrs(cur)}
            tc=cur.u32()
            if tc not in TYPES:raise CdfError('变量%s类型非法'%nm)
            vsize=cur.u32()
            begin=cur.u64() if version==2 else cur.u32()
            v=Var(nm,tc,dimids,attrs,vsize,begin)
            v.is_record=record_dim is not None and dimids[:1]==[record_dim]
            es=v.elem_size
            if v.is_record:
                per=es
                for d in dimids[1:]:per*=dims[d].size
                v.per_record=per
            else:
                total=es
                for d in dimids:total*=dims[d].size
                v.fixed_bytes=total
            vars.append(v)
    return Header(version,numrecs,dims,gatts,vars,cur.pos)
