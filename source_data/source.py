"""内存范围源与带身份核对的远端适配器。

远端适配器以长度+强ETag作为本次身份，所有range使用同一If-Match；
逐一核对206、Content-Range、实收长度与ETag。412或内容身份变化使本次
所有片段失效（抛IdentityError），禁止新旧片段拼成结果。
不连接真实资料服务，VirtualRangeSource为合成源。
"""

class SourceError(RuntimeError):
    """源响应不符合协议。"""

class IdentityError(SourceError):
    """内容身份与本次会话不符。"""

class Response:
    __slots__=('status','etag','content_range','body')
    def __init__(self,status,etag,content_range,body):
        self.status=status;self.etag=etag;self.content_range=content_range;self.body=body

class VirtualRangeSource:
    """合成范围源：整段字节或稀疏段支撑，可按脚本返回异常响应。

    behaviors按get_range调用序号生效，每项支持：
    switch_etag 更换服务端身份；lie 身份已变仍返回206；
    status/etag/content_range/short/body 覆盖响应字段。
    """
    def __init__(self,data=None,*,segments=None,length=None,etag,behaviors=()):
        if data is None and segments is None:raise ValueError('缺少数据')
        if data is not None:
            if segments is not None or length is not None:raise ValueError('data与segments/length互斥')
            data=bytes(data);length=len(data)
        else:
            segments={int(k):bytes(v) for k,v in segments.items()}
            if type(length) is not int or length<0:raise ValueError('长度非法')
            for off,blob in segments.items():
                if off<0 or off+len(blob)>length:raise ValueError('稀疏段越界')
        if not isinstance(etag,str) or not etag:raise ValueError('ETag非法')
        self.data=data;self.segments=segments;self.length=length;self.etag=etag
        self.behaviors=list(behaviors)
        self.calls=[];self.bytes_served=0
    def head(self):
        return self.length,self.etag
    def _slice(self,start,end):
        if self.data is not None:return self.data[start:end]
        out=bytearray(end-start)
        for off,blob in self.segments.items():
            lo=max(start,off);hi=min(end,off+len(blob))
            if lo<hi:out[lo-start:hi-start]=blob[lo-off:hi-off]
        return bytes(out)
    def get_range(self,start,end_incl,if_match):
        i=len(self.calls);self.calls.append((start,end_incl,if_match))
        b=dict(self.behaviors[i]) if i<len(self.behaviors) else {}
        if 'switch_etag' in b:self.etag=b['switch_etag']
        if if_match!=self.etag and not b.get('lie',False):
            resp=Response(412,self.etag,None,b'')
        elif not(0<=start<=end_incl<self.length):
            resp=Response(416,self.etag,None,b'')
        else:
            body=b.get('body',self._slice(start,end_incl+1))
            if 'short' in b:body=body[:b['short']]
            resp=Response(b.get('status',206),b.get('etag',self.etag),
                          b.get('content_range','bytes %d-%d/%d'%(start,end_incl,self.length)),body)
        self.bytes_served+=len(resp.body)
        return resp

class CheckedRemote:
    """把身份核对绑定到一次会话：所有片段同一身份，否则整体失效。"""
    def __init__(self,source,expected_etag=None):
        length,etag=source.head()
        if type(length) is not int or length<0:raise SourceError('源长度非法')
        if not isinstance(etag,str) or not etag or etag.startswith('W/'):
            raise SourceError('需要强ETag')
        if expected_etag is not None and etag!=expected_etag:
            raise IdentityError('ETag与声明身份不符')
        self.source=source;self.length=length;self.etag=etag
        self.requests=0;self.bytes_received=0
    def fetch(self,start,end_incl):
        if not(0<=start<=end_incl<self.length):raise SourceError('请求范围越界')
        resp=self.source.get_range(start,end_incl,self.etag)
        self.requests+=1
        if resp.status==412:raise IdentityError('源身份已变化(412)')
        if resp.status!=206:raise SourceError('非206响应:%r'%(resp.status,))
        if resp.etag!=self.etag:raise IdentityError('响应ETag与本次身份不符')
        want='bytes %d-%d/%d'%(start,end_incl,self.length)
        if resp.content_range!=want:raise SourceError('Content-Range不符:%r'%(resp.content_range,))
        if len(resp.body)!=end_incl-start+1:raise SourceError('实收长度不符')
        self.bytes_received+=len(resp.body)
        return resp.body
