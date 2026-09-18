import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Bar, BarChart, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useMaterialEntityStats, useMaterialInsights, useMaterialStats, useMaterialTopics, useRegenerateInsights } from '../api/hooks'
import { Card } from '../components/Card'
import { Icon, type IconName } from '../components/Icon'
import type { AgeDistributionBucket, EntityStat, InsightItem, MaterialStats, TopicInsights } from '../api/types'

// 独立页面（而不是塞进素材库页面顶部）——用户明确反馈素材库页本来就挤（采集进度日志+
// 统计卡片+表格），实体热度不该跟这些争主内容区的视觉重量；定位是"视觉亮点，值得突出"，
// 所以给它一整个页面专门做真正的散列词云（字号+位置都随出现次数变化），而不是简单的
// flex-wrap字号缩放（那种此前做过一版，用户反馈"看起来很low"）。
//
// LLM共识挖掘区块（CATEGORIES）置顶展示，是这个页面的新主角内容——词云/频次榜降级为
// 下方的数据佐证细节，见与用户的grilling讨论（素材库应该被"充分利用"，不只是热度统计）。

const WORD_CLOUD_LIMIT = 40 // 只对Top40做词云布局——容器大小有限，全量摆上去会挤到无法辨认
const MIN_FONT_SIZE = 13
const MAX_FONT_SIZE = 58
// 非前三名的颜色循环——用已有的暖色系token，制造"多彩但不杂乱"的视觉效果
const ROTATING_COLORS = ['text-brand', 'text-teal', 'text-gold', 'text-ok', 'text-brand-dark']

const MIN_ENTRIES_FOR_INSIGHTS = 5 // 跟后端src/materials/insights.py::MIN_ENTRIES_FOR_INSIGHTS保持一致

const CATEGORIES: { key: keyof Pick<TopicInsights, 'must_visit' | 'food' | 'pitfalls' | 'tips' | 'other'>; label: string; icon: IconName }[] = [
  { key: 'must_visit', label: '热门必去', icon: 'mapPin' },
  { key: 'food', label: '饮食推荐', icon: 'compass' },
  { key: 'pitfalls', label: '避雷共识', icon: 'alertTriangle' },
  { key: 'tips', label: '实用贴士', icon: 'fileText' },
  { key: 'other', label: '其他发现', icon: 'layers' },
]

interface PlacedWord extends EntityStat {
  x: number
  y: number
  width: number
  height: number
  fontSize: number
  color: string
  bold: boolean
}

let measureCanvas: HTMLCanvasElement | null = null

function measureText(text: string, fontSize: number, bold: boolean): { width: number; height: number } {
  if (!measureCanvas) measureCanvas = document.createElement('canvas')
  const ctx = measureCanvas.getContext('2d')
  if (!ctx) return { width: text.length * fontSize * 0.55, height: fontSize * 1.2 }
  ctx.font = `${bold ? '700' : '600'} ${fontSize}px "PingFang SC", "Microsoft YaHei", sans-serif`
  return { width: ctx.measureText(text).width, height: fontSize * 1.2 }
}

function rectsOverlap(
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number },
): boolean {
  return !(a.x + a.width < b.x || b.x + b.width < a.x || a.y + a.height < b.y || b.y + b.height < a.y)
}

/** Archimedean螺旋布局：从中心向外螺旋试探位置，遇到第一个不与已放置的词重叠、
 * 且不超出容器边界的位置就放下——放不下的词（容器太小装不下Top40全部）直接跳过，
 * 宁可少展示几个也不让文字叠在一起看不清。出现次数用sqrt缩放到字号，避免最热门的
 * 一个词把其它词全部挤到视觉上可忽略的大小。 */
