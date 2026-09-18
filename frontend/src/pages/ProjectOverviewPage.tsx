import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useDeleteProject, useProject, useRerunProject, useRunStatus } from '../api/hooks'
import type { StyleInterpretation } from '../api/types'
import { Card } from '../components/Card'
import { Badge } from '../components/Badge'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { ProjectTabs } from '../components/ProjectTabs'
import { Icon } from '../components/Icon'
import { executionStatusLabel, executionStatusVariant, projectStateVariant } from '../lib/status'

const PHASES = ['需求收集中', '骨架确认中', '路线规划中', '并行调研中', '整合中', '树木生成中', '完成']

const STYLE_DIMENSION_LABELS: Record<keyof StyleInterpretation, string> = {
  poi_density: '每天POI数量密度',
  stay_duration: '单点停留时长倾向',
  needs_detail_bias: 'needs_detail判断倾向',
  flexible_time: '预留弹性时间倾向',
}

function formatStyleInterpretation(styleInterpretation: StyleInterpretation | null): string {
  if (!styleInterpretation) return '未提供风格偏好'
  const parts = (Object.keys(STYLE_DIMENSION_LABELS) as Array<keyof StyleInterpretation>)
    .filter((dim) => styleInterpretation[dim])
    .map((dim) => `${STYLE_DIMENSION_LABELS[dim]}=${styleInterpretation[dim]}`)
  return parts.length > 0 ? parts.join('、') : '未提供风格偏好'
}

const AGENT_LABELS: Record<string, string> = {
  route_agent: '路线规划',
  lifestyle_agent: '衣食住行',
  pitfall_agent: '避雷',
  integration_agent: '整合',
  poi_detail_agent: 'POI详情',
}

const AGENT_STATUS_LABEL: Record<string, string> = {
  pending: '等待中',
  in_progress: '进行中',
  done: '已完成',
}

