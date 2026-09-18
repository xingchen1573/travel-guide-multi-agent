import { useState, useEffect } from 'react'
import type { ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import {
  useGeneratePoster,
  useGenerateSite,
  useProject,
  usePosterHistory,
  usePosterHistoryPrompt,
  usePosterAssets,
  usePosterPrompt,
  useSiteHistory,
} from '../api/hooks'
import { Card } from '../components/Card'
import { Icon } from '../components/Icon'
import { ProjectTabs } from '../components/ProjectTabs'

/** 归档时间戳格式是_archive_previous_output用的"%Y%m%dT%H%M%S%f"，这里只做展示层的
 * 可读性转换，不是真正的日期解析（后端返回的字符串本身就是排序键，不需要往返转换）。*/
function formatHistoryTimestamp(timestamp: string): string {
  const match = timestamp.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/)
  if (!match) return timestamp
  const [, y, mo, d, h, mi, s] = match
  return `${y}-${mo}-${d} ${h}:${mi}:${s}`
}

function GenerationLog({ log }: { log: string[] }) {
  if (log.length === 0) return null
  return (
    <div className="rounded-lg border border-border bg-bg-inset p-3 mt-3">
      <p className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-1.5">生成日志</p>
      <div className="scroll-boundary-y flex flex-col gap-1 max-h-32 overflow-y-auto">
        {log.map((line, i) => (
          <p key={i} className="text-xs text-ink-2 font-mono">
            {line}
          </p>
        ))}
      </div>
    </div>
  )
}

function HistoryList({
  timestamps,
  selected,
  onSelect,
}: {
  timestamps: string[]
  selected: string | null
  onSelect: (timestamp: string | null) => void
}) {
  if (timestamps.length === 0) return null
  return (
    <div className="mt-3">
      <p className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-1.5">
        历史版本（共 {timestamps.length} 个）
      </p>
      <div className="flex gap-1.5 flex-wrap">
        <button
          className={`text-xs font-semibold px-2.5 py-1 rounded-full border ${
            selected === null ? 'border-brand bg-brand-soft text-brand-dark' : 'border-border text-ink-2'
          }`}
          onClick={() => onSelect(null)}
        >
          当前版本
        </button>
        {timestamps.map((ts) => (
          <button
            key={ts}
            className={`text-xs font-semibold px-2.5 py-1 rounded-full border ${
              selected === ts ? 'border-brand bg-brand-soft text-brand-dark' : 'border-border text-ink-2'
            }`}
            onClick={() => onSelect(ts)}
          >
            {formatHistoryTimestamp(ts)}
          </button>
        ))}
      </div>
    </div>
  )
}

function PromptDisclosure({ prompt }: { prompt: string | undefined }) {
  const [open, setOpen] = useState(false)
  if (!prompt) return null
  return (
    <div className="mt-3">
      <button
        className="text-xs font-semibold text-brand-dark hover:underline"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? '收起' : '查看'}实际喂给生图模型的prompt（审计存档）
      </button>
      {open && (
        <pre className="scroll-boundary-y text-xs text-ink-2 leading-relaxed whitespace-pre-wrap font-mono border border-border rounded-lg p-3 mt-2 max-h-60 overflow-y-auto">
          {prompt}
        </pre>
      )}
    </div>
  )
}