function layoutWordCloud(stats: EntityStat[], width: number, height: number): PlacedWord[] {
  if (width <= 0 || height <= 0) return []
  const top = stats.slice(0, WORD_CLOUD_LIMIT)
  const counts = top.map((s) => s.count)
  const maxCount = Math.max(...counts, 1)
  const minCount = Math.min(...counts, 0)
  const spread = maxCount - minCount || 1

  const placed: PlacedWord[] = []
  const cx = width / 2
  const cy = height / 2
  const maxRadius = Math.sqrt(cx * cx + cy * cy)

  top.forEach((s, i) => {
    const ratio = (s.count - minCount) / spread
    const fontSize = MIN_FONT_SIZE + Math.sqrt(ratio) * (MAX_FONT_SIZE - MIN_FONT_SIZE)
    const bold = i < 10
    const { width: w, height: h } = measureText(s.entity, fontSize, bold)
    const color = i === 0 ? 'text-gold' : i === 1 ? 'text-teal' : i === 2 ? 'text-brand-dark' : ROTATING_COLORS[i % ROTATING_COLORS.length]

    const padding = 5
    let found: { x: number; y: number } | null = null
    for (let angle = 0, radius = 0; radius < maxRadius; angle += 0.32, radius += 2.2) {
      const x = cx + radius * Math.cos(angle)
      const y = cy + radius * Math.sin(angle)
      const rect = { x: x - w / 2 - padding, y: y - h / 2 - padding, width: w + padding * 2, height: h + padding * 2 }
      if (rect.x < 0 || rect.y < 0 || rect.x + rect.width > width || rect.y + rect.height > height) continue
      if (!placed.some((p) => rectsOverlap(rect, { x: p.x - p.width / 2 - padding, y: p.y - p.height / 2 - padding, width: p.width + padding * 2, height: p.height + padding * 2 }))) {
        found = { x, y }
        break
      }
    }
    if (found) {
      placed.push({ ...s, x: found.x, y: found.y, width: w, height: h, fontSize, color, bold })
    }
  })

  return placed
}