export function ProjectOverviewPage() {
  const { projectId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { data: project, isLoading, error, isFetching } = useProject(projectId)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const { trigger, isTriggering, status } = useRerunProject(projectId)
  const { data: runStatus, isFetching: isRunStatusFetching } = useRunStatus(projectId)
  const deleteProject = useDeleteProject()

  const refreshing = isFetching || isRunStatusFetching
  const handleRefresh = () => {
    queryClient.invalidateQueries({ queryKey: ['project', projectId] })
    queryClient.invalidateQueries({ queryKey: ['run-status', projectId] })
    queryClient.invalidateQueries({ queryKey: ['project-outputs', projectId] })
    queryClient.invalidateQueries({ queryKey: ['project-timeline', projectId] })
  }

  if (isLoading) return <p className="text-ink-3">加载中…</p>
  if (error) return <p className="text-danger">加载失败：{(error as Error).message}</p>
  if (!project) return null

  const currentIdx = PHASES.indexOf(project.progress.state)
  const rerunning = status?.status === 'running' || isTriggering
  // project.has_run_failure跟列表页同一个后端字段（_has_run_failure）：run_status.json
  // 最后一次写入是failed，就代表从那次失败之后没有任何node成功执行过覆盖它——
  // progress.state本身不会记录这个，会永远停留在崩溃前最后一次成功跳转的阶段，
  // 看起来像是"还在跑"，其实是卡死的旧状态。
  const hasRunFailure = project.has_run_failure

  return (
    <div>
      <ProjectTabs projectId={projectId} />

      <div className="flex items-center gap-4 mb-5.5">
        <div className="w-14 h-14 rounded-[14px] shrink-0 flex items-center justify-center text-white text-[22px] font-bold shadow bg-linear-to-br from-brand to-gold">
          {project.destination?.[0] ?? '?'}
        </div>
        <div>
          <h2 className="text-lg font-bold">
            {project.destination} · {project.days}天
          </h2>
          <div className="text-xs text-ink-3 font-mono mt-0.5">{project.project_id}</div>
        </div>
        <div className="flex-1" />
        <Badge variant={hasRunFailure ? 'danger' : projectStateVariant(project.progress.state)}>
          {hasRunFailure ? `运行失败 · ${project.progress.state}` : project.progress.state}
        </Badge>
        <button
          className="text-[13px] font-semibold px-3.5 py-2 rounded-lg border border-border-strong text-ink-2 hover:bg-bg-inset disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-1.5"
          disabled={refreshing}
          onClick={handleRefresh}
        >
          <Icon name="refresh" className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
          刷新
        </button>
        <button
          className="text-[13px] font-semibold px-3.5 py-2 rounded-lg border border-danger text-danger hover:bg-danger-soft disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          disabled={rerunning}
          onClick={() => setConfirmOpen(true)}
        >
          {rerunning ? '重跑中…' : '彻底重跑'}
        </button>
        <button
          className="text-[13px] font-semibold px-3.5 py-2 rounded-lg border border-border-strong text-ink-2 hover:bg-bg-inset disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          disabled={rerunning}
          onClick={() => setDeleteConfirmOpen(true)}
        >
          删除
        </button>
      </div>

      {status?.status === 'failed' && (
        <div className="rounded-xl border border-danger-soft bg-danger-soft p-3.5 mb-4">
          <p className="text-sm text-danger font-semibold">上次重跑失败</p>
          <pre className="text-xs text-danger mt-1 whitespace-pre-wrap">{status.error}</pre>
        </div>
      )}
      {status?.status !== 'failed' && hasRunFailure && (
        <div className="rounded-xl border border-danger-soft bg-danger-soft p-3.5 mb-4">
          <p className="text-sm text-danger font-semibold">
            上次图执行在「{AGENT_LABELS[runStatus?.node ?? ''] ?? runStatus?.node}」节点失败，
            进度停留在「{project.progress.state}」不是真的在运行
          </p>
          {runStatus?.error && (
            <pre className="text-xs text-danger mt-1 whitespace-pre-wrap">{runStatus.error}</pre>
          )}
        </div>
      )}
      {status?.status === 'succeeded' && (
        <div className="rounded-xl border border-ok-soft bg-ok-soft p-3.5 mb-4">
          <p className="text-sm text-ok font-semibold">重跑完成，数据已刷新。</p>
        </div>
      )}
      {status?.status === 'interrupted' && (
        <div className="rounded-xl border border-danger-soft bg-danger-soft p-3.5 mb-4">
          <p className="text-sm text-danger font-semibold">重跑已中断，可以安全重试。</p>
          {status.status_reason && <p className="text-xs text-danger mt-1">{status.status_reason}</p>}
        </div>
      )}

      <Card className="mb-5">
        <div className="grid grid-cols-4">
          <InfoCell label="目的地" value={project.destination ?? '—'} />
          <InfoCell label="天数" value={project.days ? `${project.days} 天` : '—'} />
          <InfoCell label="风格" value={project.style ?? '—'} />
          <InfoCell label="预算档位" value={project.budget ?? '—'} last />
        </div>
        {project.notes && (
          <div className="px-5 py-3.5 border-t border-border text-sm text-ink-2">
            <b className="text-ink-2">备注：</b>
            {project.notes}
          </div>
        )}
      </Card>

      <Card className="p-5 mb-5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-3">
          交付与质量状态
        </div>
        <div className="flex flex-wrap gap-2.5">
          <Badge variant={project.delivery_status === 'passed' ? 'ok' : 'warn'}>
            攻略交付：{project.delivery_status === 'passed' ? '已通过' : '待检查'}
          </Badge>
          <Badge
            variant={['passed', 'unknown'].includes(project.route_risk_status ?? 'unknown') ? 'ok' : 'warn'}
          >
            路线风险：{project.route_risk_status === 'passed' ? '已通过' : project.route_risk_status === 'unknown' ? '暂无结论' : '需要确认'}
          </Badge>
          <Badge
            variant={(project.poi_detail_completed ?? 0) >= (project.poi_detail_expected ?? 0) ? 'ok' : 'warn'}
          >
            POI完整性：{project.poi_detail_completed ?? 0}/{project.poi_detail_expected ?? 0}
          </Badge>
          <Badge variant={(project.review_findings_needs_rerun ?? 0) > 0 ? 'warn' : 'ok'}>
            待恢复：{project.review_findings_needs_rerun ?? 0}
          </Badge>
        </div>
        <p className="text-xs text-ink-3 mt-3">
          顶部“{project.progress.state}”仅表示流程阶段，不代表所有质量维度均已通过。
        </p>
      </Card>

      <Card className="p-5 mb-5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2.5">
          用户主导路线设定
        </div>
        <div className="text-sm text-ink-2 mb-2">
          <b className="text-ink-2">必去地点：</b>
          {project.must_visit.length > 0
            ? project.must_visit
                .map((item) => `${item.name}（${item.day ? `第${item.day}天` : '未指定天数'}）`)
                .join('、')
            : '无'}
        </div>
        <div className="text-sm text-ink-2">
          <b className="text-ink-2">风格解读：</b>
          {formatStyleInterpretation(project.style_interpretation)}
        </div>
      </Card>

      <Card className="p-5 mb-5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2.5">状态机流转</div>
        <div className="flex items-center">
          {PHASES.map((phase, i) => {
            const isDone = i < currentIdx
            const isCurrent = i === currentIdx
            const isFailedCurrent = isCurrent && hasRunFailure
            return (
              <div key={phase} className="flex-1 flex flex-col items-center gap-2 relative">
                {i > 0 && (
                  <div
                    className={`absolute top-[15px] -left-1/2 w-full h-0.5 z-0 ${
                      isDone || isCurrent ? (isFailedCurrent ? 'bg-danger' : 'bg-ok') : 'bg-border-strong'
                    }`}
                  />
                )}
                <div
                  className={`w-7.5 h-7.5 rounded-full flex items-center justify-center text-xs font-bold z-10 border-2 ${
                    isDone
                      ? 'bg-ok border-ok text-white'
                      : isFailedCurrent
                        ? 'bg-danger border-danger text-white shadow-[0_0_0_4px_var(--color-danger-soft)]'
                        : isCurrent
                          ? 'bg-brand border-brand text-white shadow-[0_0_0_4px_var(--color-brand-soft)]'
                          : 'bg-surface border-border-strong text-ink-3'
                  }`}
                >
                  {isDone ? '✓' : isFailedCurrent ? '!' : i + 1}
                </div>
                <div
                  className={`text-[11.5px] font-semibold text-center max-w-[90px] ${
                    isDone || isCurrent ? 'text-ink' : 'text-ink-3'
                  }`}
                >
                  {phase}
                </div>
              </div>
            )
          })}
        </div>
      </Card>

      <Card className="p-5 mb-5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2.5">
          实时Agent运行状态
        </div>
        {runStatus ? (
          <div className="flex items-center gap-3">
            <Badge variant={executionStatusVariant(runStatus.status, runStatus.health_status)}>
              {executionStatusLabel(runStatus.status, runStatus.health_status)}
            </Badge>
            <span className="text-sm font-semibold">{AGENT_LABELS[runStatus.node] ?? runStatus.node}</span>
            <span className="text-xs text-ink-3">第 {runStatus.call_count} 次调用</span>
            {runStatus.error && <span className="text-xs text-danger">{runStatus.error}</span>}
            {runStatus.status_reason && <span className="text-xs text-ink-3">{runStatus.status_reason}</span>}
          </div>
        ) : (
          <p className="text-sm text-ink-3">暂无运行记录（还没有CLI或后台触发过图执行）。</p>
        )}
      </Card>

      <Card className="p-5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-2.5">Agent 分工进度</div>
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))' }}>
          {Object.entries(project.progress.agents).length === 0 && (
            <p className="text-sm text-ink-3">暂无agent运行记录。</p>
          )}
          {Object.entries(project.progress.agents).map(([name, agentStatus]) => (
            <div key={name} className="border border-border rounded-xl px-4 py-3.5 flex items-center gap-3">
              <div className="flex-1">
                <div className="text-[13px] font-bold">{AGENT_LABELS[name] ?? name}</div>
                <div className="text-[11.5px] text-ink-3">{name}</div>
              </div>
              <Badge
                variant={agentStatus === 'done' ? 'ok' : agentStatus === 'in_progress' ? 'warn' : 'pending'}
              >
                {AGENT_STATUS_LABEL[agentStatus] ?? agentStatus}
              </Badge>
            </div>
          ))}
        </div>
      </Card>

      {confirmOpen && (
        <ConfirmDialog
          title="确定要重跑吗？"
          message="当前产出将被清空重建（旧数据会先备份到project的backups/目录），这个操作不可逆。"
          confirmLabel="确定重跑"
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => {
            setConfirmOpen(false)
            trigger()
          }}
        />
      )}
      {deleteConfirmOpen && (
        <ConfirmDialog
          title="确定要删除这个project吗？"
          message="删除后会移入回收站，可以在「回收站」页面恢复或永久删除。"
          confirmLabel="确定删除"
          onCancel={() => setDeleteConfirmOpen(false)}
          onConfirm={() => {
            setDeleteConfirmOpen(false)
            deleteProject.mutate(projectId, { onSuccess: () => navigate('/') })
          }}
        />
      )}
    </div>
  )
}

function InfoCell({ label, value, last = false }: { label: string; value: string; last?: boolean }) {
  return (
    <div className={`px-5 py-4 ${last ? '' : 'border-r border-border'}`}>
      <div className="text-[11.5px] text-ink-3 mb-1.5">{label}</div>
      <div className="text-[15px] font-bold">{value}</div>
    </div>
  )
}
