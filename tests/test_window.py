"""窗口采集的独立核对与边界测试：交错record、空选择、负byte、跨补齐、ETag变化。"""
import io
import json
import math
import struct
import sys
import unittest
from contextlib import redirect_stdout
from itertools import product
from pathlib import Path
from unittest.mock import patch
from source_data.cdf import CdfError,parse_header
from source_data.plan import SelectionError,plan_window
from source_data.source import CheckedRemote,IdentityError,SourceError,VirtualRangeSource
from source_data.window import acquire,load_header,read_window
from tests.cdfgen import build,header_bytes,name_bytes

DATA=Path('data')
WIND=(DATA/'wind.nc').read_bytes()
WIND64=(DATA/'wind64.nc').read_bytes()
SINGLE=(DATA/'single-record.nc').read_bytes()

# ---- 独立整体解码参考：不经过稀疏路径，对整文件字节自行解析并切片 ----
REF={1:('b',1),2:(None,1),3:('h',2),4:('i',4),5:('f',4),6:('d',8)}

def ref_window(data,name,start,count,stride):
    assert data[:3]==b'CDF' and data[3] in (1,2)
    ver=data[3];numrecs=struct.unpack_from('>I',data,4)[0];pos=8
    def u32():
        nonlocal pos
        v=struct.unpack_from('>I',data,pos)[0];pos+=4;return v
    def nm():
        nonlocal pos
        n=u32();s=data[pos:pos+n].decode();pos+=(n+3)&~3;return s
    def skip_attrs():
        nonlocal pos
        tag,cnt=u32(),u32()
        if tag==0:return
        for _ in range(cnt):
            nm();tc=u32();ne=u32();pos+=((ne*REF[tc][1])+3)&~3
    tag,nd=u32(),u32();dims=[]
    for _ in range(nd):dims.append((nm(),u32()))
    skip_attrs()
    tag,nv=u32(),u32();allvars=[]
    for _ in range(nv):
        vn=nm();nd2=u32();dids=[u32() for _ in range(nd2)];skip_attrs();tc=u32();u32()
        begin=struct.unpack_from('>Q' if ver==2 else '>I',data,pos)[0];pos+=8 if ver==2 else 4
        allvars.append((vn,dids,tc,begin))
    recpers=[]
    for vn,dids,tc,begin in allvars:
        if dids and dims[dids[0]][1]==0:
            per=REF[tc][1]
            for d in dids[1:]:per*=dims[d][1]
            recpers.append(per)
    recsize=recpers[0] if len(recpers)==1 else sum((p+3)&~3 for p in recpers)
    for vn,dids,tc,begin in allvars:
        if vn!=name:continue
        fmt,es=REF[tc]
        sizes=[numrecs if dims[d][1]==0 else dims[d][1] for d in dids]
        if dids and dims[dids[0]][1]==0:
            inner=1
            for s in sizes[1:]:inner*=s
            offs=[begin+r*recsize+j*es for r in range(sizes[0]) for j in range(inner)]
        else:
            total=1
            for s in sizes:total*=s
            offs=[begin+j*es for j in range(total)]
        vals=[chr(data[off]) if fmt is None else struct.unpack_from('>'+fmt,data,off)[0] for off in offs]
        out=[]
        for combo in product(*[range(start[k],start[k]+count[k]*stride[k],stride[k]) for k in range(len(dids))]):
            lin=0
            for k,idx in enumerate(combo):
                w=1
                for s in sizes[k+1:]:w*=s
                lin+=idx*w
            out.append(vals[lin])
        return out
    raise KeyError(name)

def sparse(data,windows,mrb=64,mob=8,etag='"t"'):
    src=VirtualRangeSource(data=data,etag=etag)
    remote=CheckedRemote(src,expected_etag=etag)
    header,hb=load_header(remote)
    res=acquire(remote,header,windows,max_range_bytes=mrb,max_overread_bytes=mob)
    return res,src,remote,hb

