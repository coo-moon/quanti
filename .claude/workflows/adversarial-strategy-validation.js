export const meta = {
  name: 'adversarial-strategy-validation',
  description: '多agent对抗式验证任一量化策略论断:N红队各攻一失效面(默认证伪、实际查证)+ 裁判综合判过拟合/数据可信/前瞻可重复/护栏',
  whenToUse: '有了某策略的回测数字后,上真钱前想对抗式压测「它能稳定赚钱吗、是不是过拟合、数据可信吗、前瞻能外推吗」。通过 args 传 {claim, data, context, dimensions?}。默认内置 6 个 A 股量化失效面(数据/口径/退出/过拟合/前瞻/成本),不传 dimensions 即用默认。',
  phases: [
    { title: 'Refute', detail: 'N 红队并行:各攻一个失效面,默认证伪、实际查证(查DB/调数据API/读脚本/自写脚本重算)' },
    { title: 'Judge', detail: '首席裁判综合:过拟合判定 / 数据可信度 / 「稳定」重定义 / 现实前瞻区间 / 上真钱护栏' },
  ],
}

// ── 入参(通过 Workflow 的 args 传入)─────────────────────────────
//   args.claim      : string  被验证的论断(如「可转债全等权能相对稳定赚钱」)
//   args.data       : string  回测数字/收益表/已做过的验证(喂给每个红队做靶子)
//   args.context    : string  数据源/脚本路径/DB 路径(让红队能实际去查证)
//   args.dimensions : [{key,prompt}]  覆盖默认失效面(可选;不传用下面 6 个默认)
const a = (typeof args === 'object' && args) ? args : {}
const CLAIM = a.claim || '(未提供 claim — 请在 Workflow 的 args 里传 {claim, data, context};否则红队将报「输入不足」)'
const DATA = a.data || '(未提供 data — 无回测数字可审)'
const CONTEXT = a.context || '(未提供 context — 无数据源/脚本/DB 路径可查证,红队只能凭论断推理,可信度打折)'

// A 股量化策略的 6 个通用失效面。每条=让红队默认站在「证伪」立场去攻的具体角度。
const DEFAULT_DIMS = [
  { key: 'data-survivorship', prompt:
`数据完整性 / 幸存者偏差 / 时点(PIT)正确性。攻击:(1) 回测宇宙是否漏了退市/违约/被删标的 → 收益高估?数据抓取有无系统性失败且失败者恰是输家?(2) 已退出标的的最终价/结算是否正确捕获了崩盘尾,还是停在崩盘前?(3) 选池/特征是否用到当日或未来信息(前视)?(4) 若有独立官方指数/基准,自算净值与它的相关性够高吗(不高=数据可疑)?去实际查数据库/调数据 API/重算,给量化偏差方向与幅度。` },
  { key: 'price-cost-convention', prompt:
`价格与费用口径:复权 / 分红票息 / 佣金印花过户费 / 滑点。攻击:价格序列是原始价还是复权价?分红/票息是含在价里(可能假跳变高估)还是漏掉(低估)?费用模型是否漏项或用了不现实的低值?实际拉一两个标的的序列对照其分红/付息/复权记录判断,给「净成本口径是高估还是低估收益、幅度多少」。` },
  { key: 'exit-settlement', prompt:
`成交与退出建模的乐观性:T+1 / 涨跌停 / 停牌 / 强赎/到期/违约退出 / 次日开盘 vs 当日收盘。攻击:成交价假设是否可实现(如同 bar 收盘价决策+成交=前视)?退出标的按什么价结算,是否系统性乐观(如用崩盘前/冲高价)?停牌与涨跌停封单是否让某些成交实际不可能?抽查若干退出标的的路径,跑敏感性(如退出强制按保守价结算后收益变多少)。` },
  { key: 'overfit-stat', prompt:
`过拟合与统计稳健性(核心)。攻击:(1) 起点敏感性——把回测起点前移/后移几年,年化/夏普/最大回撤变多少?头条数字是否被 cherry-pick 的起点撑起?(2) 剔除表现最好的 1-3 个子期(年/季)后还剩多少?edge 是否≈100%来自少数不可预期的大年?(3) 逐期正负计数——「稳定」若定义为「每期正」是否成立?(4) 夏普的标准误(含肥尾修正)是否覆盖 0?若报了 DSR/PBO,其基准是否退化(sr0≈0 时 DSR=纯 PSR,不能当抗过拟合证据)?(5) 若策略有可调参数/变体,PBO 是否>0.5(选变体=过拟合)?自己写脚本用原始数据独立重算,给「曲线过拟合 / 叙事过拟合(regime依赖假稳定)/ 稳健」的明确裁决。` },
  { key: 'forward-regime', prompt:
`前瞻可重复性与 regime 依赖。攻击:历史收益里有多少是「一次性、不可重复」成分(如一次性利率re-rating、某段风格牛市、便宜的历史买点、更宽的历史宇宙)?当前估值/利率/宇宙宽度处于历史什么分位——若在极端端则均值回归会透支前瞻收益?有无结构性变化(制度改革、违约、退市新规)削弱了策略赖以成立的核心假设?联网核实当前市场状态 + 用历史数据算分位,给「前瞻现实预期」的下修判断。` },
  { key: 'cost-liquidity-capacity', prompt:
`成本、流动性、容量、可执行性。攻击:组合是否重仓了低流动性/小市值/薄成交标的?成本假设是否严重低估真实买卖价差+冲击?按不同资金量(如 100万 / 3000万 / 3亿 / 10亿)算真实冲击后净收益还剩多少,容量墙在哪?加流动性下限(如剔日成交<某额)后收益如何变化——若转负则「可执行核心」是负的。查数据库的成交量列 + 调实时行情测价差,给按资金量分层的净收益与容量上限。` },
]
const DIMS = (Array.isArray(a.dimensions) && a.dimensions.length) ? a.dimensions : DEFAULT_DIMS

