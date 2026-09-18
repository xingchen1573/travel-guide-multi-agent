import { Link } from 'react-router-dom'
import {
  useFeishuActivityStats,
  useFeishuBindings,
  useFeishuHeartbeatHistory,
  useFeishuHealth,
  useFeishuPushLog,
} from '../api/hooks'
import { Card } from '../components/Card'
import { Badge } from '../components/Badge'
import { feishuHealthVariant, projectStateVariant } from '../lib/status'
import type {
  ActivityStat,
  FeishuBinding,
  FeishuHealth,
  HeartbeatHistoryResponse,
  PushLogEntry,
} from '../api/types'

const HEALTH_LABELS: Record<string, string> = {
  healthy: '运行中',
  stale: '心跳超时',
  not_running: '未运行',
}

function formatSeconds(seconds: number | null): string {
  if (seconds === null) return ''
  if (seconds < 60) return `${Math.round(seconds)}秒前`
  return `${Math.round(seconds / 60)}分钟前`
}

function formatTimestamp(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'medium' })
}

export function FeishuOpsPage() {
  const healthQuery = useFeishuHealth()
  const bindingsQuery = useFeishuBindings()
  const pushLogQuery = useFeishuPushLog(50)
  const heartbeatHistoryQuery = useFeishuHeartbeatHistory()
  const activityStatsQuery = useFeishuActivityStats()

  return (
    <div className="flex flex-col gap-5">
      <HealthCard health={healthQuery.data} isLoading={healthQuery.isLoading} error={healthQuery.error} />
      <BindingsCard
        bindings={bindingsQuery.data}
        isLoading={bindingsQuery.isLoading}
        error={bindingsQuery.error}
      />
      <ActivityStatsCard
        stats={activityStatsQuery.data}
        isLoading={activityStatsQuery.isLoading}
        error={activityStatsQuery.error}
      />
      <PushLogCard entries={pushLogQuery.data} isLoading={pushLogQuery.isLoading} error={pushLogQuery.error} />
      <HeartbeatHistoryCard
        data={heartbeatHistoryQuery.data}
        isLoading={heartbeatHistoryQuery.isLoading}
        error={heartbeatHistoryQuery.error}
      />
    </div>
  )
}