def header_of(data):
    src=VirtualRangeSource(data=data,etag='"t"')
    remote=CheckedRemote(src)
    return load_header(remote)[0]

class ParseTests(unittest.TestCase):
    def test_fixture_layout(self):
        h=header_of(WIND)
        self.assertEqual((h.version,h.numrecs,h.header_size,h.recsize),(1,2,144,8))
        self.assertEqual([d.name for d in h.dims],['time','cell'])
        self.assertEqual(h.var('power').nc_type,3)
        self.assertEqual(h.var('status').nc_type,1)
        self.assertEqual((h.var('power').begin,h.var('status').begin),(144,148))
        h64=header_of(WIND64)
        self.assertEqual((h64.version,h64.header_size,h64.recsize),(2,152,8))
        self.assertEqual((h64.var('power').begin,h64.var('status').begin),(152,156))
        h1=header_of(SINGLE)
        self.assertEqual((h1.header_size,h1.recsize),(96,2))
        self.assertEqual(h1.var('flag').vsize,4)

class CrossCheckTests(unittest.TestCase):
    def check(self,data,name,sizes,windows):
        for start,count,stride in windows:
            with self.subTest(var=name,sel=(start,count,stride)):
                res,_src,_r,_hb=sparse(data,{name:{'start':start,'count':count,'stride':stride}})
                self.assertEqual(res['windows'][name]['raw'],ref_window(data,name,start,count,stride))
    def test_fixtures(self):
        full=[[0,0],[2,2],[1,1]]
        cases=[full,[[0,0],[2,1],[1,2]],[[1,1],[1,1],[1,1]],[[0,0],[0,2],[1,1]],
               [[1,0],[1,2],[1,1]],[[0,1],[2,1],[1,1]]]
        for data in (WIND,WIND64):
            for name in ('power','status'):self.check(data,name,None,cases)
        self.check(SINGLE,'flag',None,cases)
    def test_interleaved_records(self):
        res,_,_,_=sparse(WIND,{'power':{'start':[0,0],'count':[2,2],'stride':[1,1]},
                               'status':{'start':[0,0],'count':[2,2],'stride':[1,1]}})
        self.assertEqual(res['windows']['power']['raw'],[10,-999,30,40])
        st=res['windows']['status']
        self.assertEqual(st['raw'],[1,2,3,4])
        self.assertEqual(st['byte_ranges'],[[148,158]])
        self.assertEqual(st['transfer'],{'requests':1,'bytes':10,'overread_bytes':6})
    def test_single_record_no_padding(self):
        res,_,_,_=sparse(SINGLE,{'flag':{'start':[0,0],'count':[2,2],'stride':[1,1]}})
        w=res['windows']['flag']
        self.assertEqual(w['raw'],[1,-1,2,-2])
        self.assertEqual(w['byte_ranges'],[[96,100]])
        self.assertEqual(w['transfer']['overread_bytes'],0)
        self.assertEqual(len(SINGLE),96+2*2)
    def test_empty_selection(self):
        src=VirtualRangeSource(data=WIND,etag='"t"')
        remote=CheckedRemote(src)
        header,_=load_header(remote)
        before=remote.requests
        res=acquire(remote,header,{'power':{'start':[0,0],'count':[0,2],'stride':[1,1]}},
                    max_range_bytes=64,max_overread_bytes=8)
        w=res['windows']['power']
        self.assertEqual(w['shape'],[0,2])
        self.assertEqual((w['raw'],w['mask'],w['values'],w['byte_ranges']),([],[],[],[]))
        self.assertEqual(remote.requests,before)
    def test_sparse_not_whole_record_region(self):
        res,_,_,_=sparse(WIND,{'power':{'start':[0,0],'count':[1,1],'stride':[1,1]}})
        w=res['windows']['power']
        self.assertEqual(w['byte_ranges'],[[144,146]])
        self.assertEqual(w['transfer']['bytes'],2)

class SyntheticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.attr_file,cls.attr_info=build(1,0,[('cell',4),('nc',5)],[('title',2,b'synthetic')],[
            {'name':'t','dimids':[0],'nc_type':3,
             'attrs':[('_FillValue',3,[-999]),('missing_value',3,[-1,-2]),
                      ('scale_factor',5,[0.5]),('add_offset',6,[1.0])],
             'data':struct.pack('>4h',10,-999,-2,20)},
            {'name':'f','dimids':[0],'nc_type':5,
             'attrs':[('_FillValue',5,[float('nan')]),('missing_value',5,[-9999.0])],
             'data':struct.pack('>4f',1.5,float('nan'),-9999.0,2.5)},
            {'name':'d','dimids':[0],'nc_type':6,'attrs':[('missing_value',6,[-1.0])],
             'data':struct.pack('>4d',1.0,-1.0,2.0,-1.0)},
            {'name':'b','dimids':[0],'nc_type':1,'attrs':[('_FillValue',1,[-128])],
             'data':struct.pack('>4b',-128,5,-1,7)},
            {'name':'z','dimids':[0],'nc_type':6,'attrs':[('_FillValue',6,[0.0])],
             'data':struct.pack('>4d',0.0,-0.0,1.0,0.0)},
            {'name':'c','dimids':[1],'nc_type':2,'attrs':[('scale_factor',5,[2.0])],
             'data':b'ABCDE'},
            {'name':'s','dimids':[0],'nc_type':3,'data':struct.pack('>4h',7,8,9,10)},
            {'name':'g','dimids':[],'nc_type':6,'data':struct.pack('>d',3.25)},
        ])
        cls.rec_file,cls.rec_info=build(1,3,[('time',0),('cell',3)],[],[
            {'name':'rb','dimids':[0,1],'nc_type':1,
             'records':[struct.pack('>3b',1,2,3),struct.pack('>3b',4,5,6),struct.pack('>3b',-1,-2,-3)]},
            {'name':'ri','dimids':[0],'nc_type':4,
             'records':[struct.pack('>i',100),struct.pack('>i',200),struct.pack('>i',300)]},
        ])
        cls.one_file,cls.one_info=build(1,2,[('time',0),('cell',3)],[],[
            {'name':'rs','dimids':[0,1],'nc_type':3,
             'records':[struct.pack('>3h',1,2,3),struct.pack('>3h',4,5,6)]},
        ])
    def test_fill_missing_scale(self):
        res,_,_,_=sparse(self.attr_file,{'t':{'start':[0],'count':[4],'stride':[1]}})
        w=res['windows']['t']
        self.assertEqual(w['raw'],[10,-999,-2,20])
        self.assertEqual(w['mask'],[False,True,True,False])
        self.assertEqual(w['values'],[6.0,None,None,11.0])
    def test_nan_and_float_missing(self):
        res,_,_,_=sparse(self.attr_file,{'f':{'start':[0],'count':[4],'stride':[1]}})
        w=res['windows']['f']
        self.assertTrue(math.isnan(w['raw'][1]))
        self.assertEqual(w['mask'],[False,True,True,False])
        self.assertEqual(w['values'],[1.5,None,None,2.5])
        res,_,_,_=sparse(self.attr_file,{'d':{'start':[0],'count':[4],'stride':[1]}})
        self.assertEqual(res['windows']['d']['mask'],[False,True,False,True])
    def test_packed_domain_signed_zero(self):
        res,_,_,_=sparse(self.attr_file,{'z':{'start':[0],'count':[4],'stride':[1]}})
        w=res['windows']['z']
        self.assertEqual(w['mask'],[True,False,False,True])
        self.assertEqual(math.copysign(1.0,w['raw'][1]),-1.0)
    def test_negative_signed_byte(self):
        res,_,_,_=sparse(self.attr_file,{'b':{'start':[0],'count':[4],'stride':[1]}})
        w=res['windows']['b']
        self.assertEqual(w['raw'],[-128,5,-1,7])
        self.assertEqual(w['mask'],[True,False,False,False])
    def test_char_stays_char(self):
        res,_,_,_=sparse(self.attr_file,{'c':{'start':[0],'count':[3],'stride':[2]}})
        w=res['windows']['c']
        self.assertEqual(w['raw'],['A','C','E'])
        self.assertEqual(w['values'],['A','C','E'])
        self.assertEqual(w['mask'],[False,False,False])
    def test_cross_padding_between_fixed_vars(self):
        cbegin=self.attr_info['vars']['c']['begin']
        res,_,_,_=sparse(self.attr_file,{'c':{'start':[3],'count':[2],'stride':[1]},
                                         's':{'start':[0],'count':[4],'stride':[1]}})
        w=res['windows']['c']
        self.assertEqual(w['raw'],['D','E'])
        self.assertEqual(w['byte_ranges'],[[cbegin+3,cbegin+5]])
        self.assertEqual(res['windows']['s']['raw'],[7,8,9,10])
    def test_scalar_var(self):
        res,_,_,_=sparse(self.attr_file,{'g':{'start':[],'count':[],'stride':[]}})
        self.assertEqual(res['windows']['g']['raw'],[3.25])
    def test_record_slot_padding(self):
        info=self.rec_info
        self.assertEqual(info['recsize'],8)
        res,_,_,_=sparse(self.rec_file,{'rb':{'start':[0,0],'count':[3,3],'stride':[1,1]}},mob=0)
        w=res['windows']['rb']
        base=info['vars']['rb']['begin']
        self.assertEqual(w['raw'],[1,2,3,4,5,6,-1,-2,-3])
        self.assertEqual(w['byte_ranges'],[[base,base+3],[base+8,base+11],[base+16,base+19]])
        self.assertEqual(w['transfer']['overread_bytes'],0)
        res,_,_,_=sparse(self.rec_file,{'ri':{'start':[0],'count':[3],'stride':[1]}})
        self.assertEqual(res['windows']['ri']['raw'],[100,200,300])
    def test_record_merge_budget(self):
        res,_,_,_=sparse(self.rec_file,{'rb':{'start':[0,0],'count':[3,3],'stride':[1,1]}},mrb=64,mob=5)
        w=res['windows']['rb']
        self.assertEqual(len(w['byte_ranges']),1)
        self.assertEqual(w['transfer'],{'requests':1,'bytes':19,'overread_bytes':10})
    def test_single_record_short_unpadded(self):
        self.assertEqual(self.one_info['recsize'],6)
        self.assertEqual(self.one_info['vars']['rs']['vsize'],8)
        res,_,_,_=sparse(self.one_file,{'rs':{'start':[0,0],'count':[2,3],'stride':[1,1]}})
        self.assertEqual(res['windows']['rs']['raw'],[1,2,3,4,5,6])
        self.assertEqual(res['windows']['rs']['raw'],
                         ref_window(self.one_file,'rs',[0,0],[2,3],[1,1]))
    def test_synthetic_cross_check(self):
        for data,name,sel in [(self.attr_file,'t',([0],[4],[1])),(self.attr_file,'s',([0],[4],[1])),
                              (self.rec_file,'rb',([0,0],[3,3],[1,1])),(self.rec_file,'ri',([0],[3],[1])),
                              (self.one_file,'rs',([0,0],[2,3],[1,1]))]:
            res,_,_,_=sparse(data,{name:{'start':sel[0],'count':sel[1],'stride':sel[2]}})
            self.assertEqual(res['windows'][name]['raw'],ref_window(data,name,*sel))