const HEADER =
`【被验证的论断】${CLAIM}
【回测数字 / 收益表 / 已做过的验证(你的靶子)】
${DATA}
【数据源 / 脚本 / 数据库路径(去这里实际查证,不要只凭推理)】
${CONTEXT}`

const VERDICT = {
  type: 'object',
  properties: {
    dimension: { type: 'string' },
    attack: { type: 'string', description: '你试图证伪论断的具体角度' },
    what_you_checked: { type: 'string', description: '实际跑了什么(查DB/调API/读脚本/自写脚本重算)——不是推理' },
    finding: { type: 'string' },
    verdict: { type: 'string', enum: ['论断站得住', '有严重问题需推翻', '有真实瑕疵但不致命', '无法判定(输入/证据不足)'] },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'none'] },
    quantified_impact: { type: 'string', description: '若有偏差,量化对头条收益/夏普/回撤的影响方向与幅度' },
  },
  required: ['dimension', 'attack', 'what_you_checked', 'finding', 'verdict', 'severity'],
}

log(`对抗式验证:${DIMS.length} 个失效面 × 红队证伪 + 裁判综合`)

phase('Refute')
const verdicts = await parallel(DIMS.map(d => () =>
  agent(
    `${HEADER}\n\n对抗维度 = ${d.key}。默认立场:证伪(假设论断有问题,去找它)。\n${d.prompt}\n\n务必**实际动手查证**(查数据库 / 调数据 API / 读脚本 / 自写脚本用原始数据重算),严格区分「证据 vs 猜测」。若 context 不足以查证,如实报「无法判定」。`,
    { label: `refute:${d.key}`, phase: 'Refute', schema: VERDICT }
  )))

phase('Judge')
const clean = verdicts.filter(Boolean)
const judgment = await agent(
  `你是首席风控 / 量化裁判。${DIMS.length} 个红队各攻一个失效面验证以下论断,结果(JSON):

【论断】${CLAIM}

【已做过的验证/数字】
${DATA}

【红队裁决】
${JSON.stringify(clean, null, 2)}

给出最终裁决(中文,结论优先,≤700字):
1. **过拟合判定**:曲线过拟合 / 叙事过拟合(regime依赖假稳定)/ 稳健?区分「无可调参的 beta」与「有变体选择的 tilt」。DSR/PBO 若被降级要点明。
2. **数据可信度判定**:数据可信吗?最致命的数据瑕疵是什么,影响方向(高估/低估)与幅度?多个方向相反的偏差是否对冲?
3. **「能稳定赚钱」最终对抗后结论**:重新定义「稳定」(每期正? 还是 回撤可控+多期持有正?);给净成本、前瞻衰减、结构风险修正后的**现实预期区间**(可按资金量分层)与置信度。
4. **上真金白银必须加的护栏**(流动性下限/容量上限/分散度/信用/退出规则/仓位)。
诚实压过乐观——这是要拿真钱的判断。若红队普遍「无法判定(输入不足)」,明说验证未能完成、需要补什么输入。`,
  { label: 'judge:final', phase: 'Judge' })

return { claim: CLAIM, dimensions: DIMS.map(d => d.key), verdicts: clean, judgment }