// 深色遮罩灯箱：iframe 全屏预览 + 海报大图预览共用。比 PosterConfirmDialog 的
// bg-black/30 更深（媒体查看器惯例），ESC/点遮罩可关闭。z-50 跟确认弹窗同级，但
// 两者业务路径互斥不会同时出现。
function Lightbox({
  title,
  onClose,
  children,
  headerActions,
  wide,
  bodyClassName,
}: {
  title: string
  onClose: () => void
  children: ReactNode
  headerActions?: ReactNode
  wide?: boolean
  bodyClassName?: string
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className={`bg-surface rounded-xl shadow-lg w-full flex flex-col overflow-hidden ${
          wide ? 'max-w-5xl h-[85vh]' : 'max-w-2xl max-h-[90vh]'
        }`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-border shrink-0">
          <span className="text-[13px] font-semibold text-ink-2">{title}</span>
          <div className="flex items-center gap-3">
            {headerActions}
            <button
              type="button"
              onClick={onClose}
              className="text-ink-3 hover:text-ink p-1 -m-1 rounded"
              aria-label="关闭"
            >
              <Icon name="x" className="w-4 h-4" />
            </button>
          </div>
        </div>
        <div className={`scroll-boundary-y ${bodyClassName ?? 'flex-1 overflow-auto p-4 bg-bg-inset flex items-center justify-center'}`}>
          {children}
        </div>
      </div>
    </div>
  )
}

// iframe 的"浏览器窗口"画框：中性灰圆点（不用红黄绿——项目色板没有那三色，
// 硬套会显得像抄 macOS chrome）+ 右侧"全屏查看"入口。iframe 本身去裸边框改
// shadow-sm，嵌在 bg-bg-inset 衬底里像一块屏幕，而不是贴边框的裸元素。
function BrowserFrame({ src, onExpand }: { src: string; onExpand: () => void }) {
  return (
    <div className="mt-3 rounded-xl bg-bg-inset border border-border p-2.5">
      <div className="flex items-center justify-between px-1 pb-2">
        <div className="flex items-center gap-1.5" aria-hidden="true">
          <span className="w-2.5 h-2.5 rounded-full bg-border-strong" />
          <span className="w-2.5 h-2.5 rounded-full bg-border-strong" />
          <span className="w-2.5 h-2.5 rounded-full bg-border-strong" />
        </div>
        <button
          type="button"
          onClick={onExpand}
          className="text-[12px] font-semibold text-ink-2 hover:text-brand-dark flex items-center gap-1"
        >
          <Icon name="externalLink" className="w-3.5 h-3.5" />
          全屏查看
        </button>
      </div>
      <iframe
        key={src}
        title="静态展示网站预览"
        src={src}
        className="w-full h-[520px] rounded-lg border-0 bg-white shadow-sm"
      />
    </div>
  )
}

export function PresentationPage() {
  const { projectId = '' } = useParams()
  const project = useProject(projectId)
  const site = useGenerateSite(projectId)
  const poster = useGeneratePoster(projectId, { scope: 'overall' })
  const [posterMode, setPosterMode] = useState<'overall' | 'day'>('overall')
  const [selectedDay, setSelectedDay] = useState(1)
  const dailyPoster = useGeneratePoster(projectId, { scope: 'day', day: selectedDay })
  const posterAssets = usePosterAssets(projectId)
  const [posterConfirmOpen, setPosterConfirmOpen] = useState(false)
  const [siteLightboxOpen, setSiteLightboxOpen] = useState(false)
  const [posterLightboxOpen, setPosterLightboxOpen] = useState(false)
  const tripDayCount = project.data?.days && project.data.days > 0 ? project.data.days : null
  const overallPosterLabel = tripDayCount ? `${tripDayCount}天整体` : '整体海报'
  const overallPosterTitle = tripDayCount ? `${tripDayCount}天整体海报` : '整体海报'
  const selectableDays = Array.from({ length: tripDayCount ?? 1 }, (_, index) => index + 1)

  useEffect(() => {
    if (tripDayCount && selectedDay > tripDayCount) {
      setSelectedDay(tripDayCount)
      setSelectedPosterVersion(null)
    }
  }, [selectedDay, tripDayCount])

  const siteRunning = ['queued', 'running'].includes(site.status?.status ?? '') || site.isTriggering
  const activePoster = posterMode === 'overall' ? poster : dailyPoster
  const posterRunning = ['queued', 'running'].includes(activePoster.status?.status ?? '') || activePoster.isTriggering
  const posterScope = posterMode === 'overall' ? 'overall' : 'day'
  const posterOptions = { scope: posterScope as 'overall' | 'day', day: posterMode === 'day' ? selectedDay : undefined }

  const siteHistory = useSiteHistory(projectId)
  const [selectedSiteVersion, setSelectedSiteVersion] = useState<string | null>(null)
  const siteHasCurrent = site.status?.status === 'succeeded' || (siteHistory.data?.length ?? 0) > 0
  const siteSrc = selectedSiteVersion
    ? `/api/projects/${projectId}/site/history/${encodeURIComponent(selectedSiteVersion)}`
    : `/api/projects/${projectId}/site`

  const posterHistory = usePosterHistory(projectId)
  const dailyPosterHistory = usePosterHistory(projectId, { scope: 'day', day: selectedDay })
  const [selectedPosterVersion, setSelectedPosterVersion] = useState<string | null>(null)
  const activePosterHistory = posterMode === 'overall' ? posterHistory : dailyPosterHistory
  const currentPosterAsset = posterMode === 'overall'
    ? posterAssets.data?.overall
    : posterAssets.data?.days[String(selectedDay)]
  const posterHasCurrent = activePoster.status?.status === 'succeeded' || currentPosterAsset?.available === true || (activePosterHistory.data?.length ?? 0) > 0
  const posterSrc = selectedPosterVersion
    ? posterMode === 'overall'
      ? `/api/projects/${projectId}/poster/history/${encodeURIComponent(selectedPosterVersion)}`
      : `/api/projects/${projectId}/poster/day/${selectedDay}/history/${encodeURIComponent(selectedPosterVersion)}`
    : posterMode === 'overall'
      ? `/api/projects/${projectId}/poster`
      : `/api/projects/${projectId}/poster/day/${selectedDay}`
  const currentPrompt = usePosterPrompt(
    projectId,
    selectedPosterVersion === null && posterHasCurrent && currentPosterAsset?.source === 'generated',
    posterOptions,
  )
  const historyPrompt = usePosterHistoryPrompt(projectId, selectedPosterVersion, posterOptions)

  return (
    <div>
      <ProjectTabs projectId={projectId} />

      <Card className="p-5 mb-5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2.5">
          静态展示网站（森林+树木）
        </div>
        <p className="text-sm text-ink-2 mb-3">
          纯程序拼接生成，不调用LLM，读final_guide.md + outputs/poi_details/*.json。
        </p>
        <div className="flex items-center gap-3">
          <button
            className="px-4 py-2 rounded-lg text-[13px] font-semibold bg-brand text-white disabled:opacity-50"
            disabled={siteRunning}
            onClick={() => {
              setSelectedSiteVersion(null)
              site.trigger()
            }}
          >
            {siteRunning ? '生成中…' : '生成网站'}
          </button>
          {siteHasCurrent && (
            <a
              className="text-[13px] font-semibold text-brand-dark underline"
              href={siteSrc}
              target="_blank"
              rel="noreferrer"
            >
              在新标签页打开
            </a>
          )}
        </div>
        {site.status?.status === 'failed' && (
          <pre className="text-xs text-danger mt-2 whitespace-pre-wrap">{site.status.error}</pre>
        )}
        <GenerationLog log={site.status?.log ?? []} />
        <HistoryList
          timestamps={siteHistory.data ?? []}
          selected={selectedSiteVersion}
          onSelect={setSelectedSiteVersion}
        />
        {siteHasCurrent && (
          <BrowserFrame src={siteSrc} onExpand={() => setSiteLightboxOpen(true)} />
        )}
      </Card>

      <Card className="p-5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2.5">系列海报</div>
        <div className="flex items-center gap-3 mb-3 flex-wrap">
          <div className="inline-flex rounded-lg border border-border p-1" role="tablist" aria-label="海报类型">
            <button
              className={`px-3 py-1.5 rounded-md text-[13px] font-semibold ${posterMode === 'overall' ? 'bg-brand text-white' : 'text-ink-2 hover:bg-bg-inset'}`}
              role="tab"
              aria-selected={posterMode === 'overall'}
              onClick={() => {
                setPosterMode('overall')
                setSelectedPosterVersion(null)
              }}
            >
              {overallPosterLabel}
            </button>
            <button
              className={`px-3 py-1.5 rounded-md text-[13px] font-semibold ${posterMode === 'day' ? 'bg-brand text-white' : 'text-ink-2 hover:bg-bg-inset'}`}
              role="tab"
              aria-selected={posterMode === 'day'}
              onClick={() => {
                setPosterMode('day')
                setSelectedPosterVersion(null)
              }}
            >
              单日海报
            </button>
          </div>
          {posterMode === 'day' && (
            <select
              aria-label="选择天数"
              className="border border-border rounded-md px-2.5 py-1.5 bg-bg text-ink-1 text-sm"
              value={selectedDay}
              onChange={(event) => {
                setSelectedDay(Number(event.target.value))
                setSelectedPosterVersion(null)
              }}
            >
              {selectableDays.map((day) => (
                <option key={day} value={day}>Day{day}</option>
              ))}
            </select>
          )}
        </div>
        <p className="text-sm text-ink-2 mb-3">
          {posterMode === 'overall'
            ? `生成一张包含完整路线与整体节奏的${tripDayCount ? `${tripDayCount}天` : ''}总览海报。`
            : `生成 Day${selectedDay} 的单日攻略海报，使用该天的完整时间轴与信息模块。`}
          <br />调用生图API按次收费（gpt-image-2），请确认后再触发。
        </p>
        <div className="flex items-center gap-3">
          <button
            className="px-4 py-2 rounded-lg text-[13px] font-semibold bg-brand text-white disabled:opacity-50"
            disabled={posterRunning}
            onClick={() => setPosterConfirmOpen(true)}
          >
            {posterRunning ? '生成中…' : posterMode === 'overall' ? '生成海报' : `生成 Day${selectedDay} 海报`}
          </button>
          {posterHasCurrent && (
            <a
              className="text-[13px] font-semibold text-brand-dark underline"
              href={posterSrc}
              target="_blank"
              rel="noreferrer"
            >
              在新标签页打开
            </a>
          )}
          {currentPosterAsset?.source === 'imported' && (
            <span className="text-xs text-ink-3">已导入项目海报资产</span>
          )}
        </div>
        {activePoster.status?.status === 'failed' && (
          <pre className="text-xs text-danger mt-2 whitespace-pre-wrap">{activePoster.status.error}</pre>
        )}
        <GenerationLog log={activePoster.status?.log ?? []} />
        <HistoryList
          timestamps={activePosterHistory.data ?? []}
          selected={selectedPosterVersion}
          onSelect={setSelectedPosterVersion}
        />
        {posterHasCurrent && (
          <div className="mt-3 rounded-xl bg-bg-inset border border-border p-5 sm:p-8 flex justify-center">
            <button
              type="button"
              onClick={() => setPosterLightboxOpen(true)}
              className="relative group rounded-lg overflow-hidden focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
            >
              <img
                key={posterSrc}
                src={posterSrc}
                alt="概览海报预览"
                className="max-w-full max-h-[600px] rounded-lg shadow-md group-hover:opacity-90 transition-opacity"
              />
              <span className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity bg-black/20">
                <span className="text-white text-[13px] font-semibold px-3 py-1.5 rounded-full bg-black/50">
                  点击查看大图
                </span>
              </span>
            </button>
          </div>
        )}
        <PromptDisclosure
          prompt={selectedPosterVersion ? historyPrompt.data?.prompt : currentPrompt.data?.prompt}
        />
      </Card>

      {siteLightboxOpen && siteHasCurrent && (
        <Lightbox
          title="静态展示网站预览"
          onClose={() => setSiteLightboxOpen(false)}
          wide
          bodyClassName="flex-1 bg-white"
          headerActions={
            <a
              className="text-[12px] font-semibold text-brand-dark hover:underline flex items-center gap-1"
              href={siteSrc}
              target="_blank"
              rel="noreferrer"
            >
              <Icon name="externalLink" className="w-3.5 h-3.5" />
              在新标签页打开
            </a>
          }
        >
          <iframe
            title="静态展示网站预览（全屏）"
            src={siteSrc}
            className="w-full h-full border-0"
          />
        </Lightbox>
      )}

      {posterLightboxOpen && posterHasCurrent && (
        <Lightbox
          title={posterMode === 'overall' ? overallPosterTitle : `Day${selectedDay} 海报`}
          onClose={() => setPosterLightboxOpen(false)}
          headerActions={
            <a
              className="text-[12px] font-semibold text-brand-dark hover:underline"
              href={posterSrc}
              download={`${projectId}-${posterMode === 'overall' ? 'overall' : `day${selectedDay}`}-poster.png`}
            >
              下载原图
            </a>
          }
        >
          <img src={posterSrc} alt="海报大图" className="max-w-full max-h-full object-contain rounded-lg shadow-lg" />
        </Lightbox>
      )}

      {posterConfirmOpen && (
        <PosterConfirmDialog
          title={posterMode === 'overall' ? `确定要生成${overallPosterTitle}吗？` : `确定要生成 Day${selectedDay} 海报吗？`}
          description={posterMode === 'overall'
            ? '这是一次按次收费的生图API调用（gpt-image-2），确认后才会真正发起请求。'
            : `这是一次按次收费的生图API调用（gpt-image-2），将生成独立的 Day${selectedDay} 海报，不会覆盖${overallPosterTitle}。`}
          confirmLabel={posterMode === 'overall' ? '确定生成' : `确定生成 Day${selectedDay}`}
          onCancel={() => setPosterConfirmOpen(false)}
          onConfirm={() => {
            setPosterConfirmOpen(false)
            setSelectedPosterVersion(null)
            activePoster.trigger()
          }}
        />
      )}
    </div>
  )
}

function PosterConfirmDialog({
  title,
  description,
  confirmLabel,
  onCancel,
  onConfirm,
}: {
  title: string
  description: string
  confirmLabel: string
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" role="dialog" aria-modal="true">
      <Card className="p-6 max-w-sm mx-4">
        <h3 className="font-bold text-base mb-2">{title}</h3>
        <p className="text-sm text-ink-2 mb-5">{description}</p>
        <div className="flex justify-end gap-2.5">
          <button
            className="px-4 py-2 rounded-lg text-[13px] font-semibold border border-border-strong text-ink-2 hover:bg-bg-inset"
            onClick={onCancel}
          >
            取消
          </button>
          <button
            className="px-4 py-2 rounded-lg text-[13px] font-semibold bg-brand text-white hover:opacity-90"
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </Card>
    </div>
  )
}