function HealthCard({
  health,
  isLoading,
  error,
}: {
  health: FeishuHealth | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Card className="p-5">
      <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-1">
        Bot 连接健康状态
      </div>
      <p className="text-xs text-ink-3 mb-3">
        run_feishu_worker.py每20秒写一次心跳，这里展示的是"worker进程是否还活着、上次真正
        工作是什么时候"，不是"WS连接在飞书那端真的通"的证明——网络分区导致连接被静默断开时
        进程本身可能仍未崩溃，心跳仍会显示健康。
      </p>
      {isLoading && <p className="text-ink-3 text-sm">加载中…</p>}
      {!!error && <p className="text-danger text-sm">加载失败：{(error as Error).message}</p>}
      {health && (
        <div className="flex items-center gap-4">
          <Badge variant={feishuHealthVariant(health.status)}>
            {HEALTH_LABELS[health.status] ?? health.status}
          </Badge>
          {health.status === 'not_running' ? (
            <span className="text-xs text-ink-3">
              从未收到过心跳，检查 run_feishu_worker.py 是否已启动
            </span>
          ) : (
            <div className="flex gap-4 text-xs text-ink-3">
              <span>{`PID：${health.pid}`}</span>
              <span>{`启动于：${health.started_at}`}</span>
              <span>{`最近心跳：${formatSeconds(health.seconds_since_heartbeat)}`}</span>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

function BindingsCard({
  bindings,
  isLoading,
  error,
}: {
  bindings: FeishuBinding[] | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Card className="p-5">
      <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-1">
        群 ↔ Project 绑定关系
      </div>
      <p className="text-xs text-ink-3 mb-3">
        只读展示，不支持在页面上解绑——飞书群↔project关系只能通过真实群聊行为改变
        （第一条消息自动创建绑定，一个群固定绑定一个project）。点击一行可跳转到对应
        project的详情页（仅当project仍然存在时可点击）。
      </p>
      {isLoading && <p className="text-ink-3 text-sm">加载中…</p>}
      {!!error && <p className="text-danger text-sm">加载失败：{(error as Error).message}</p>}
      {bindings && bindings.length === 0 && (
        <p className="text-sm text-ink-3">还没有任何飞书群绑定过project。</p>
      )}
      {bindings && bindings.length > 0 && (
        <div className="flex flex-col gap-2">
          {bindings.map((b) => {
            const rowContent = (
              <>
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[13px]">{b.chat_id}</span>
                  {b.project_exists && b.state ? (
                    <Badge variant={projectStateVariant(b.state)}>{b.state}</Badge>
                  ) : (
                    <Badge variant="danger">project不存在</Badge>
                  )}
                </div>
                <div className="flex items-center gap-3 mt-2 text-xs text-ink-3">
                  <span>project_id：{b.project_id}</span>
                  {b.destination && <span>目的地：{b.destination}</span>}
                  {b.awaiting && <span>等待回复：{b.awaiting}</span>}
                </div>
              </>
            )
            return b.project_exists ? (
              <Link
                key={b.chat_id}
                to={`/projects/${b.project_id}`}
                state={{ projectParent: { to: '/feishu', label: '飞书运维' } }}
                className="border border-border rounded-lg p-3.5 hover:border-border-strong hover:shadow-sm transition-all"
              >
                {rowContent}
              </Link>
            ) : (
              <div key={b.chat_id} className="border border-border rounded-lg p-3.5 opacity-70">
                {rowContent}
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}

function ActivityStatsCard({
  stats,
  isLoading,
  error,
}: {
  stats: ActivityStat[] | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Card className="p-5">
      <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-1">活跃度统计</div>
      <p className="text-xs text-ink-3 mb-3">
        按project/chat聚合推送次数/成功/失败/最后一次推送时间，直接由推送日志聚合而来，
        不是独立的数据源。
      </p>
      {isLoading && <p className="text-ink-3 text-sm">加载中…</p>}
      {!!error && <p className="text-danger text-sm">加载失败：{(error as Error).message}</p>}
      {stats && stats.length === 0 && <p className="text-sm text-ink-3">还没有任何活跃度统计数据。</p>}
      {stats && stats.length > 0 && (
        <table className="w-full text-sm">
          <thead className="bg-bg-inset text-ink-3 text-xs">
            <tr>
              <th className="text-left px-3 py-2 font-semibold">project_id / chat_id</th>
              <th className="text-left px-3 py-2 font-semibold">推送次数</th>
              <th className="text-left px-3 py-2 font-semibold">成功</th>
              <th className="text-left px-3 py-2 font-semibold">失败</th>
              <th className="text-left px-3 py-2 font-semibold">最后一次推送</th>
            </tr>
          </thead>
          <tbody>
            {stats.map((s) => (
              <tr key={s.project_id ?? s.chat_id} className="border-t border-border">
                <td className="px-3 py-2 font-mono text-[12.5px]">{s.project_id ?? s.chat_id}</td>
                <td className="px-3 py-2">{s.push_count}</td>
                <td className="px-3 py-2 text-ok">{s.success_count}</td>
                <td className={s.failure_count > 0 ? 'px-3 py-2 text-danger font-semibold' : 'px-3 py-2'}>
                  {s.failure_count}
                </td>
                <td className="px-3 py-2 text-ink-3">{formatTimestamp(s.last_push_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}

function PushLogCard({
  entries,
  isLoading,
  error,
}: {
  entries: PushLogEntry[] | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Card className="p-5">
      <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-1">推送日志</div>
      <p className="text-xs text-ink-3 mb-3">最近50条推送尝试，最新的排最前面。</p>
      {isLoading && <p className="text-ink-3 text-sm">加载中…</p>}
      {!!error && <p className="text-danger text-sm">加载失败：{(error as Error).message}</p>}
      {entries && entries.length === 0 && <p className="text-sm text-ink-3">还没有任何推送记录。</p>}
      {entries && entries.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {entries.map((e, i) => (
            <div key={i} className="flex items-center gap-3 px-3 py-2 rounded-lg border border-border text-xs">
              <span className="text-ink-3 shrink-0">{formatTimestamp(e.timestamp)}</span>
              <Badge variant={e.success ? 'ok' : 'danger'}>{e.success ? '成功' : '失败'}</Badge>
              <span className="text-ink-2">{e.push_type}</span>
              <span className="font-mono text-ink-3">{e.project_id ?? e.chat_id}</span>
              {e.error && <span className="text-danger truncate">{e.error}</span>}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

function HeartbeatHistoryCard({
  data,
  isLoading,
  error,
}: {
  data: HeartbeatHistoryResponse | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Card className="p-5">
      <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-1">心跳历史</div>
      <p className="text-xs text-ink-3 mb-3">
        每条心跳的原始时间戳记录，疑似掉线时段（相邻两次心跳间隔过长）在读取时计算得出。
      </p>
      {isLoading && <p className="text-ink-3 text-sm">加载中…</p>}
      {!!error && <p className="text-danger text-sm">加载失败：{(error as Error).message}</p>}
      {data && (
        <>
          <p className="text-sm text-ink-2 mb-2">
            最近 {data.history.length} 次心跳，共检测到 {data.anomalies.length} 次疑似掉线。
          </p>
          {data.anomalies.length > 0 && (
            <div className="flex flex-col gap-1.5">
              {data.anomalies.map((a, i) => (
                <div key={i} className="px-3 py-2 rounded-lg border border-danger-soft bg-danger-soft text-xs text-danger">
                  {formatTimestamp(a.started_at)} → {formatTimestamp(a.ended_at)}
                  （间隔 {Math.round(a.gap_seconds)} 秒）
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Card>
  )
}
