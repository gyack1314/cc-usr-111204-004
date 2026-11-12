# 经典CDF源

输入只支持CDF-1/CDF-2，所有数值大端。维度、属性、变量列表使用经典头部；空列表由两个零32位字段表示。CDF-2只改变变量begin为64位，其他字段按经典格式。无限维长度0由numrecs给当前记录数，numrecs=0xffffffff流式声明不支持。vsize、begin及数据范围须整体核对，不信任文件长度或某个局部偏移。

start/count/stride均按声明维序，count=0产生空输出且无数据range。强ETag是固定对象身份，range范围含两端；虚拟源可按配置返回206、412、错误长度或更换ETag。字节合并计划应记录多读字节数，不把预算当全量下载许可。原类型byte为有符号8位，char与数值分开；NaN缺测哨兵按NaN判定策略保存，填充值比较先于scale/offset。

现有domain只读取8字节前导和文件长度，不解析维度或规划窗口。

# 窗口采集补充

- 布局独立核算：固定变量自begin连续存放；record变量以无限维为首维跨记录交错，record步长为各record变量单记录补齐字节之和；仅一个record变量时记录尾不补齐，步长取真实字节数（声明vsize可为补齐或未补齐值，布局一致）。vsize、begin与数据范围整体核对，重叠或越界即拒绝，不靠读到的数值判定。
- 选择按声明维序给出start/count/stride,start/count非负、stride为正、count可为零；越界即拒绝。读取计划只对选中元素计算偏移，不取整个record区，不构建全部源元素索引；相邻范围在max_range_bytes与max_overread_bytes内合并，计划记录多读字节数，传输量可按计划复算。
- 远端身份为长度+强ETag，所有range使用同一If-Match；逐一核对206、Content-Range、实收长度与ETag;412或内容身份变化使本次全部片段失效，禁止新旧片段拼成结果。
- _FillValue与missing_value在打包域比较（浮点按位模式，NaN哨兵按NaN判定），命中先标缺测，再对非缺测应用scale_factor与add_offset;char保持字符数组，不变成数字；不对文件中的时间或坐标做猜测转换。
- 每个窗口输出维度、形状、缺测掩码、原始量、解包量、命中字节区间（半开区间）与传输统计。