class Cdf2BigBeginTests(unittest.TestCase):
    def test_begin_not_truncated(self):
        begin=2**32+64
        hdr=header_bytes(2,0,[('cell',2)],[],[('x',[0],[],4,8,begin)])
        data=struct.pack('>2i',111,222)
        src=VirtualRangeSource(segments={0:hdr,begin:data},length=begin+len(data),etag='"big"')
        remote=CheckedRemote(src,expected_etag='"big"')
        header,_=load_header(remote)
        self.assertEqual(header.var('x').begin,begin)
        res=acquire(remote,header,{'x':{'start':[0],'count':[2],'stride':[1]}},
                    max_range_bytes=64,max_overread_bytes=8)
        w=res['windows']['x']
        self.assertEqual(w['raw'],[111,222])
        self.assertEqual(w['byte_ranges'],[[begin,begin+8]])

class IdentityTests(unittest.TestCase):
    def test_etag_switch_mid_transfer_412(self):
        src=VirtualRangeSource(data=WIND,etag='"v1"',behaviors=[{},{},{'switch_etag':'"v2"'}])
        remote=CheckedRemote(src,expected_etag='"v1"')
        header,_=load_header(remote)
        with self.assertRaises(IdentityError):
            acquire(remote,header,{'power':{'start':[0,0],'count':[2,1],'stride':[1,2]}},
                    max_range_bytes=64,max_overread_bytes=0)
    def test_etag_switch_mid_transfer_lying_206(self):
        src=VirtualRangeSource(data=WIND,etag='"v1"',behaviors=[{},{},{'switch_etag':'"v2"','lie':True}])
        remote=CheckedRemote(src,expected_etag='"v1"')
        header,_=load_header(remote)
        with self.assertRaises(IdentityError):
            acquire(remote,header,{'power':{'start':[0,0],'count':[2,1],'stride':[1,2]}},
                    max_range_bytes=64,max_overread_bytes=0)
    def test_412_on_first_range(self):
        src=VirtualRangeSource(data=WIND,etag='"v1"',behaviors=[{'switch_etag':'"v2"'}])
        remote=CheckedRemote(src,expected_etag='"v1"')
        with self.assertRaises(IdentityError):load_header(remote)
    def test_wrong_length(self):
        src=VirtualRangeSource(data=WIND,etag='"v1"',behaviors=[{},{'short':1}])
        remote=CheckedRemote(src,expected_etag='"v1"')
        header,_=load_header(remote)
        with self.assertRaises(SourceError):
            acquire(remote,header,{'power':{'start':[0,0],'count':[2,2],'stride':[1,1]}},
                    max_range_bytes=64,max_overread_bytes=8)
    def test_wrong_content_range(self):
        src=VirtualRangeSource(data=WIND,etag='"v1"',behaviors=[{},{'content_range':'bytes 0-1/160'}])
        remote=CheckedRemote(src,expected_etag='"v1"')
        header,_=load_header(remote)
        with self.assertRaises(SourceError):
            acquire(remote,header,{'power':{'start':[0,0],'count':[2,2],'stride':[1,1]}},
                    max_range_bytes=64,max_overread_bytes=8)
    def test_weak_etag_rejected(self):
        with self.assertRaises(SourceError):
            CheckedRemote(VirtualRangeSource(data=WIND,etag='W/"x"'))
    def test_config_etag_mismatch(self):
        with self.assertRaises(IdentityError):
            CheckedRemote(VirtualRangeSource(data=WIND,etag='"v1"'),expected_etag='"other"')
    def test_if_match_sent_on_every_range(self):
        src=VirtualRangeSource(data=WIND,etag='"v1"')
        remote=CheckedRemote(src,expected_etag='"v1"')
        header,_=load_header(remote)
        acquire(remote,header,{'power':{'start':[0,0],'count':[2,2],'stride':[1,1]}},
                max_range_bytes=64,max_overread_bytes=8)
        self.assertTrue(src.calls)
        self.assertTrue(all(c[2]=='"v1"' for c in src.calls))

