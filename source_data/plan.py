"""按索引窗口生成稀疏读取计划：字节范围与目标元素的映射。

只对选中的元素计算偏移，不取整个record区，也不构建全部源元素索引。
相邻范围在单次字节预算(max_range_bytes)与多读字节预算(max_overread_bytes)
内合并；计划记录多读字节数，预算不是全量下载许可。
输出维序保持文件声明顺序，count=0产生空计划且无数据range。
"""
from itertools import product

class SelectionError(ValueError):
    """窗口选择或预算非法。"""

def _ints(seq,label):
    if not isinstance(seq,(list,tuple)):
        raise SelectionError('%s必须为整数列表'%label)
    out=[]
    for x in seq:
        if type(x) is not int:raise SelectionError('%s必须为整数'%label)
        out.append(x)
    return out

class Range:
    """一段合并后的读取范围：[start,end)半开区间，targets为(输出序号,范围内偏移)。"""
    __slots__=('start','end','targets','needed')
    def __init__(self,start,end):
        self.start=start;self.end=end;self.targets=[];self.needed=0

class Plan:
    __slots__=('out_shape','out_size','ranges','total_bytes','needed_bytes','overread_bytes')

def plan_window(header,var,start,count,stride,max_range_bytes,max_overread_bytes):
    if type(max_range_bytes) is not int or type(max_overread_bytes) is not int:
        raise SelectionError('预算必须为整数')
    if max_overread_bytes<0:raise SelectionError('多读预算不能为负')
    if max_range_bytes<var.elem_size:raise SelectionError('单次预算容不下一个元素')
    nd=len(var.dimids)
    start=_ints(start,'start');count=_ints(count,'count');stride=_ints(stride,'stride')
    if not(len(start)==len(count)==len(stride)==nd):
        raise SelectionError('选择维数与变量维数不符')
    sizes=[header.dim_size(i) for i in var.dimids]
    for k in range(nd):
        s,c,t=start[k],count[k],stride[k]
        if s<0 or c<0:raise SelectionError('start/count不能为负')
        if t<1:raise SelectionError('stride必须为正')
        if c and s+(c-1)*t>=sizes[k]:raise SelectionError('选择越界')
    plan=Plan();plan.out_shape=list(count);plan.ranges=[]
    out_size=1
    for c in count:out_size*=c
    plan.out_size=out_size
    if out_size:
        esize=var.elem_size
        # 每维字节步长：record变量首维为record步长，其余按声明形状连续存放
        byte_strides=[]
        for k in range(nd):
            if k==0 and var.is_record:
                byte_strides.append(header.recsize)
            else:
                p=esize
                for j in range(k+1,nd):p*=sizes[j]
                byte_strides.append(p)
        idxs=[list(range(start[k],start[k]+count[k]*stride[k],stride[k])) for k in range(nd)]
        out_i=0
        for combo in product(*idxs):
            off=var.begin
            for k in range(nd):off+=combo[k]*byte_strides[k]
            end=off+esize
            r=plan.ranges[-1] if plan.ranges else None
            if r is not None and 0<=off-r.end<=max_overread_bytes and end-r.start<=max_range_bytes:
                r.targets.append((out_i,off-r.start));r.end=end;r.needed+=esize
            else:
                r=Range(off,end);r.targets.append((out_i,0));r.needed=esize
                plan.ranges.append(r)
            out_i+=1
    plan.total_bytes=sum(r.end-r.start for r in plan.ranges)
    plan.needed_bytes=sum(r.needed for r in plan.ranges)
    plan.overread_bytes=plan.total_bytes-plan.needed_bytes
    return plan