function InsightCategoryCard({
  label,
  icon,
  items,
  expandedKey,
  onToggle,
}: {
  label: string
  icon: IconName
  items: InsightItem[]
  expandedKey: string | null
  onToggle: (key: string) => void
}) {
  return (
    <Card className="p-4">
      <div className="flex items-center gap-2 mb-3">
        <div className="w-6.5 h-6.5 rounded-lg bg-brand-soft text-brand-dark flex items-center justify-center shrink-0">
          <Icon name={icon} className="w-3.5 h-3.5" />
        </div>
        <span className="text-xs font-bold text-ink-3 uppercase tracking-wide">{label}</span>
      </div>
      {items.length === 0 && <p className="text-sm text-ink-3">暂无发现。</p>}
      <div className="scroll-boundary-y flex flex-col gap-2.5 max-h-[520px] overflow-y-auto pr-1">
        {items.map((item, i) => {
          const itemKey = `${label}-${i}`
          const isExpanded = expandedKey === itemKey
          return (
            <div key={itemKey} className="border border-border rounded-lg p-2.5">
              <div className="flex items-start justify-between gap-2">
                <p className="text-[13px] flex-1">{item.summary}</p>
                {item.confidence === 'consensus' ? (
                  <span className="text-[10.5px] font-semibold text-ok bg-ok/10 rounded-full px-2 py-0.5 shrink-0">多方印证</span>
                ) : (
                  <span className="text-[10.5px] font-semibold text-ink-3 bg-bg-inset rounded-full px-2 py-0.5 shrink-0">仅单一来源，谨慎参考</span>
                )}
              </div>
              <button
                className="text-[11px] text-brand mt-1.5 hover:underline"
                onClick={() => onToggle(itemKey)}
              >
                {isExpanded ? '收起原文依据 ▲' : `查看原文依据（${item.sources.length}条）▼`}
              </button>
              {isExpanded && (
                <div className="mt-2 flex flex-col gap-2">
                  {item.sources.map((src) => (
                    <div key={src.chunk_id || src.doc_id} className="bg-bg-inset rounded-md p-2 text-[11.5px]">
                      <div className="flex items-center justify-between mb-1 text-ink-3">
                        <span className="font-semibold">{src.author || '未知作者'}</span>
                        <span>{src.doc_id}</span>
                      </div>
                      <p className="text-ink-2">{src.excerpt || '（该素材没有可展示的原文片段）'}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// 素材库统计画像（2026-08-22 grilling讨论新增）：平台分布+字数/内容年龄/核心占比/
// 作者多样性/拒绝理由。用recharts画横向条形图（用户明确要求"好看"，纯Tailwind div
// 条形图效果上限取决于打磨程度，讨论后决定引入图表库），配色用index.css里@theme
// 注册的品牌色hex值（SVG fill吃不了Tailwind类名，只能用实际颜色值）。
// ---------------------------------------------------------------------------

const STAT_BAR_PALETTE = ['#d97757', '#3f8a82', '#c8963e', '#3f8a5c', '#b85c3f', '#9c9284']

interface BarDatum {
  label: string
  value: number
}

function StatBarChart({
  data,
  valueFormatter = (v: number) => String(v),
}: {
  data: BarDatum[]
  valueFormatter?: (v: number) => string
}) {
  if (data.length === 0) return <p className="text-sm text-ink-3">暂无数据。</p>
  const height = Math.max(64, data.length * 44)
  // Y轴标签宽度按最长label动态算，不能写死——固定宽度装不下"B站（语音转写）"这类长标签，
  // 会从左边被recharts裁掉（真实踩过的坑：显示成"站（语音转写）"）。按字符数粗略估算
  // （每字13px，覆盖中英文混排场景），不追求像素级精确，只保证装得下。
  const maxLabelLen = Math.max(...data.map((d) => d.label.length), 1)
  const yAxisWidth = Math.min(150, Math.max(60, maxLabelLen * 13 + 12))
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} layout="vertical" margin={{ top: 2, right: 52, left: 2, bottom: 2 }}>
        <XAxis type="number" hide domain={[0, (max: number) => max * 1.15]} />
        <YAxis
          type="category"
          dataKey="label"
          width={yAxisWidth}
          tickLine={false}
          axisLine={false}
          tick={{ fill: '#6b6055', fontSize: 12, fontWeight: 600 }}
        />
        <Tooltip
          cursor={{ fill: '#f3efe7' }}
          formatter={(value) => [valueFormatter(Number(Array.isArray(value) ? value[0] : (value ?? 0))), '']}
          labelStyle={{ fontWeight: 600, color: '#262019' }}
          contentStyle={{ borderRadius: 8, border: '1px solid #eae3d6', fontSize: 12 }}
        />
        <Bar dataKey="value" radius={[0, 6, 6, 0]} barSize={20} isAnimationActive={false}>
          {data.map((d, i) => (
            <Cell key={d.label} fill={STAT_BAR_PALETTE[i % STAT_BAR_PALETTE.length]} />
          ))}
          <LabelList
            dataKey="value"
            position="right"
            formatter={(v: unknown) => valueFormatter(Number(v ?? 0))}
            style={{ fill: '#262019', fontSize: 12, fontWeight: 700 }}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

function StatHeader({ icon, label }: { icon: IconName; label: string }) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <div className="w-6.5 h-6.5 rounded-lg bg-brand-soft text-brand-dark flex items-center justify-center shrink-0">
        <Icon name={icon} className="w-3.5 h-3.5" />
      </div>
      <span className="text-xs font-bold text-ink-3 uppercase tracking-wide">{label}</span>
    </div>
  )
}

// platform字段存的是ingest管线内部值（跟chunker分派key一致，见data/material_library/
// README.md），不是给人看的——展示层统一转中文，_asr后缀转成"（语音转写）"标注
// （携带真实信息：这份内容是从视频语音转写来的，跟图文原生内容的可信度/风格不同，
// 不是纯装饰）。未收录的新平台值原样兜底展示，不会因为漏更新这张表就显示空白。
const PLATFORM_LABELS: Record<string, string> = {
  bilibili: 'B站',
  bilibili_asr: 'B站（语音转写）',
  bilibili_comment: 'B站评论',
  douyin: '抖音',
  douyin_asr: '抖音（语音转写）',
  xiaohongshu: '小红书',
}

function platformLabel(platform: string): string {
  return PLATFORM_LABELS[platform] ?? platform
}

function PlatformBreakdownCard({ stats }: { stats: MaterialStats }) {
  const data: BarDatum[] = stats.platform_breakdown.map((p) => ({ label: platformLabel(p.platform), value: p.word_count }))
  return (
    <Card className="p-4">
      <StatHeader icon="database" label={`平台分布 · 共${stats.total_word_count.toLocaleString()}字`} />
      <StatBarChart data={data} valueFormatter={(v) => `${v.toLocaleString()}字`} />
      <div className="flex flex-col gap-1.5 mt-3">
        {stats.platform_breakdown.map((p) => (
          <div key={p.platform} className="text-[11.5px] border-t border-border pt-1.5 first:border-t-0 first:pt-0">
            <div className="flex items-center justify-between text-ink-2">
              <span className="font-semibold">{platformLabel(p.platform)}</span>
              <span className="text-ink-3">{p.count}条 · {p.percentage}%</span>
            </div>
            {p.longest && p.longest.word_count > 0 && (
              <p className="text-ink-3 mt-0.5 truncate">
                ⭐ 最长一篇《{p.longest.title || '未命名'}》{p.longest.word_count.toLocaleString()}字
              </p>
            )}
          </div>
        ))}
      </div>
    </Card>
  )
}

function AgeDistributionCard({ stats }: { stats: MaterialStats }) {
  const data: BarDatum[] = stats.age_distribution.map((b) => ({ label: b.bucket, value: b.count }))
  const unknownBucket = stats.age_distribution.find((b: AgeDistributionBucket) => b.bucket === '未知')
  const unknownPlatforms = unknownBucket ? Object.entries(unknownBucket.by_platform).sort((a, b) => b[1] - a[1]) : []
  return (
    <Card className="p-4">
      <StatHeader icon="clock" label="内容年龄分布" />
      <p className="text-[11px] text-ink-3 -mt-2 mb-3">按发布日期计算，只是"内容可能过时"的弱代理信号，不代表内容一定已过时。</p>
      <StatBarChart data={data} valueFormatter={(v) => `${v}条`} />
      {unknownBucket && unknownBucket.count > 0 && (
        <p className="text-[11.5px] text-ink-3 mt-3 border-t border-border pt-2">
          "未知"按平台拆分：{unknownPlatforms.map(([p, c]) => `${platformLabel(p)} ${c}条`).join(' · ')}
        </p>
      )}
    </Card>
  )
}

function CoreRatioCard({ stats }: { stats: MaterialStats }) {
  const data: BarDatum[] = [
    { label: '核心素材', value: stats.core_ratio.core_count },
    { label: '非核心', value: stats.core_ratio.non_core_count },
  ]
  return (
    <Card className="p-4">
      <StatHeader icon="check" label="核心素材占比" />
      <StatBarChart data={data} valueFormatter={(v) => `${v}条`} />
    </Card>
  )
}

function AuthorRankingCard({ stats }: { stats: MaterialStats }) {
  const data: BarDatum[] = stats.author_ranking.top.map((a) => ({ label: a.author, value: a.count }))
  return (
    <Card className="p-4">
      <StatHeader icon="layers" label="作者多样性" />
      <StatBarChart data={data} valueFormatter={(v) => `${v}条`} />
      {stats.author_ranking.other_author_count > 0 && (
        <p className="text-[11.5px] text-ink-3 mt-3 border-t border-border pt-2">
          其余 {stats.author_ranking.other_author_count} 位作者共贡献 {stats.author_ranking.other_entry_count} 条
        </p>
      )}
    </Card>
  )
}

function RejectionStatsCard({ stats }: { stats: MaterialStats }) {
  const [expanded, setExpanded] = useState(false)
  const r = stats.rejection_stats
  const total = r.too_short_count + r.irrelevant_count
  return (
    <Card className="p-4">
      <StatHeader icon="alertTriangle" label="拒绝理由" />
      {total === 0 ? (
        <p className="text-sm text-ink-3">入库过程中没有素材被拒绝。</p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-lg bg-bg-inset p-3">
              <p className="text-[11px] text-ink-3 mb-1">内容过短</p>
              <p className="text-xl font-bold text-ink">{r.too_short_count}</p>
            </div>
            <div className="rounded-lg bg-bg-inset p-3">
              <p className="text-[11px] text-ink-3 mb-1">内容不相关</p>
              <p className="text-xl font-bold text-ink">{r.irrelevant_count}</p>
            </div>
          </div>
          {r.irrelevant_reasons.length > 0 && (
            <>
              <button className="text-[11px] text-brand mt-3 hover:underline" onClick={() => setExpanded((v) => !v)}>
                {expanded ? '收起理由 ▲' : `查看不相关理由（${r.irrelevant_reasons.length}条）▼`}
              </button>
              {expanded && (
                <div className="scroll-boundary-y mt-2 flex flex-col gap-1.5 max-h-64 overflow-y-auto">
                  {r.irrelevant_reasons.map((item) => (
                    <div key={item.source_file} className="bg-bg-inset rounded-md p-2 text-[11.5px]">
                      <p className="font-semibold text-ink-2 truncate">{item.title || item.source_file}</p>
                      <p className="text-ink-3 mt-0.5">{item.reason}</p>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </>
      )}
    </Card>
  )
}

function MaterialStatsSection({ stats, isLoading }: { stats: MaterialStats | undefined; isLoading: boolean }) {
  if (isLoading) return <p className="text-ink-3 text-sm">加载中…</p>
  if (!stats || stats.entry_count === 0) {
    return (
      <Card className="p-5">
        <p className="text-sm text-ink-3">暂无素材，无法生成统计画像。</p>
      </Card>
    )
  }
  return (
    <div className="flex flex-col gap-4">
      <PlatformBreakdownCard stats={stats} />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <AgeDistributionCard stats={stats} />
        <CoreRatioCard stats={stats} />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <AuthorRankingCard stats={stats} />
        <RejectionStatsCard stats={stats} />
      </div>
    </div>
  )
}

export function MaterialInsightsPage() {
  const { data: topics, isLoading: topicsLoading } = useMaterialTopics()
  const [searchParams] = useSearchParams()
  const [activeTopic, setActiveTopic] = useState(searchParams.get('topic') ?? '')
  const [keyword, setKeyword] = useState('')
  const containerRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ width: 0, height: 420 })
  const navigate = useNavigate()

  useEffect(() => {
    if (topics && topics.length > 0 && !topics.some((topic) => topic.topic === activeTopic)) {
      setActiveTopic(topics[0].topic)
    }
  }, [activeTopic, topics])

  const { data: entityStats, isLoading: statsLoading } = useMaterialEntityStats(activeTopic)
  const { data: materialStats, isLoading: materialStatsLoading } = useMaterialStats(activeTopic)
  const { data: insights, isLoading: insightsLoading } = useMaterialInsights(activeTopic)
  const regenerateMutation = useRegenerateInsights(activeTopic)
  const [expandedKey, setExpandedKey] = useState<string | null>(null)
  const activeTopicSummary = (topics ?? []).find((t) => t.topic === activeTopic)

  useLayoutEffect(() => {
    const el = containerRef.current
    if (!el) return
    const update = () => setSize({ width: el.clientWidth, height: Math.max(340, Math.min(520, Math.round(el.clientWidth * 0.45))) })
    update()
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const placedWords = useMemo(
    () => (entityStats && entityStats.length > 0 ? layoutWordCloud(entityStats, size.width, size.height) : []),
    [entityStats, size.width, size.height],
  )

  const stats = entityStats ?? []
  const maxCount = Math.max(...stats.map((s) => s.count), 1)
  const filteredList = stats.filter((s) => s.entity.toLowerCase().includes(keyword.trim().toLowerCase()))

  const goToMaterial = (entity: string) => {
    navigate(`/materials?topic=${encodeURIComponent(activeTopic)}&search=${encodeURIComponent(entity)}`)
  }

  if (topicsLoading) return <p className="text-ink-3">加载中…</p>

  return (
    <div>
      <Link
        to={activeTopic ? `/materials?topic=${encodeURIComponent(activeTopic)}` : '/materials'}
        className="mb-3 inline-flex items-center gap-1 text-xs font-medium text-ink-3 transition-colors hover:text-brand-dark"
      >
        <Icon name="chevronRight" className="h-3.5 w-3.5 rotate-180" />
        返回素材库
      </Link>
      <div className="flex items-center justify-between mb-4.5 flex-wrap gap-3">
        <div>
          <h2 className="text-lg font-bold">素材洞察</h2>
          <p className="text-xs text-ink-3 mt-0.5">
            素材库的统计画像与LLM共识挖掘——生成一份攻略时用了多少素材、分别来自哪些平台，都能在这里追溯。
          </p>
        </div>
        <div className="flex gap-2">
          {(topics ?? []).map((t) => (
            <button
              key={t.topic}
              onClick={() => setActiveTopic(t.topic)}
              className={`flex items-center gap-2 px-3.5 py-2 rounded-lg border text-[13px] font-semibold ${
                activeTopic === t.topic ? 'border-brand bg-brand-soft text-brand-dark' : 'border-border bg-surface'
              }`}
            >
              {t.topic} <span className="text-ink-3 font-normal">· {t.entry_count}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="mb-5">
        <h3 className="text-sm font-bold mb-3">素材库统计画像</h3>
        <MaterialStatsSection stats={materialStats} isLoading={materialStatsLoading} />
      </div>

      <div className="mb-5">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <div>
            <h3 className="text-sm font-bold">大模型素材洞察</h3>
            {insights && (
              <p className="text-[11px] text-ink-3 mt-0.5">
                基于 {insights.based_on_entry_count} 条素材 · {new Date(insights.generated_at).toLocaleString('zh-CN')} 生成，
                若素材库之后有增补，这份洞察可能已经滞后
              </p>
            )}
          </div>
          <button
            className="text-[13px] font-semibold px-3.5 py-2 rounded-lg bg-brand text-white disabled:opacity-50"
            disabled={regenerateMutation.isPending}
            onClick={() => regenerateMutation.mutate()}
          >
            {regenerateMutation.isPending ? '生成中…' : '重新生成洞察'}
          </button>
        </div>

        {regenerateMutation.isError && (
          <p className="text-xs text-danger mb-2">
            {regenerateMutation.error instanceof Error ? regenerateMutation.error.message : '生成失败'}
          </p>
        )}

        {insightsLoading && <p className="text-ink-3 text-sm">加载中…</p>}

        {!insightsLoading && !insights && activeTopicSummary && activeTopicSummary.entry_count < MIN_ENTRIES_FOR_INSIGHTS && (
          <Card className="p-5">
            <p className="text-sm text-ink-3">
              该主题目前只有 {activeTopicSummary.entry_count} 条素材，低于生成洞察所需的最少 {MIN_ENTRIES_FOR_INSIGHTS} 条，暂无法生成有意义的共识。建议先补充采集素材。
            </p>
          </Card>
        )}

        {!insightsLoading && !insights && activeTopicSummary && activeTopicSummary.entry_count >= MIN_ENTRIES_FOR_INSIGHTS && (
          <Card className="p-5">
            <p className="text-sm text-ink-3">还没有生成过洞察，点击右上角"重新生成洞察"试试。</p>
          </Card>
        )}

        {!insightsLoading && insights && (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 items-start">
            {CATEGORIES.map((cat) => (
              <InsightCategoryCard
                key={cat.key}
                label={cat.label}
                icon={cat.icon}
                items={insights[cat.key]}
                expandedKey={expandedKey}
                onToggle={(key) => setExpandedKey((prev) => (prev === key ? null : key))}
              />
            ))}
          </div>
        )}
      </div>

      <Card className="p-5 mb-5 relative overflow-hidden bg-linear-to-br from-brand-soft/40 via-surface to-teal-soft/30">
        <Icon
          name="compass"
          className="w-44 h-44 absolute -right-8 -top-8 text-brand opacity-[0.06] pointer-events-none"
        />
        <div className="flex items-center gap-2 mb-3 relative">
          <div className="w-7 h-7 rounded-lg bg-brand-soft text-brand-dark flex items-center justify-center shrink-0">
            <Icon name="mapPin" className="w-3.75 h-3.75" />
          </div>
          <span className="text-xs font-bold text-ink-3 uppercase tracking-wide">热门实体词云</span>
        </div>
        <div ref={containerRef} className="relative w-full" style={{ height: size.height }}>
          {statsLoading && <p className="text-ink-3 text-sm">加载中…</p>}
          {!statsLoading && stats.length === 0 && <p className="text-ink-3 text-sm">暂无实体数据。</p>}
          {placedWords.map((w) => (
            <button
              key={w.entity}
              className={`absolute leading-none whitespace-nowrap hover:underline hover:opacity-75 transition-opacity ${w.color} ${w.bold ? 'font-bold' : 'font-semibold'}`}
              style={{ left: w.x, top: w.y, fontSize: `${w.fontSize}px`, transform: 'translate(-50%, -50%)' }}
              title={`出现在 ${w.count} 条素材中，点击查看相关素材`}
              onClick={() => goToMaterial(w.entity)}
            >
              {w.entity}
            </button>
          ))}
        </div>
      </Card>

      <Card className="p-5">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <h3 className="text-xs font-bold text-ink-3 uppercase tracking-wide">完整频次榜（共 {stats.length} 个实体）</h3>
          <input
            className="border border-border rounded-lg px-3 py-1.5 text-sm w-56"
            placeholder="筛选实体…"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
        </div>
        <p className="text-xs text-ink-3 mb-3">
          按实体字符串精确匹配统计，同一地点的不同写法（如"武侯祠"/"武侯祠博物馆"）会分开计数，不做别名合并。
        </p>
        <div className="scroll-boundary-y flex flex-col gap-2.5 max-h-[420px] overflow-y-auto">
          {filteredList.length === 0 && <p className="text-sm text-ink-3">没有匹配的实体。</p>}
          {filteredList.map((s) => {
            const rank = stats.indexOf(s)
            const pct = Math.max(4, Math.round((s.count / maxCount) * 100))
            const badgeColor =
              rank === 0 ? 'bg-gold text-white' : rank === 1 ? 'bg-teal text-white' : rank === 2 ? 'bg-brand-dark text-white' : 'bg-bg-inset text-ink-3'
            const barColor = rank === 0 ? 'bg-gold' : rank === 1 ? 'bg-teal' : rank === 2 ? 'bg-brand-dark' : 'bg-brand'
            return (
              <button
                key={s.entity}
                className="flex items-center gap-2.5 text-left hover:bg-bg-inset rounded-lg px-1 py-1 -mx-1"
                onClick={() => goToMaterial(s.entity)}
              >
                <span className={`w-5.5 h-5.5 rounded-full flex items-center justify-center text-[10.5px] font-bold shrink-0 ${badgeColor}`}>
                  {rank + 1}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[13px] font-semibold truncate">{s.entity}</span>
                    <span className="text-[11px] text-ink-3 shrink-0 ml-2">出现在 {s.count} 条素材中</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-bg-inset overflow-hidden">
                    <div className={`h-full rounded-full ${barColor}`} style={{ width: `${pct}%` }} />
                  </div>
                </div>
              </button>
            )
          })}
        </div>
      </Card>
    </div>
  )
}