class TransferTests(unittest.TestCase):
    def test_transfer_recomputable_from_plan(self):
        for cfg_name in ('normal.json','cdf2.json','single-byte-record.json'):
            cfg=json.loads((DATA/cfg_name).read_text())
            data=(DATA/cfg['file']).read_bytes()
            with self.subTest(cfg=cfg_name):
                res,src,remote,hb=sparse(data,cfg['windows'],
                                         mrb=cfg['max_range_bytes'],mob=cfg['max_overread_bytes'],
                                         etag=cfg['strong_etag'])
                planned=sum(r[1]-r[0] for w in res['windows'].values() for r in w['byte_ranges'])
                self.assertEqual(res['totals']['bytes'],planned)
                self.assertEqual(res['totals']['bytes']+hb,remote.bytes_received)
                self.assertEqual(remote.bytes_received,src.bytes_served)
                self.assertLess(planned,len(data))
    def test_merge_respects_single_read_budget(self):
        res,_,_,_=sparse(WIND,{'power':{'start':[0,0],'count':[2,2],'stride':[1,1]}},mrb=6,mob=8)
        w=res['windows']['power']
        self.assertEqual(w['byte_ranges'],[[144,148],[152,156]])
        self.assertEqual(w['transfer']['requests'],2)

class InvalidStructureTests(unittest.TestCase):
    def test_bad_signature(self):
        for raw in [(DATA/'invalid-signature.bin').read_bytes(),b'\x89HDF\r\n\x1a\n'+b'\0'*32,
                    b'CDF\x05'+b'\0'*28,b'CDF\x00'+b'\0'*28]:
            with self.assertRaises(CdfError):parse_header(raw)
    def test_streaming_numrecs(self):
        with self.assertRaises(CdfError):parse_header(b'CDF\x01'+struct.pack('>I',0xffffffff))
    def test_truncated_header(self):
        src=VirtualRangeSource(data=WIND[:60],etag='"t"')
        with self.assertRaises(CdfError):load_header(CheckedRemote(src))
    def craft(self,var_headers,dims=[('cell',2)],numrecs=0,length=None,gatts=[]):
        hdr=header_bytes(1,numrecs,dims,gatts,var_headers)
        return hdr,(len(hdr)+64 if length is None else length)
    def test_overlap(self):
        hdr,_=self.craft([('x',[0],[],3,4,64),('y',[0],[],3,4,64)])
        with self.assertRaises(CdfError):parse_header(hdr).validate(len(hdr)+64)
    def test_begin_beyond_length(self):
        hdr=header_bytes(1,0,[('cell',2)],[],[('x',[0],[],3,4,1000)])
        with self.assertRaises(CdfError):parse_header(hdr).validate(200)
    def test_vsize_mismatch(self):
        hdr=header_bytes(1,0,[('cell',2)],[],[('x',[0],[],3,8,64)])
        with self.assertRaises(CdfError):parse_header(hdr).validate(len(hdr)+64)
    def test_record_region_beyond_length(self):
        hdr=header_bytes(1,3,[('time',0),('cell',2)],[],[('f',[0,1],[],1,4,96)])
        with self.assertRaises(CdfError):parse_header(hdr).validate(len(hdr)+4)
    def test_unlimited_not_first(self):
        hdr=header_bytes(1,0,[('cell',2),('time',0)],[],[])
        with self.assertRaises(CdfError):parse_header(hdr)
    def test_two_unlimited(self):
        hdr=header_bytes(1,0,[('a',0),('b',0)],[],[])
        with self.assertRaises(CdfError):parse_header(hdr)
    def test_dimid_out_of_range(self):
        hdr=header_bytes(1,0,[('cell',2)],[],[('x',[7],[],3,4,64)])
        with self.assertRaises(CdfError):parse_header(hdr)
    def test_record_dim_not_first_in_var(self):
        hdr=header_bytes(1,1,[('time',0),('cell',2)],[],[('x',[1,0],[],3,4,64)])
        with self.assertRaises(CdfError):parse_header(hdr)
    def test_bad_dim_tag(self):
        with self.assertRaises(CdfError):parse_header(b'CDF\x01'+struct.pack('>III',0,99,0))
    def test_bad_attr_type(self):
        gatt=struct.pack('>II',12,1)+name_bytes('bad')+struct.pack('>II',9,1)+b'\0'*4
        hdr=b'CDF\x01'+struct.pack('>I',0)+struct.pack('>II',0,0)+gatt+struct.pack('>II',0,0)
        with self.assertRaises(CdfError):parse_header(hdr)
    def test_single_record_vsize_variants(self):
        # 唯一record变量：声明vsize为补齐值或未补齐值都合法，布局一致
        for vs in (8,6):
            hdr=header_bytes(1,2,[('time',0),('cell',3)],[],[('rs',[0,1],[],3,vs,96)])
            h=parse_header(hdr).validate(96+12)
            self.assertEqual(h.recsize,6)
    def test_fill_type_mismatch(self):
        data,_=build(1,0,[('cell',2)],[],[
            {'name':'t','dimids':[0],'nc_type':3,'attrs':[('_FillValue',5,[-999.0])],
             'data':struct.pack('>2h',1,2)}])
        with self.assertRaises(CdfError):
            sparse(data,{'t':{'start':[0],'count':[2],'stride':[1]}})

class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.header=header_of(WIND)
        self.var=self.header.var('power')
    def plan(self,start,count,stride,mrb=64,mob=8):
        return plan_window(self.header,self.var,start,count,stride,mrb,mob)
    def test_bad_selections(self):
        for sel in [([-1,0],[2,2],[1,1]),([0,0],[2,2],[1,0]),([0,0],[3,1],[1,1]),
                    ([5,0],[1,1],[1,1]),([0],[2],[1]),([True,0],[2,2],[1,1]),
                    ([0,0],[1.5,1],[1,1])]:
            with self.subTest(sel=sel),self.assertRaises(SelectionError):
                self.plan(*sel)
    def test_bad_budgets(self):
        with self.assertRaises(SelectionError):self.plan([0,0],[2,2],[1,1],mrb=1)
        with self.assertRaises(SelectionError):self.plan([0,0],[2,2],[1,1],mob=-1)

class CliTests(unittest.TestCase):
    def run_cli(self,*argv):
        from source_data import cli
        buf=io.StringIO()
        with patch.object(sys,'argv',['cli',*argv]),redirect_stdout(buf):
            cli.main()
        return json.loads(buf.getvalue())
    def test_window_normal(self):
        out=self.run_cli('window','data/normal.json')
        w=out['windows']['power']
        self.assertEqual((out['format'],out['numrecs']),(1,2))
        self.assertEqual(w['dimensions'],['time','cell'])
        self.assertEqual(w['shape'],[2,1])
        self.assertEqual(w['raw'],[10,30])
        self.assertEqual(w['values'],[10,30])
        self.assertEqual(w['mask'],[False,False])
        self.assertEqual(w['byte_ranges'],[[144,154]])
        self.assertEqual(out['totals']['bytes'],10)
        self.assertEqual(out['totals']['overread_bytes'],6)
        self.assertEqual(out['totals']['received_bytes'],out['totals']['bytes']+out['totals']['header_bytes'])
    def test_window_cdf2(self):
        out=self.run_cli('window','data/cdf2.json')
        self.assertEqual(out['format'],2)
        self.assertEqual(out['windows']['power']['raw'],[10,30])
        self.assertEqual(out['windows']['power']['byte_ranges'],[[152,162]])
    def test_window_single_byte_record(self):
        out=self.run_cli('window','data/single-byte-record.json')
        w=out['windows']['flag']
        self.assertEqual(w['raw'],[1,-1,2,-2])
        self.assertEqual(w['byte_ranges'],[[96,100]])
    def test_window_invalid_domain(self):
        with self.assertRaises(ValueError):self.run_cli('window','data/invalid-domain.json')
    def test_inspect_still_works(self):
        out=self.run_cli('inspect','data/normal.json')
        self.assertEqual(out['summary']['format'],1)

if __name__=='__main__':unittest.main()
